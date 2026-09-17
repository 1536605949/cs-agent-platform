"""End-user HTTP API: chat, streaming, history and satisfaction."""

from __future__ import annotations

import json

from app.core.intent import Intent


def test_healthz(client):
    assert client.get("/api/healthz").json()["status"] == "ok"


def test_readyz_reports_knowledge_base(client):
    payload = client.get("/api/readyz").json()
    assert payload["status"] == "ready"
    assert payload["faq_entries"] > 0
    assert payload["engine_mode"] == "builtin"


def test_chat_returns_structured_answer(client, user_headers):
    response = client.post("/api/chat", json={"message": "帮我查一下订单 ORD-1001 的物流"}, headers=user_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == Intent.ORDER_QUERY.value
    assert body["conversation_id"]
    assert body["answer"]
    assert body["intent_label"] == "订单查询"
    assert body["suggestions"]


def test_chat_validates_empty_message(client, user_headers):
    assert client.post("/api/chat", json={"message": "   "}, headers=user_headers).status_code == 422


def test_chat_rejects_overlong_message(client, user_headers):
    response = client.post("/api/chat", json={"message": "x" * 5000}, headers=user_headers)
    assert response.status_code == 422


def test_chat_continues_the_same_conversation(client, user_headers):
    first = client.post("/api/chat", json={"message": "你好"}, headers=user_headers).json()
    second = client.post(
        "/api/chat",
        json={"message": "帮我查订单 ORD-1001", "conversation_id": first["conversation_id"]},
        headers=user_headers,
    ).json()
    assert second["conversation_id"] == first["conversation_id"]


def test_chat_stream_emits_meta_delta_done(client, user_headers):
    with client.stream(
        "POST",
        "/api/chat/stream",
        json={"message": "怎么开发票？"},
        headers=user_headers,
    ) as response:
        assert response.status_code == 200
        payload = "".join(response.iter_text())

    assert "event: meta" in payload
    assert "event: delta" in payload
    assert "event: done" in payload
    assert "event: end" in payload

    meta_line = [line for line in payload.split("\n") if line.startswith("data: ") and '"intent"' in line][0]
    meta = json.loads(meta_line[6:])
    assert meta["intent"] == Intent.FAQ.value


def test_conversation_history_is_persisted(client, user_headers):
    created = client.post("/api/chat", json={"message": "收不到验证码"}, headers=user_headers).json()
    conversation_id = created["conversation_id"]

    listed = client.get("/api/conversations", headers=user_headers).json()
    assert any(item["conversation_id"] == conversation_id for item in listed)

    detail = client.get(f"/api/conversations/{conversation_id}", headers=user_headers).json()
    assert len(detail["messages"]) == 2
    assert detail["messages"][0]["role"] == "user"
    assert detail["messages"][1]["role"] == "assistant"


def test_conversations_are_scoped_to_the_caller(client, user_headers):
    created = client.post("/api/chat", json={"message": "你好"}, headers=user_headers).json()
    other_user = {"X-User-Id": "user999"}
    assert client.get(f"/api/conversations/{created['conversation_id']}", headers=other_user).status_code == 404


def test_satisfaction_is_recorded(client, user_headers):
    created = client.post("/api/chat", json={"message": "怎么开发票？"}, headers=user_headers).json()
    conversation_id = created["conversation_id"]

    response = client.post(
        f"/api/conversations/{conversation_id}/satisfaction",
        json={"score": 4, "comment": "还不错"},
        headers=user_headers,
    )
    assert response.status_code == 200
    assert response.json()["satisfaction_score"] == 4

    detail = client.get(f"/api/conversations/{conversation_id}", headers=user_headers).json()
    assert detail["satisfaction_score"] == 4
    assert detail["satisfaction_comment"] == "还不错"


def test_satisfaction_score_is_validated(client, user_headers):
    created = client.post("/api/chat", json={"message": "你好"}, headers=user_headers).json()
    conversation_id = created["conversation_id"]
    assert client.post(
        f"/api/conversations/{conversation_id}/satisfaction", json={"score": 9}, headers=user_headers
    ).status_code == 422


def test_handoff_flow_over_http(client, user_headers):
    response = client.post("/api/chat", json={"message": "我要转人工"}, headers=user_headers).json()
    assert response["handoff"] is True
    assert response["handoff_reason"] == "user_requested"

    detail = client.get(f"/api/conversations/{response['conversation_id']}", headers=user_headers).json()
    assert detail["status"] == "handoff_pending"


def test_close_conversation(client, user_headers):
    created = client.post("/api/chat", json={"message": "你好"}, headers=user_headers).json()
    response = client.post(f"/api/conversations/{created['conversation_id']}/close", headers=user_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "closed"


def test_me_endpoint_returns_profile_and_orders(client, user_headers):
    payload = client.get("/api/me", headers=user_headers).json()
    assert payload["user_id"] == "user001"
    assert payload["order_count"] > 0
    assert payload["orders"]


def test_delete_conversation(client, user_headers):
    created = client.post("/api/chat", json={"message": "临时会话"}, headers=user_headers).json()
    conversation_id = created["conversation_id"]
    assert client.delete(f"/api/conversations/{conversation_id}", headers=user_headers).status_code == 204
    assert client.get(f"/api/conversations/{conversation_id}", headers=user_headers).status_code == 404
