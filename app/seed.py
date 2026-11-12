"""Тестовый приход из задания. Повторный запуск не удваивает остатки: ключи прихода фиксированы."""

from contextlib import contextmanager

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.inventory import receipt
from app.models import Base, Operation, Product, Store, Warehouse

LOCK_ID = 814201
SEED_MARKER = "seed-SKU-101-Север"
RECEIPTS = (
    ("SKU-101", "Север", 10),
    ("SKU-101", "Юг", 6),
    ("SKU-102", "Север", 5),
    ("SKU-103", "Юг", 4),
)


@contextmanager
def _seed_lock():
    session = SessionLocal()
    session.execute(text("SELECT pg_advisory_lock(CAST(:lock_id AS bigint))"), {"lock_id": LOCK_ID})
    session.commit()
    try:
        yield
    finally:
        session.execute(text("SELECT pg_advisory_unlock(CAST(:lock_id AS bigint))"), {"lock_id": LOCK_ID})
        session.commit()
        session.close()


def _insert_masters(session: Session) -> None:
    store = Store(name="Магазин")
    session.add(store)
    session.flush()
    session.add_all(
        [
            Warehouse(store_id=store.id, name="Север", name_folded="Север".casefold(), status="active"),
            Warehouse(store_id=store.id, name="Юг", name_folded="Юг".casefold(), status="active"),
            Product(store_id=store.id, sku="SKU-101", name="Органайзер"),
            Product(store_id=store.id, sku="SKU-102", name="Контейнер"),
            Product(store_id=store.id, sku="SKU-103", name="Корзина"),
        ]
    )


def _post_receipts() -> None:
    with SessionLocal() as session:
        warehouses = {row.name: row.id for row in session.scalars(select(Warehouse))}
        products = {row.sku: row.id for row in session.scalars(select(Product))}
    for sku, warehouse_name, qty in RECEIPTS:
        with SessionLocal() as session:
            receipt(
                session,
                f"seed-{sku}-{warehouse_name}",
                {
                    "warehouse_id": warehouses[warehouse_name],
                    "product_id": products[sku],
                    "qty": qty,
                },
            )


def _ensure_demo_contents() -> bool:
    with SessionLocal() as session:
        marker = session.scalar(select(Operation.id).where(Operation.idempotency_key == SEED_MARKER))
        if marker is not None:
            return False
        if session.scalar(select(Product.id).limit(1)) is None:
            _insert_masters(session)
            session.commit()
    _post_receipts()
    return True


def _truncate(session: Session) -> None:
    names = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    session.execute(text(f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"))


def seed_demo() -> bool:
    with _seed_lock():
        return _ensure_demo_contents()


def reset_demo() -> None:
    with _seed_lock():
        with SessionLocal() as session, session.begin():
            _truncate(session)
        _ensure_demo_contents()
