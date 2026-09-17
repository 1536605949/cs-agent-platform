"""FastAPI application entry point.

Run locally::

    python -m uvicorn app.main:app --reload --port 8000

Then open http://localhost:8000 for the customer chat and
http://localhost:8000/admin for the back office (default admin / admin123).
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import admin, chat, health
from .config import get_settings, validate_settings
from .core.llm import get_llm
from .database import SessionLocal, init_db
from .tools.business import seed_demo_data

settings = get_settings()
validate_settings(settings)

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    if settings.auto_seed:
        with SessionLocal() as db:
            created = seed_demo_data(db)
            if any(created.values()):
                logger.info("seeded_demo_data %s", created)
    logger.info(
        "startup engine_mode=%s llm_enabled=%s database=%s",
        settings.engine_mode,
        settings.llm_enabled,
        settings.database_url.split("://", 1)[0],
    )
    yield
    await get_llm().aclose()


app = FastAPI(
    title="AI Customer Service Platform",
    description=(
        "Multi-turn customer service assistant with FAQ retrieval, intent recognition, "
        "human handoff, conversation persistence and a back-office console."
    ),
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    request.state.request_id = request_id
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception:
        logger.exception("unhandled_error request_id=%s path=%s", request_id, request.url.path)
        raise
    finally:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        if not request.url.path.startswith("/static"):
            logger.info(
                "request path=%s status=%s elapsed_ms=%s request_id=%s",
                request.url.path,
                status_code,
                elapsed_ms,
                request_id,
            )

    response.headers["X-Request-ID"] = request_id
    return response


app.include_router(health.router)
app.include_router(chat.router)
app.include_router(admin.router)

app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(settings.static_dir / "index.html")


@app.get("/admin", include_in_schema=False)
def admin_console() -> FileResponse:
    return FileResponse(settings.static_dir / "admin.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> JSONResponse:
    return JSONResponse(status_code=204, content=None)
