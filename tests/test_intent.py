"""Intent recognition and slot extraction."""

from __future__ import annotations

import pytest

from app.core.intent import Intent, detect_intent, extract_slots, normalize_order_id


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("我想申请退款", Intent.REFUND.value),
        ("订单 ORD-1002 我要退货", Intent.REFUND.value),
        ("退款多久能到账", Intent.REFUND.value),
        ("帮我查一下订单 ORD-1001 的物流", Intent.ORDER_QUERY.value),
        ("我的订单什么时候发货", Intent.ORDER_QUERY.value),
        ("快递到哪了", Intent.ORDER_QUERY.value),
        ("App 一直闪退打不开", Intent.TECH_SUPPORT.value),
        ("收不到验证码怎么办", Intent.TECH_SUPPORT.value),
        ("登录提示密码错误", Intent.TECH_SUPPORT.value),
        ("我要转人工", Intent.HUMAN_HANDOFF.value),
        ("帮我转接人工客服", Intent.HUMAN_HANDOFF.value),
        ("我要投诉", Intent.HUMAN_HANDOFF.value),
        ("怎么开发票？", Intent.FAQ.value),
        ("支持哪些支付方式", Intent.FAQ.value),
    ],
)
def test_intent_classification(text, expected):
    assert detect_intent(text).intent == expected


def test_handoff_has_high_confidence():
    result = detect_intent("请帮我转人工客服")
    assert result.is_handoff
    assert result.confidence >= 0.9


def test_small_talk_is_flagged():
    """A pure greeting is a confident 'not a business request'."""
    result = detect_intent("嗯嗯")
    assert result.intent == Intent.OTHER.value
    assert result.smalltalk is True


def test_unrecognised_text_has_low_confidence():
    result = detect_intent("zzqqxx wwvvuu")
    assert result.intent == Intent.OTHER.value
    assert result.confidence < 0.5
    assert result.smalltalk is False


def test_short_follow_up_inherits_previous_intent():
    """A short ambiguous follow-up should inherit the previous topic."""
    result = detect_intent("那大概多久", previous_intent=Intent.REFUND.value)
    assert result.intent == Intent.REFUND.value


def test_contextual_boost_prefers_previous_topic():
    ambiguous = detect_intent("这个能退吗", previous_intent=Intent.ORDER_QUERY.value)
    assert ambiguous.intent in (Intent.REFUND.value, Intent.ORDER_QUERY.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("ORD-1001", "ORD-1001"), ("ord 1002", "ORD-1002"), ("ORD1003", "ORD-1003"), ("ORD_2001", "ORD-2001")],
)
def test_normalize_order_id(raw, expected):
    assert normalize_order_id(raw) == expected


def test_extract_slots_finds_order_and_amount():
    slots = extract_slots("订单号 ORD-1001 的金额是 899 元，我想退款")
    assert slots["order_id"] == "ORD-1001"
    assert slots["amount"] == 899.0


def test_extract_slots_empty_when_nothing_present():
    assert extract_slots("你好呀") == {}
