"""Liveness / readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .. import __version__
from ..config import get_settings
from ..core.faq import get_index
from ..core.factory import get_engine
from ..database import get_db
from ..models import Conversation, FaqEntry, Message

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "version": __version__}


@router.get("/readyz")
def readyz(db: Session = Depends(get_db)) -> dict:
    settings = get_settings()

    database_ok = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        database_ok = False

    faq_count = db.execute(select(func.count(FaqEntry.id))).scalar_one()
    conversation_count = db.execute(select(func.count(Conversation.id))).scalar_one()
    message_count = db.execute(select(func.count(Message.id))).scalar_one()

    return {
        "status": "ready" if database_ok else "not_ready",
        "version": __version__,
        "engine_mode": settings.engine_mode,
        "engine": getattr(get_engine(settings), "engine_name", settings.engine_mode),
        "llm_enabled": settings.llm_enabled,
        "database": "ok" if database_ok else "unavailable",
        "faq_entries": faq_count,
        "conversations": conversation_count,
        "messages": message_count,
        "faq_index_size": len(getattr(get_index(db), "_docs", [])),
    }
