"""Bridge Manager — FastAPI.

Arranque:
    uvicorn app.main:app --host 0.0.0.0 --port 8088
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.limits import LimitarCuerpo
from app.api.routes import router
from app.config import get_settings
from app.manager.slot_manager import SlotManager
from app.utils import setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()
    manager = SlotManager(settings)
    await manager.start()
    app.state.slot_manager = manager
    yield
    await manager.shutdown()


app = FastAPI(
    title="MT5 Bridge Manager",
    version="1.0.0",
    description="Pool de terminales MT5 portables y eventos hacia el Core.",
    lifespan=lifespan,
)
app.include_router(router, prefix="/api/v1")
# El tope va delante de todo: la autenticación es una dependencia de ruta y se
# resuelve DESPUÉS de leer el cuerpo, así que sin esto un anónimo puede hacer
# trabajar al proceso que atiende a todas las cuentas.
app.add_middleware(LimitarCuerpo, max_bytes=get_settings().max_request_bytes)


@app.get("/")
def root() -> dict:
    return {"service": "mt5-bridge", "docs": "/docs"}
