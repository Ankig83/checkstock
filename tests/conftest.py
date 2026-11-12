import os
import time

from sqlalchemy.engine.url import make_url

DEFAULT_URL = "postgresql+psycopg://checkstock:checkstock@localhost:5432/checkstock"
test_url = make_url(os.environ.get("DATABASE_URL", DEFAULT_URL)).set(database="checkstock_test")
os.environ["DATABASE_URL"] = test_url.render_as_string(hide_password=False)
os.environ["SEED_ON_STARTUP"] = "0"
TEST_URL = os.environ["DATABASE_URL"]

from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import OperationalError

from fastapi.testclient import TestClient
import pytest


def ensure_database() -> None:
    url = make_url(TEST_URL)
    last_error: OperationalError | None = None
    for _ in range(30):
        admin = None
        try:
            admin = create_engine(
                url.set(database="postgres").render_as_string(hide_password=False),
                isolation_level="AUTOCOMMIT",
            )
            with admin.connect() as connection:
                exists = connection.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = 'checkstock_test'")
                ).scalar()
                if not exists:
                    connection.execute(text("CREATE DATABASE checkstock_test"))
            return
        except OperationalError as error:
            last_error = error
            time.sleep(1)
        finally:
            if admin is not None:
                admin.dispose()
    raise RuntimeError(
        f"PostgreSQL недоступен по адресу {url.host}:{url.port}. Запустите сервер или: docker compose up -d"
    ) from last_error


ensure_database()

from app.db import engine
from app.main import app
from app.models import Base


@pytest.fixture()
def client():
    engine.dispose()
    Base.metadata.drop_all(engine)
    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()
