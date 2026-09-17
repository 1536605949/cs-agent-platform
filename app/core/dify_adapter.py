"""Optional Dify orchestration adapter.

Set ``ENGINE_MODE=dify`` to delegate intent detection, retrieval and answer
generation to an existing Dify chat application. The adapter keeps the same
``handle()`` contract as the built-in engine, so persistence, the back office,
satisfaction ratings and handoff all keep working unchanged.

The Dify conversation id is remembered in ``conversation.context`` which gives
the same multi-turn continuity the built-in engine provides.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import (
    Conversation,
    ConversationStatus,
    HandoffStatus,
    HandoffTask,
    Message,
    MessageRole,
    new_id,
)
from ..tools import business
from .engine import ChatOutcome
from .intent import INTENT_LABELS, INTENT_SUGGESTIONS, Intent

logger = logging.getLogger("app.dify")


class DifyError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class DifyEngine:
    """Thin orchestration adapter around the Dify Service API."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.engine_name = "dify"

    # ------------------------------------------------------------------ #
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.dify_api_key}", "Content-Type": "application/json"}

    async def _call(self, *, query: str, user: str, dify_conversation_id: str) -> dict[str, Any]:
        payload = {
            "inputs": {},
            "query": query,
            "response_mode": "blocking",
            "conversation_id": dify_conversation_id,
            "user": user,
        }
        try:
            async with httpx.AsyncClient(timeout=self.settings.dify_timeout_seconds) as client:
                response = await client.post(
                    f"{self.settings.dify_base_url.rstrip('/')}/chat-messages",
                    headers=self._headers(),
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise DifyError(504, f"Dify timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise DifyError(502, f"Dify unreachable: {exc}") from exc

        if response.status_code >= 400:
            raise DifyError(response.status_code, f"Dify error {response.status_code}: {response.text[:500]}")
        try:
            return response.json()
        except ValueError as exc:
            raise DifyError(502, f"Dify returned invalid JSON: {exc}") from exc

    # ------------------------------------------------------------------ #
    async def handle(
        self,
        db: Session,
        *,
        user_id: str,
        message: str,
        conversation_id: str = "",
    ) -> ChatOutcome:
        started = time.perf_counter()
        message = (message or "").strip()

        conversation = self._get_or_create(db, user_id, conversation_id)
        db.add(Message(conversation_id=conversation.conversation_id, role=MessageRole.USER, content=message))
        conversation.message_count += 1
        if conversation.message_count == 1 or conversation.title == "新会话":
            conversation.title = (message[:40] + "…") if len(message) > 40 else (message or "新会话")
        db.commit()

        context = dict(conversation.context or {})
        dify_conversation_id = str(context.get("dify_conversation_id", ""))

        try:
            result = await self._call(query=message, user=user_id, dify_conversation_id=dify_conversation_id)
        except DifyError as exc:
            logger.warning("dify_call_failed status=%s error=%s", exc.status_code, exc)
            return self._degraded(db, conversation, message, started, str(exc))

        answer = (result.get("answer") or "").strip()
        context["dify_conversation_id"] = result.get("conversation_id", dify_conversation_id)
        conversation.context = context

        metadata = result.get("metadata") or {}
        raw_intent = str(metadata.get("intent") or metadata.get("primary_intent") or "").strip().lower()
        intent = raw_intent if raw_intent in INTENT_LABELS else self._guess_intent(message)
        confidence = float(metadata.get("confidence") or 0.75)

        handoff = bool(metadata.get("handoff")) or "转人工" in answer
        sources = self._extract_sources(metadata)

        assistant = Message(
            conversation_id=conversation.conversation_id,
            role=MessageRole.ASSISTANT,
            content=answer,
            intent=intent,
            confidence=confidence,
            sources=sources,
            handoff=handoff,
        )
        db.add(assistant)

        conversation.message_count += 1
        conversation.last_intent = intent
        if not conversation.primary_intent:
            conversation.primary_intent = intent
        conversation.updated_at = assistant.created_at

        outcome = ChatOutcome(
            conversation_id=conversation.conversation_id,
            message_id=0,
            segments=[answer] if answer else ["抱歉，我暂时无法回答这个问题，请回复「转人工」由人工客服协助。"],
            intent=intent,
            confidence=confidence,
            sources=sources,
            handoff=handoff,
            handoff_reason="dify_signal" if handoff else "",
            suggestions=INTENT_SUGGESTIONS.get(intent, INTENT_SUGGESTIONS[Intent.OTHER.value]),
            engine="dify",
        )

        if handoff:
            conversation.handoff_count += 1
            conversation.status = ConversationStatus.HANDOFF_PENDING
            db.add(
                HandoffTask(
                    conversation_id=conversation.conversation_id,
                    user_id=user_id,
                    reason="dify_signal",
                    summary=f"Dify 应用请求转人工。用户最后一句：{message[:200]}",
                    status=HandoffStatus.PENDING,
                )
            )

        db.commit()
        db.refresh(assistant)
        db.refresh(conversation)

        outcome.message_id = assistant.id
        outcome.conversation_status = conversation.status
        outcome.ask_satisfaction = (
            conversation.satisfaction_score is None
            and conversation.message_count >= 4
            and conversation.status != ConversationStatus.HANDOFF_PENDING
        )
        outcome.latency_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    # ------------------------------------------------------------------ #
    def _get_or_create(self, db: Session, user_id: str, conversation_id: str) -> Conversation:
        from sqlalchemy import select

        if conversation_id:
            found = db.execute(
                select(Conversation).where(
                    Conversation.conversation_id == conversation_id,
                    Conversation.user_id == user_id,
                )
            ).scalars().first()
            if found is not None:
                return found
        conversation = Conversation(conversation_id=conversation_id or new_id("CONV"), user_id=user_id)
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
        return conversation

    def _degraded(
        self,
        db: Session,
        conversation: Conversation,
        message: str,
        started: float,
        error: str,
    ) -> ChatOutcome:
        """Dify is down: fall back to a knowledge-base answer and escalate."""
        from .faq import retrieve_faq

        hits = retrieve_faq(db, message, top_k=2, min_score=self.settings.faq_min_score)
        if hits:
            segments = [hits[0].answer]
            sources = [hit.as_source() for hit in hits]
        else:
            segments = ["抱歉，智能客服暂时不可用，已为您转接人工客服。"]
            sources = []

        handoff = not hits
        outcome = ChatOutcome(
            conversation_id=conversation.conversation_id,
            message_id=0,
            segments=segments,
            intent=self._guess_intent(message),
            confidence=0.3,
            sources=sources,
            handoff=handoff,
            handoff_reason="dify_unavailable" if handoff else "",
            engine="dify",
        )

        assistant = Message(
            conversation_id=conversation.conversation_id,
            role=MessageRole.ASSISTANT,
            content=outcome.answer,
            intent=outcome.intent,
            confidence=outcome.confidence,
            sources=sources,
            handoff=handoff,
        )
        db.add(assistant)
        conversation.message_count += 1
        conversation.last_intent = outcome.intent
        if not conversation.primary_intent:
            conversation.primary_intent = outcome.intent

        if handoff:
            conversation.handoff_count += 1
            conversation.status = ConversationStatus.HANDOFF_PENDING
            db.add(
                HandoffTask(
                    conversation_id=conversation.conversation_id,
                    user_id=conversation.user_id,
                    reason="dify_unavailable",
                    summary=f"Dify 调用失败（{error[:120]}）。用户最后一句：{message[:200]}",
                    status=HandoffStatus.PENDING,
                )
            )
        business.audit(db, "dify_degraded", conversation.user_id, conversation.conversation_id, {"error": error[:300]})
        db.commit()
        db.refresh(assistant)
        db.refresh(conversation)

        outcome.message_id = assistant.id
        outcome.conversation_status = conversation.status
        outcome.latency_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    @staticmethod
    def _extract_sources(metadata: dict) -> list[dict[str, Any]]:
        resources = metadata.get("retriever_resources") or metadata.get("resources") or []
        sources: list[dict[str, Any]] = []
        for item in resources[:5]:
            if isinstance(item, dict):
                sources.append(
                    {
                        "title": str(item.get("document_name") or item.get("title") or "知识库片段")[:200],
                        "category": str(item.get("dataset_name") or "dify"),
                        "score": float(item.get("score") or 0.0),
                    }
                )
        return sources

    @staticmethod
    def _guess_intent(message: str) -> str:
        from .intent import detect_intent

        return detect_intent(message).intent


__all__ = ["DifyEngine", "DifyError"]
