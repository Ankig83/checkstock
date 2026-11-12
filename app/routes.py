from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query

from app.db import SessionLocal
from app.errors import InventoryError, UTF8JSONResponse
from app.inventory import (
    Applied,
    Replay,
    archive_warehouse,
    cancel_fbs,
    create_warehouse,
    receipt,
    receive_fbo,
    receive_transfer,
    rename_warehouse,
    reserve_fbs,
    ship_fbo,
    ship_fbs,
    transfer,
)
from app.queries import operation_history, stock_snapshot
from app.schemas import FboIn, ReceiptIn, ReserveIn, TransferIn, WarehouseCreate, WarehouseRename
from app.seed import reset_demo

router = APIRouter(prefix="/api")


def get_db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    if idempotency_key is None or not idempotency_key.strip():
        raise InventoryError(
            400,
            "idempotency_key_required",
            "Нужен заголовок Idempotency-Key — без него повтор кнопки можно принять за новую отгрузку.",
        )
    key = idempotency_key.strip()
    if len(key) > 200:
        raise InventoryError(400, "idempotency_key_required", "Idempotency-Key длиннее 200 символов")
    return key


def present(result: Applied | Replay):
    if isinstance(result, Replay):
        return UTF8JSONResponse(content=result.body, status_code=result.status)
    return result.body


@router.get("/health")
def health():
    from sqlalchemy import text

    with SessionLocal() as session:
        session.execute(text("SELECT 1"))
    return {"status": "ok"}


@router.get("/stock")
def stock(db=Depends(get_db)):
    return stock_snapshot(db)


@router.get("/operations")
def operations(limit: int = Query(default=500, ge=1, le=2000), db=Depends(get_db)):
    return operation_history(db, limit=limit)


@router.post("/demo/reset")
def demo_reset():
    reset_demo()
    return {"message": "Тестовые данные загружены: склады Север и Юг, приход по трём артикулам."}


@router.post("/warehouses")
def post_warehouse(payload: WarehouseCreate, db=Depends(get_db), key: str = Depends(idempotency_key)):
    return present(create_warehouse(db, key, payload.model_dump()))


@router.patch("/warehouses/{warehouse_id}")
def patch_warehouse(
    warehouse_id: int,
    payload: WarehouseRename,
    db=Depends(get_db),
    key: str = Depends(idempotency_key),
):
    return present(rename_warehouse(db, key, warehouse_id, payload.name))


@router.post("/warehouses/{warehouse_id}/archive")
def post_archive(warehouse_id: int, db=Depends(get_db), key: str = Depends(idempotency_key)):
    return present(archive_warehouse(db, key, warehouse_id))


@router.post("/receipts")
def post_receipt(payload: ReceiptIn, db=Depends(get_db), key: str = Depends(idempotency_key)):
    return present(receipt(db, key, payload.model_dump()))


@router.post("/transfers")
def post_transfer(payload: TransferIn, db=Depends(get_db), key: str = Depends(idempotency_key)):
    return present(transfer(db, key, payload.model_dump()))


@router.post("/transfers/{shipment_id}/receive")
def post_receive_transfer(shipment_id: int, db=Depends(get_db), key: str = Depends(idempotency_key)):
    return present(receive_transfer(db, key, shipment_id))


@router.post("/fbs/reservations")
def post_reserve(payload: ReserveIn, db=Depends(get_db), key: str = Depends(idempotency_key)):
    return present(reserve_fbs(db, key, payload.model_dump()))


@router.post("/fbs/reservations/{reservation_id}/cancel")
def post_cancel(reservation_id: int, db=Depends(get_db), key: str = Depends(idempotency_key)):
    return present(cancel_fbs(db, key, reservation_id))


@router.post("/fbs/reservations/{reservation_id}/ship")
def post_ship_fbs(reservation_id: int, db=Depends(get_db), key: str = Depends(idempotency_key)):
    return present(ship_fbs(db, key, reservation_id))


@router.post("/fbo/shipments")
def post_fbo(payload: FboIn, db=Depends(get_db), key: str = Depends(idempotency_key)):
    return present(ship_fbo(db, key, payload.model_dump()))


@router.post("/fbo/shipments/{shipment_id}/receive")
def post_receive_fbo(shipment_id: int, db=Depends(get_db), key: str = Depends(idempotency_key)):
    return present(receive_fbo(db, key, shipment_id))
