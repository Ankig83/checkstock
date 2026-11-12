import time

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    from app.models import Base

    last_error: OperationalError | None = None
    for _ in range(30):
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            Base.metadata.create_all(engine)
            return
        except OperationalError as error:
            last_error = error
            time.sleep(1)
    raise RuntimeError("PostgreSQL недоступен. Запустите: docker compose up -d") from last_error


def session_scope() -> Session:
    return SessionLocal()
