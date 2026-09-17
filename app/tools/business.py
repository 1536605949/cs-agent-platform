"""Deterministic business capabilities used by the conversation engine.

These are the "tools" the assistant may call. They are intentionally written as
plain functions over a SQLAlchemy session so they can be unit-tested directly
and reused by the admin API.

Refund safety model
-------------------
An assistant can only *propose* a refund. The transition to ``approved`` is
performed exclusively by an administrator, and ``execute`` consumes that
approval exactly once through a conditional UPDATE. A hallucinated or replayed
tool call therefore cannot move money.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import (
    AuditEvent,
    FaqEntry,
    Order,
    RefundProposal,
    Ticket,
    User,
    new_id,
    utcnow,
)

logger = logging.getLogger("app.tools")

FAQ_SEED_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "faq.json"


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
def audit(db: Session, event_type: str, user_id: str = "", resource_id: str = "", detail: dict | None = None) -> None:
    db.add(
        AuditEvent(
            event_type=event_type,
            user_id=user_id,
            resource_id=resource_id,
            detail=detail or {},
        )
    )


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #
def get_order(db: Session, order_id: str) -> Order | None:
    return db.get(Order, order_id.strip().upper())


def get_owned_order(db: Session, user_id: str, order_id: str) -> Order | None:
    """Fetch an order only when it belongs to ``user_id``.

    Ownership is always re-checked here rather than trusted from the model.
    """
    order = get_order(db, order_id)
    if order is None or order.user_id != user_id:
        return None
    return order


def list_user_orders(db: Session, user_id: str, limit: int = 5) -> list[Order]:
    return list(
        db.execute(
            select(Order).where(Order.user_id == user_id).order_by(Order.created_at.desc()).limit(limit)
        ).scalars()
    )


def describe_order(order: Order) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "product_name": order.product_name,
        "status": order.status,
        "amount": order.amount,
        "refund_available": order.refund_available,
    }


# --------------------------------------------------------------------------- #
# Refunds
# --------------------------------------------------------------------------- #
def create_refund_proposal(
    db: Session,
    user_id: str,
    order_id: str,
    reason: str = "",
    conversation_id: str = "",
) -> tuple[RefundProposal | None, str | None]:
    """Create (or return the existing) refund proposal for an order."""
    order = get_owned_order(db, user_id, order_id)
    if order is None:
        return None, "order_not_found"
    if not order.refund_available:
        return None, "refund_not_available"

    existing = db.execute(
        select(RefundProposal)
        .where(
            RefundProposal.user_id == user_id,
            RefundProposal.order_id == order.order_id,
            RefundProposal.status.in_(("pending_review", "approved")),
        )
        .order_by(RefundProposal.created_at.desc())
    ).scalars().first()
    if existing is not None:
        return existing, None

    proposal = RefundProposal(
        proposal_id=new_id("RP"),
        user_id=user_id,
        order_id=order.order_id,
        conversation_id=conversation_id,
        reason=reason.strip() or "用户申请退款",
        status="pending_review",
    )
    db.add(proposal)
    audit(
        db,
        "refund_proposal_created",
        user_id,
        proposal.proposal_id,
        {"order_id": order.order_id, "reason": proposal.reason, "conversation_id": conversation_id},
    )
    db.commit()
    db.refresh(proposal)
    return proposal, None


def approve_or_reject_refund(
    db: Session,
    order_id: str,
    decision: str,
    reviewer_id: str,
    comment: str = "",
) -> tuple[RefundProposal | None, str | None]:
    """Administrator decision. The only path from ``pending_review`` to a verdict."""
    if decision not in ("approve", "reject"):
        return None, "invalid_decision"

    proposal = db.execute(
        select(RefundProposal)
        .where(
            RefundProposal.order_id == order_id.strip().upper(),
            RefundProposal.status == "pending_review",
        )
        .order_by(RefundProposal.created_at.desc())
    ).scalars().first()
    if proposal is None:
        return None, "pending_proposal_not_found"

    proposal.status = "approved" if decision == "approve" else "rejected"
    proposal.review_comment = comment
    proposal.reviewer_id = reviewer_id
    audit(
        db,
        "refund_reviewed",
        proposal.user_id,
        proposal.proposal_id,
        {"order_id": proposal.order_id, "decision": decision, "reviewer_id": reviewer_id, "comment": comment},
    )
    db.commit()
    db.refresh(proposal)
    return proposal, None


def execute_approved_refund(db: Session, order_id: str) -> tuple[RefundProposal | None, str | None]:
    """Execute an approved refund exactly once."""
    order_id = order_id.strip().upper()
    proposal = db.execute(
        select(RefundProposal)
        .where(RefundProposal.order_id == order_id, RefundProposal.status == "approved")
        .order_by(RefundProposal.created_at.desc())
    ).scalars().first()
    if proposal is None:
        return None, "approval_required"

    order = get_order(db, order_id)
    if order is None:
        return None, "order_not_found"
    if not order.refund_available:
        return None, "refund_already_processed"

    # Conditional transition: a second concurrent call matches 0 rows and the
    # approval can never be consumed twice.
    consumed = db.execute(
        update(RefundProposal)
        .where(RefundProposal.proposal_id == proposal.proposal_id, RefundProposal.status == "approved")
        .values(status="executed", updated_at=utcnow())
    ).rowcount
    if consumed != 1:
        db.rollback()
        return None, "approval_already_consumed"

    order.status = "refund_processing"
    order.refund_available = False
    audit(db, "refund_executed", proposal.user_id, proposal.proposal_id, {"order_id": order_id})
    db.commit()
    db.refresh(proposal)
    return proposal, None


def list_refunds(db: Session, status: str = "", limit: int = 200) -> list[RefundProposal]:
    statement = select(RefundProposal)
    if status:
        statement = statement.where(RefundProposal.status == status)
    return list(db.execute(statement.order_by(RefundProposal.created_at.desc()).limit(limit)).scalars())


# --------------------------------------------------------------------------- #
# Tickets
# --------------------------------------------------------------------------- #
def create_ticket(
    db: Session,
    user_id: str,
    issue: str,
    category: str = "tech_support",
    conversation_id: str = "",
) -> Ticket:
    ticket = Ticket(
        ticket_id=new_id("TK"),
        user_id=user_id,
        conversation_id=conversation_id,
        category=category,
        issue=issue.strip()[:2000],
        status="created",
    )
    db.add(ticket)
    audit(db, "ticket_created", user_id, ticket.ticket_id, {"category": category, "conversation_id": conversation_id})
    db.commit()
    db.refresh(ticket)
    return ticket


def list_tickets(db: Session, status: str = "", limit: int = 200) -> list[Ticket]:
    statement = select(Ticket)
    if status:
        statement = statement.where(Ticket.status == status)
    return list(db.execute(statement.order_by(Ticket.created_at.desc()).limit(limit)).scalars())


# --------------------------------------------------------------------------- #
# Seed data
# --------------------------------------------------------------------------- #
_DEMO_USERS = [
    {"user_id": "user001", "display_name": "张伟", "level": "VIP"},
    {"user_id": "user002", "display_name": "李娜", "level": "STANDARD"},
    {"user_id": "user003", "display_name": "王强", "level": "GOLD"},
]

_DEMO_ORDERS = [
    {"order_id": "ORD-1001", "user_id": "user001", "product_name": "无线降噪耳机 Pro", "status": "shipped", "amount_cents": 89900},
    {"order_id": "ORD-1002", "user_id": "user001", "product_name": "机械键盘 87 键", "status": "processing", "amount_cents": 45900},
    {"order_id": "ORD-1003", "user_id": "user001", "product_name": "4K 显示器 27 寸", "status": "delivered", "amount_cents": 189900},
    {"order_id": "ORD-2001", "user_id": "user002", "product_name": "便携充电宝 20000mAh", "status": "processing", "amount_cents": 12900},
    {"order_id": "ORD-2002", "user_id": "user002", "product_name": "智能手环 5 代", "status": "shipped", "amount_cents": 29900},
    {"order_id": "ORD-3001", "user_id": "user003", "product_name": "空气炸锅 5L", "status": "delivered", "amount_cents": 39900},
]


def load_faq_seed(path: Path | None = None) -> list[dict]:
    path = path or FAQ_SEED_PATH
    if not path.exists():
        logger.warning("faq_seed_missing path=%s", path)
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("faq_seed_unreadable path=%s error=%s", path, exc)
        return []
    return payload.get("entries", []) if isinstance(payload, dict) else list(payload)


def seed_demo_data(db: Session, *, with_faq: bool = True) -> dict[str, int]:
    """Idempotently load demo accounts, orders and the FAQ knowledge base."""
    created = {"users": 0, "orders": 0, "faqs": 0}

    for payload in _DEMO_USERS:
        if db.get(User, payload["user_id"]) is None:
            db.add(User(**payload))
            created["users"] += 1

    for payload in _DEMO_ORDERS:
        if db.get(Order, payload["order_id"]) is None:
            db.add(Order(**payload))
            created["orders"] += 1

    if with_faq:
        existing = {
            (row.question or "").strip()
            for row in db.execute(select(FaqEntry)).scalars()
        }
        for entry in load_faq_seed():
            question = (entry.get("question") or "").strip()
            if not question or question in existing:
                continue
            db.add(
                FaqEntry(
                    category=entry.get("category", "general"),
                    question=question,
                    answer=entry.get("answer", ""),
                    keywords=entry.get("keywords", ""),
                    enabled=True,
                )
            )
            created["faqs"] += 1

    db.commit()
    return created
