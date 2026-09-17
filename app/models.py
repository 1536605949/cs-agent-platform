"""ORM models.

Design notes
------------
* Statuses and intents are stored as short strings instead of database enums so
  the exact same schema works on SQLite and PostgreSQL.
* ``Message`` is the append-only transcript; ``Conversation`` holds the derived
  state (intent, counters, satisfaction) that the back office queries directly.
* ``RefundProposal`` keeps the human-in-the-loop state machine: an assistant can
  only *propose* a refund, an administrator must *approve* it, and execution
  consumes the approval exactly once.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


# --------------------------------------------------------------------------- #
# Conversation state vocabulary
# --------------------------------------------------------------------------- #
class ConversationStatus:
    ACTIVE = "active"
    HANDOFF_PENDING = "handoff_pending"
    HANDOFF_REPLIED = "handoff_replied"
    RESOLVED = "resolved"
    CLOSED = "closed"

    ALL = (ACTIVE, HANDOFF_PENDING, HANDOFF_REPLIED, RESOLVED, CLOSED)


class MessageRole:
    USER = "user"
    ASSISTANT = "assistant"
    AGENT = "agent"          # human customer service reply
    SYSTEM = "system"

    ALL = (USER, ASSISTANT, AGENT, SYSTEM)


class HandoffStatus:
    PENDING = "pending"
    REPLIED = "replied"
    RESOLVED = "resolved"

    ALL = (PENDING, REPLIED, RESOLVED)


# --------------------------------------------------------------------------- #
# Business domain
# --------------------------------------------------------------------------- #
class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    level: Mapped[str] = mapped_column(String(32), default="STANDARD")
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Order(Base):
    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    product_name: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(32), default="processing")
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    refund_available: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def amount(self) -> float:
        return round(self.amount_cents / 100, 2)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, default=lambda: new_id("CONV"))
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(200), default="新会话")
    status: Mapped[str] = mapped_column(String(32), default=ConversationStatus.ACTIVE, index=True)

    primary_intent: Mapped[str] = mapped_column(String(32), default="", index=True)
    last_intent: Mapped[str] = mapped_column(String(32), default="")
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    fallback_count: Mapped[int] = mapped_column(Integer, default=0)
    handoff_count: Mapped[int] = mapped_column(Integer, default=0)

    # remembered slots for multi-turn context (order id, topic, ...)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    satisfaction_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    satisfaction_comment: Mapped[str] = mapped_column(Text, default="")
    rated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.id",
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("conversations.conversation_id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16), index=True)
    content: Mapped[str] = mapped_column(Text)
    intent: Mapped[str] = mapped_column(String(32), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    handoff: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class RefundProposal(Base):
    __tablename__ = "refund_proposals"

    proposal_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("RP"))
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    conversation_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="pending_review", index=True)
    review_comment: Mapped[str] = mapped_column(Text, default="")
    reviewer_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Ticket(Base):
    __tablename__ = "tickets"

    ticket_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("TK"))
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    conversation_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    category: Mapped[str] = mapped_column(String(32), default="tech_support", index=True)
    issue: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="created", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class HandoffTask(Base):
    __tablename__ = "handoff_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    reason: Mapped[str] = mapped_column(String(64), default="low_confidence")
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default=HandoffStatus.PENDING, index=True)
    agent_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FaqEntry(Base):
    __tablename__ = "faq_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String(32), default="general", index=True)
    question: Mapped[str] = mapped_column(String(500))
    answer: Mapped[str] = mapped_column(Text)
    keywords: Mapped[str] = mapped_column(String(500), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    resource_id: Mapped[str] = mapped_column(String(128), default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


Index("ix_messages_conversation_created", Message.conversation_id, Message.created_at)
Index("ix_conversations_user_updated", Conversation.user_id, Conversation.updated_at)
