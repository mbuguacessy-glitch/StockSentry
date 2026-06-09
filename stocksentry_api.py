from fastapi import FastAPI, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime, timezone
from typing import Optional
from stocksentry_models import engine, ShiftRecord, SalesOrder, IntWarehouseMovement, AuditLog, generate_id
from anthropic import Anthropic
from dotenv import load_dotenv
import httpx
import json
import os

load_dotenv()

app = FastAPI(title="StockSentry — Warehouse Stock Reconciliation")
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")

with open("brands.json", "r") as f:
    BRANDS_DATA = json.load(f)


# --- Pydantic schemas ---

class OpenShift(BaseModel):
    warehouse_id: str
    shift: str
    clerk_name: str
    date: str
    opening_stock: dict  # {brand_id: quantity}


class CloseShift(BaseModel):
    shift_record_id: str
    closing_stock: dict  # {brand_id: quantity}
    clerk_name: str


class RecordSale(BaseModel):
    shift_record_id: str
    warehouse_id: str
    order_number: str
    delivery_number: str
    client_name: str
    truck_number: str
    checker_name: str
    forklift_operator: str
    clerk_name: str
    items: list  # [{brand_id, brand_name, ordered, dispatched}]


class SupervisorSignoff(BaseModel):
    shift_record_id: str
    supervisor_name: str
    notes: Optional[str] = None
    approved: bool


class IntermovementCreate(BaseModel):
    from_warehouse_id: str
    from_warehouse_name: str
    to_warehouse_id: str
    to_warehouse_name: str
    brand_id: str
    brand_name: str
    quantity_sent: int
    authorised_by: str
    notes: Optional[str] = None


class IntermovementReceive(BaseModel):
    movement_id: str
    quantity_received: int
    received_by: str


class EscalateOrder(BaseModel):
    order_id: str
    escalation_notes: str
    escalated_by: str


# --- Helper functions ---

def get_brand_name(brand_id: str) -> str:
    for brand in BRANDS_DATA["brands"]:
        if brand["id"] == brand_id:
            return brand["name"]
    return brand_id


def calculate_variances(opening: dict, closing: dict, sales: dict,
                        movements_in: dict, movements_out: dict) -> dict:
    """Calculate variance per brand."""
    variances = {}
    all_brands = set(list(opening.keys()) + list(closing.keys()))

    for brand_id in all_brands:
        open_qty = opening.get(brand_id, 0)
        close_qty = closing.get(brand_id, 0)
        sold_qty = sales.get(brand_id, 0)
        in_qty = movements_in.get(brand_id, 0)
        out_qty = movements_out.get(brand_id, 0)

        expected = open_qty + in_qty - sold_qty - out_qty
        variance = close_qty - expected

        variances[brand_id] = {
            "brand_name": get_brand_name(brand_id),
            "opening": open_qty,
            "sold": sold_qty,
            "movements_in": in_qty,
            "movements_out": out_qty,
            "expected_closing": expected,
            "actual_closing": close_qty,
            "variance": variance,
            "status": "ok" if variance == 0 else "flagged"
        }

    return variances


async def send_slack_notification(channel: str, message: str):
    """Send Slack notification."""
    try:
        async with httpx.AsyncClient() as http:
            await http.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                json={"channel": channel, "text": message}
            )
    except Exception as e:
        print(f"Slack notification failed: {e}")


def generate_shift_summary(shift_record: dict, variances: dict) -> str:
    """Claude generates plain language shift summary."""
    flagged = {k: v for k, v in variances.items() if v["status"] == "flagged"}
    ok_count = len(variances) - len(flagged)

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": f"""You are a warehouse management AI for {BRANDS_DATA['company']}.

Write a concise shift summary in plain language for the supervisor.
Keep it under 150 words. Be direct and factual.

Shift Details:
- Warehouse: {shift_record['warehouse_name']}
- Shift: {shift_record['shift']}
- Date: {shift_record['date']}
- Clerk: {shift_record['clerk_name']}
- Brands with no variance: {ok_count}
- Brands with variance: {len(flagged)}

Variances detected:
{json.dumps(flagged, indent=2) if flagged else "None"}

Write the summary covering:
1. Overall shift status
2. Any variances and what action is needed
3. Recommended next step for the supervisor"""
        }]
    )

    return response.content[0].text


def process_shift_close(shift_record_id: str, closing_stock: dict,
                        clerk_name: str):
    """Background task: calculate variances and notify supervisor."""
    with Session(engine) as session:
        shift = session.get(ShiftRecord, shift_record_id)
        if not shift:
            return

        # Get sales for this shift
        sales = session.query(SalesOrder).filter(
            SalesOrder.shift_record_id == shift_record_id
        ).all()

        sales_totals = {}
        for sale in sales:
            for item in sale.items:
                brand_id = item["brand_id"]
                sales_totals[brand_id] = sales_totals.get(
                    brand_id, 0) + item["dispatched"]

        # Get interwarehouse movements
        movements_in = session.query(IntWarehouseMovement).filter(
            IntWarehouseMovement.to_warehouse_id == shift.warehouse_id,
            IntWarehouseMovement.status == "received"
        ).all()

        movements_out = session.query(IntWarehouseMovement).filter(
            IntWarehouseMovement.from_warehouse_id == shift.warehouse_id,
            IntWarehouseMovement.status.in_(["in_transit", "received"])
        ).all()

        in_totals = {}
        for m in movements_in:
            in_totals[m.brand_id] = in_totals.get(
                m.brand_id, 0) + (m.quantity_received or 0)

        out_totals = {}
        for m in movements_out:
            out_totals[m.brand_id] = out_totals.get(
                m.brand_id, 0) + m.quantity_sent

        # Calculate variances
        variances = calculate_variances(
            shift.opening_stock, closing_stock,
            sales_totals, in_totals, out_totals
        )

        # Check for flagged variances
        flagged = {k: v for k, v in variances.items()
                   if v["status"] == "flagged"}
        has_variance = len(flagged) > 0

        # Update shift record
        shift.closing_stock = closing_stock
        shift.sales = sales_totals
        shift.variances = variances
        shift.status = "flagged" if has_variance else "pending_approval"
        shift.updated_at = datetime.now(timezone.utc)
        session.commit()

        # Generate Claude summary
        shift_dict = {
            "warehouse_name": shift.warehouse_name,
            "shift": shift.shift,
            "date": shift.date,
            "clerk_name": clerk_name
        }
        summary = generate_shift_summary(shift_dict, variances)

        # Build Slack message
        status_emoji = "🔴" if has_variance else "✅"
        variance_text = ""
        if flagged:
            variance_text = "\n\n*Variances Detected:*\n"
            for brand_id, data in flagged.items():
                variance_text += f"• {data['brand_name']}: Expected {data['expected_closing']} | Actual {data['actual_closing']} | Variance: {data['variance']:+d}\n"

        slack_message = f"""{status_emoji} *StockSentry Shift Summary*
*Warehouse:* {shift.warehouse_name}
*Shift:* {shift.shift} | *Date:* {shift.date}
*Clerk:* {clerk_name}

*AI Analysis:*
{summary}
{variance_text}
*Shift ID:* `{shift_record_id}`
Use this ID to sign off or investigate."""

        import asyncio
        asyncio.run(send_slack_notification(
            BRANDS_DATA["escalation_contacts"]["supervisor_slack"],
            slack_message
        ))

        if has_variance:
            asyncio.run(send_slack_notification(
                BRANDS_DATA["escalation_contacts"]["security_slack"],
                f"🔴 *VARIANCE ALERT* — {shift.warehouse_name} {shift.shift} shift on {shift.date}. {len(flagged)} brand(s) flagged. Shift ID: `{shift_record_id}`"
            ))

        # Log audit
        audit = AuditLog(
            id=generate_id("aud"),
            event_type="shift_closed",
            warehouse_id=shift.warehouse_id,
            description=f"Shift closed by {clerk_name}. {len(flagged)} variances detected.",
            clerk_name=clerk_name,
            decided_by="system",
            outcome="flagged" if has_variance else "pending_approval"
        )
        session.add(audit)
        session.commit()


# --- API Endpoints ---

@app.post("/shifts/open")
def open_shift(data: OpenShift):
    """Clerk opens a new shift and records opening stock."""
    with Session(engine) as session:
        shift = ShiftRecord(
            id=generate_id("shf"),
            warehouse_id=data.warehouse_id,
            warehouse_name=next(
                (w["name"] for w in BRANDS_DATA["warehouses"]
                 if w["id"] == data.warehouse_id), data.warehouse_id
            ),
            shift=data.shift,
            clerk_name=data.clerk_name,
            date=data.date,
            opening_stock=data.opening_stock,
            status="open"
        )
        session.add(shift)

        audit = AuditLog(
            id=generate_id("aud"),
            event_type="shift_opened",
            warehouse_id=data.warehouse_id,
            description=f"Shift opened by {data.clerk_name}",
            clerk_name=data.clerk_name,
            decided_by="clerk",
            outcome="shift_open"
        )
        session.add(audit)
        session.commit()

        return {
            "shift_record_id": shift.id,
            "message": f"Shift opened for {data.shift} on {data.date}",
            "warehouse": shift.warehouse_name,
            "clerk": data.clerk_name
        }


@app.post("/shifts/close")
def close_shift(data: CloseShift,
                background_tasks: BackgroundTasks):
    """Clerk closes shift — triggers variance calculation and supervisor notification."""
    with Session(engine) as session:
        shift = session.get(ShiftRecord, data.shift_record_id)
        if not shift:
            raise HTTPException(status_code=404,
                                detail="Shift record not found")
        if shift.status != "open":
            raise HTTPException(status_code=400,
                                detail="Shift is not open")

    background_tasks.add_task(
        process_shift_close,
        data.shift_record_id,
        data.closing_stock,
        data.clerk_name
    )

    return {
        "message": "Shift closing in progress. Supervisor will be notified via Slack.",
        "shift_record_id": data.shift_record_id
    }


@app.post("/orders/record")
def record_sale(data: RecordSale):
    """Record a sales order dispatched during a shift."""
    items_with_variance = []
    total_variance = 0

    for item in data.items:
        variance = item["dispatched"] - item["ordered"]
        items_with_variance.append({
            "brand_id": item["brand_id"],
            "brand_name": item["brand_name"],
            "ordered": item["ordered"],
            "dispatched": item["dispatched"],
            "variance": variance
        })
        total_variance += abs(variance)

    with Session(engine) as session:
        order = SalesOrder(
            id=generate_id("ord"),
            shift_record_id=data.shift_record_id,
            warehouse_id=data.warehouse_id,
            order_number=data.order_number,
            delivery_number=data.delivery_number,
            client_name=data.client_name,
            truck_number=data.truck_number,
            checker_name=data.checker_name,
            forklift_operator=data.forklift_operator,
            clerk_name=data.clerk_name,
            items=items_with_variance,
            total_variance=total_variance,
            status="dispatched" if total_variance == 0 else "flagged",
            escalated=False
        )
        session.add(order)

        audit = AuditLog(
            id=generate_id("aud"),
            event_type="order_recorded",
            warehouse_id=data.warehouse_id,
            description=f"Order {data.order_number} / Delivery {data.delivery_number} to {data.client_name}. Variance: {total_variance}",
            clerk_name=data.clerk_name,
            decided_by="clerk",
            outcome="flagged" if total_variance > 0 else "dispatched"
        )
        session.add(audit)
        session.commit()

        if total_variance > 0:
            import asyncio
            asyncio.run(send_slack_notification(
                BRANDS_DATA["escalation_contacts"]["supervisor_slack"],
                f"⚠️ *ORDER VARIANCE* — Order {data.order_number} / Delivery {data.delivery_number}\nClient: {data.client_name} | Truck: {data.truck_number}\nChecker: {data.checker_name} | Forklift: {data.forklift_operator}\nTotal variance: {total_variance} units\nOrder ID: `{order.id}`"
            ))

        return {
            "order_id": order.id,
            "order_number": data.order_number,
            "delivery_number": data.delivery_number,
            "total_variance": total_variance,
            "status": order.status,
            "message": "Order recorded" if total_variance == 0 else f"Order flagged — variance of {total_variance} units detected"
        }


@app.post("/orders/escalate")
def escalate_order(data: EscalateOrder):
    """Escalate a flagged order for investigation."""
    with Session(engine) as session:
        order = session.get(SalesOrder, data.order_id)
        if not order:
            raise HTTPException(status_code=404,
                                detail="Order not found")

        order.escalated = True
        order.escalation_notes = data.escalation_notes
        order.status = "escalated"
        session.commit()

        import asyncio
        asyncio.run(send_slack_notification(
            BRANDS_DATA["escalation_contacts"]["security_slack"],
            f"🚨 *ORDER ESCALATED*\nOrder: {order.order_number} | Delivery: {order.delivery_number}\nClient: {order.client_name} | Truck: {order.truck_number}\nChecker: {order.checker_name} | Forklift: {order.forklift_operator}\nReason: {data.escalation_notes}\nEscalated by: {data.escalated_by}"
        ))

        audit = AuditLog(
            id=generate_id("aud"),
            event_type="order_escalated",
            warehouse_id=order.warehouse_id,
            description=f"Order {order.order_number} escalated: {data.escalation_notes}",
            clerk_name=data.escalated_by,
            decided_by="human",
            outcome="escalated_to_security"
        )
        session.add(audit)
        session.commit()

        return {
            "message": "Order escalated to security team",
            "order_id": data.order_id,
            "order_number": order.order_number,
            "delivery_number": order.delivery_number
        }


@app.post("/movements/send")
def record_movement_out(data: IntermovementCreate):
    """Record stock leaving a warehouse to another warehouse."""
    with Session(engine) as session:
        movement = IntWarehouseMovement(
            id=generate_id("mov"),
            from_warehouse_id=data.from_warehouse_id,
            from_warehouse_name=data.from_warehouse_name,
            to_warehouse_id=data.to_warehouse_id,
            to_warehouse_name=data.to_warehouse_name,
            brand_id=data.brand_id,
            brand_name=data.brand_name,
            quantity_sent=data.quantity_sent,
            authorised_by=data.authorised_by,
            notes=data.notes,
            status="in_transit"
        )
        session.add(movement)

        audit = AuditLog(
            id=generate_id("aud"),
            event_type="movement_sent",
            warehouse_id=data.from_warehouse_id,
            description=f"{data.quantity_sent} crates of {data.brand_name} sent to {data.to_warehouse_name}",
            clerk_name=data.authorised_by,
            decided_by="human",
            outcome="in_transit"
        )
        session.add(audit)
        session.commit()

        import asyncio
        asyncio.run(send_slack_notification(
            BRANDS_DATA["escalation_contacts"]["supervisor_slack"],
            f"🔄 *INTERWAREHOUSE MOVEMENT*\n{data.quantity_sent} crates of {data.brand_name}\nFrom: {data.from_warehouse_name} → To: {data.to_warehouse_name}\nAuthorised by: {data.authorised_by}\nMovement ID: `{movement.id}`\n\nReceiving clerk: please confirm receipt using the movement ID."
        ))

        return {
            "movement_id": movement.id,
            "message": f"Movement recorded. Receiving warehouse notified via Slack.",
            "brand": data.brand_name,
            "quantity": data.quantity_sent,
            "from": data.from_warehouse_name,
            "to": data.to_warehouse_name
        }


@app.post("/movements/receive")
def confirm_movement_received(data: IntermovementReceive):
    """Receiving warehouse confirms stock received."""
    with Session(engine) as session:
        movement = session.get(IntWarehouseMovement, data.movement_id)
        if not movement:
            raise HTTPException(status_code=404,
                                detail="Movement not found")

        variance = data.quantity_received - movement.quantity_sent
        movement.quantity_received = data.quantity_received
        movement.received_by = data.received_by
        movement.received_at = datetime.now(timezone.utc)
        movement.variance = variance
        movement.status = "received" if variance == 0 else "flagged"
        session.commit()

        if variance != 0:
            import asyncio
            asyncio.run(send_slack_notification(
                BRANDS_DATA["escalation_contacts"]["supervisor_slack"],
                f"⚠️ *MOVEMENT VARIANCE*\n{movement.brand_name}\nSent: {movement.quantity_sent} | Received: {data.quantity_received} | Variance: {variance:+d}\nFrom: {movement.from_warehouse_name} → {movement.to_warehouse_name}\nReceived by: {data.received_by}"
            ))

        audit = AuditLog(
            id=generate_id("aud"),
            event_type="movement_received",
            warehouse_id=movement.to_warehouse_id,
            description=f"Received {data.quantity_received} of {movement.brand_name}. Variance: {variance}",
            clerk_name=data.received_by,
            decided_by="clerk",
            outcome="received" if variance == 0 else "flagged"
        )
        session.add(audit)
        session.commit()

        return {
            "movement_id": data.movement_id,
            "brand": movement.brand_name,
            "sent": movement.quantity_sent,
            "received": data.quantity_received,
            "variance": variance,
            "status": movement.status,
            "message": "Movement confirmed" if variance == 0 else f"Variance of {variance:+d} detected and flagged"
        }


@app.post("/shifts/signoff")
def supervisor_signoff(data: SupervisorSignoff):
    """Supervisor approves or flags a shift record."""
    with Session(engine) as session:
        shift = session.get(ShiftRecord, data.shift_record_id)
        if not shift:
            raise HTTPException(status_code=404,
                                detail="Shift record not found")

        shift.supervisor_name = data.supervisor_name
        shift.supervisor_notes = data.notes
        shift.signed_off = data.approved
        shift.signed_off_at = datetime.now(timezone.utc)
        shift.status = "approved" if data.approved else "flagged"
        session.commit()

        audit = AuditLog(
            id=generate_id("aud"),
            event_type="supervisor_signoff",
            warehouse_id=shift.warehouse_id,
            description=f"Shift {'approved' if data.approved else 'flagged'} by {data.supervisor_name}",
            clerk_name=data.supervisor_name,
            decided_by="human",
            outcome="approved" if data.approved else "flagged"
        )
        session.add(audit)
        session.commit()

        return {
            "message": f"Shift {'approved' if data.approved else 'flagged'} by {data.supervisor_name}",
            "shift_record_id": data.shift_record_id,
            "status": shift.status,
            "signed_off_at": shift.signed_off_at
        }


@app.get("/shifts/{shift_record_id}")
def get_shift(shift_record_id: str):
    """Get full shift details including variances."""
    with Session(engine) as session:
        shift = session.get(ShiftRecord, shift_record_id)
        if not shift:
            raise HTTPException(status_code=404,
                                detail="Shift not found")
        return {
            "id": shift.id,
            "warehouse": shift.warehouse_name,
            "shift": shift.shift,
            "date": shift.date,
            "clerk": shift.clerk_name,
            "opening_stock": shift.opening_stock,
            "closing_stock": shift.closing_stock,
            "sales": shift.sales,
            "variances": shift.variances,
            "status": shift.status,
            "supervisor": shift.supervisor_name,
            "signed_off": shift.signed_off,
            "signed_off_at": shift.signed_off_at
        }


@app.get("/shifts")
def list_shifts(warehouse_id: str = None, date: str = None):
    """List all shifts optionally filtered by warehouse or date."""
    with Session(engine) as session:
        query = session.query(ShiftRecord)
        if warehouse_id:
            query = query.filter(
                ShiftRecord.warehouse_id == warehouse_id)
        if date:
            query = query.filter(ShiftRecord.date == date)
        shifts = query.order_by(ShiftRecord.created_at.desc()).all()
        return [
            {
                "id": s.id,
                "warehouse": s.warehouse_name,
                "shift": s.shift,
                "date": s.date,
                "clerk": s.clerk_name,
                "status": s.status,
                "signed_off": s.signed_off,
                "created_at": s.created_at
            }
            for s in shifts
        ]


@app.get("/orders")
def list_orders(warehouse_id: str = None, status: str = None):
    """List all sales orders."""
    with Session(engine) as session:
        query = session.query(SalesOrder)
        if warehouse_id:
            query = query.filter(
                SalesOrder.warehouse_id == warehouse_id)
        if status:
            query = query.filter(SalesOrder.status == status)
        orders = query.order_by(SalesOrder.created_at.desc()).all()
        return [
            {
                "id": o.id,
                "order_number": o.order_number,
                "delivery_number": o.delivery_number,
                "client": o.client_name,
                "truck": o.truck_number,
                "checker": o.checker_name,
                "forklift_operator": o.forklift_operator,
                "total_variance": o.total_variance,
                "status": o.status,
                "escalated": o.escalated,
                "created_at": o.created_at
            }
            for o in orders
        ]


@app.get("/movements")
def list_movements(warehouse_id: str = None):
    """List all interwarehouse movements."""
    with Session(engine) as session:
        query = session.query(IntWarehouseMovement)
        if warehouse_id:
            query = query.filter(
                (IntWarehouseMovement.from_warehouse_id == warehouse_id) |
                (IntWarehouseMovement.to_warehouse_id == warehouse_id)
            )
        movements = query.order_by(
            IntWarehouseMovement.created_at.desc()).all()
        return [
            {
                "id": m.id,
                "brand": m.brand_name,
                "from": m.from_warehouse_name,
                "to": m.to_warehouse_name,
                "sent": m.quantity_sent,
                "received": m.quantity_received,
                "variance": m.variance,
                "status": m.status,
                "authorised_by": m.authorised_by,
                "received_by": m.received_by,
                "created_at": m.created_at
            }
            for m in movements
        ]


@app.get("/audit")
def get_audit(warehouse_id: str = None, limit: int = 50):
    """Get audit log."""
    with Session(engine) as session:
        query = session.query(AuditLog)
        if warehouse_id:
            query = query.filter(
                AuditLog.warehouse_id == warehouse_id)
        logs = query.order_by(
            AuditLog.created_at.desc()).limit(limit).all()
        return [
            {
                "id": l.id,
                "event_type": l.event_type,
                "warehouse_id": l.warehouse_id,
                "description": l.description,
                "clerk_name": l.clerk_name,
                "decided_by": l.decided_by,
                "outcome": l.outcome,
                "created_at": l.created_at
            }
            for l in logs
        ]


@app.get("/report/monthly")
def monthly_report(warehouse_id: str, month: str):
    """Generate monthly reconciliation report — replaces the manual 2-day process."""
    with Session(engine) as session:
        shifts = session.query(ShiftRecord).filter(
            ShiftRecord.warehouse_id == warehouse_id,
            ShiftRecord.date.like(f"{month}%")
        ).all()

        total_shifts = len(shifts)
        flagged_shifts = len([s for s in shifts if s.status == "flagged"])
        approved_shifts = len(
            [s for s in shifts if s.status == "approved"])

        all_variances = {}
        for shift in shifts:
            if shift.variances:
                for brand_id, data in shift.variances.items():
                    if brand_id not in all_variances:
                        all_variances[brand_id] = {
                            "brand_name": data["brand_name"],
                            "total_variance": 0,
                            "flagged_count": 0
                        }
                    all_variances[brand_id]["total_variance"] += data[
                        "variance"]
                    if data["status"] == "flagged":
                        all_variances[brand_id]["flagged_count"] += 1

        orders = session.query(SalesOrder).filter(
            SalesOrder.warehouse_id == warehouse_id,
            SalesOrder.created_at.cast(
                String).like(f"{month}%")
        ).all()

        return {
            "warehouse_id": warehouse_id,
            "month": month,
            "summary": {
                "total_shifts": total_shifts,
                "approved_shifts": approved_shifts,
                "flagged_shifts": flagged_shifts,
                "total_orders": len(orders),
                "escalated_orders": len(
                    [o for o in orders if o.escalated])
            },
            "brand_variances": all_variances,
            "message": f"Monthly report generated. Previously took 2 days manually."
        }


@app.get("/health")
def health():
    return {"status": "ok", "message": "StockSentry API is running"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8010)
