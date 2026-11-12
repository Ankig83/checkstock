from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


WAREHOUSE_ACTIVE = "active"
WAREHOUSE_ARCHIVED = "archived"

SHIPMENT_TRANSFER = "transfer"
SHIPMENT_FBO = "fbo"
SHIPMENT_FBS = "fbs"
SHIPMENT_IN_TRANSIT = "in_transit"
SHIPMENT_RECEIVED = "received"

RESERVATION_ACTIVE = "active"
RESERVATION_CANCELLED = "cancelled"
RESERVATION_SHIPPED = "shipped"


class Base(DeclarativeBase):
    pass


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Warehouse(Base):
    __tablename__ = "warehouses"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="ck_warehouse_status"),
        Index(
            "uq_active_warehouse_name",
            "name_folded",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    name_folded: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=WAREHOUSE_ACTIVE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), nullable=False)
    sku: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StockBalance(Base):
    """Физический остаток на ФФ и резерв FBS внутри него.

    Доступно = on_hand - reserved_fbs. Резерв не складывается с физическим
    остатком второй раз, когда сверяется уравнение остатков.
    """

    __tablename__ = "stock_balances"
    __table_args__ = (
        CheckConstraint(
            "on_hand >= 0 AND reserved_fbs >= 0 AND reserved_fbs <= on_hand",
            name="ck_balance_nonnegative",
        ),
    )

    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reserved_fbs: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class MarketplaceStock(Base):
    __tablename__ = "marketplace_stock"
    __table_args__ = (CheckConstraint("on_hand >= 0", name="ck_marketplace_nonnegative"),)

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class Shipment(Base):
    __tablename__ = "shipments"
    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_shipment_qty"),
        CheckConstraint("kind IN ('transfer', 'fbo', 'fbs')", name="ck_shipment_kind"),
        CheckConstraint("status IN ('in_transit', 'received')", name="ck_shipment_status"),
        CheckConstraint(
            "(kind = 'transfer' AND to_warehouse_id IS NOT NULL) OR (kind <> 'transfer')",
            name="ck_transfer_destination",
        ),
        Index("ix_shipment_open", "status", "kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    from_warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False)
    to_warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (
        CheckConstraint("qty > 0", name="ck_reservation_qty"),
        CheckConstraint("status IN ('active', 'cancelled', 'shipped')", name="ck_reservation_status"),
        Index("ix_reservation_open", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=RESERVATION_ACTIVE)
    shipment_id: Mapped[int | None] = mapped_column(ForeignKey("shipments.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Operation(Base):
    __tablename__ = "operations"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_operation_idempotency_key"),
        CheckConstraint("qty IS NULL OR qty > 0", name="ck_operation_qty"),
        Index("ix_operations_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    op_type: Mapped[str] = mapped_column(String(64), nullable=False)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    qty: Mapped[int | None] = mapped_column(Integer)
    origin_warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))
    destination_warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"))
    origin_text: Mapped[str | None] = mapped_column(String(200))
    destination_text: Mapped[str | None] = mapped_column(String(200))
    shipment_id: Mapped[int | None] = mapped_column(ForeignKey("shipments.id"))
    reservation_id: Mapped[int | None] = mapped_column(ForeignKey("reservations.id"))
    response_status: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    response_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
