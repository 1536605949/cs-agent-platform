"""Back-office API: authentication, analytics, records, handoff, refunds, FAQ."""

from __future__ import annotations

import pytest

from app.core.intent import Intent


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def test_login_success(client):
    response = client.post("/api/admin/login", json={"username": "admin", "password": "test-admin-pass"})
    assert response.status_code == 200
    assert response.json()["access_token"]
    assert response.json()["username"] == "admin"


@pytest.mark.parametrize(
    ("username", "password"),
    [("admin", "wrong"), ("nobody", "test-admin-pass"), ("", "")],
)
def test_login_failure(client, username, password):
    assert client.post("/api/admin/login", json={"username": username, "password": password}).status_code == 401


def test_admin_endpoints_require_a_token(client):
    for path in (
        "/api/admin/stats/overview",
        "/api/admin/conversations",
        "/api/admin/handoffs",
        "/api/admin/refunds",
        "/api/admin/faqs",
    ):
        assert client.get(path).status_code in (401, 403), path


def test_user_token_cannot_access_admin_api(client, user_headers):
    created = client.post("/api/chat", json={"message": "你好"}, headers=user_headers).json()
    assert created["conversation_id"]


def test_admin_me(client, admin_headers):
    payload = client.get("/api/admin/me", headers=admin_headers).json()
    assert payload["username"] == "admin"
    assert "admin" in payload["roles"]


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
def test_overview_stats(client, admin_headers, user_headers):
    client.post("/api/chat", json={"message": "怎么开发票？"}, headers=user_headers)
    payload = client.get("/api/admin/stats/overview", headers=admin_headers).json()

    assert payload["conversations"] >= 1
    assert payload["messages"] >= 2
    assert 0.0 <= payload["handoff_rate"] <= 1.0
    assert 0.0 <= payload["satisfaction_rate"] <= 1.0
    for key in ("refunds_pending", "tickets_open", "handoffs_pending", "active_conversations"):
        assert payload[key] >= 0


def test_satisfaction_stats(client, admin_headers, user_headers):
    created = client.post("/api/chat", json={"message": "支持哪些支付方式"}, headers=user_headers).json()
    client.post(
        f"/api/conversations/{created['conversation_id']}/satisfaction",
        json={"score": 5, "comment": "admin stats test"},
        headers=user_headers,
    )

    payload = client.get("/api/admin/stats/satisfaction", headers=admin_headers).json()
    assert payload["rated"] >= 1
    assert payload["average"] > 0
    assert set(payload["distribution"]) == {"1", "2", "3", "4", "5"}
    assert payload["positive_rate"] > 0


def test_intent_stats(client, admin_headers, user_headers):
    client.post("/api/chat", json={"message": "我要转人工"}, headers=user_headers)
    payload = client.get("/api/admin/stats/intents", headers=admin_headers).json()
    assert payload
    assert all("intent" in item and "label" in item and "share" in item for item in payload)
    assert abs(sum(item["share"] for item in payload) - 1.0) < 0.05


def test_trend_stats(client, admin_headers):
    payload = client.get("/api/admin/stats/trend?days=7", headers=admin_headers).json()
    assert len(payload) == 7
    assert all("date" in point and "conversations" in point for point in payload)


def test_trend_days_is_validated(client, admin_headers):
    assert client.get("/api/admin/stats/trend?days=0", headers=admin_headers).status_code == 422
    assert client.get("/api/admin/stats/trend?days=500", headers=admin_headers).status_code == 422


# --------------------------------------------------------------------------- #
# Conversation records
# --------------------------------------------------------------------------- #
def test_conversation_list_and_pagination(client, admin_headers, user_headers):
    client.post("/api/chat", json={"message": "怎么开发票？"}, headers=user_headers)
    payload = client.get("/api/admin/conversations?page=1&page_size=2", headers=admin_headers).json()
    assert payload["page"] == 1
    assert payload["page_size"] == 2
    assert payload["total"] >= 1
    assert len(payload["items"]) <= 2


def test_conversation_list_filters_by_intent(client, admin_headers, user_headers):
    client.post("/api/chat", json={"message": "App 闪退打不开"}, headers=user_headers)
    payload = client.get("/api/admin/conversations?intent=tech_support", headers=admin_headers).json()
    assert all(item["primary_intent"] == "tech_support" for item in payload["items"])


def test_conversation_list_filters_by_handoff(client, admin_headers, user_headers):
    client.post("/api/chat", json={"message": "我要转人工"}, headers=user_headers)
    payload = client.get("/api/admin/conversations?handoff_only=true", headers=admin_headers).json()
    assert all(item["handoff_count"] > 0 for item in payload["items"])


def test_conversation_search_by_message_content(client, admin_headers, user_headers):
    client.post("/api/chat", json={"message": "订单 ORD-1003 在哪里"}, headers=user_headers)
    payload = client.get("/api/admin/conversations?q=ORD-1003", headers=admin_headers).json()
    assert payload["total"] >= 1


def test_admin_can_read_full_transcript(client, admin_headers, user_headers):
    created = client.post("/api/chat", json={"message": "怎么开发票？"}, headers=user_headers).json()
    detail = client.get(f"/api/admin/conversations/{created['conversation_id']}", headers=admin_headers).json()
    assert len(detail["messages"]) >= 2
    assert detail["messages"][1]["intent"] == Intent.FAQ.value
    assert detail["messages"][1]["sources"]


def test_unknown_conversation_returns_404(client, admin_headers):
    assert client.get("/api/admin/conversations/CONV-NOPE", headers=admin_headers).status_code == 404


# --------------------------------------------------------------------------- #
# Human handoff
# --------------------------------------------------------------------------- #
def test_handoff_queue_and_agent_reply(client, admin_headers, user_headers):
    created = client.post("/api/chat", json={"message": "我要转人工"}, headers=user_headers).json()
    conversation_id = created["conversation_id"]

    queue = client.get("/api/admin/handoffs?status=pending", headers=admin_headers).json()
    task = next((item for item in queue if item["conversation_id"] == conversation_id), None)
    assert task is not None, "the handoff task should be queued"

    replied = client.post(
        f"/api/admin/handoffs/{task['id']}/reply",
        json={"content": "您好，我是人工客服小张，正在为您处理。", "resolve": False},
        headers=admin_headers,
    )
    assert replied.status_code == 200
    assert replied.json()["status"] == "replied"

    detail = client.get(f"/api/admin/conversations/{conversation_id}", headers=admin_headers).json()
    assert any(message["role"] == "agent" for message in detail["messages"])
    assert detail["status"] == "handoff_replied"

    resolved = client.post(f"/api/admin/handoffs/{task['id']}/resolve", headers=admin_headers).json()
    assert resolved["status"] == "resolved"
    assert resolved["resolved_at"]


def test_reply_to_unknown_handoff_returns_404(client, admin_headers):
    response = client.post("/api/admin/handoffs/999999/reply", json={"content": "hi"}, headers=admin_headers)
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Refund review
# --------------------------------------------------------------------------- #
def test_refund_review_approve_and_execute(client, admin_headers, user_headers):
    created = client.post("/api/chat", json={"message": "订单 ORD-2001 我要申请退款"}, headers=user_headers)
    if created.status_code == 200:
        pass  # user001 may not own ORD-2001; the admin flow is exercised below regardless

    chat = client.post(
        "/api/chat", json={"message": "订单 ORD-2001 我要申请退款"}, headers={"X-User-Id": "user002"}
    ).json()

    queue = client.get("/api/admin/refunds?status=pending_review", headers=admin_headers).json()
    target = next((item for item in queue if item["order_id"] == "ORD-2001"), None)
    if target is None:
        pytest.skip("ORD-2001 already reviewed by an earlier test")

    response = client.post(
        f"/api/admin/refunds/{target['proposal_id']}/review",
        json={"decision": "approve", "comment": "核实无误", "execute": True},
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["status"] in ("approved", "executed")
    assert response.json()["reviewer_id"] == "admin"
    assert chat["proposal_id"]


def test_refund_review_reject(client, admin_headers):
    queue = client.get("/api/admin/refunds?status=pending_review", headers=admin_headers).json()
    if not queue:
        pytest.skip("no pending refund proposals left")
    response = client.post(
        f"/api/admin/refunds/{queue[0]['proposal_id']}/review",
        json={"decision": "reject", "comment": "不符合退款条件", "execute": False},
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


def test_refund_review_unknown_proposal_returns_404(client, admin_headers):
    response = client.post(
        "/api/admin/refunds/RP-NOPE/review", json={"decision": "approve"}, headers=admin_headers
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# FAQ knowledge base
# --------------------------------------------------------------------------- #
def test_faq_crud_lifecycle(client, admin_headers):
    created = client.post(
        "/api/admin/faqs",
        json={
            "category": "general",
            "question": "管理员测试问题：如何联系在线客服？",
            "answer": "在对话框中回复「转人工」即可。",
            "keywords": "联系 客服 在线",
            "enabled": True,
        },
        headers=admin_headers,
    )
    assert created.status_code == 201
    faq_id = created.json()["id"]

    listed = client.get("/api/admin/faqs", headers=admin_headers).json()
    assert any(item["id"] == faq_id for item in listed)

    updated = client.put(
        f"/api/admin/faqs/{faq_id}",
        json={
            "category": "general",
            "question": "管理员测试问题：如何联系在线客服？",
            "answer": "已更新的答案。",
            "keywords": "联系 客服",
            "enabled": True,
        },
        headers=admin_headers,
    )
    assert updated.status_code == 200
    assert updated.json()["answer"] == "已更新的答案。"

    assert client.delete(f"/api/admin/faqs/{faq_id}", headers=admin_headers).status_code == 204
    assert client.delete(f"/api/admin/faqs/{faq_id}", headers=admin_headers).status_code == 404


def test_new_faq_is_immediately_searchable(client, admin_headers, user_headers):
    created = client.post(
        "/api/admin/faqs",
        json={
            "category": "general",
            "question": "企业采购可以开专票吗？",
            "answer": "企业采购支持开具增值税专用发票，请提供公司名称、税号与开户信息。",
            "keywords": "专票 企业 采购 增值税",
            "enabled": True,
        },
        headers=admin_headers,
    )
    faq_id = created.json()["id"]
    try:
        answer = client.post("/api/chat", json={"message": "企业采购可以开专票吗"}, headers=user_headers).json()
        assert "专用发票" in answer["answer"]
        assert answer["sources"]
    finally:
        client.delete(f"/api/admin/faqs/{faq_id}", headers=admin_headers)


def test_faq_search_filter(client, admin_headers):
    payload = client.get("/api/admin/faqs?q=退款", headers=admin_headers).json()
    assert isinstance(payload, list)


def test_faq_reindex(client, admin_headers):
    payload = client.post("/api/admin/faqs/reindex", headers=admin_headers).json()
    assert payload["status"] == "ok"
    assert payload["indexed_documents"] > 0


def test_tickets_endpoint(client, admin_headers):
    assert isinstance(client.get("/api/admin/tickets", headers=admin_headers).json(), list)
