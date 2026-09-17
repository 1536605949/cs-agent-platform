# 架构说明

## 分层

```text
┌──────────────────────────────────────────────────────────────┐
│ 表现层    app/static/                                         │
│   聊天页 index.html + app.js   管理后台 admin.html + admin.js  │
│   免构建：原生 HTML/CSS/JS，无 CDN，无打包步骤                  │
├──────────────────────────────────────────────────────────────┤
│ 接口层    app/api/                                            │
│   chat.py   对话 / SSE 流式 / 历史 / 满意度                    │
│   admin.py  统计 / 记录 / 工单 / 退款 / FAQ                    │
│   health.py 存活与就绪探针                                     │
├──────────────────────────────────────────────────────────────┤
│ 安全层    app/security.py · app/rate_limit.py                 │
│   PBKDF2 密码哈希 · HS256 Token · 角色校验 · 滑动窗口限流       │
├──────────────────────────────────────────────────────────────┤
│ 编排层    app/core/                                           │
│   intent.py  意图识别与槽位抽取                                │
│   faq.py     BM25 检索与索引缓存                               │
│   engine.py  流水线编排：路由 → 接地 → 升级                     │
│   llm.py     可选 LLM（失败即降级）                            │
│   dify_adapter.py  可选 Dify 编排后端                          │
├──────────────────────────────────────────────────────────────┤
│ 领域层    app/tools/business.py                               │
│   订单查询 · 退款状态机 · 工单 · 审计事件 · 种子数据             │
├──────────────────────────────────────────────────────────────┤
│ 持久层    app/models.py · app/database.py                     │
│   SQLAlchemy 2.0 ORM，SQLite 与 PostgreSQL 同一套代码           │
└──────────────────────────────────────────────────────────────┘
```

## 关键流程

### 1. 对话处理

`Engine.handle()` 是唯一的对话入口，同步完成「持久化 → 识别 → 路由 → 接地 → 升级 → 持久化」六个阶段，返回 `ChatOutcome`。`stream_outcome()` 在其之上包装为 SSE 事件序列，因此流式与非流式共用同一套业务逻辑，不存在两套实现漂移的问题。

`ChatOutcome` 携带 `segments`（分段回答）而非单一字符串，使流式推送可以按逻辑段落投递，而不是把已经生成好的答案机械地切开。

### 2. 意图识别

```text
文本
 ├─ 加权关键词打分          _RULES: 意图 → {短语: 权重}
 ├─ 小聊判定                仅命中 _SMALL_TALK → other + smalltalk=True
 ├─ 转人工短路              handoff 得分 ≥ 2.4 → 直接返回，置信度 0.98
 ├─ 上下文平滑              短消息且与上一轮意图重叠 → ×1.25
 ├─ 无关键词处理            短追问（≤12 字）继承上一轮意图
 ├─ 槽位线索                含订单号且无其他意图 → order_query
 └─ 置信度                 0.35 + 0.5 × 主导度 × (0.4 + 0.6 × 信号强度)
```

置信度低于 `HANDOFF_CONFIDENCE_THRESHOLD` 且启用 LLM 时，才会调用模型兜底；模型返回不可用或非法标签时保留规则结果。

### 3. FAQ 检索

```text
文本 → tokenize
        ├─ ASCII 段：小写词
        └─ 中文段：单字 + 相邻二元组

文档 = 问题×3 + 关键词×2 + 答案×1
得分 = Σ idf(t) × tf(t)×(k1+1) / (tf(t) + k1×(1-b+b×len/avgLen))
       + 2.5（查询与问题互为子串时）

idf(t) = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))   // 恒正
```

索引以 `(条目数, 最新 updated_at)` 为指纹缓存；FAQ 增删改后调用 `invalidate_faq_cache()`，下次查询自动重建。

### 4. 升级与转人工

未接地（既无知识库命中，也无工具结果）时 `fallback_count += 1`；达到 `HANDOFF_MAX_FALLBACK_TURNS` 即创建 `HandoffTask` 并把会话置为 `handoff_pending`。任一成功回答都会把计数器清零。

`HandoffTask.summary` 由引擎生成，包含用户、意图、置信度、触发原因、关联订单/工单/退款单号与用户最后一句话——人工客服据此可直接接手。

### 5. 退款状态机

```text
create_refund_proposal      → pending_review
approve_or_reject_refund    → approved | rejected     （仅管理员）
execute_approved_refund     → executed                （条件 UPDATE，仅一次）
```

`execute_approved_refund` 使用 `UPDATE ... WHERE proposal_id = ? AND status = 'approved'`，通过受影响行数判断是否成功消费审批结果。并发或重放场景下第二次调用匹配 0 行并返回错误，审批结果不可能被使用两次。

## 可观测性

- 每个请求生成 `X-Request-ID` 并回写响应头，日志中贯穿该 ID 与耗时。
- `/api/healthz` 为存活探针；`/api/readyz` 额外返回引擎模式、LLM 开关、数据库连通性、FAQ 条目数、会话与消息总数。
- `audit_events` 表记录退款申请 / 审批 / 执行、工单创建、转人工等关键业务动作。

## 扩展点

| 需求 | 修改位置 |
| --- | --- |
| 换成向量检索 | 重写 `app/core/faq.py::retrieve_faq`，保持返回 `FaqHit` 列表 |
| 增加新的业务意图 | 在 `app/core/intent.py::_RULES` 增加规则，在 `Engine._route` 增加分支 |
| 接入真实订单系统 | 替换 `app/tools/business.py` 中 `get_owned_order` / `list_user_orders` 的实现 |
| 分布式限流 | 替换 `app/rate_limit.py` 为 Redis 实现 |
| 人工客服实时推送 | 在 `HandoffTask` 回复后向 `conversations/{id}` 推送 WebSocket 消息 |
| 接入其他编排引擎 | 实现 `handle()` 与 `astream()` 契约，在 `app/core/factory.py` 注册 |
