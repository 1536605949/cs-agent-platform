"""Back-office API: conversation records, satisfaction analytics, human queue,
refund review and FAQ knowledge base management."""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..core.faq import invalidate_faq_cache
from ..core.intent import INTENT_LABELS
from ..database import get_db
from ..models import (
    Conversation,
    ConversationStatus,
    FaqEntry,
    HandoffStatus,
    HandoffTask,
    Message,
    MessageRole,
    RefundProposal,
    Ticket,
    utcnow,
)
from ..schemas import (
    AdminLoginRequest,
    AdminToken,
    ConversationDetail,
    ConversationOut,
    ConversationPage,
    FaqIn,
    FaqOut,
    HandoffOut,
    HandoffReplyRequest,
    IntentSlice,
    MessageOut,
    OverviewStats,
    RefundOut,
    RefundReviewRequest,
    SatisfactionStats,
    TrendPoint,
)
from ..security import Principal, authenticate_admin, create_token, current_admin
from ..tools import business

logger = logging.getLogger("app.api.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])

_LABELS = INTENT_LABELS
_LABELS[""] = "未识别"


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
@router.post("/login", response_model=AdminToken)
def login(payload: AdminLoginRequest) -> AdminToken:
    settings = get_settings()
    if not authenticate_admin(payload.username, payload.password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_token(payload.username.strip(), roles=["admin"])
    return AdminToken(
        access_token=token,
        expires_in=settings.jwt_expire_minutes * 60,
        username=payload.username.strip(),
    )


@router.get("/me")
def whoami(principal: Principal = Depends(current_admin)) -> dict:
    return {"username": principal.user_id, "roles": sorted(principal.roles)}


# --------------------------------------------------------------------------- #
# Dashboard statistics
# --------------------------------------------------------------------------- #
@router.get("/stats/overview", response_model=OverviewStats)
def stats_overview(
    _: Principal = Depends(current_admin),
    db: Session = Depends(get_db),
) -> OverviewStats:
    total_conversations = db.execute(select(func.count(Conversation.id))).scalar_one()
    total_messages = db.execute(select(func.count(Message.id))).scalar_one()
    active = db.execute(
        select(func.count(Conversation.id)).where(Conversation.status == ConversationStatus.ACTIVE)
    ).scalar_one()
    resolved = db.execute(
        select(func.count(Conversation.id)).where(
            Conversation.status.in_((ConversationStatus.RESOLVED, ConversationStatus.CLOSED))
        )
    ).scalar_one()
    handoff = db.execute(
        select(func.count(Conversation.id)).where(Conversation.handoff_count > 0)
    ).scalar_one()
    rated = db.execute(
        select(func.count(Conversation.id)).where(Conversation.satisfaction_score.is_not(None))
    ).scalar_one()
    satisfaction_avg = db.execute(select(func.avg(Conversation.satisfaction_score))).scalar()

    return OverviewStats(
        conversations=total_conversations,
        messages=total_messages,
        active_conversations=active,
        resolved_conversations=resolved,
        handoff_conversations=handoff,
        handoff_rate=round(handoff / total_conversations, 4) if total_conversations else 0.0,
        rated_conversations=rated,
        satisfaction_avg=round(float(satisfaction_avg), 2) if satisfaction_avg is not None else 0.0,
        satisfaction_rate=round(rated / total_conversations, 4) if total_conversations else 0.0,
        refunds_pending=db.execute(
            select(func.count(RefundProposal.proposal_id)).where(RefundProposal.status == "pending_review")
        ).scalar_one(),
        tickets_open=db.execute(
            select(func.count(Ticket.ticket_id)).where(Ticket.status == "created")
        ).scalar_one(),
        handoffs_pending=db.execute(
            select(func.count(HandoffTask.id)).where(HandoffTask.status == HandoffStatus.PENDING)
        ).scalar_one(),
    )


@router.get("/stats/satisfaction", response_model=SatisfactionStats)
def stats_satisfaction(
    _: Principal = Depends(current_admin),
    db: Session = Depends(get_db),
) -> SatisfactionStats:
    rows = db.execute(
        select(Conversation.satisfaction_score, func.count(Conversation.id))
        .where(Conversation.satisfaction_score.is_not(None))
        .group_by(Conversation.satisfaction_score)
    ).all()

    distribution = {str(score): 0 for score in range(1, 6)}
    for score, count in rows:
        if score is not None:
            distribution[str(int(score))] = count

    rated = sum(distribution.values())
    average = sum(int(score) * count for score, count in distribution.items()) / rated if rated else 0.0
    positive = sum(count for score, count in distribution.items() if int(score) >= 4)
    negative = sum(count for score, count in distribution.items() if int(score) <= 2)

    return SatisfactionStats(
        rated=rated,
        average=round(average, 2),
        distribution=distribution,
        positive_rate=round(positive / rated, 4) if rated else 0.0,
        negative_rate=round(negative / rated, 4) if rated else 0.0,
    )


@router.get("/stats/intents", response_model=list[IntentSlice])
def stats_intents(
    _: Principal = Depends(current_admin),
    db: Session = Depends(get_db),
) -> list[IntentSlice]:
    rows = db.execute(
        select(Conversation.primary_intent, func.count(Conversation.id))
        .group_by(Conversation.primary_intent)
        .order_by(func.count(Conversation.id).desc())
    ).all()

    total = sum(count for _, count in rows) or 0
    return [
        IntentSlice(
            intent=intent or "unknown",
            label=_LABELS.get(intent or "", intent or "未识别"),
            count=count,
            share=round(count / total, 4) if total else 0.0,
        )
        for intent, count in rows
    ]


@router.get("/stats/trend", response_model=list[TrendPoint])
def stats_trend(
    _: Principal = Depends(current_admin),
    db: Session = Depends(get_db),
    days: int = Query(default=14, ge=1, le=90),
) -> list[TrendPoint]:
    since = utcnow() - timedelta(days=days - 1)

    conversation_rows = db.execute(
        select(
            func.date(Conversation.created_at),
            func.count(Conversation.id),
            func.avg(Conversation.satisfaction_score),
        )
        .where(Conversation.created_at >= since)
        .group_by(func.date(Conversation.created_at))
    ).all()

    message_rows = db.execute(
        select(func.date(Message.created_at), func.count(Message.id))
        .where(Message.created_at >= since)
        .group_by(func.date(Message.created_at))
    ).all()

    buckets: dict[str, dict] = {}
    for date_value, count, avg_score in conversation_rows:
        key = str(date_value)[:10]
        buckets.setdefault(key, {"conversations": 0, "messages": 0, "satisfaction_avg": None})
        buckets[key]["conversations"] = count
        buckets[key]["satisfaction_avg"] = round(float(avg_score), 2) if avg_score is not None else None

    for date_value, count in message_rows:
        key = str(date_value)[:10]
        buckets.setdefault(key, {"conversations": 0, "messages": 0, "satisfaction_avg": None})
        buckets[key]["messages"] = count

    today = utcnow().date()
    points: list[TrendPoint] = []
    for offset in range(days):
        key = (today - timedelta(days=days - 1 - offset)).isoformat()
        bucket = buckets.get(key, {"conversations": 0, "messages": 0, "satisfaction_avg": None})
        points.append(TrendPoint(date=key, **bucket))
    return points


# --------------------------------------------------------------------------- #
# Conversation records
# --------------------------------------------------------------------------- #
@router.get("/conversations", response_model=ConversationPage)
def list_conversations(
    _: Principal = Depends(current_admin),
    db: Session = Depends(get_db),
    intent: str = Query(default=""),
    status: str = Query(default=""),
    q: str = Query(default=""),
    user_id: str = Query(default=""),
    handoff_only: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> ConversationPage:
    statement = select(Conversation)
    if intent:
        statement = statement.where(Conversation.primary_intent == intent)
    if status:
        statement = statement.where(Conversation.status == status)
    if user_id:
        statement = statement.where(Conversation.user_id == user_id)
    if handoff_only:
        statement = statement.where(Conversation.handoff_count > 0)
    if q:
        pattern = f"%{q.strip()}%"
        matching_ids = select(Message.conversation_id).where(Message.content.like(pattern))
        statement = statement.where(
            or_(
                Conversation.title.like(pattern),
                Conversation.user_id.like(pattern),
                Conversation.conversation_id.like(pattern),
                Conversation.conversation_id.in_(matching_ids),
            )
        )

    total = db.execute(select(func.count()).select_from(statement.subquery())).scalar_one()
    rows = db.execute(
        statement.order_by(Conversation.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).scalars()

    return ConversationPage(
        total=total,
        page=page,
        page_size=page_size,
        items=[ConversationOut.model_validate(row, from_attributes=True) for row in rows],
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: str,
    _: Principal = Depends(current_admin),
    db: Session = Depends(get_db),
) -> ConversationDetail:
    conversation = db.execute(
        select(Conversation).where(Conversation.conversation_id == conversation_id)
    ).scalars().first()
    if conversation is None:
        raise HTTPException(status_code=404, detail="conversation not found")

    messages = db.execute(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id)
    ).scalars()

    detail = ConversationDetail.model_validate(conversation, from_attributes=True)
    detail.messages = [MessageOut.model_validate(row, from_attributes=True) for row in messages]
    return detail


@router.get("/messages", response_model=list[MessageOut])
def search_messages(
    _: Principal = Depends(current_admin),
    db: Session = Depends(get_db),
    q: str = Query(default="", min_length=1),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[MessageOut]:
    rows = db.execute(
        select(Message).where(Message.content.like(f"%{q.strip()}%")).order_by(Message.id.desc()).limit(limit)
    ).scalars()
    return [MessageOut.model_validate(row, from_attributes=True) for row in rows]


# --------------------------------------------------------------------------- #
# Human handoff queue
# --------------------------------------------------------------------------- #
@router.get("/handoffs", response_model=list[HandoffOut])
def list_handoffs(
    _: Principal = Depends(current_admin),
    db: Session = Depends(get_db),
    status: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[HandoffOut]:
    statement = select(HandoffTask)
    if status:
        statement = statement.where(HandoffTask.status == status)
    rows = db.execute(statement.order_by(HandoffTask.created_at.desc()).limit(limit)).scalars()
    return [HandoffOut.model_validate(row, from_attributes=True) for row in rows]


@router.post("/handoffs/{handoff_id}/reply", response_model=HandoffOut)
def reply_handoff(
    handoff_id: int,
    payload: HandoffReplyRequest,
    principal: Principal = Depends(current_admin),
    db: Session = Depends(get_db),
) -> HandoffOut:
    task = db.get(HandoffTask, handoff_id)
    if task is None:
        raise HTTPException(status_code=404, detail="handoff task not found")

    conversation = db.execute(
        select(Conversation).where(Conversation.conversation_id == task.conversation_id)
    ).scalars().first()

    db.add(
        Message(
            conversation_id=task.conversation_id,
            role=MessageRole.AGENT,
            content=payload.content.strip(),
            intent=conversation.last_intent if conversation else "",
            confidence=1.0,
        )
    )

    task.agent_id = principal.user_id
    task.status = HandoffStatus.RESOLVED if payload.resolve else HandoffStatus.REPLIED
    if payload.resolve:
        task.resolved_at = utcnow()

    if conversation is not None:
        conversation.message_count += 1
        conversation.updated_at = utcnow()
        conversation.status = ConversationStatus.RESOLVED if payload.resolve else ConversationStatus.HANDOFF_REPLIED

    business.audit(
        db,
        "handoff_replied",
        task.user_id,
        task.conversation_id,
        {"agent_id": principal.user_id, "resolved": payload.resolve},
    )
    db.commit()
    db.refresh(task)
    return HandoffOut.model_validate(task, from_attributes=True)


@router.post("/handoffs/{handoff_id}/resolve", response_model=HandoffOut)
def resolve_handoff(
    handoff_id: int,
    principal: Principal = Depends(current_admin),
    db: Session = Depends(get_db),
) -> HandoffOut:
    task = db.get(HandoffTask, handoff_id)
    if task is None:
        raise HTTPException(status_code=404, detail="handoff task not found")

    task.status = HandoffStatus.RESOLVED
    task.agent_id = task.agent_id or principal.user_id
    task.resolved_at = utcnow()

    conversation = db.execute(
        select(Conversation).where(Conversation.conversation_id == task.conversation_id)
    ).scalars().first()
    if conversation is not None:
        conversation.status = ConversationStatus.RESOLVED
        conversation.updated_at = utcnow()

    db.commit()
    db.refresh(task)
    return HandoffOut.model_validate(task, from_attributes=True)


# --------------------------------------------------------------------------- #
# Refund review queue
# --------------------------------------------------------------------------- #
@router.get("/refunds", response_model=list[RefundOut])
def list_refunds(
    _: Principal = Depends(current_admin),
    db: Session = Depends(get_db),
    status: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=500),
) -> list[RefundOut]:
    rows = business.list_refunds(db, status=status, limit=limit)
    return [RefundOut.model_validate(row, from_attributes=True) for row in rows]


@router.post("/refunds/{proposal_id}/review", response_model=RefundOut)
def review_refund(
    proposal_id: str,
    payload: RefundReviewRequest,
    principal: Principal = Depends(current_admin),
    db: Session = Depends(get_db),
) -> RefundOut:
    proposal = db.get(RefundProposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="refund proposal not found")

    reviewed, error = business.approve_or_reject_refund(
        db,
        proposal.order_id,
        payload.decision,
        principal.user_id,
        payload.comment,
    )
    if error:
        raise HTTPException(status_code=409, detail=error)

    if payload.decision == "approve" and payload.execute:
        executed, exec_error = business.execute_approved_refund(db, proposal.order_id)
        if exec_error is None and executed is not None:
            reviewed = executed
        else:
            logger.warning("refund_execute_skipped proposal=%s error=%s", proposal_id, exec_error)

    return RefundOut.model_validate(reviewed, from_attributes=True)


@router.get("/tickets")
def list_tickets(
    _: Principal = Depends(current_admin),
    db: Session = Depends(get_db),
    status: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=500),
) -> list[dict]:
    rows = business.list_tickets(db, status=status, limit=limit)
    return [
        {
            "ticket_id": row.ticket_id,
            "user_id": row.user_id,
            "conversation_id": row.conversation_id,
            "category": row.category,
            "issue": row.issue,
            "status": row.status,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


# --------------------------------------------------------------------------- #
# FAQ knowledge base management
# --------------------------------------------------------------------------- #
@router.get("/faqs", response_model=list[FaqOut])
def list_faqs(
    _: Principal = Depends(current_admin),
    db: Session = Depends(get_db),
    category: str = Query(default=""),
    q: str = Query(default=""),
    limit: int = Query(default=500, ge=1, le=1000),
) -> list[FaqOut]:
    statement = select(FaqEntry)
    if category:
        statement = statement.where(FaqEntry.category == category)
    if q:
        pattern = f"%{q.strip()}%"
        statement = statement.where(or_(FaqEntry.question.like(pattern), FaqEntry.keywords.like(pattern)))
    rows = db.execute(statement.order_by(FaqEntry.category, FaqEntry.id).limit(limit)).scalars()
    return [FaqOut.model_validate(row, from_attributes=True) for row in rows]


@router.post("/faqs", response_model=FaqOut, status_code=201)
def create_faq(
    payload: FaqIn,
    _: Principal = Depends(current_admin),
    db: Session = Depends(get_db),
) -> FaqOut:
    entry = FaqEntry(
        category=payload.category.strip() or "general",
        question=payload.question.strip(),
        answer=payload.answer.strip(),
        keywords=payload.keywords.strip(),
        enabled=payload.enabled,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    invalidate_faq_cache()
    return FaqOut.model_validate(entry, from_attributes=True)


@router.put("/faqs/{faq_id}", response_model=FaqOut)
def update_faq(
    faq_id: int,
    payload: FaqIn,
    _: Principal = Depends(current_admin),
    db: Session = Depends(get_db),
) -> FaqOut:
    entry = db.get(FaqEntry, faq_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="faq entry not found")

    entry.category = payload.category.strip() or "general"
    entry.question = payload.question.strip()
    entry.answer = payload.answer.strip()
    entry.keywords = payload.keywords.strip()
    entry.enabled = payload.enabled
    db.commit()
    db.refresh(entry)
    invalidate_faq_cache()
    return FaqOut.model_validate(entry, from_attributes=True)


@router.delete("/faqs/{faq_id}", status_code=204, response_model=None)
def delete_faq(
    faq_id: int,
    _: Principal = Depends(current_admin),
    db: Session = Depends(get_db),
) -> None:
    entry = db.get(FaqEntry, faq_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="faq entry not found")
    db.delete(entry)
    db.commit()
    invalidate_faq_cache()


@router.post("/faqs/reindex")
def reindex_faqs(
    _: Principal = Depends(current_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Rebuild the retrieval index and report its size."""
    from ..core.faq import get_index

    invalidate_faq_cache()
    index = get_index(db)
    return {"status": "ok", "indexed_documents": len(getattr(index, "_docs", []))}
