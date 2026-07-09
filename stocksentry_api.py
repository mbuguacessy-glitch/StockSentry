from fastapi.responses import Response
from fastapi import Request
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime, timezone
from typing import Optional
from stocksentry_models import engine, ShiftRecord, SalesOrder, IntWarehouseMovement, AuditLog, Breakage, ShiftEdit, ExpiryAlert, generate_id
from anthropic import Anthropic
from dotenv import load_dotenv
import httpx
import json
import os

load_dotenv(dotenv_path=os.path.join(
    os.path.dirname(__file__), '.env'), override=False)

app = FastAPI(title="StockSentry — Warehouse Stock Reconciliation")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_cors_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response


@app.options("/{rest_of_path:path}")
async def preflight_handler(rest_of_path: str):
    return Response(
        status_code=200,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        }
    )


@app.middleware("http")
async def add_cors_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response


@app.options("/{rest_of_path:path}")
async def preflight_handler(rest_of_path: str):
    return Response(
        status_code=200,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        }
    )
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


class RecordBreakage(BaseModel):
    shift_record_id: str
    warehouse_id: str
    brand_id: str
    brand_name: str
    quantity: int
    reason: str  # breakage or leaker
    description: Optional[str] = None
    clerk_name: str


class RequestShiftEdit(BaseModel):
    shift_record_id: str
    warehouse_id: str
    field_edited: str  # opening_stock or closing_stock
    brand_id: str
    brand_name: str
    original_value: int
    edited_value: int
    reason: str
    edited_by: str


class ApproveShiftEdit(BaseModel):
    edit_id: str
    approved_by: str
    approved: bool


class RecordExpiryAlert(BaseModel):
    shift_record_id: str
    warehouse_id: str
    brand_id: str
    brand_name: str
    quantity: int
    expiry_date: str
    clerk_name: str

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


def generate_shift_summary(shift_record: dict, variances: dict) -> dict:
    """Claude generates structured shift summary with AI analysis."""
    flagged = {k: v for k, v in variances.items() if v["status"] == "flagged"}
    ok_count = len(variances) - len(flagged)

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=500,
        temperature=0.7,
        system=f"""You are a warehouse management AI for {BRANDS_DATA['company']}.
Your job is to write concise shift summaries for supervisors.

Rules you always follow:
- Keep every summary under 150 words
- Be direct and factual, no filler language
- Always state the number of brands with variance
- If variances exist, always recommend a specific next action
- Never speculate about theft unless pattern data confirms it
- Write in plain English a non-technical supervisor can understand

Respond ONLY in valid JSON. No explanation, no preamble, no markdown code blocks.
Use exactly this structure:
{{
  "summary": "plain language shift summary under 150 words",
  "shift_status": "clean or flagged",
  "variance_count": 0,
  "recommended_action": "specific next step for the supervisor",
  "security_required": false
}}""",
        messages=[{
            "role": "user",
            "content": f"""Write the shift summary for:
- Warehouse: {shift_record['warehouse_name']}
- Shift: {shift_record['shift']}
- Date: {shift_record['date']}
- Clerk: {shift_record['clerk_name']}
- Brands with no variance: {ok_count}
- Brands with variance: {len(flagged)}

Variances detected:
{json.dumps(flagged, indent=2) if flagged else "None"}"""
        }]
    )

    try:
        result = json.loads(response.content[0].text)
    except json.JSONDecodeError:
        result = {
            "summary": response.content[0].text,
            "shift_status": "flagged" if flagged else "clean",
            "variance_count": len(flagged),
            "recommended_action": "Review variance details manually.",
            "security_required": False
        }

    return result


def classify_variance_causes(flagged: dict, shift_record: dict) -> dict:
    """Claude uses chain of thought to classify likely cause of each variance."""
    if not flagged:
        return {}

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=800,
        temperature=0.5,
        system=f"""You are a warehouse investigation AI for {BRANDS_DATA['company']}.
Your job is to classify the likely cause of each stock variance detected at shift close.

Classification rules you always follow:
- DATA_ENTRY_ERROR: small variance of 1-2 units, isolated to one brand, first occurrence
- LOADING_ERROR: variance on multiple brands in same shift, involves high volume brands
- THEFT: recurring variance on same brand across multiple shifts, or large unexplained variance above 5 units
- UNKNOWN: insufficient information to classify confidently

Respond ONLY in valid JSON. No explanation, no preamble, no markdown code blocks.
Use exactly this structure for each brand:
{{
  "brand_id": {{
    "brand_name": "string",
    "variance": 0,
    "reasoning": "step by step thinking about the cause",
    "classification": "DATA_ENTRY_ERROR or LOADING_ERROR or THEFT or UNKNOWN",
    "confidence": "high or medium or low",
    "recommended_check": "specific physical action to take"
  }}
}}""",
        messages=[{
            "role": "user",
            "content": f"""Classify the likely cause of each variance detected.
Think through each one step by step before classifying.

Warehouse: {shift_record['warehouse_name']}
Shift: {shift_record['shift']}
Date: {shift_record['date']}
Clerk: {shift_record['clerk_name']}

Flagged variances:
{json.dumps(flagged, indent=2)}"""
        }]
    )

    try:
        result = json.loads(response.content[0].text)
    except json.JSONDecodeError:
        result = {}

    return result


def check_historical_pattern(warehouse_id: str, flagged: dict) -> dict:
    """Query database for recurring variance patterns per brand."""
    patterns = {}

    with Session(engine) as session:
        for brand_id in flagged.keys():
            recent_shifts = session.query(ShiftRecord).filter(
                ShiftRecord.warehouse_id == warehouse_id,
                ShiftRecord.status.in_(["flagged", "approved"]),
                ShiftRecord.variances.isnot(None)
            ).order_by(ShiftRecord.created_at.desc()).limit(30).all()

            brand_variances = []
            for shift in recent_shifts:
                if shift.variances and brand_id in shift.variances:
                    v = shift.variances[brand_id]
                    if v["status"] == "flagged":
                        brand_variances.append({
                            "date": shift.date,
                            "shift": shift.shift,
                            "clerk": shift.clerk_name,
                            "variance": v["variance"]
                        })

            if brand_variances:
                total_variance = sum(v["variance"] for v in brand_variances)
                patterns[brand_id] = {
                    "brand_name": flagged[brand_id]["brand_name"],
                    "occurrences": len(brand_variances),
                    "total_variance": total_variance,
                    "history": brand_variances,
                    "is_recurring": len(brand_variances) >= 3,
                    "escalate_to_security": len(brand_variances) >= 3 or abs(total_variance) >= 10
                }

    return patterns


def process_shift_close(shift_record_id: str, closing_stock: dict,
                        clerk_name: str):
    """Background task: calculate variances, classify causes, detect patterns, notify supervisor."""
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

        # Build shift dict for AI functions
        shift_dict = {
            "warehouse_name": shift.warehouse_name,
            "shift": shift.shift,
            "date": shift.date,
            "clerk_name": clerk_name
        }

        # Generate structured shift summary
        summary_data = generate_shift_summary(shift_dict, variances)

        # Classify variance causes and check patterns if variances exist
        variance_classifications = {}
        patterns = {}
        if has_variance:
            variance_classifications = classify_variance_causes(
                flagged, shift_dict)
            patterns = check_historical_pattern(
                shift.warehouse_id, flagged)

        # Determine if security notification needed
        security_required = summary_data.get("security_required", False)
        recurring_brands = [
            brand_id for brand_id, p in patterns.items()
            if p.get("escalate_to_security", False)
        ]
        if recurring_brands:
            security_required = True

        # Build variance detail text for Slack
        variance_text = ""
        if flagged:
            variance_text = "\n\n*Variances Detected:*\n"
            for brand_id, data in flagged.items():
                classification = variance_classifications.get(brand_id, {})
                pattern = patterns.get(brand_id, {})

                variance_text += f"• *{data['brand_name']}*: Expected {data['expected_closing']} | Actual {data['actual_closing']} | Variance: {data['variance']:+d}\n"

                if classification:
                    variance_text += f"  _Likely cause: {classification.get('classification', 'UNKNOWN')} ({classification.get('confidence', 'low')} confidence)_\n"
                    variance_text += f"  _Check: {classification.get('recommended_check', 'Review manually')}_\n"

                if pattern and pattern.get("is_recurring"):
                    variance_text += f"  ⚠️ _RECURRING: {pattern['occurrences']} occurrences in last 30 shifts. Total loss: {pattern['total_variance']:+d} units_\n"

        # Build Slack message
        status_emoji = "🔴" if has_variance else "✅"
        slack_message = f"""{status_emoji} *StockSentry Shift Summary*
*Warehouse:* {shift.warehouse_name}
*Shift:* {shift.shift} | *Date:* {shift.date}
*Clerk:* {clerk_name}

*AI Analysis:*
{summary_data.get('summary', 'Summary unavailable')}

*Recommended Action:* {summary_data.get('recommended_action', 'None')}
{variance_text}
*Shift ID:* `{shift_record_id}`
Use this ID to sign off or investigate."""

        import asyncio
        asyncio.run(send_slack_notification(
            BRANDS_DATA["escalation_contacts"]["supervisor_slack"],
            slack_message
        ))

        # Security notification — triggered by summary AI or recurring pattern
        if security_required:
            security_message = f"🔴 *SECURITY ALERT* — {shift.warehouse_name} {shift.shift} shift on {shift.date}.\n"
            if recurring_brands:
                for brand_id in recurring_brands:
                    p = patterns[brand_id]
                    security_message += f"• {p['brand_name']}: {p['occurrences']} recurring variances. Total: {p['total_variance']:+d} units.\n"
            security_message += f"\nShift ID: `{shift_record_id}` — Immediate investigation recommended."

            asyncio.run(send_slack_notification(
                BRANDS_DATA["escalation_contacts"]["security_slack"],
                security_message
            ))
        elif has_variance:
            asyncio.run(send_slack_notification(
                BRANDS_DATA["escalation_contacts"]["security_slack"],
                f"🔴 *VARIANCE ALERT* — {shift.warehouse_name} {shift.shift} shift on {shift.date}. {len(flagged)} brand(s) flagged. Shift ID: `{shift_record_id}`"
            ))

        # Log audit
        audit = AuditLog(
            id=generate_id("aud"),
            event_type="shift_closed",
            warehouse_id=shift.warehouse_id,
            description=f"Shift closed by {clerk_name}. {len(flagged)} variances detected. Security required: {security_required}.",
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
            SalesOrder.warehouse_id == warehouse_id
        ).all()

 # Filter by month in Python instead
    orders = [o for o in orders if str(o.created_at).startswith(month)]

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


@app.post("/breakages/record")
def record_breakage(data: RecordBreakage):
    """Record stock lost to breakage or leakage during a shift."""
    with Session(engine) as session:
        shift = session.get(ShiftRecord, data.shift_record_id)
        if not shift:
            raise HTTPException(
                status_code=404, detail="Shift record not found")
        if shift.status != "open":
            raise HTTPException(status_code=400, detail="Shift is not open")

        breakage = Breakage(
            id=generate_id("brk"),
            shift_record_id=data.shift_record_id,
            warehouse_id=data.warehouse_id,
            brand_id=data.brand_id,
            brand_name=data.brand_name,
            quantity=data.quantity,
            reason=data.reason,
            description=data.description,
            clerk_name=data.clerk_name
        )
        session.add(breakage)

        audit = AuditLog(
            id=generate_id("aud"),
            event_type="breakage_recorded",
            warehouse_id=data.warehouse_id,
            description=f"{data.quantity} crates of {data.brand_name} lost to {data.reason}. {data.description or ''}",
            clerk_name=data.clerk_name,
            decided_by="clerk",
            outcome="breakage_logged"
        )
        session.add(audit)
        session.commit()

        return {
            "breakage_id": breakage.id,
            "brand": data.brand_name,
            "quantity": data.quantity,
            "reason": data.reason,
            "message": f"{data.quantity} crates of {data.brand_name} recorded as {data.reason}"
        }


@app.get("/breakages")
def list_breakages(shift_record_id: str = None, warehouse_id: str = None):
    """List all breakages optionally filtered by shift or warehouse."""
    with Session(engine) as session:
        query = session.query(Breakage)
        if shift_record_id:
            query = query.filter(Breakage.shift_record_id == shift_record_id)
        if warehouse_id:
            query = query.filter(Breakage.warehouse_id == warehouse_id)
        breakages = query.order_by(Breakage.created_at.desc()).all()
        return [
            {
                "id": b.id,
                "shift_record_id": b.shift_record_id,
                "brand": b.brand_name,
                "quantity": b.quantity,
                "reason": b.reason,
                "description": b.description,
                "clerk": b.clerk_name,
                "created_at": b.created_at
            }
            for b in breakages
        ]


@app.post("/shifts/edit/request")
def request_shift_edit(data: RequestShiftEdit):
    """Clerk requests an edit to a shift record after reconciliation."""
    with Session(engine) as session:
        shift = session.get(ShiftRecord, data.shift_record_id)
        if not shift:
            raise HTTPException(
                status_code=404, detail="Shift record not found")
        if shift.status == "open":
            raise HTTPException(status_code=400,
                                detail="Shift is still open. Edit directly before closing.")

        edit = ShiftEdit(
            id=generate_id("edt"),
            shift_record_id=data.shift_record_id,
            warehouse_id=data.warehouse_id,
            field_edited=data.field_edited,
            brand_id=data.brand_id,
            brand_name=data.brand_name,
            original_value=data.original_value,
            edited_value=data.edited_value,
            reason=data.reason,
            edited_by=data.edited_by,
            status="pending"
        )
        session.add(edit)

        audit = AuditLog(
            id=generate_id("aud"),
            event_type="shift_edit_requested",
            warehouse_id=data.warehouse_id,
            description=f"Edit requested by {data.edited_by} on {data.field_edited} for {data.brand_name}. Original: {data.original_value} Edited: {data.edited_value}. Reason: {data.reason}",
            clerk_name=data.edited_by,
            decided_by="clerk",
            outcome="pending_supervisor_approval"
        )
        session.add(audit)
        session.commit()

        import asyncio
        asyncio.run(send_slack_notification(
            BRANDS_DATA["escalation_contacts"]["supervisor_slack"],
            f"✏️ *SHIFT EDIT REQUEST*\nShift: `{data.shift_record_id}`\nBrand: {data.brand_name}\nField: {data.field_edited}\nOriginal: {data.original_value} | Edited: {data.edited_value}\nReason: {data.reason}\nRequested by: {data.edited_by}\nEdit ID: `{edit.id}`"
        ))

        return {
            "edit_id": edit.id,
            "status": "pending",
            "message": "Edit request submitted. Supervisor approval required."
        }


@app.post("/shifts/edit/approve")
def approve_shift_edit(data: ApproveShiftEdit):
    """Supervisor approves or rejects a shift edit request."""
    with Session(engine) as session:
        edit = session.get(ShiftEdit, data.edit_id)
        if not edit:
            raise HTTPException(
                status_code=404, detail="Edit request not found")

        edit.approved_by = data.approved_by
        edit.status = "approved" if data.approved else "rejected"

        if data.approved:
            shift = session.get(ShiftRecord, edit.shift_record_id)
            if shift:
                stock = dict(getattr(shift, edit.field_edited) or {})
                stock[edit.brand_id] = edit.edited_value
                setattr(shift, edit.field_edited, stock)
                shift.updated_at = datetime.now(timezone.utc)

        audit = AuditLog(
            id=generate_id("aud"),
            event_type="shift_edit_approved" if data.approved else "shift_edit_rejected",
            warehouse_id=edit.warehouse_id,
            description=f"Edit {'approved' if data.approved else 'rejected'} by {data.approved_by}. Brand: {edit.brand_name}. Original: {edit.original_value} Edited: {edit.edited_value}",
            clerk_name=data.approved_by,
            decided_by="supervisor",
            outcome="approved" if data.approved else "rejected"
        )
        session.add(audit)
        session.commit()

        return {
            "edit_id": data.edit_id,
            "status": edit.status,
            "message": f"Edit {'approved and applied' if data.approved else 'rejected'} by {data.approved_by}"
        }


@app.get("/shifts/edits/{shift_record_id}")
def get_shift_edits(shift_record_id: str):
    """Get all edit requests for a shift showing original and edited values."""
    with Session(engine) as session:
        edits = session.query(ShiftEdit).filter(
            ShiftEdit.shift_record_id == shift_record_id
        ).order_by(ShiftEdit.created_at.desc()).all()
        return [
            {
                "id": e.id,
                "field_edited": e.field_edited,
                "brand": e.brand_name,
                "original_value": e.original_value,
                "edited_value": e.edited_value,
                "reason": e.reason,
                "edited_by": e.edited_by,
                "approved_by": e.approved_by,
                "status": e.status,
                "created_at": e.created_at
            }
            for e in edits
        ]


@app.post("/expiry/record")
def record_expiry_alert(data: RecordExpiryAlert):
    """Record a short expiry alert for a brand."""
    from datetime import date
    expiry = datetime.strptime(data.expiry_date, "%Y-%m-%d").date()
    today = date.today()
    days_to_expiry = (expiry - today).days

    with Session(engine) as session:
        alert = ExpiryAlert(
            id=generate_id("exp"),
            shift_record_id=data.shift_record_id,
            warehouse_id=data.warehouse_id,
            brand_id=data.brand_id,
            brand_name=data.brand_name,
            quantity=data.quantity,
            expiry_date=data.expiry_date,
            days_to_expiry=days_to_expiry,
            clerk_name=data.clerk_name,
            status="active"
        )
        session.add(alert)

        audit = AuditLog(
            id=generate_id("aud"),
            event_type="expiry_alert",
            warehouse_id=data.warehouse_id,
            description=f"{data.quantity} crates of {data.brand_name} expiring in {days_to_expiry} days on {data.expiry_date}",
            clerk_name=data.clerk_name,
            decided_by="clerk",
            outcome="expiry_flagged"
        )
        session.add(audit)
        session.commit()

        if days_to_expiry <= 30:
            import asyncio
            asyncio.run(send_slack_notification(
                BRANDS_DATA["escalation_contacts"]["supervisor_slack"],
                f"⚠️ *SHORT EXPIRY ALERT*\nBrand: {data.brand_name}\nQuantity: {data.quantity} crates\nExpiry date: {data.expiry_date}\nDays remaining: {days_to_expiry}\nWarehouse: {data.warehouse_id}\nReported by: {data.clerk_name}"
            ))

        return {
            "alert_id": alert.id,
            "brand": data.brand_name,
            "quantity": data.quantity,
            "expiry_date": data.expiry_date,
            "days_to_expiry": days_to_expiry,
            "message": f"{data.brand_name} expiring in {days_to_expiry} days. Supervisor notified." if days_to_expiry <= 30 else f"Expiry alert recorded. {days_to_expiry} days remaining."
        }


@app.get("/expiry/alerts")
def list_expiry_alerts(warehouse_id: str = None, status: str = "active"):
    """List all expiry alerts optionally filtered by warehouse."""
    with Session(engine) as session:
        query = session.query(ExpiryAlert).filter(
            ExpiryAlert.status == status
        )
        if warehouse_id:
            query = query.filter(ExpiryAlert.warehouse_id == warehouse_id)
        alerts = query.order_by(ExpiryAlert.days_to_expiry.asc()).all()
        return [
            {
                "id": a.id,
                "brand": a.brand_name,
                "quantity": a.quantity,
                "expiry_date": a.expiry_date,
                "days_to_expiry": a.days_to_expiry,
                "clerk": a.clerk_name,
                "status": a.status,
                "created_at": a.created_at
            }
            for a in alerts
        ]


@app.get("/inventory/current")
def current_inventory(warehouse_id: str, date: str = None):
    """Get current stock levels per brand calculated from all shift activity."""
    target_date = date or datetime.now().strftime("%Y-%m-%d")

    with Session(engine) as session:
        shifts = session.query(ShiftRecord).filter(
            ShiftRecord.warehouse_id == warehouse_id,
            ShiftRecord.date == target_date
        ).order_by(ShiftRecord.created_at.desc()).all()

        if not shifts:
            return {
                "warehouse_id": warehouse_id,
                "date": target_date,
                "message": "No shifts found for this date",
                "inventory": {}
            }

        latest_shift = shifts[0]
        inventory = {}

        if latest_shift.closing_stock:
            base_stock = latest_shift.closing_stock
        else:
            base_stock = latest_shift.opening_stock or {}

        for brand_id, qty in base_stock.items():
            inventory[brand_id] = {
                "brand_name": get_brand_name(brand_id),
                "quantity": qty,
                "status": "ok"
            }

        breakages = session.query(Breakage).filter(
            Breakage.shift_record_id == latest_shift.id
        ).all()

        for b in breakages:
            if b.brand_id in inventory:
                inventory[b.brand_id]["quantity"] -= b.quantity
                inventory[b.brand_id]["breakage_loss"] = b.quantity

        expiry_alerts = session.query(ExpiryAlert).filter(
            ExpiryAlert.warehouse_id == warehouse_id,
            ExpiryAlert.status == "active"
        ).all()

        expiring_brands = {a.brand_id for a in expiry_alerts}
        for brand_id in expiring_brands:
            if brand_id in inventory:
                inventory[brand_id]["expiry_warning"] = True

        return {
            "warehouse_id": warehouse_id,
            "date": target_date,
            "last_updated": latest_shift.updated_at,
            "inventory": inventory
        }


@app.get("/health")
def health():
    return {"status": "ok", "message": "StockSentry API is running"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8010)
