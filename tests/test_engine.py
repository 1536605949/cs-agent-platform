"""Conversation engine: routing, multi-turn context, grounding and escalation."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.engine import Engine
from app.core.intent import Intent
from app.models import Conversation, ConversationStatus, Message, MessageRole, RefundProposal
from app.tools import business


@pytest.fixture
def engine():
    return Engine()


def _messages(db, conversation_id):
    return list(
        db.execute(select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id)).scalars()
    )


async def test_order_query_with_explicit_id(db, engine):
    outcome = await engine.handle(db, user_id="user001", message="帮我查一下订单 ORD-1001 的物流")
    assert outcome.intent == Intent.ORDER_QUERY.value
    assert "ORD-1001" in outcome.answer
    assert outcome.handoff is False


async def test_order_query_without_id_lists_orders(db, engine):
    outcome = await engine.handle(db, user_id="user001", message="我的订单到哪了")
    assert outcome.intent == Intent.ORDER_QUERY.value
    assert "ORD-" in outcome.answer


async def test_order_of_another_user_is_not_disclosed(db, engine):
    outcome = await engine.handle(db, user_id="user002", message="帮我查订单 ORD-1001")
    assert "ORD-1001" not in outcome.answer or "没有查到" in outcome.answer


async def test_faq_question_is_grounded_with_sources(db, engine):
    outcome = await engine.handle(db, user_id="user001", message="怎么开发票？")
    assert outcome.intent == Intent.FAQ.value
    assert outcome.sources, "grounded answers must cite the knowledge base"
    assert "发票" in outcome.answer


async def test_tech_support_returns_troubleshooting_steps(db, engine):
    outcome = await engine.handle(db, user_id="user001", message="App 一直闪退打不开怎么办")
    assert outcome.intent == Intent.TECH_SUPPORT.value
    assert outcome.sources


async def test_refund_creates_pending_proposal(db, engine):
    outcome = await engine.handle(db, user_id="user001", message="订单 ORD-1002 我要申请退款")
    assert outcome.intent == Intent.REFUND.value
    assert outcome.proposal_id.startswith("RP-")

    proposal = db.get(RefundProposal, outcome.proposal_id)
    assert proposal is not None
    assert proposal.status == "pending_review", "assistant may only propose, never approve"


async def test_refund_for_unknown_order_does_not_create_proposal(db, engine):
    outcome = await engine.handle(db, user_id="user001", message="订单 ORD-9999 我要退款")
    assert outcome.proposal_id == ""
    assert "没有查到" in outcome.answer


async def test_multi_turn_slot_memory_reuses_order_id(db, engine):
    """Turn 1 states the order id; turn 2 refers to it implicitly."""
    first = await engine.handle(db, user_id="user001", message="帮我查订单 ORD-1003")
    assert first.conversation_id

    second = await engine.handle(
        db,
        user_id="user001",
        message="那这个能退款吗",
        conversation_id=first.conversation_id,
    )
    assert second.intent == Intent.REFUND.value
    assert second.proposal_id, "the order id from the previous turn must be reused"


async def test_conversation_context_persists_order_id(db, engine):
    outcome = await engine.handle(db, user_id="user001", message="帮我查订单 ORD-1003")
    conversation = db.execute(
        select(Conversation).where(Conversation.conversation_id == outcome.conversation_id)
    ).scalars().first()
    assert conversation.context.get("order_id") == "ORD-1003"


async def test_explicit_handoff_creates_task(db, engine):
    outcome = await engine.handle(db, user_id="user001", message="我要转人工")
    assert outcome.handoff is True
    assert outcome.handoff_reason == "user_requested"

    conversation = db.execute(
        select(Conversation).where(Conversation.conversation_id == outcome.conversation_id)
    ).scalars().first()
    assert conversation.status == ConversationStatus.HANDOFF_PENDING


async def test_auto_handoff_after_repeated_unresolved_turns(db, engine):
    """Out-of-domain ASCII tokens cannot hit the FAQ index, so the turn stays
    unresolved and the escalation counter eventually forces a handoff."""
    first = await engine.handle(db, user_id="user003", message="zzqqxx wwvvuu")
    assert first.handoff is False, "the first unresolved turn should not escalate yet"

    second = await engine.handle(
        db,
        user_id="user003",
        message="aabbcc ddeeff",
        conversation_id=first.conversation_id,
    )
    assert second.handoff is True
    assert second.handoff_reason == "unresolved"


async def test_resolved_turn_resets_the_escalation_counter(db, engine):
    first = await engine.handle(db, user_id="user003", message="zzqqxx wwvvuu")
    second = await engine.handle(
        db, user_id="user003", message="怎么开发票？", conversation_id=first.conversation_id
    )
    assert second.handoff is False

    conversation = db.execute(
        select(Conversation).where(Conversation.conversation_id == first.conversation_id)
    ).scalars().first()
    assert conversation.fallback_count == 0, "a successful answer must reset the counter"


async def test_greeting_gets_a_helpful_reply_not_a_failure(db, engine):
    outcome = await engine.handle(db, user_id="user003", message="你好")
    assert outcome.handoff is False
    assert "订单" in outcome.answer or "转人工" in outcome.answer


async def test_transcript_is_persisted_in_order(db, engine):
    outcome = await engine.handle(db, user_id="user002", message="怎么开发票？")
    rows = _messages(db, outcome.conversation_id)
    assert [row.role for row in rows] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert rows[0].content == "怎么开发票？"
    assert rows[1].intent == Intent.FAQ.value


async def test_conversation_counters_and_title(db, engine):
    outcome = await engine.handle(db, user_id="user002", message="帮我查一下订单 ORD-2001 的物流")
    conversation = db.execute(
        select(Conversation).where(Conversation.conversation_id == outcome.conversation_id)
    ).scalars().first()
    assert conversation.message_count == 2
    assert conversation.primary_intent == Intent.ORDER_QUERY.value
    assert conversation.title


async def test_reply_is_never_empty(db, engine):
    for message in ["你好", "嗯", "在吗", "随便问问"]:
        outcome = await engine.handle(db, user_id="user003", message=message)
        assert outcome.answer.strip(), f"empty answer for {message!r}"
