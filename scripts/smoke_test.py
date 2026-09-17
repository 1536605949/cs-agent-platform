#!/usr/bin/env python
"""End-to-end smoke test against a running server.

Exercises the full user journey: multi-turn context, intent recognition, FAQ
retrieval, order lookup, refund proposal, human handoff, satisfaction rating and
the admin statistics endpoints.

Usage::

    python scripts/smoke_test.py --base-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import sys

import httpx

PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
_results: list[tuple[bool, str]] = []


def check(condition: bool, label: str) -> bool:
    _results.append((bool(condition), label))
    print(f"  [{PASS if condition else FAIL}] {label}")
    return bool(condition)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--user", default="user001")
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-password", default="admin123")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    user_headers = {"X-User-Id": args.user}
    client = httpx.Client(base_url=base, timeout=30.0)

    print(f"\n== 健康检查 ({base}) ==")
    health = client.get("/api/healthz")
    check(health.status_code == 200, "GET /api/healthz 返回 200")
    ready = client.get("/api/readyz").json()
    check(ready.get("status") == "ready", f"readyz 状态 ready（引擎 {ready.get('engine_mode')}，FAQ {ready.get('faq_entries')} 条）")
    check(ready.get("faq_entries", 0) > 0, "FAQ 知识库已加载")

    print("\n== 多轮对话 + 意图识别 ==")
    first = client.post("/api/chat", json={"message": "你好，我想问个问题"}, headers=user_headers).json()
    conversation_id = first["conversation_id"]
    check(bool(conversation_id), "创建会话并返回 conversation_id")

    second = client.post(
        "/api/chat",
        json={"message": "帮我查一下订单 ORD-1001 的物流", "conversation_id": conversation_id},
        headers=user_headers,
    ).json()
    check(second["intent"] == "order_query", f"意图识别为订单查询（实际 {second['intent']}）")
    check("ORD-1001" in second["answer"], "订单查询返回了订单信息")

    print("\n== 多轮上下文（槽位记忆） ==")
    third = client.post(
        "/api/chat",
        json={"message": "那这个订单能退款吗", "conversation_id": conversation_id},
        headers=user_headers,
    ).json()
    check(third["intent"] == "refund", f"追问被识别为退款意图（实际 {third['intent']}）")
    check(bool(third.get("proposal_id")), f"复用上文订单号并创建退款申请（{third.get('proposal_id')}）")

    print("\n== FAQ 知识库检索 ==")
    faq = client.post("/api/chat", json={"message": "怎么开发票？", "conversation_id": conversation_id},
                      headers=user_headers).json()
    check(faq["intent"] == "faq", f"识别为常见问题（实际 {faq['intent']}）")
    check(len(faq["sources"]) > 0, f"返回知识库引用 {len(faq['sources'])} 条")
    check("发票" in faq["answer"], "回答内容来自知识库")

    print("\n== 技术支持与工单 ==")
    tech = client.post("/api/chat", json={"message": "App 报错闪退打不开", "conversation_id": conversation_id},
                       headers=user_headers).json()
    check(tech["intent"] == "tech_support", f"识别为技术支持（实际 {tech['intent']}）")
    check(len(tech["answer"]) > 0, "返回了排查建议")

    print("\n== 自动转人工 ==")
    handoff_conv = client.post("/api/chat", json={"message": "我要转人工", "conversation_id": ""},
                               headers=user_headers).json()
    check(handoff_conv["handoff"] is True, "显式转人工请求触发 handoff")
    check("转接" in handoff_conv["answer"] or "人工" in handoff_conv["answer"], "回复中包含转人工提示")

    print("\n== 对话历史持久化 ==")
    conversations = client.get("/api/conversations", headers=user_headers).json()
    check(len(conversations) >= 2, f"会话列表返回 {len(conversations)} 个会话")
    detail = client.get(f"/api/conversations/{conversation_id}", headers=user_headers).json()
    check(len(detail["messages"]) >= 8, f"会话详情返回 {len(detail['messages'])} 条消息")

    print("\n== 满意度评价 ==")
    rated = client.post(f"/api/conversations/{conversation_id}/satisfaction",
                        json={"score": 5, "comment": "smoke test"}, headers=user_headers).json()
    check(rated["satisfaction_score"] == 5, "满意度评分已持久化")

    print("\n== 管理后台 ==")
    login = client.post("/api/admin/login", json={"username": args.admin_user, "password": args.admin_password})
    check(login.status_code == 200, "管理员登录成功")
    token = login.json()["access_token"]
    admin_headers = {"Authorization": "Bearer " + token}

    unauthorized = client.get("/api/admin/stats/overview")
    check(unauthorized.status_code in (401, 403), "未授权访问统计接口被拒绝")

    overview = client.get("/api/admin/stats/overview", headers=admin_headers).json()
    check(overview["conversations"] > 0, f"概览统计：{overview['conversations']} 个会话 / {overview['messages']} 条消息")
    check(overview["satisfaction_avg"] > 0, f"平均满意度 {overview['satisfaction_avg']}")

    satisfaction = client.get("/api/admin/stats/satisfaction", headers=admin_headers).json()
    check(satisfaction["rated"] > 0, f"满意度统计：{satisfaction['rated']} 条评价，好评率 {satisfaction['positive_rate']}")

    intents = client.get("/api/admin/stats/intents", headers=admin_headers).json()
    check(len(intents) > 0, f"意图分布返回 {len(intents)} 类")

    trend = client.get("/api/admin/stats/trend?days=7", headers=admin_headers).json()
    check(len(trend) == 7, f"趋势数据返回 {len(trend)} 天")

    admin_convs = client.get("/api/admin/conversations?page=1&page_size=5", headers=admin_headers).json()
    check(admin_convs["total"] > 0, f"管理端对话记录总数 {admin_convs['total']}")

    admin_detail = client.get(f"/api/admin/conversations/{conversation_id}", headers=admin_headers).json()
    check(len(admin_detail["messages"]) > 0, "管理端可查看完整对话内容")

    handoffs = client.get("/api/admin/handoffs", headers=admin_headers).json()
    check(len(handoffs) > 0, f"人工工单队列返回 {len(handoffs)} 条")

    faqs = client.get("/api/admin/faqs", headers=admin_headers).json()
    check(len(faqs) > 0, f"FAQ 管理返回 {len(faqs)} 条")

    refunds = client.get("/api/admin/refunds?status=pending_review", headers=admin_headers).json()
    check(len(refunds) > 0, f"退款待审核队列返回 {len(refunds)} 条")

    print("\n== 退款人工审核闭环 ==")
    if refunds:
        reviewed = client.post(f"/api/admin/refunds/{refunds[0]['proposal_id']}/review",
                               json={"decision": "approve", "comment": "smoke test", "execute": True},
                               headers=admin_headers)
        check(reviewed.status_code == 200, "管理员审核通过并执行退款")
        check(reviewed.json()["status"] in ("executed", "approved"), f"退款状态为 {reviewed.json()['status']}")

    client.close()

    passed = sum(1 for ok, _ in _results if ok)
    total = len(_results)
    print(f"\n{'=' * 52}\n结果：{passed}/{total} 项通过\n{'=' * 52}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
