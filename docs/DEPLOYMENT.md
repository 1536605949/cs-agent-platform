# 部署指南

## 一、本地运行

```bash
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

首次启动会自动建表并加载 `data/faq.json` 中的 FAQ 知识库。如需生成带满意度评分的演示数据：

```bash
python scripts/seed.py --reset --with-demo-chats 14
```

## 二、Docker 部署

### 单容器 + SQLite（最简单）

```bash
cp .env.example .env          # 按需修改
docker compose up -d --build
```

### 应用 + PostgreSQL（生产推荐）

```bash
cp deploy/.env.production.example .env
# 修改 .env 中所有 CHANGE_ME 占位值，并确认 DATABASE_URL 指向 postgres
docker compose --profile postgres up -d --build
```

Compose 会等待 PostgreSQL 健康检查通过后再启动应用。数据分别持久化在 `app_data` 与 `postgres_data` 卷中。

### 查看状态与日志

```bash
docker compose ps
docker compose logs -f app
docker compose exec app python scripts/smoke_test.py --base-url http://127.0.0.1:8000
```

### 升级

```bash
git pull
docker compose up -d --build
```

数据保存在命名卷中，重建镜像不会丢失数据。

## 三、反向代理

`deploy/nginx.conf` 提供了可直接使用的配置，关键点：

- `/api/chat/stream` 必须关闭缓冲（`proxy_buffering off`）并放宽 `proxy_read_timeout`，否则 SSE 流式输出会被攒批，用户看不到逐段呈现效果。
- 建议在代理层额外配置 TLS、访问日志与请求体大小限制。
- 建议把 `/api/admin` 限制在内网或接入 VPN / 单点登录。

## 四、环境变量要点

| 变量 | 生产建议 |
| --- | --- |
| `ENVIRONMENT` | `production`（启用安全校验，占位密钥会导致启动失败） |
| `AUTH_MODE` | `jwt` |
| `JWT_SECRET` | `openssl rand -hex 32` 生成，≥32 位 |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | 必须修改 |
| `DATABASE_URL` | 指向 PostgreSQL |
| `AUTO_SEED` | `false` |
| `RATE_LIMIT_PER_MINUTE` | 按实际流量调整 |
| `CORS_ORIGINS` | 填写真实前端域名 |

`ENVIRONMENT=production` 时的启动校验会检查：

- `JWT_SECRET` 非占位值且长度 ≥ 32
- `ADMIN_PASSWORD` 非默认值
- `AUTH_MODE=jwt`
- `ENGINE_MODE=dify` 时 `DIFY_API_KEY` 为真实值
- `LLM_ENABLED=true` 时 `LLM_API_KEY` 为真实值

任一不满足，进程会直接拒绝启动并打印具体原因。

## 五、数据与备份

### SQLite

```bash
# 热备份（WAL 模式下安全）
sqlite3 data/app.db ".backup 'backups/app-$(date +%F).db'"
```

### PostgreSQL

```bash
docker compose exec postgres pg_dump -U cs_user cs_platform | gzip > backup-$(date +%F).sql.gz
```

### 恢复

```bash
gunzip -c backup-2026-09-17.sql.gz | docker compose exec -T postgres psql -U cs_user -d cs_platform
```

## 六、扩容与高可用

当前实现为单进程设计，多副本部署前需要处理以下两点：

1. **限流**：`app/rate_limit.py` 为进程内滑动窗口。多副本应替换为 Redis 实现，或在网关层统一限流。
2. **会话一致性**：会话状态存储在数据库中，天然支持多副本；但 SSE 长连接需要在负载均衡上启用会话保持，或改用短轮询。

数据库层面，PostgreSQL 可通过只读副本分担统计查询压力。

## 七、健康检查与监控

| 端点 | 用途 |
| --- | --- |
| `/api/healthz` | 存活探针，不访问数据库 |
| `/api/readyz` | 就绪探针，返回数据库连通性与知识库规模 |

Dockerfile 与 docker-compose.yml 已内置基于 `/api/healthz` 的 `HEALTHCHECK`。

建议监控指标：请求量与 P95 延迟、`/api/chat` 的转人工率、`/api/admin/stats/satisfaction` 的平均分与差评率、`/api/admin/stats/overview` 的待处理工单积压量。

## 八、故障排查

| 现象 | 排查方向 |
| --- | --- |
| 启动即退出并打印 `Unsafe production configuration` | 按提示替换对应的占位密钥 |
| 聊天回答总是「未找到相关知识库内容」 | 检查 `/api/readyz` 的 `faq_entries` 是否为 0；若为 0，执行 `python scripts/seed.py` |
| 流式输出一次性全部出现 | 反向代理未关闭缓冲，检查 `proxy_buffering off` |
| 修改 FAQ 后回答未更新 | 正常情况下索引会自动重建；可调用 `POST /api/admin/faqs/reindex` 手动确认 |
| 管理后台登录后立即跳回登录页 | Token 已过期（默认 12 小时）或 `JWT_SECRET` 在运行期间被更改 |
| 退款审批返回 `409 approval_required` | 该订单的申请尚未审批，或审批结果已被消费 |
| 频繁出现 `429` | 调高 `RATE_LIMIT_PER_MINUTE`，或确认是否多个用户共用同一 `user_id` |
