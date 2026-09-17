"""End-user conversation API."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core.engine import stream_outcome
from ..core.factory import get_engine
from ..database import get_db
from ..models import (
    Conversation,
    ConversationStatus,
    Message,
    MessageRole,
    Order,
    User,
    utcnow,
)
from ..rate_limit import SlidingWindowLimiter
from ..schemas import (
    ChatRequest,
    ChatResponse,
    ConversationDetail,
    ConversationOut,
    MessageOut,
    SatisfactionRequest,
    SourceRef,
)
from ..security import Principal, current_principal
from ..tools import business

logger = logging.getLogger("app.api.chat")

router = APIRouter(prefix="/api", tags=["chat"])

_settings = get_settings()
_limiter = SlidingWindowLimiter(_settings.rate_limit_per_minute)


def _enforce_rate_limit(principal: Principal) -> None:
    if not _limiter.allow(principal.user_id):
        raise HTTPException(status_code=429, detail="rate limit exceeded, please slow down")


def _conversation_or_404(db: Session, principal: Principal, conversation_id: str) -> Conversation:
    conversation = db.execute(
        select(Conversation).where(
            Conversation.conversation_id == conversation_id,
            Conversation.user_id == principal.user_id,
        )
    ).scalars().first()
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conversation


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #
@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    request: Request,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
) -> ChatResponse:
    _enforce_rate_limit(principal)
    engine = get_engine()

    outcome = await engine.handle(
        db,
        user_id=principal.user_id,
        message=payload.message,
        conversation_id=payload.conversation_id,
    )

    return ChatResponse(
        request_id=getattr(request.state, "request_id", ""),
        conversation_id=outcome.conversation_id,
        message_id=outcome.message_id,
        answer=outcome.answer,
        intent=outcome.intent,
        intent_label=outcome.intent_label,
        confidence=outcome.confidence,
        sources=[SourceRef(**source) for source in outcome.sources],
        handoff=outcome.handoff,
        handoff_reason=outcome.handoff_reason,
        ticket_id=outcome.ticket_id,
        proposal_id=outcome.proposal_id,
        suggestions=outcome.suggestions,
        latency_ms=outcome.latency_ms,
    )


@router.post("/chat/stream")
async def chat_stream(
    payload: ChatRequest,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    _enforce_rate_limit(principal)
    engine = get_engine()

    async def event_source():
        def sse(event: str, data: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        try:
            async for event, data in stream_outcome(
                engine,
                db,
                user_id=principal.user_id,
                message=payload.message,
                conversation_id=payload.conversation_id,
            ):
                yield sse(event, data)
        except Exception as exc:  # noqa: BLE001 - surface a terminal SSE event
            logger.exception("chat_stream_failed")
            yield sse("error", {"message": str(exc)})
        finally:
            yield "event: end\ndata: {}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# --------------------------------------------------------------------------- #
# Conversation history
# --------------------------------------------------------------------------- #
@router.get("/conversations", response_model=list[ConversationOut])
def list_conversations(
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
    limit: int = Query(default=30, ge=1, le=200),
) -> list[ConversationOut]:
    rows = db.execute(
        select(Conversation)
        .where(Conversation.user_id == principal.user_id)
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
    ).scalars()
    return [ConversationOut.model_validate(row, from_attributes=True) for row in rows]


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: str,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
) -> ConversationDetail:
    conversation = _conversation_or_404(db, principal, conversation_id)
    messages = db.execute(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id)
    ).scalars()

    detail = ConversationDetail.model_validate(conversation, from_attributes=True)
    detail.messages = [MessageOut.model_validate(row, from_attributes=True) for row in messages]
    return detail


@router.delete("/conversations/{conversation_id}", status_code=204, response_model=None)
def delete_conversation(
    conversation_id: str,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
) -> None:
    conversation = _conversation_or_404(db, principal, conversation_id)
    db.delete(conversation)
    db.commit()


@router.post("/conversations/{conversation_id}/close", response_model=ConversationOut)
def close_conversation(
    conversation_id: str,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
) -> ConversationOut:
    conversation = _conversation_or_404(db, principal, conversation_id)
    conversation.status = ConversationStatus.CLOSED
    conversation.updated_at = utcnow()
    db.commit()
    db.refresh(conversation)
    return ConversationOut.model_validate(conversation, from_attributes=True)


@router.post("/conversations/{conversation_id}/satisfaction", response_model=ConversationOut)
def rate_conversation(
    conversation_id: str,
    payload: SatisfactionRequest,
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
) -> ConversationOut:
    conversation = _conversation_or_404(db, principal, conversation_id)
    conversation.satisfaction_score = payload.score
    conversation.satisfaction_comment = payload.comment.strip()
    conversation.rated_at = utcnow()
    conversation.updated_at = utcnow()
    if conversation.status == ConversationStatus.HANDOFF_REPLIED:
        conversation.status = ConversationStatus.RESOLVED
    db.commit()
    db.refresh(conversation)
    return ConversationOut.model_validate(conversation, from_attributes=True)


# --------------------------------------------------------------------------- #
# Profile / business data for the chat widget
# --------------------------------------------------------------------------- #
@router.get("/me")
def me(
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
) -> dict:
    user = db.get(User, principal.user_id)
    orders = business.list_user_orders(db, principal.user_id, limit=5)
    return {
        "user_id": principal.user_id,
        "display_name": user.display_name if user else "",
        "level": user.level if user else "STANDARD",
        "orders": [business.describe_order(order) for order in orders],
        "order_count": db.execute(
            select(func.count(Order.order_id)).where(Order.user_id == principal.user_id)
        ).scalar_one(),
    }


@router.get("/messages")
def recent_messages(
    principal: Principal = Depends(current_principal),
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    """Flat recent transcript across all of the caller's conversations."""
    rows = db.execute(
        select(Message)
        .join(Conversation, Conversation.conversation_id == Message.conversation_id)
        .where(Conversation.user_id == principal.user_id)
        .order_by(Message.id.desc())
        .limit(limit)
    ).scalars()
    return {
        "items": [
            {
                "id": row.id,
                "conversation_id": row.conversation_id,
                "role": row.role,
                "content": row.content,
                "intent": row.intent,
                "created_at": row.created_at.isoformat(),
            }
            for row in reversed(list(rows))
        ]
    }


__all__ = ["router"]
