# API 参考

- 基础地址：`http://localhost:8000`
- 交互式文档：`/docs`（Swagger UI）· `/redoc`（ReDoc）
- 用户身份：`AUTH_MODE=dev` 时使用 `X-User-Id` 请求头；`AUTH_MODE=jwt` 时使用 `Authorization: Bearer <token>`
- 管理员身份：`Authorization: Bearer <admin token>`

## 系统

### `GET /api/healthz`

```json
{ "status": "ok", "version": "1.0.0" }
```

### `GET /api/readyz`

```json
{
  "status": "ready",
  "version": "1.0.0",
  "engine_mode": "builtin",
  "engine": "builtin",
  "llm_enabled": false,
  "database": "ok",
  "faq_entries": 39,
  "conversations": 14,
  "messages": 74,
  "faq_index_size": 39
}
```

## 对话

### `POST /api/chat`

**请求**

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `message` | string | 是 | 1–4000 字符，空白字符会被拒绝 |
| `conversation_id` | string | 否 | 传入则延续该会话，留空则新建 |
| `user_id` | string | 否 | 仅 `AUTH_MODE=dev` 下作为身份兜底 |

**响应** `200`

```json
{
  "request_id": "3f1c…",
  "conversation_id": "CONV-9A2B…",
  "message_id": 88,
  "answer": "订单 **ORD-1001** 的最新状态：\n- 商品：无线降噪耳机 Pro\n…",
  "intent": "order_query",
  "intent_label": "订单查询",
  "confidence": 0.82,
  "sources": [{ "title": "订单 ORD-1001", "category": "order", "score": 1.0 }],
  "handoff": false,
  "handoff_reason": "",
  "ticket_id": "",
  "proposal_id": "",
  "suggestions": ["帮我查一下订单 ORD-1001 的物流", "…"],
  "latency_ms": 12
}
```

`handoff_reason` 取值：`user_requested` · `unresolved` · `dify_signal` · `dify_unavailable`。

**错误**

| 状态码 | 场景 |
| --- | --- |
| `422` | `message` 为空或超长 |
| `429` | 超过 `RATE_LIMIT_PER_MINUTE` |
| `401` | `AUTH_MODE=jwt` 且 Token 缺失 / 无效 |

### `POST /api/chat/stream`

请求体同 `/api/chat`，返回 `text/event-stream`：

```text
event: meta
data: {"conversation_id":"…","intent":"refund","confidence":0.59,"handoff":false,"sources":[…],"suggestions":[…]}

event: delta
data: {"index":0,"text":"退款申请经客服审核通过后…"}

event: delta
data: {"index":1,"text":"退款申请提交后进入人工审核队列…"}

event: done
data: {"answer":"…","conversation_id":"…","message_id":88,"latency_ms":12,"ask_satisfaction":false}

event: end
data: {}
```

`error` 事件会在异常时替代 `done` 出现，`end` 始终最后发出。

## 会话与满意度

### `GET /api/conversations?limit=30`

返回当前用户的历史会话，按更新时间倒序。

### `GET /api/conversations/{conversation_id}`

返回会话详情，`messages` 数组包含完整消息流水（含每轮 `intent`、`confidence`、`sources`、`handoff`）。会话不属于调用者时返回 `404`。

### `DELETE /api/conversations/{conversation_id}`

删除会话及其消息，返回 `204`。

### `POST /api/conversations/{conversation_id}/close`

将会话状态置为 `closed`。

### `POST /api/conversations/{conversation_id}/satisfaction`

```json
{ "score": 5, "comment": "问题解决得很快" }
```

`score` 取值 1–5，越界返回 `422`。

### `GET /api/me`

```json
{
  "user_id": "user001",
  "display_name": "张伟",
  "level": "VIP",
  "orders": [{ "order_id": "ORD-1001", "product_name": "…", "status": "shipped", "amount": 899.0, "refund_available": true }],
  "order_count": 3
}
```

## 管理后台

### `POST /api/admin/login`

```json
{ "username": "admin", "password": "admin123" }
```

返回：

```json
{ "access_token": "eyJ…", "token_type": "bearer", "expires_in": 43200, "username": "admin" }
```

失败返回 `401`。

### `GET /api/admin/me`

返回当前管理员用户名与角色。

### 统计

| 接口 | 说明 |
| --- | --- |
| `GET /api/admin/stats/overview` | `conversations` `messages` `active_conversations` `resolved_conversations` `handoff_conversations` `handoff_rate` `rated_conversations` `satisfaction_avg` `satisfaction_rate` `refunds_pending` `tickets_open` `handoffs_pending` |
| `GET /api/admin/stats/satisfaction` | `rated` `average` `distribution`（1–5 分计数）`positive_rate`（≥4 分）`negative_rate`（≤2 分） |
| `GET /api/admin/stats/intents` | `[{ intent, label, count, share }]` |
| `GET /api/admin/stats/trend?days=14` | `[{ date, conversations, messages, satisfaction_avg }]`，`days` 范围 1–90 |

### 对话记录

`GET /api/admin/conversations`

| 参数 | 说明 |
| --- | --- |
| `q` | 匹配标题、用户 ID、会话 ID 或消息内容 |
| `intent` | `refund` / `order_query` / `tech_support` / `human_handoff` / `faq` / `other` |
| `status` | `active` / `handoff_pending` / `handoff_replied` / `resolved` / `closed` |
| `user_id` | 精确匹配用户 |
| `handoff_only` | 仅返回发生过转人工的会话 |
| `page` / `page_size` | 分页，`page_size` 上限 100 |

`GET /api/admin/conversations/{id}` 返回完整对话流水。
`GET /api/admin/messages?q=关键词` 跨会话搜索消息内容。

### 人工工单

| 接口 | 说明 |
| --- | --- |
| `GET /api/admin/handoffs?status=pending` | 工单队列，`status` 可选 `pending` / `replied` / `resolved` |
| `POST /api/admin/handoffs/{id}/reply` | `{ "content": "…", "resolve": false }`，以 `agent` 角色写入会话 |
| `POST /api/admin/handoffs/{id}/resolve` | 标记已解决并将会话置为 `resolved` |

### 退款审核

| 接口 | 说明 |
| --- | --- |
| `GET /api/admin/refunds?status=pending_review` | 退款申请队列 |
| `POST /api/admin/refunds/{proposal_id}/review` | `{ "decision": "approve" \| "reject", "comment": "…", "execute": true }` |

`execute=true` 且审批通过时会立即执行退款；执行失败不会回滚审批结果，仅记录日志。

### FAQ 知识库

| 接口 | 说明 |
| --- | --- |
| `GET /api/admin/faqs?category=&q=` | 列表 / 检索 |
| `POST /api/admin/faqs` | 新增，返回 `201` |
| `PUT /api/admin/faqs/{id}` | 更新 |
| `DELETE /api/admin/faqs/{id}` | 删除，返回 `204` |
| `POST /api/admin/faqs/reindex` | 重建检索索引 |

写入操作会自动使检索索引失效，下一次查询即时生效，无需重启服务。

### 工单

`GET /api/admin/tickets?status=created` 返回技术支持工单列表。
