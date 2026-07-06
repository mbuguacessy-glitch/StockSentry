from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey
from sqlalchemy.orm import declarative_base, Session, relationship
from sqlalchemy.dialects.postgresql import JSONB
from datetime import datetime, timezone
from dotenv import load_dotenv
import os
import uuid

load_dotenv(override=False)

DATABASE_URL = os.getenv("DATABASE_URL")
engine = create_engine(DATABASE_URL, echo=False)
Base = declarative_base()


class ShiftRecord(Base):
    """Records every shift's stock count and sales."""
    __tablename__ = "shift_records"

    id = Column(String, primary_key=True)
    warehouse_id = Column(String, nullable=False)
    warehouse_name = Column(String, nullable=False)
    shift = Column(String, nullable=False)  # morning, afternoon, night
    clerk_name = Column(String, nullable=False)
    date = Column(String, nullable=False)
    opening_stock = Column(JSONB)   # {brand_id: quantity}
    closing_stock = Column(JSONB)   # {brand_id: quantity}
    sales = Column(JSONB)   # {brand_id: quantity}
    variances = Column(JSONB)   # {brand_id: variance}
    # open, pending_approval, approved, flagged
    status = Column(String, default="open")
    supervisor_name = Column(String)
    supervisor_notes = Column(Text)
    signed_off = Column(Boolean, default=False)
    signed_off_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<ShiftRecord id={self.id} warehouse={self.warehouse_name} shift={self.shift} date={self.date}>"


class SalesOrder(Base):
    """Records every sales order dispatched."""
    __tablename__ = "sales_orders"

    id = Column(String, primary_key=True)
    shift_record_id = Column(String, nullable=False)
    warehouse_id = Column(String, nullable=False)
    order_number = Column(String, nullable=False)
    delivery_number = Column(String, unique=True, nullable=False)
    client_name = Column(String, nullable=False)
    truck_number = Column(String)
    checker_name = Column(String)
    forklift_operator = Column(String)
    # [{brand_id, brand_name, ordered, dispatched, variance}]
    items = Column(JSONB)
    total_variance = Column(Integer, default=0)
    # dispatched, flagged, resolved, truck_recalled
    status = Column(String, default="dispatched")
    escalated = Column(Boolean, default=False)
    escalation_notes = Column(Text)
    clerk_name = Column(String)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<SalesOrder id={self.id} order={self.order_number} client={self.client_name}>"


class IntWarehouseMovement(Base):
    """Records stock movements between warehouses."""
    __tablename__ = "interwarehouse_movements"

    id = Column(String, primary_key=True)
    from_warehouse_id = Column(String, nullable=False)
    from_warehouse_name = Column(String, nullable=False)
    to_warehouse_id = Column(String, nullable=False)
    to_warehouse_name = Column(String, nullable=False)
    brand_id = Column(String, nullable=False)
    brand_name = Column(String, nullable=False)
    quantity_sent = Column(Integer, nullable=False)
    quantity_received = Column(Integer, nullable=True)
    variance = Column(Integer, default=0)
    # in_transit, received, flagged
    status = Column(String, default="in_transit")
    authorised_by = Column(String)
    received_by = Column(String)
    notes = Column(Text)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    received_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<Movement id={self.id} from={self.from_warehouse_name} to={self.to_warehouse_name} brand={self.brand_name}>"


class AuditLog(Base):
    """Logs every system action for full accountability."""
    __tablename__ = "stocksentry_audit"

    id = Column(String, primary_key=True)
    # shift_open, shift_close, variance_flagged, order_escalated, movement_logged
    event_type = Column(String)
    warehouse_id = Column(String)
    description = Column(Text)
    clerk_name = Column(String)
    decided_by = Column(String)  # clerk, supervisor, system, claude
    outcome = Column(String)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<AuditLog id={self.id} event={self.event_type} warehouse={self.warehouse_id}>"


class Breakage(Base):
    """Records stock lost to breakage or leakage during a shift."""
    __tablename__ = "breakages"

    id = Column(String, primary_key=True)
    shift_record_id = Column(String, nullable=False)
    warehouse_id = Column(String, nullable=False)
    brand_id = Column(String, nullable=False)
    brand_name = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False)
    reason = Column(String, nullable=False)  # breakage or leaker
    description = Column(Text)
    clerk_name = Column(String, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<Breakage id={self.id} brand={self.brand_name} qty={self.quantity} reason={self.reason}>"


class ShiftEdit(Base):
    """Records every edit made to a shift record after reconciliation."""
    __tablename__ = "shift_edits"

    id = Column(String, primary_key=True)
    shift_record_id = Column(String, nullable=False)
    warehouse_id = Column(String, nullable=False)
    # opening_stock or closing_stock
    field_edited = Column(String, nullable=False)
    brand_id = Column(String, nullable=False)
    brand_name = Column(String, nullable=False)
    original_value = Column(Integer, nullable=False)
    edited_value = Column(Integer, nullable=False)
    reason = Column(Text, nullable=False)
    edited_by = Column(String, nullable=False)
    approved_by = Column(String, nullable=True)
    status = Column(String, default="pending")  # pending, approved, rejected
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<ShiftEdit id={self.id} shift={self.shift_record_id} brand={self.brand_name} original={self.original_value} edited={self.edited_value}>"


class ExpiryAlert(Base):
    """Records short expiry flags per brand per shift."""
    __tablename__ = "expiry_alerts"

    id = Column(String, primary_key=True)
    shift_record_id = Column(String, nullable=False)
    warehouse_id = Column(String, nullable=False)
    brand_id = Column(String, nullable=False)
    brand_name = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False)
    expiry_date = Column(String, nullable=False)
    days_to_expiry = Column(Integer, nullable=False)
    clerk_name = Column(String, nullable=False)
    status = Column(String, default="active")  # active, resolved
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def __repr__(self):
        return f"<ExpiryAlert id={self.id} brand={self.brand_name} expiry={self.expiry_date} days={self.days_to_expiry}>"


def generate_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


if __name__ == "__main__":
    print("Creating StockSentry tables...")
    Base.metadata.create_all(engine)
    print("Tables created:")
    print("  - shift_records")
    print("  - sales_orders")
    print("  - interwarehouse_movements")
    print("  - stocksentry_audit")
    print("  - breakages")
    print("  - shift_edits")
    print("  - expiry_alerts")
