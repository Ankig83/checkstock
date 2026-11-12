from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

from app.db import init_db
from app.errors import InventoryError, UTF8JSONResponse
from app.routes import router
from app.seed import seed_demo

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    from app.config import settings

    if settings.seed_on_startup:
        seed_demo()
    yield


app = FastAPI(title="CheckStock", version="1.0.0", lifespan=lifespan, default_response_class=UTF8JSONResponse)
app.include_router(router)


@app.exception_handler(InventoryError)
def handle_inventory(_request, exc: InventoryError):
    return UTF8JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message, "details": exc.details},
    )


@app.exception_handler(RequestValidationError)
def handle_validation(_request, exc: RequestValidationError):
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"] if item != "body")
        parts.append(f"{location}: {error['msg']}" if location else error["msg"])
    return UTF8JSONResponse(
        status_code=422,
        content={
            "code": "validation",
            "message": "; ".join(parts) or "Некорректный запрос",
            "details": {},
        },
    )


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


class NoCacheStatic(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response


app.mount("/static", NoCacheStatic(directory=STATIC), name="static")
