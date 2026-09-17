"""Request / response contracts for the public and admin APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #
class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str = ""
    user_id: str = ""

    @field_validator("message")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must not be blank")
        return value


class SourceRef(BaseModel):
    title: str
    category: str = ""
    score: float = 0.0


class ChatResponse(BaseModel):
    request_id: str
    conversation_id: str
    message_id: int
    answer: str
    intent: str
    intent_label: str
    confidence: float
    sources: list[SourceRef] = Field(default_factory=list)
    handoff: bool = False
    handoff_reason: str = ""
    ticket_id: str = ""
    proposal_id: str = ""
    suggestions: list[str] = Field(default_factory=list)
    latency_ms: int = 0


class SatisfactionRequest(BaseModel):
    score: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=1000)


# --------------------------------------------------------------------------- #
# Conversation read models
# --------------------------------------------------------------------------- #
class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    intent: str = ""
    confidence: float = 0.0
    sources: list[dict[str, Any]] = Field(default_factory=list)
    handoff: bool = False
    created_at: datetime


class ConversationOut(BaseModel):
    conversation_id: str
    user_id: str
    title: str
    status: str
    primary_intent: str
    message_count: int
    handoff_count: int
    satisfaction_score: int | None = None
    satisfaction_comment: str = ""
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationOut):
    messages: list[MessageOut] = Field(default_factory=list)


class ConversationPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[ConversationOut]


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
class AdminLoginRequest(BaseModel):
    username: str
    password: str


class AdminToken(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    username: str


class OverviewStats(BaseModel):
    conversations: int
    messages: int
    active_conversations: int
    resolved_conversations: int
    handoff_conversations: int
    handoff_rate: float
    rated_conversations: int
    satisfaction_avg: float
    satisfaction_rate: float
    refunds_pending: int
    tickets_open: int
    handoffs_pending: int


class SatisfactionStats(BaseModel):
    rated: int
    average: float
    distribution: dict[str, int]
    positive_rate: float
    negative_rate: float


class IntentSlice(BaseModel):
    intent: str
    label: str
    count: int
    share: float


class TrendPoint(BaseModel):
    date: str
    conversations: int
    messages: int
    satisfaction_avg: float | None = None


class FaqIn(BaseModel):
    category: str = "general"
    question: str = Field(min_length=2, max_length=500)
    answer: str = Field(min_length=1)
    keywords: str = ""
    enabled: bool = True


class FaqOut(BaseModel):
    id: int
    category: str
    question: str
    answer: str
    keywords: str
    enabled: bool
    updated_at: datetime


class HandoffOut(BaseModel):
    id: int
    conversation_id: str
    user_id: str
    reason: str
    summary: str
    status: str
    agent_id: str
    created_at: datetime
    resolved_at: datetime | None = None


class HandoffReplyRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    resolve: bool = False


class RefundOut(BaseModel):
    proposal_id: str
    user_id: str
    order_id: str
    conversation_id: str
    reason: str
    status: str
    review_comment: str
    reviewer_id: str
    created_at: datetime


class RefundReviewRequest(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=500)
    execute: bool = True
