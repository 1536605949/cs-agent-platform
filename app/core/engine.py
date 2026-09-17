"""The built-in conversation engine.

One turn flows through a fixed, auditable pipeline:

    persist user turn
      -> intent recognition (rules, optional LLM fallback)
      -> slot extraction + multi-turn context merge
      -> route to capability (FAQ retrieval / order tool / refund tool / ticket)
      -> compose answer (template, optionally rewritten by an LLM)
      -> escalation check (unresolved or low confidence -> human handoff)
      -> persist assistant turn + update conversation state

Everything the assistant says is grounded in either a retrieved FAQ passage or
a deterministic tool result. When neither is available the engine escalates
instead of inventing an answer.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
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
from .faq import retrieve_faq
from .intent import INTENT_LABELS, INTENT_SUGGESTIONS, Intent, IntentResult, detect_intent
from .llm import LLMClient, get_llm

logger = logging.getLogger("app.engine")

_MAX_HISTORY_TURNS = 6
_TITLE_MAX_LEN = 40


@dataclass
class ChatOutcome:
    conversation_id: str
    message_id: int
    segments: list[str] = field(default_factory=list)
    intent: str = Intent.OTHER.value
    confidence: float = 0.0
    sources: list[dict[str, Any]] = field(default_factory=list)
    handoff: bool = False
    handoff_reason: str = ""
    ticket_id: str = ""
    proposal_id: str = ""
    suggestions: list[str] = field(default_factory=list)
    ask_satisfaction: bool = False
    latency_ms: int = 0
    conversation_status: str = ConversationStatus.ACTIVE
    engine: str = "builtin"

    @property
    def answer(self) -> str:
        return "\n\n".join(segment for segment in self.segments if segment)

    @property
    def intent_label(self) -> str:
        return INTENT_LABELS.get(self.intent, self.intent)

    def meta(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "intent": self.intent,
            "intent_label": self.intent_label,
            "confidence": round(self.confidence, 3),
            "handoff": self.handoff,
            "handoff_reason": self.handoff_reason,
            "ticket_id": self.ticket_id,
            "proposal_id": self.proposal_id,
            "suggestions": self.suggestions,
            "sources": self.sources,
            "ask_satisfaction": self.ask_satisfaction,
            "conversation_status": self.conversation_status,
            "engine": self.engine,
        }

    def as_dict(self) -> dict[str, Any]:
        payload = self.meta()
        payload.update({"answer": self.answer, "segments": self.segments, "latency_ms": self.latency_ms})
        return payload


class Engine:
    """Self-contained engine: no external service required."""

    def __init__(self, settings: Settings | None = None, llm: LLMClient | None = None):
        self.settings = settings or get_settings()
        self.llm = llm or get_llm()

    # ------------------------------------------------------------------ #
    # Conversation plumbing
    # ------------------------------------------------------------------ #
    def get_or_create_conversation(self, db: Session, user_id: str, conversation_id: str = "") -> Conversation:
        if conversation_id:
            conversation = db.execute(
                select(Conversation).where(
                    Conversation.conversation_id == conversation_id,
                    Conversation.user_id == user_id,
                )
            ).scalars().first()
            if conversation is not None:
                return conversation

        conversation = Conversation(conversation_id=conversation_id or new_id("CONV"), user_id=user_id)
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
        return conversation

    def _history(self, db: Session, conversation_id: str, limit: int = _MAX_HISTORY_TURNS * 2) -> list[Message]:
        rows = list(
            db.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.id.desc())
                .limit(limit)
            ).scalars()
        )
        rows.reverse()
        return rows

    # ------------------------------------------------------------------ #
    # Main entry point
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

        conversation = self.get_or_create_conversation(db, user_id, conversation_id)
        if conversation.status in (ConversationStatus.CLOSED, ConversationStatus.RESOLVED):
            conversation.status = ConversationStatus.ACTIVE

        user_message = Message(
            conversation_id=conversation.conversation_id,
            role=MessageRole.USER,
            content=message,
        )
        db.add(user_message)
        conversation.message_count += 1
        if conversation.message_count == 1 or not conversation.title or conversation.title == "新会话":
            conversation.title = self._make_title(message)
        db.commit()

        # --- 1. intent -------------------------------------------------- #
        intent_result = await self._resolve_intent(message, conversation)

        outcome = ChatOutcome(
            conversation_id=conversation.conversation_id,
            message_id=0,
            intent=intent_result.intent,
            confidence=intent_result.confidence,
            engine="builtin",
        )

        # --- 2. route --------------------------------------------------- #
        resolved = False
        if intent_result.intent == Intent.HUMAN_HANDOFF.value:
            outcome.segments = [self._handoff_ack()]
            outcome.handoff = True
            outcome.handoff_reason = "user_requested"
        else:
            resolved = await self._route(db, conversation, intent_result, outcome, message)

        # --- 3. escalation ---------------------------------------------- #
        if not resolved and not outcome.handoff:
            conversation.fallback_count += 1
            threshold = max(1, self.settings.handoff_max_fallback_turns)
            if conversation.fallback_count >= threshold:
                outcome.handoff = True
                outcome.handoff_reason = "unresolved"
                outcome.segments.append(self._handoff_ack())
            else:
                outcome.segments.append(
                    "如果您希望我继续为您转接人工客服，直接回复「转人工」即可。"
                )
        elif resolved:
            conversation.fallback_count = 0

        # --- 4. persist ------------------------------------------------- #
        assistant_message = Message(
            conversation_id=conversation.conversation_id,
            role=MessageRole.ASSISTANT,
            content=outcome.answer,
            intent=outcome.intent,
            confidence=outcome.confidence,
            sources=outcome.sources,
            handoff=outcome.handoff,
        )
        db.add(assistant_message)

        conversation.message_count += 1
        conversation.last_intent = outcome.intent
        if not conversation.primary_intent:
            conversation.primary_intent = outcome.intent
        conversation.context = self._merge_context(conversation, intent_result.slots, message)

        if outcome.handoff:
            conversation.handoff_count += 1
            conversation.status = ConversationStatus.HANDOFF_PENDING
            self._open_handoff_task(db, conversation, outcome, message)

        db.commit()
        db.refresh(assistant_message)
        db.refresh(conversation)

        outcome.message_id = assistant_message.id
        outcome.conversation_status = conversation.status
        outcome.ask_satisfaction = (
            conversation.satisfaction_score is None
            and conversation.message_count >= 4
            and conversation.status != ConversationStatus.HANDOFF_PENDING
        )
        if not outcome.suggestions:
            outcome.suggestions = INTENT_SUGGESTIONS.get(outcome.intent, INTENT_SUGGESTIONS[Intent.OTHER.value])
        outcome.latency_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    # ------------------------------------------------------------------ #
    # Intent
    # ------------------------------------------------------------------ #
    async def _resolve_intent(self, message: str, conversation: Conversation) -> IntentResult:
        result = detect_intent(
            message,
            previous_intent=conversation.last_intent,
            context_slots=conversation.context or {},
        )

        # Ask the model only when the deterministic rules are unsure.
        if (
            self.llm.available
            and result.confidence < self.settings.handoff_confidence_threshold
            and not result.is_handoff
        ):
            try:
                verdict = await self.llm.classify_intent(message, previous_intent=conversation.last_intent)
            except Exception as exc:  # noqa: BLE001 - never fail a turn on the optional path
                logger.warning("llm_intent_failed error=%s", exc)
                verdict = None
            if verdict is not None:
                intent, confidence = verdict
                result = IntentResult(
                    intent=intent,
                    confidence=confidence,
                    scores=result.scores,
                    matched=result.matched,
                    slots=result.slots,
                )
        return result

    # ------------------------------------------------------------------ #
    # Capability routing
    # ------------------------------------------------------------------ #
    async def _route(
        self,
        db: Session,
        conversation: Conversation,
        intent_result: IntentResult,
        outcome: ChatOutcome,
        message: str,
    ) -> bool:
        intent = intent_result.intent
        slots = intent_result.slots
        order_id = slots.get("order_id", "")

        if intent_result.smalltalk:
            outcome.segments = [self._smalltalk_reply()]
            return True

        if intent == Intent.ORDER_QUERY.value:
            return await self._handle_order_query(db, conversation, outcome, order_id, message)

        if intent == Intent.REFUND.value:
            return await self._handle_refund(db, conversation, outcome, order_id, message)

        if intent == Intent.TECH_SUPPORT.value:
            return await self._handle_tech_support(db, conversation, outcome, message)

        return await self._handle_knowledge(db, conversation, outcome, message)

    # -- order query ---------------------------------------------------- #
    async def _handle_order_query(
        self,
        db: Session,
        conversation: Conversation,
        outcome: ChatOutcome,
        order_id: str,
        message: str,
    ) -> bool:
        if order_id:
            order = business.get_owned_order(db, conversation.user_id, order_id)
            if order is None:
                outcome.segments = [
                    f"我没有查到属于您的订单 **{order_id}**。请确认订单号是否正确（格式如 ORD-1001），"
                    "或者告诉我您下单时使用的账号。"
                ]
                return False

            outcome.segments = [
                f"订单 **{order.order_id}** 的最新状态：\n"
                f"- 商品：{order.product_name}\n"
                f"- 金额：¥{order.amount:.2f}\n"
                f"- 状态：{self._order_status_label(order.status)}\n"
                f"- 可退款：{'是' if order.refund_available else '否（已申请或已处理）'}"
            ]
            outcome.sources.append({"title": f"订单 {order.order_id}", "category": "order", "score": 1.0})

            hits = retrieve_faq(
                db,
                message,
                top_k=1,
                min_score=self.settings.faq_min_score * 2,
            )
            if hits:
                outcome.segments.append(hits[0].answer)
                outcome.sources.append(hits[0].as_source())
            return True

        orders = business.list_user_orders(db, conversation.user_id)
        if not orders:
            outcome.segments = [
                "您当前没有可查询的订单。如果您刚刚下单，请稍等几分钟后再试，"
                "或者把订单号发给我，我直接帮您查。"
            ]
            return False

        lines = [f"- **{order.order_id}** {order.product_name}｜{self._order_status_label(order.status)}｜¥{order.amount:.2f}" for order in orders]
        outcome.segments = [
            "这是您最近的订单：\n" + "\n".join(lines) + "\n\n请把要查询的订单号发给我，我帮您看详细物流和状态。"
        ]
        outcome.sources.append({"title": "订单列表", "category": "order", "score": 1.0})
        return True

    # -- refund --------------------------------------------------------- #
    async def _handle_refund(
        self,
        db: Session,
        conversation: Conversation,
        outcome: ChatOutcome,
        order_id: str,
        message: str,
    ) -> bool:
        if not order_id:
            hits = retrieve_faq(db, f"退款政策 {message}", top_k=2, min_score=self.settings.faq_min_score)
            for hit in hits:
                outcome.segments.append(hit.answer)
                outcome.sources.append(hit.as_source())
            outcome.segments.append(
                "要发起退款，请把订单号发给我（格式如 ORD-1001），我会立即为您提交退款申请。"
            )
            return bool(hits)

        proposal, error = business.create_refund_proposal(
            db,
            conversation.user_id,
            order_id,
            reason=message,
            conversation_id=conversation.conversation_id,
        )
        if error == "order_not_found":
            outcome.segments = [f"我没有查到属于您的订单 **{order_id}**，请确认订单号后再试。"]
            return False
        if error == "refund_not_available":
            outcome.segments = [
                f"订单 **{order_id}** 当前不支持再次退款（可能已提交申请或已完成退款）。"
                "如果这与您的实际情况不符，我可以为您转接人工客服核实。"
            ]
            return False

        outcome.proposal_id = proposal.proposal_id
        outcome.sources.append({"title": f"退款申请 {proposal.proposal_id}", "category": "refund", "score": 1.0})
        outcome.segments = [
            f"已为您提交退款申请：\n"
            f"- 申请单号：**{proposal.proposal_id}**\n"
            f"- 订单号：{proposal.order_id}\n"
            f"- 退款原因：{proposal.reason}\n"
            f"- 当前状态：待人工审核\n\n"
            "退款申请需经客服审核后执行，审核通过后款项将按原支付方式退回（通常 1-3 个工作日到账）。"
            "您可以在本会话中随时回复申请单号查询进度。"
        ]
        return True

    # -- tech support --------------------------------------------------- #
    async def _handle_tech_support(
        self,
        db: Session,
        conversation: Conversation,
        outcome: ChatOutcome,
        message: str,
    ) -> bool:
        hits = retrieve_faq(db, message, top_k=self.settings.faq_top_k, min_score=self.settings.faq_min_score)
        if hits:
            outcome.segments = [hit.answer for hit in hits[:1]]
            for hit in hits:
                outcome.sources.append(hit.as_source())
            if len(hits) > 1:
                outcome.segments.append("如果上述方法没有解决，您可以补充描述具体的报错信息，我继续帮您排查。")
            return True

        # No knowledge base coverage: open a ticket so nothing is lost, then
        # let the escalation logic decide whether a human must take over.
        ticket = business.create_ticket(
            db,
            conversation.user_id,
            message,
            category="tech_support",
            conversation_id=conversation.conversation_id,
        )
        outcome.ticket_id = ticket.ticket_id
        outcome.sources.append({"title": f"工单 {ticket.ticket_id}", "category": "ticket", "score": 1.0})
        outcome.segments = [
            f"抱歉，知识库里暂时没有完全匹配的解决方案。我已为您创建技术支持工单 **{ticket.ticket_id}**，"
            "并记录了您描述的问题。"
        ]
        return False

    # -- FAQ / general -------------------------------------------------- #
    async def _handle_knowledge(
        self,
        db: Session,
        conversation: Conversation,
        outcome: ChatOutcome,
        message: str,
    ) -> bool:
        hits = retrieve_faq(db, message, top_k=self.settings.faq_top_k, min_score=self.settings.faq_min_score)
        if not hits:
            outcome.segments = [
                "抱歉，我暂时没有找到与您问题相关的知识库内容。"
                "您可以换一种说法再问一次，或者直接回复「转人工」由人工客服为您处理。"
            ]
            return False

        blocks = [hit.answer for hit in hits[:1]]
        outcome.sources = [hit.as_source() for hit in hits]

        composed = await self._compose_with_llm(message, blocks, conversation)
        outcome.segments = [composed] if composed else blocks
        if not composed and len(hits) > 1:
            outcome.segments.append("您也可以参考：" + hits[1].question)
        return True

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    async def _compose_with_llm(
        self,
        message: str,
        blocks: list[str],
        conversation: Conversation,
    ) -> str | None:
        if not self.llm.available or not blocks:
            return None
        try:
            history = [
                (row.role, row.content)
                for row in conversation.messages[-6:]
                if row.role in (MessageRole.USER, MessageRole.ASSISTANT, MessageRole.AGENT)
            ]
            return await self.llm.compose_answer(message, context_blocks=blocks, history=history)
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm_compose_failed error=%s", exc)
            return None

    def _open_handoff_task(self, db: Session, conversation: Conversation, outcome: ChatOutcome, message: str) -> None:
        existing = db.execute(
            select(HandoffTask).where(
                HandoffTask.conversation_id == conversation.conversation_id,
                HandoffTask.status == HandoffStatus.PENDING,
            )
        ).scalars().first()
        if existing is not None:
            return

        summary = self._build_handoff_summary(db, conversation, outcome, message)
        db.add(
            HandoffTask(
                conversation_id=conversation.conversation_id,
                user_id=conversation.user_id,
                reason=outcome.handoff_reason or "unresolved",
                summary=summary,
                status=HandoffStatus.PENDING,
            )
        )
        business.audit(
            db,
            "handoff_requested",
            conversation.user_id,
            conversation.conversation_id,
            {"reason": outcome.handoff_reason, "intent": outcome.intent},
        )

    def _build_handoff_summary(
        self,
        db: Session,
        conversation: Conversation,
        outcome: ChatOutcome,
        message: str,
    ) -> str:
        lines = [
            f"用户：{conversation.user_id}",
            f"识别意图：{outcome.intent_label}（置信度 {outcome.confidence:.2f}）",
            f"触发原因：{outcome.handoff_reason or 'unresolved'}",
        ]
        if outcome.ticket_id:
            lines.append(f"关联工单：{outcome.ticket_id}")
        if outcome.proposal_id:
            lines.append(f"关联退款申请：{outcome.proposal_id}")
        context_order = (conversation.context or {}).get("order_id")
        if context_order:
            lines.append(f"上下文订单：{context_order}")
        lines.append(f"用户最后一句话：{message[:200]}")
        return "\n".join(lines)

    @staticmethod
    def _handoff_ack() -> str:
        return (
            "已为您转接人工客服，请稍候。人工客服会看到我们刚才的对话记录，"
            "您不需要重复描述问题。您也可以继续留言，人工客服上线后会一并回复。"
        )

    @staticmethod
    def _smalltalk_reply() -> str:
        return (
            "您好，我在的 🙂 我可以帮您处理以下几类问题：\n"
            "- **订单查询**：把订单号发给我，例如「查一下 ORD-1001 的物流」\n"
            "- **退款申请**：例如「订单 ORD-1002 我要退款」\n"
            "- **技术支持**：例如「App 闪退打不开怎么办」\n"
            "- **常见问题**：发票、优惠券、会员、配送等\n\n"
            "如果需要人工客服，随时回复「转人工」即可。"
        )

    @staticmethod
    def _order_status_label(status: str) -> str:
        return {
            "processing": "处理中（待发货）",
            "shipped": "已发货（运输中）",
            "delivered": "已签收",
            "refund_processing": "退款处理中",
            "cancelled": "已取消",
        }.get(status, status)

    @staticmethod
    def _make_title(message: str) -> str:
        cleaned = re.sub(r"\s+", " ", message).strip()
        return (cleaned[:_TITLE_MAX_LEN] + "…") if len(cleaned) > _TITLE_MAX_LEN else (cleaned or "新会话")

    @staticmethod
    def _merge_context(conversation: Conversation, slots: dict[str, Any], message: str) -> dict[str, Any]:
        context = dict(conversation.context or {})
        for key in ("order_id", "ticket_id", "proposal_id"):
            if slots.get(key):
                context[key] = slots[key]
        context["last_message"] = message[:200]
        context["last_intent"] = conversation.last_intent
        return context


# --------------------------------------------------------------------------- #
# Streaming wrapper
# --------------------------------------------------------------------------- #
async def stream_outcome(engine: Engine, db: Session, *, user_id: str, message: str, conversation_id: str = ""):
    """Yield SSE-ready events for one turn.

    ``meta`` is emitted first so the client can render intent / handoff state
    immediately, then the answer is delivered segment by segment, and ``done``
    carries the identifiers the client needs for follow-up calls.
    """
    outcome = await engine.handle(db, user_id=user_id, message=message, conversation_id=conversation_id)
    yield "meta", outcome.meta()
    for index, segment in enumerate(outcome.segments):
        if segment:
            yield "delta", {"index": index, "text": segment}
    yield "done", {
        "answer": outcome.answer,
        "conversation_id": outcome.conversation_id,
        "message_id": outcome.message_id,
        "latency_ms": outcome.latency_ms,
        "ask_satisfaction": outcome.ask_satisfaction,
    }


__all__ = ["ChatOutcome", "Engine", "stream_outcome"]
