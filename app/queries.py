from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.models import (
    SHIPMENT_FBO,
    SHIPMENT_FBS,
    SHIPMENT_IN_TRANSIT,
    SHIPMENT_TRANSFER,
    WAREHOUSE_ARCHIVED,
    RESERVATION_ACTIVE,
    MarketplaceStock,
    Operation,
    Product,
    Reservation,
    Shipment,
    StockBalance,
    Store,
    Warehouse,
)

TYPE_LABELS = {
    "receipt": "Приход",
    "transfer_ship": "Отправка на другой ФФ",
    "transfer_receive": "Приёмка на ФФ",
    "fbs_reserve": "Резерв FBS",
    "fbs_cancel": "Отмена резерва FBS",
    "fbs_ship": "Отправка клиенту FBS",
    "fbo_ship": "Отправка FBO",
    "fbo_receive": "Приёмка FBO",
    "warehouse_create": "Новый склад",
    "warehouse_rename": "Переименование склада",
    "warehouse_archive": "Удаление склада",
}


def _place(warehouse: Warehouse | None, explicit: str | None) -> str:
    if warehouse is not None:
        if warehouse.status == WAREHOUSE_ARCHIVED:
            return f"{warehouse.name} (архив)"
        return warehouse.name
    return explicit or ""


def _summary(type_label: str, qty: int | None, sku: str | None, product_name: str | None, origin: str, destination: str) -> str:
    if sku and qty:
        route = " → ".join(part for part in (origin, destination) if part)
        item = f"{sku} {product_name}".strip()
        if route:
            return f"{type_label} · {qty} шт. · {item} · {route}"
        return f"{type_label} · {qty} шт. · {item}"
    route = " → ".join(part for part in (origin, destination) if part)
    if route:
        return f"{type_label} · {route}"
    return type_label


def _sum_by_product(rows) -> dict[int, int]:
    return {product_id: int(total or 0) for product_id, total in rows}


def stock_snapshot(session: Session) -> dict:
    store = session.scalar(select(Store).order_by(Store.id).limit(1))
    products = list(session.scalars(select(Product).order_by(Product.sku)))
    warehouses = list(session.scalars(select(Warehouse).order_by(Warehouse.id)))
    balances = list(session.scalars(select(StockBalance)))
    balance_map = {(row.warehouse_id, row.product_id): row for row in balances}

    balance_rows = []
    for warehouse in warehouses:
        for product in products:
            row = balance_map.get((warehouse.id, product.id))
            on_hand = row.on_hand if row else 0
            reserved = row.reserved_fbs if row else 0
            balance_rows.append(
                {
                    "warehouse_id": warehouse.id,
                    "product_id": product.id,
                    "on_hand": on_hand,
                    "reserved_fbs": reserved,
                    "available": on_hand - reserved,
                }
            )

    open_shipments = list(
        session.scalars(
            select(Shipment).where(Shipment.status == SHIPMENT_IN_TRANSIT).order_by(Shipment.id.desc())
        )
    )

    def shipment_payload(shipment: Shipment) -> dict:
        return {
            "id": shipment.id,
            "product_id": shipment.product_id,
            "qty": shipment.qty,
            "from_warehouse_id": shipment.from_warehouse_id,
            "to_warehouse_id": shipment.to_warehouse_id,
            "status": shipment.status,
        }

    transfers = [shipment_payload(row) for row in open_shipments if row.kind == SHIPMENT_TRANSFER]
    fbo_shipments = [shipment_payload(row) for row in open_shipments if row.kind == SHIPMENT_FBO]
    fbs_shipments = [shipment_payload(row) for row in open_shipments if row.kind == SHIPMENT_FBS]

    reservations = [
        {
            "id": row.id,
            "product_id": row.product_id,
            "warehouse_id": row.warehouse_id,
            "qty": row.qty,
            "status": row.status,
        }
        for row in session.scalars(
            select(Reservation).where(Reservation.status == RESERVATION_ACTIVE).order_by(Reservation.id.desc())
        )
    ]

    marketplace_rows = {row.product_id: row.on_hand for row in session.scalars(select(MarketplaceStock))}
    marketplace = [{"product_id": product.id, "qty": int(marketplace_rows.get(product.id, 0))} for product in products]

    received = _sum_by_product(
        session.execute(
            select(Operation.product_id, func.sum(Operation.qty))
            .where(Operation.op_type == "receipt", Operation.product_id.is_not(None))
            .group_by(Operation.product_id)
        ).all()
    )
    on_hand = _sum_by_product(
        session.execute(
            select(StockBalance.product_id, func.sum(StockBalance.on_hand)).group_by(StockBalance.product_id)
        ).all()
    )
    in_transit = _sum_by_product(
        session.execute(
            select(Shipment.product_id, func.sum(Shipment.qty))
            .where(Shipment.status == SHIPMENT_IN_TRANSIT)
            .group_by(Shipment.product_id)
        ).all()
    )

    reconciliation_rows = []
    for product in products:
        got = received.get(product.id, 0)
        physical = on_hand.get(product.id, 0)
        transit = in_transit.get(product.id, 0)
        market = int(marketplace_rows.get(product.id, 0))
        reconciliation_rows.append(
            {
                "product_id": product.id,
                "sku": product.sku,
                "received": got,
                "on_hand": physical,
                "in_transit": transit,
                "marketplace": market,
                "balanced": got == physical + transit + market,
            }
        )

    return {
        "store": None if store is None else {"id": store.id, "name": store.name},
        "products": [{"id": product.id, "sku": product.sku, "name": product.name} for product in products],
        "warehouses": [
            {"id": warehouse.id, "name": warehouse.name, "status": warehouse.status} for warehouse in warehouses
        ],
        "balances": balance_rows,
        "transfers": transfers,
        "fbo_shipments": fbo_shipments,
        "fbs_shipments": fbs_shipments,
        "reservations": reservations,
        "marketplace": marketplace,
        "reconciliation": {
            "ok": all(row["balanced"] for row in reconciliation_rows) if reconciliation_rows else True,
            "products": reconciliation_rows,
        },
    }


def operation_history(session: Session, limit: int = 500) -> dict:
    origin = aliased(Warehouse)
    destination = aliased(Warehouse)
    rows = session.execute(
        select(Operation, Product, origin, destination)
        .outerjoin(Product, Product.id == Operation.product_id)
        .outerjoin(origin, origin.id == Operation.origin_warehouse_id)
        .outerjoin(destination, destination.id == Operation.destination_warehouse_id)
        .order_by(Operation.id.desc())
        .limit(limit)
    ).all()

    operations = []
    for operation, product, origin_warehouse, destination_warehouse in rows:
        origin_name = _place(origin_warehouse, operation.origin_text)
        destination_name = _place(destination_warehouse, operation.destination_text)
        sku = product.sku if product is not None else None
        product_name = product.name if product is not None else None
        type_label = TYPE_LABELS.get(operation.op_type, operation.op_type)
        operations.append(
            {
                "id": operation.id,
                "type": operation.op_type,
                "type_label": type_label,
                "created_at": operation.created_at.isoformat(),
                "sku": sku,
                "product_name": product_name,
                "qty": operation.qty,
                "origin": origin_name,
                "destination": destination_name,
                "summary": _summary(type_label, operation.qty, sku, product_name, origin_name, destination_name),
            }
        )
    return {"operations": operations}
