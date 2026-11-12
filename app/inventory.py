"""Проводки CheckStock.

Инварианты:
- доступно = физически на ФФ − резерв FBS, резерв не больше физического остатка;
- приход, перемещение, резерв и отправка FBO списывают только доступное;
- отправка FBS списывает уже созданный резерв, даже если свободного остатка нет;
- документ в пути принимается один раз;
- повтор того же Idempotency-Key с тем же телом возвращает первую проводку.
"""

import copy
import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import InventoryError
from app.models import (
    RESERVATION_ACTIVE,
    RESERVATION_CANCELLED,
    RESERVATION_SHIPPED,
    SHIPMENT_FBO,
    SHIPMENT_FBS,
    SHIPMENT_IN_TRANSIT,
    SHIPMENT_RECEIVED,
    SHIPMENT_TRANSFER,
    WAREHOUSE_ACTIVE,
    WAREHOUSE_ARCHIVED,
    MarketplaceStock,
    Operation,
    Product,
    Reservation,
    Shipment,
    StockBalance,
    Store,
    Warehouse,
    utcnow,
)


@dataclass
class Applied:
    body: dict
    status: int = 200


@dataclass
class Replay:
    body: dict
    status: int = 200


@dataclass
class Draft:
    body: dict
    product_id: int | None = None
    qty: int | None = None
    origin_warehouse_id: int | None = None
    destination_warehouse_id: int | None = None
    origin_text: str | None = None
    destination_text: str | None = None
    shipment_id: int | None = None
    reservation_id: int | None = None


@dataclass
class NoOp:
    """Успех без новой строки в истории: название склада не изменилось."""

    body: dict


def canonical_hash(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def advisory_key(value: str) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def constraint_name(error: IntegrityError) -> str:
    diagnostic = getattr(error.orig, "diag", None)
    if diagnostic is not None and getattr(diagnostic, "constraint_name", None):
        return diagnostic.constraint_name
    return ""


def require_qty(qty: int) -> int:
    if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
        raise InventoryError(422, "validation", "Количество должно быть целым числом больше нуля")
    return qty


def normalize_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise InventoryError(422, "validation", "Укажите название склада")
    if len(cleaned) > 200:
        raise InventoryError(422, "validation", "Название склада длиннее 200 символов")
    return cleaned


def get_product(session: Session, product_id: int) -> Product:
    product = session.get(Product, product_id)
    if product is None:
        raise InventoryError(404, "product_not_found", "Товар не найден")
    return product


def get_warehouse(session: Session, warehouse_id: int, *, active: bool) -> Warehouse:
    warehouse = session.get(Warehouse, warehouse_id)
    if warehouse is None:
        raise InventoryError(404, "warehouse_not_found", "Склад не найден")
    if active and warehouse.status != WAREHOUSE_ACTIVE:
        raise InventoryError(
            409,
            "warehouse_archived",
            f"Склад «{warehouse.name}» удалён и лежит в архиве. Новые операции на нём закрыты, остатки по-прежнему видны в таблице.",
        )
    return warehouse


def ensure_same_store(product: Product, warehouse: Warehouse) -> None:
    if product.store_id != warehouse.store_id:
        raise InventoryError(409, "store_mismatch", "Товар и склад относятся к разным магазинам")


def lock_balance(session: Session, warehouse_id: int, product_id: int) -> StockBalance:
    session.execute(
        pg_insert(StockBalance)
        .values(warehouse_id=warehouse_id, product_id=product_id, on_hand=0, reserved_fbs=0)
        .on_conflict_do_nothing(index_elements=["warehouse_id", "product_id"])
    )
    balance = session.scalar(
        select(StockBalance)
        .where(StockBalance.warehouse_id == warehouse_id, StockBalance.product_id == product_id)
        .with_for_update()
    )
    if balance is None:
        raise RuntimeError("Не удалось заблокировать строку остатка")
    return balance


def lock_marketplace(session: Session, product_id: int) -> MarketplaceStock:
    session.execute(
        pg_insert(MarketplaceStock)
        .values(product_id=product_id, on_hand=0)
        .on_conflict_do_nothing(index_elements=["product_id"])
    )
    row = session.scalar(
        select(MarketplaceStock).where(MarketplaceStock.product_id == product_id).with_for_update()
    )
    if row is None:
        raise RuntimeError("Не удалось заблокировать остаток маркетплейса")
    return row


def lock_shipment(session: Session, shipment_id: int) -> Shipment:
    shipment = session.scalar(select(Shipment).where(Shipment.id == shipment_id).with_for_update())
    if shipment is None:
        raise InventoryError(404, "shipment_not_found", "Отгрузка не найдена")
    return shipment


def lock_reservation(session: Session, reservation_id: int) -> Reservation:
    reservation = session.scalar(
        select(Reservation).where(Reservation.id == reservation_id).with_for_update()
    )
    if reservation is None:
        raise InventoryError(404, "reservation_not_found", "Резерв не найден")
    return reservation


def assert_available(
    session: Session,
    warehouse: Warehouse,
    product: Product,
    balance: StockBalance,
    qty: int,
    *,
    purpose: str,
) -> None:
    available = balance.on_hand - balance.reserved_fbs
    if qty <= available:
        return

    rows = session.execute(
        select(Warehouse, StockBalance)
        .join(StockBalance, StockBalance.warehouse_id == Warehouse.id)
        .where(StockBalance.product_id == product.id, Warehouse.id != warehouse.id)
    ).all()
    elsewhere: list[dict] = []
    archived_qty = 0
    for other, other_balance in rows:
        free = other_balance.on_hand - other_balance.reserved_fbs
        if free <= 0:
            continue
        if other.status == WAREHOUSE_ACTIVE:
            elsewhere.append({"warehouse_id": other.id, "name": other.name, "available": free})
        else:
            archived_qty += free
    elsewhere.sort(key=lambda item: item["name"])
    elsewhere_qty = sum(item["available"] for item in elsewhere)
    names = ", ".join(f"«{item['name']}» — {item['available']}" for item in elsewhere)

    if elsewhere_qty > 0 and available + elsewhere_qty >= qty:
        if purpose == "transfer":
            tail = f" На других складах доступно {elsewhere_qty} шт. ({names}). Выберите один из них. Этот запрос ничего не списал."
        else:
            tail = (
                f" На других складах доступно {elsewhere_qty} шт. ({names}). "
                "В сумме товара хватает, но с невыбранного склада он не снимается. "
                "Сначала оформите перемещение. Этот запрос ничего не списал."
            )
    elif elsewhere_qty > 0:
        tail = (
            f" На других складах доступно ещё {elsewhere_qty} шт. ({names}). "
            "Этого тоже не хватает. Этот запрос ничего не списал."
        )
    else:
        tail = " На других активных складах доступного остатка нет. Этот запрос ничего не списал."
    if archived_qty:
        tail += f" Ещё {archived_qty} шт. числится на архивных складах и этим запросом не списывается."

    raise InventoryError(
        409,
        "insufficient_at_warehouse",
        f"На складе «{warehouse.name}» доступно {available} шт. {product.sku}, запрошено {qty}.{tail}",
        {
            "available_here": available,
            "requested": qty,
            "sku": product.sku,
            "product_name": product.name,
            "warehouse_id": warehouse.id,
            "warehouse_name": warehouse.name,
            "available_elsewhere": elsewhere,
            "available_on_active_warehouses": available + elsewhere_qty,
            "available_on_archived_warehouses": archived_qty,
        },
    )


def apply(session: Session, key: str, op_type: str, payload: dict, action) -> Applied | Replay:
    """Проводит действие один раз на ключ, в той же транзакции, что и проводка."""

    request_hash = canonical_hash({"op": op_type, "body": payload})
    try:
        with session.begin():
            session.execute(
                text("SELECT pg_advisory_xact_lock(CAST(:lock_id AS bigint))"),
                {"lock_id": advisory_key(key)},
            )
            existing = session.scalar(select(Operation).where(Operation.idempotency_key == key))
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise InventoryError(
                        409,
                        "idempotency_conflict",
                        "Этот ключ уже использован для другой операции. Новый запрос не выполнен.",
                    )
                body = copy.deepcopy(existing.response_json)
                body["idempotent_replay"] = True
                return Replay(body=body, status=existing.response_status)

            result = action()
            if isinstance(result, NoOp):
                body = dict(result.body)
                body["idempotent_replay"] = False
                body["operation_type"] = op_type
                return Applied(body=body)

            operation = Operation(
                idempotency_key=key,
                request_hash=request_hash,
                op_type=op_type,
                product_id=result.product_id,
                qty=result.qty,
                origin_warehouse_id=result.origin_warehouse_id,
                destination_warehouse_id=result.destination_warehouse_id,
                origin_text=result.origin_text,
                destination_text=result.destination_text,
                shipment_id=result.shipment_id,
                reservation_id=result.reservation_id,
                response_status=200,
                response_json={},
            )
            session.add(operation)
            session.flush()
            body = dict(result.body)
            body["idempotent_replay"] = False
            body["operation_id"] = operation.id
            body["operation_type"] = op_type
            operation.response_json = body
            return Applied(body=body)
    except IntegrityError as error:
        name = constraint_name(error)
        if name == "uq_operation_idempotency_key":
            existing = session.scalar(select(Operation).where(Operation.idempotency_key == key))
            if existing is not None and existing.request_hash == request_hash:
                body = copy.deepcopy(existing.response_json)
                body["idempotent_replay"] = True
                return Replay(body=body, status=existing.response_status)
            raise InventoryError(
                409,
                "idempotency_conflict",
                "Этот ключ уже использован для другой операции. Новый запрос не выполнен.",
            ) from error
        if name == "uq_active_warehouse_name":
            raise InventoryError(409, "warehouse_exists", "Активный склад с таким названием уже есть") from error
        raise


def receipt(session: Session, key: str, payload: dict) -> Applied | Replay:
    def action() -> Draft:
        qty = require_qty(payload["qty"])
        product = get_product(session, payload["product_id"])
        warehouse = get_warehouse(session, payload["warehouse_id"], active=True)
        ensure_same_store(product, warehouse)
        balance = lock_balance(session, warehouse.id, product.id)
        balance.on_hand += qty
        return Draft(
            body={
                "message": f"Приход {qty} шт. {product.sku} {product.name} на склад «{warehouse.name}»",
            },
            product_id=product.id,
            qty=qty,
            destination_warehouse_id=warehouse.id,
            origin_text="Поставка",
        )

    return apply(session, key, "receipt", payload, action)


def transfer(session: Session, key: str, payload: dict) -> Applied | Replay:
    def action() -> Draft:
        qty = require_qty(payload["qty"])
        product = get_product(session, payload["product_id"])
        source = get_warehouse(session, payload["from_warehouse_id"], active=True)
        destination = get_warehouse(session, payload["to_warehouse_id"], active=True)
        if source.id == destination.id:
            raise InventoryError(409, "same_warehouse", "Нельзя переместить товар на тот же склад")
        ensure_same_store(product, source)
        ensure_same_store(product, destination)
        balance = lock_balance(session, source.id, product.id)
        assert_available(session, source, product, balance, qty, purpose="transfer")
        balance.on_hand -= qty
        shipment = Shipment(
            kind=SHIPMENT_TRANSFER,
            status=SHIPMENT_IN_TRANSIT,
            product_id=product.id,
            qty=qty,
            from_warehouse_id=source.id,
            to_warehouse_id=destination.id,
        )
        session.add(shipment)
        session.flush()
        return Draft(
            body={
                "message": f"В пути {qty} шт. {product.sku} {product.name}: «{source.name}» → «{destination.name}»",
                "shipment_id": shipment.id,
            },
            product_id=product.id,
            qty=qty,
            origin_warehouse_id=source.id,
            destination_warehouse_id=destination.id,
            shipment_id=shipment.id,
        )

    return apply(session, key, "transfer_ship", payload, action)


def _receive(session: Session, shipment_id: int, kind: str) -> Draft:
    shipment = lock_shipment(session, shipment_id)
    if shipment.kind != kind:
        raise InventoryError(409, "wrong_shipment", "Документ другого типа")
    if shipment.status != SHIPMENT_IN_TRANSIT:
        raise InventoryError(
            409,
            "already_received",
            "Поставка уже принята. Повторная приёмка с новым ключом ничего не добавляет.",
        )
    product = get_product(session, shipment.product_id)
    destination_warehouse_id = None
    destination_text = None
    if kind == SHIPMENT_TRANSFER:
        if shipment.to_warehouse_id is None:
            raise InventoryError(409, "wrong_shipment", "У перемещения не указан склад назначения")
        destination = get_warehouse(session, shipment.to_warehouse_id, active=False)
        balance = lock_balance(session, destination.id, product.id)
        balance.on_hand += shipment.qty
        destination_warehouse_id = destination.id
        message = f"Принято {shipment.qty} шт. {product.sku} {product.name} на склад «{destination.name}»"
    else:
        marketplace = lock_marketplace(session, product.id)
        marketplace.on_hand += shipment.qty
        destination_text = "Маркетплейс"
        message = f"Маркетплейс принял {shipment.qty} шт. {product.sku} {product.name}"
    shipment.status = SHIPMENT_RECEIVED
    shipment.received_at = utcnow()
    return Draft(
        body={"message": message, "shipment_id": shipment.id},
        product_id=product.id,
        qty=shipment.qty,
        origin_text="В пути",
        destination_warehouse_id=destination_warehouse_id,
        destination_text=destination_text,
        shipment_id=shipment.id,
    )


def receive_transfer(session: Session, key: str, shipment_id: int) -> Applied | Replay:
    return apply(
        session,
        key,
        "transfer_receive",
        {"shipment_id": shipment_id},
        lambda: _receive(session, shipment_id, SHIPMENT_TRANSFER),
    )


def ship_fbo(session: Session, key: str, payload: dict) -> Applied | Replay:
    def action() -> Draft:
        qty = require_qty(payload["qty"])
        product = get_product(session, payload["product_id"])
        warehouse = get_warehouse(session, payload["warehouse_id"], active=True)
        ensure_same_store(product, warehouse)
        balance = lock_balance(session, warehouse.id, product.id)
        assert_available(session, warehouse, product, balance, qty, purpose="fbo")
        balance.on_hand -= qty
        shipment = Shipment(
            kind=SHIPMENT_FBO,
            status=SHIPMENT_IN_TRANSIT,
            product_id=product.id,
            qty=qty,
            from_warehouse_id=warehouse.id,
        )
        session.add(shipment)
        session.flush()
        return Draft(
            body={
                "message": (
                    f"Отправлено на маркетплейс {qty} шт. {product.sku} {product.name} "
                    f"со склада «{warehouse.name}». До приёмки товар в пути"
                ),
                "shipment_id": shipment.id,
            },
            product_id=product.id,
            qty=qty,
            origin_warehouse_id=warehouse.id,
            destination_text="Маркетплейс (в пути)",
            shipment_id=shipment.id,
        )

    return apply(session, key, "fbo_ship", payload, action)


def receive_fbo(session: Session, key: str, shipment_id: int) -> Applied | Replay:
    return apply(
        session,
        key,
        "fbo_receive",
        {"shipment_id": shipment_id},
        lambda: _receive(session, shipment_id, SHIPMENT_FBO),
    )


def reserve_fbs(session: Session, key: str, payload: dict) -> Applied | Replay:
    def action() -> Draft:
        qty = require_qty(payload["qty"])
        product = get_product(session, payload["product_id"])
        warehouse = get_warehouse(session, payload["warehouse_id"], active=True)
        ensure_same_store(product, warehouse)
        balance = lock_balance(session, warehouse.id, product.id)
        assert_available(session, warehouse, product, balance, qty, purpose="reserve")
        balance.reserved_fbs += qty
        reservation = Reservation(
            warehouse_id=warehouse.id,
            product_id=product.id,
            qty=qty,
            status=RESERVATION_ACTIVE,
        )
        session.add(reservation)
        session.flush()
        return Draft(
            body={
                "message": f"Резерв FBS {qty} шт. {product.sku} {product.name} на складе «{warehouse.name}»",
                "reservation_id": reservation.id,
            },
            product_id=product.id,
            qty=qty,
            origin_warehouse_id=warehouse.id,
            reservation_id=reservation.id,
        )

    return apply(session, key, "fbs_reserve", payload, action)


def cancel_fbs(session: Session, key: str, reservation_id: int) -> Applied | Replay:
    def action() -> Draft:
        reservation = lock_reservation(session, reservation_id)
        if reservation.status != RESERVATION_ACTIVE:
            raise InventoryError(
                409,
                "reservation_closed",
                "Резерв уже закрыт. Повторная отмена ничего не меняет.",
            )
        warehouse = get_warehouse(session, reservation.warehouse_id, active=False)
        product = get_product(session, reservation.product_id)
        balance = lock_balance(session, warehouse.id, product.id)
        if balance.reserved_fbs < reservation.qty:
            raise InventoryError(409, "reservation_broken", "Резерв не сходится с остатком склада. Отмена остановлена.")
        balance.reserved_fbs -= reservation.qty
        reservation.status = RESERVATION_CANCELLED
        reservation.closed_at = utcnow()
        return Draft(
            body={
                "message": f"Резерв FBS снят: {reservation.qty} шт. {product.sku} {product.name}, склад «{warehouse.name}»",
                "reservation_id": reservation.id,
            },
            product_id=product.id,
            qty=reservation.qty,
            origin_warehouse_id=warehouse.id,
            reservation_id=reservation.id,
        )

    return apply(session, key, "fbs_cancel", {"reservation_id": reservation_id}, action)


def ship_fbs(session: Session, key: str, reservation_id: int) -> Applied | Replay:
    def action() -> Draft:
        reservation = lock_reservation(session, reservation_id)
        if reservation.status != RESERVATION_ACTIVE:
            raise InventoryError(
                409,
                "reservation_closed",
                "Резерв уже закрыт, отправить его ещё раз нельзя.",
            )
        warehouse = get_warehouse(session, reservation.warehouse_id, active=False)
        product = get_product(session, reservation.product_id)
        balance = lock_balance(session, warehouse.id, product.id)
        if balance.reserved_fbs < reservation.qty or balance.on_hand < reservation.qty:
            raise InventoryError(409, "reservation_broken", "Резерв не сходится с остатком склада. Отправка остановлена.")
        balance.reserved_fbs -= reservation.qty
        balance.on_hand -= reservation.qty
        shipment = Shipment(
            kind=SHIPMENT_FBS,
            status=SHIPMENT_IN_TRANSIT,
            product_id=product.id,
            qty=reservation.qty,
            from_warehouse_id=warehouse.id,
        )
        session.add(shipment)
        session.flush()
        reservation.status = RESERVATION_SHIPPED
        reservation.shipment_id = shipment.id
        reservation.closed_at = utcnow()
        return Draft(
            body={
                "message": f"Отправлено клиенту {reservation.qty} шт. {product.sku} {product.name} со склада «{warehouse.name}»",
                "reservation_id": reservation.id,
                "shipment_id": shipment.id,
            },
            product_id=product.id,
            qty=reservation.qty,
            origin_warehouse_id=warehouse.id,
            destination_text="Клиент",
            shipment_id=shipment.id,
            reservation_id=reservation.id,
        )

    return apply(session, key, "fbs_ship", {"reservation_id": reservation_id}, action)


def _name_taken(session: Session, folded: str, except_id: int | None = None) -> bool:
    query = select(Warehouse.id).where(
        Warehouse.status == WAREHOUSE_ACTIVE,
        Warehouse.name_folded == folded,
    )
    if except_id is not None:
        query = query.where(Warehouse.id != except_id)
    return session.scalar(query) is not None


def create_warehouse(session: Session, key: str, payload: dict) -> Applied | Replay:
    def action() -> Draft:
        name = normalize_name(payload["name"])
        folded = name.casefold()
        if _name_taken(session, folded):
            raise InventoryError(409, "warehouse_exists", "Активный склад с таким названием уже есть")
        store = session.scalar(select(Store).order_by(Store.id).limit(1))
        if store is None:
            raise InventoryError(409, "no_store", "Сначала нужен магазин")
        warehouse = Warehouse(store_id=store.id, name=name, name_folded=folded, status=WAREHOUSE_ACTIVE)
        session.add(warehouse)
        session.flush()
        return Draft(
            body={"message": f"Склад «{name}» создан", "warehouse_id": warehouse.id},
            destination_warehouse_id=warehouse.id,
        )

    return apply(session, key, "warehouse_create", payload, action)


def rename_warehouse(session: Session, key: str, warehouse_id: int, name: str) -> Applied | Replay:
    cleaned = normalize_name(name)

    def action() -> Draft | NoOp:
        warehouse = get_warehouse(session, warehouse_id, active=False)
        if warehouse.name == cleaned:
            return NoOp(
                body={
                    "message": f"Склад уже называется «{cleaned}»",
                    "warehouse_id": warehouse.id,
                }
            )
        folded = cleaned.casefold()
        if _name_taken(session, folded, except_id=warehouse.id):
            raise InventoryError(409, "warehouse_exists", "Активный склад с таким названием уже есть")
        old = warehouse.name
        warehouse.name = cleaned
        warehouse.name_folded = folded
        return Draft(
            body={
                "message": f"Склад «{old}» теперь называется «{cleaned}»",
                "warehouse_id": warehouse.id,
            },
            origin_text=old,
            destination_text=cleaned,
        )

    return apply(session, key, "warehouse_rename", {"warehouse_id": warehouse_id, "name": cleaned}, action)


def archive_warehouse(session: Session, key: str, warehouse_id: int) -> Applied | Replay:
    def action() -> Draft:
        warehouse = get_warehouse(session, warehouse_id, active=False)
        if warehouse.status == WAREHOUSE_ARCHIVED:
            raise InventoryError(
                409,
                "warehouse_archived",
                f"Склад «{warehouse.name}» уже в архиве. Остатки на месте.",
            )
        warehouse.status = WAREHOUSE_ARCHIVED
        warehouse.archived_at = utcnow()
        return Draft(
            body={
                "message": f"Склад «{warehouse.name}» удалён. Остатки и история сохранены, в таблице он отмечен как архив.",
                "warehouse_id": warehouse.id,
            },
            origin_warehouse_id=warehouse.id,
        )

    return apply(session, key, "warehouse_archive", {"warehouse_id": warehouse_id}, action)
