# R4 工程栈调研：广告控制平面 Python 脚手架

调研日期：2026-08-28。方法：公开文档 WebFetch/WebSearch + 本机只读命令验证（未安装任何东西）。
标注约定：凡属"综合推断"（把多个来源拼成的实现建议）均显式标 **[综合]**；其余为来源直述事实。

---

## A. Transactional Outbox + at-most-once provider submit

### 事实与引用

**1. Outbox 模式（业界标准定义）** — microservices.io
- 问题：服务需要"更新数据库 + 发消息"原子化，而不用 2PC。
- 解法：不直接发消息，而是在**同一个数据库事务**里更新业务实体并向 outbox 表插入消息记录；独立的 relay 进程异步发布。
- Relay 两种实现：**Polling Publisher**（轮询 outbox 表）与 **Transaction Log Tailing**（尾随 WAL/binlog）。
- 交付语义：**at-least-once**。原文："The Message relay might publish a message more than once. It might, for example, crash after publishing a message but before recording the fact that it has done so." 因此 "a message consumer must be idempotent, perhaps by tracking the IDs of the messages that it has already processed."
- 来源：https://microservices.io/patterns/data/transactional-outbox.html

**2. "Exactly-once delivery 不可能"经典论证** — Tyler Treat, *You Cannot Have Exactly-Once Delivery*, bravenewgeek.com, 2015-03-25
- 论证基础：Two Generals 问题 + FLP 结果。原文："FLP and the Two Generals Problem are not design complexities, they are *impossibility results*."
- 只有两种可行交付模型：at-most-once（先 ack 后处理，可能丢）与 at-least-once（先处理后 ack，可能重）。
- 实际出路：幂等操作或去重（"fake it" via idempotency/dedup）。
- 来源：https://bravenewgeek.com/you-cannot-have-exactly-once-delivery/

**3. SELECT FOR UPDATE SKIP LOCKED（PostgreSQL 官方文档）**
- 原文："With SKIP LOCKED, any selected rows that cannot be immediately locked are skipped."
- 官方明确背书队列场景："Skipping locked rows provides an inconsistent view of the data, so this is not suitable for general purpose work, but can be used to avoid lock contention with multiple consumers accessing a queue-like table."
- 来源：https://www.postgresql.org/docs/current/sql-select.html （The Locking Clause 节）

**4. PostgreSQL 实现要点** **[综合]**（由上述来源 + Nango/DB Pro 等工程实践文合成）
- **同事务写 intent + outbox**：`INSERT INTO campaign_intent ...; INSERT INTO outbox(intent_id, payload, status='pending') ...; COMMIT;` —— 保证"意图已落库 ⟺ 待提交任务存在"。
- **领取**：worker 用 `SELECT ... FROM outbox WHERE status='pending' AND run_at<=now() ORDER BY id LIMIT n FOR UPDATE SKIP LOCKED`，多 worker 互不阻塞、不重复领取。
- **对外提交的 at-most-once**：领取后**先把状态置为 `submitting` 并 COMMIT（write-ahead claim），再调用广告平台 API**。若进程在"已发出请求、未记录结果"间崩溃，行停在 `submitting`，**不自动重试**，进入人工/对账（reconciliation）恢复——宁可漏投一次待人工确认，不可盲目重发造成重复出价/重复预算变更。这正是 Treat 二选一里的 at-most-once 一侧；与 outbox 默认 at-least-once 的区别是把"重试"从自动改为对账驱动。
- **唯一约束防重**：submissions 表对 `(intent_id)` 或 `(provider, idempotency_key)` 加 UNIQUE，即使代码路径重入，第二次插入也会撞约束失败——数据库层兜底。若广告平台 API 支持 idempotency key，则传同一 key，可把语义提升为"effectively-once"。
- 佐证（SKIP LOCKED 作业表模式的生产案例）：Nango 用 Postgres 表 + `SELECT ... FOR UPDATE SKIP LOCKED` 处理每月数百万任务（https://nango.dev/blog/migrating-from-temporal-to-a-postgres-based-task-orchestrator/）。

---

## B. PostgreSQL Row-Level Security 多租户

### 事实与引用

**1. 官方语义**（https://www.postgresql.org/docs/current/ddl-rowsecurity.html）
- 启用 RLS 后若**无策略匹配，默认拒绝**（default-deny）：看不到也改不了任何行。
- **绕过 RLS 的三类人**：superuser、带 `BYPASSRLS` 属性的角色、表 owner（owner 可用 `ALTER TABLE ... FORCE ROW LEVEL SECURITY` 自愿受限）。
- `TRUNCATE` 和 `REFERENCES` 不受 RLS 约束。
- 策略分 permissive（OR 合并，默认）与 restrictive（AND 合并）；`USING` 管可见性、`WITH CHECK` 管写入。

**2. 标准多租户模式与坑**（Viktar Patotski, https://patotski.com/blog/postgres-row-level-security-multi-tenant/ ；QueryPlane, https://queryplane.com/blog/postgres-row-level-security-in-practice/ 等）

标准三件套：
```sql
ALTER TABLE app_data ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app_data
  USING (tenant_id = current_setting('app.current_tenant')::uuid);
ALTER TABLE app_data FORCE ROW LEVEL SECURITY;
```
每请求：
```sql
BEGIN;
SET LOCAL app.current_tenant = '<uuid>';
-- 业务查询
COMMIT;
```

四个 footgun：
- **连接池泄漏（头号事故源）**：裸 `SET` 的作用域是整个连接生命周期，池化连接被下一个请求复用时**继承上一个租户的上下文**。必须用 `SET LOCAL`（事务作用域，commit/rollback/归还连接即失效）。不要在池的 on_acquire 钩子里用请求值 SET——要在 handler 自己的事务内设。
- **owner/superuser 绕过**：应用必须用**专用非 owner、非 superuser 角色**连库（如 `app_runtime`），迁移用 owner 角色跑；业务表一律 `FORCE ROW LEVEL SECURITY`；给应用角色绝不授 `BYPASSRLS`。
- **索引**：RLS 等价于隐式 `WHERE tenant_id=...`，所有租户表索引 `tenant_id` 放最前列（复合索引下策略求值 ~0.3ms，`SET LOCAL` 开销 <0.1ms；缺索引慢两个数量级）。
- **未设变量的行为**：`current_setting('app.current_tenant')` 未设时直接**报错**；`current_setting('app.current_tenant', true)`（missing_ok=true）返回 NULL → `tenant_id = NULL` 求值为 NULL → 视为不通过 → **零行可见**，即 fail-closed。

**3. fail-closed 推荐写法** **[综合]**（覆盖"从未设置→NULL"与"被设为空串→''"两种情况；空串直接 `::uuid` 会抛 cast 错误，虽也算 fail 但报错不可控）：
```sql
USING (tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
```
两种缺失形态都归一为 NULL → 零行。语义：**上下文缺失 = 什么都看不见，绝不可能是看见全部**。

**4. asyncpg / SQLAlchemy 下的落地** **[综合]**
- SQLAlchemy 2.0 async + asyncpg：在 `async with session.begin():` 后第一条语句执行 `await session.execute(text("SET LOCAL app.current_tenant = :t"), ...)`——注意 `SET LOCAL` 不吃 bind 参数，需用 `set_config('app.current_tenant', :t, true)` 函数形式（第三参 true = local），可参数化、防注入。
- 若前面再加 PgBouncer **transaction pooling**，`SET LOCAL` 语义仍安全（事务内），但 asyncpg 的 prepared statement 缓存与 transaction pooling 冲突，需关 statement cache——首期建议只用 SQLAlchemy 自带池，不引入 PgBouncer。
- 测试要点：写一个"忘设 tenant → 查询返回 0 行"的强制回归测试，锁死 fail-closed 行为。

---

## C. MCP Python SDK 现状

### 事实与引用
- 官方 SDK：https://github.com/modelcontextprotocol/python-sdk ，PyPI 包名 `mcp`，**最新版 2.1.1（2026-08-25 发布），Python ≥3.10**，24.1k stars，支持 2026-07-28 版 MCP 规范及所有更早版本。文档站：https://py.sdk.modelcontextprotocol.io/
- **高层 API**：SDK v2 的高层服务器类为 **`MCPServer`**（v1 时代内置的 `mcp.server.fastmcp.FastMCP` 的演进/更名；注意独立第三方包 `fastmcp`/gofastmcp.com 即 "FastMCP 2.x" 是另一个项目，勿混淆）。装饰器 `@mcp.tool()` / `@mcp.resource()`，类型注解自动生成 JSON Schema。
- **Streamable HTTP server**：一等公民。`mcp.run(transport="streamable-http")`，或以 ASGI 应用形式（`streamable_http_app()`）**挂载进既有 Starlette/FastAPI 应用**；已知坑：挂载时必须把 MCP 的 session manager lifespan 传给外层应用，否则不初始化（官方 issue #1367 有记录）。
- **认证钩子**：内置 OAuth 2.1 resource-server 模型。`TokenVerifier` 协议（`async def verify_token(self, token: str) -> AccessToken | None`）+ `AuthSettings(issuer_url=..., resource_server_url=..., required_scopes=[...])`，二者成对传入 `MCPServer(...)`；handler 内用 `get_access_token()` 取当前请求的 `AccessToken`（含 client_id/scopes/subject/claims）。自动暴露 `/.well-known/oauth-protected-resource/mcp` 发现元数据。**认证只作用于 HTTP transport，stdio 不校验**。
- 来源：https://py.sdk.modelcontextprotocol.io/run/authorization/ ；https://pypi.org/project/mcp/ ；https://github.com/modelcontextprotocol/python-sdk/issues/1367

### 结论：适合做 "Internal MCP server" 基座
官方维护、版本已到 2.x（API 进入稳定期）、streamable HTTP + 认证钩子齐全、可独立进程也可挂进 FastAPI。对内网 MCP server，`TokenVerifier` 可实现为校验内部 service token / OIDC token，不必搭完整 OAuth AS。**[综合]** 建议独立进程部署（与控制平面 API 分开），共享 domain/service 层代码，避免 lifespan 挂载坑与故障耦合。

---

## D. FastAPI + Pydantic v2 + SQLAlchemy 2.0 + Alembic；Decimal；Python/uv

### 版本现状（均为 PyPI 当前最新，2026-08-28 查询）

| 库 | 版本 | 发布日期 | Python 要求 | 备注 |
|---|---|---|---|---|
| FastAPI | 0.141.1 | 2026-07-29 | ≥3.10 | Pydantic v2 完整兼容 |
| Pydantic | 2.13.4 | 2026-05-06 | ≥3.9 | pydantic-core 已并入主仓库 |
| SQLAlchemy | 2.0.52 | 2026-08-11 | 官方轮子覆盖 3.8–3.15 | asyncio 为 extra；2.1.0 已有 beta |
| Alembic | 1.19.1 | 2026-08-08 | ≥3.10 | SQLAlchemy 作者维护 |
| asyncpg | 0.31.0 | 2025-11-24 | ≥3.9 | 支持 PG 9.5–18；自测称比 psycopg3 平均快 5x |
| mcp | 2.1.1 | 2026-08-25 | ≥3.10 | 见 C |

这套组合（FastAPI + Pydantic v2 + SQLAlchemy 2.0 async + Alembic + asyncpg）是当前 Python 后端事实标准栈，四者互相声明兼容，无版本冲突。

### Decimal 处理（Pydantic v2 官方文档）
- 校验：接受 `Decimal` 实例及"any value accepted by the `Decimal` constructor"。
- **JSON 序列化默认**：官方原文 "In JSON mode, they are serialized as strings."（Python mode 保持 Decimal 原样）。→ 金额走 JSON 不丢精度，这是广告预算/出价场景想要的默认。
- 约束：`Field(max_digits=..., decimal_places=..., ge/gt/le/lt/multiple_of, allow_inf_nan)`。
- 自定义：`Annotated[Decimal, PlainSerializer(float, when_used='json')]` 可改序列化为 number（**不建议**用于金额）；条件校验用 `field_validator` / `model_validator`。
- 来源：https://pydantic.dev/docs/validation/latest/api/pydantic/standard_library_types/
- **[综合]** 端到端精度链：Pydantic `Decimal` ↔ SQLAlchemy `Numeric(asdecimal=True)` ↔ PG `NUMERIC`，JSON 边界为字符串——全链路无 float。

### Python 3.12/3.13 与 uv（本机已验证 ✅）
本机只读验证结果（2026-08-28）：
- `uv 0.11.3`（2026-04-01 build，aarch64-apple-darwin）；`uv init` / `uv run` 子命令确认存在（`uv add` 属同一 CLI 家族）。
- `uv python list`：**cpython 3.12.13 已安装**（uv 管理，`~/.local/bin/python3.12`）；**3.13.12 可一键下载**；3.14.3/3.15.0a7 亦可下载。系统 `python3` 是 3.11.15（browser-use venv）、`/usr/bin/python3` 是 3.9.6——**不要依赖系统 python，一律 `uv run`**。
- PostgreSQL：`psql`/`docker` 均不在 PATH；但 **Postgres.app 在 ~/Applications，内含 PostgreSQL 18.4**（`~/Applications/Postgres.app/Contents/Versions/latest/bin/psql`）。本机开发可直接用 Postgres.app，无需 Docker。
- git 2.50.1。

**[综合]** 目标版本建议 **Python 3.12**（本机已装、生态轮子最全），`requires-python = ">=3.12"`，CI 加 3.13 矩阵。

---

## E. Temporal：maximumAttempts=1 与"首期不用"的论据

### 事实与引用
**1. 官方对副作用 activity 的支持方式**（https://docs.temporal.io/encyclopedia/retry-policies）
- Activity 默认自动重试：Initial Interval = 1s, Backoff = 2.0, Maximum Interval = 100×, **Maximum Attempts = ∞**。
- 关闭重试的官方方式就是 **maximumAttempts=1**，原文："Setting the value to 1 means a single execution attempt and no retries."
- 官方同时强调 activity 应尽量幂等（"Activities will need to re-execute upon failure"）；永久性失败应抛 non-retryable error（"it is better to surface them than to retry them"）。
- 即：**Temporal 对 at-most-once 副作用 activity 的答案 = maximumAttempts:1 + 失败后由 workflow 代码显式决定补偿/对账**，与 A 节的 at-most-once submit 语义完全对应。

**2. "首期用 PG job 表、不用 Temporal"的取舍论据**
- Nango 生产案例（https://nango.dev/blog/migrating-from-temporal-to-a-postgres-based-task-orchestrator/）：因用不上 workflow resumption，Temporal 退化为 "a pretty expensive and complex queuing and scheduling system"；且是企业客户安全合规评审的障碍；迁到 Postgres 表 + `FOR UPDATE SKIP LOCKED`，每月数百万任务、10x 余量。
- 规模阈值参考："Postgres is the only Queue you need (until 50k jobs/sec)"（https://medium.com/@harsh.vaghela.work/postgres-is-the-only-queue-you-need-until-50k-jobs-sec-5931611b551c）：MVP/中小规模先 PG，有证据再上专用系统。
- 反方（何时该上 workflow 引擎）：任务是多阶段持久流程、要在失败/等 webhook/跨部署重试中"记住走到哪"时，队列不够，需要 workflow（https://mfyz.com/durable-queue-workers-with-just-postgres/）；高并发 worker 疯狂 SKIP LOCKED 轮询会让 CPU 与 vacuum 出问题（https://techcommunity.microsoft.com/blog/adforpostgresql/potential-consequences-of-using-postgres-as-a-job-queue/4514332）。

**[综合] 对本项目的结论**：广告控制平面首期提交量远低于 PG 上限、单次提交是"一段式"副作用而非长活多阶段流程、且核心诉求（事务性、审计、at-most-once）恰是 PG 强项 → 首期 PG job 表 + outbox 正确；把 worker 的"领取→提交→记录"写成独立纯函数，边界即未来 Temporal activity 边界（迁移时套 maximumAttempts=1 即可），保留迁移通道。

---

## F. Ed25519 + RFC 8785 (JCS) 的 Python 库支持

### 事实与引用
**1. pyca/cryptography（首选）** — https://cryptography.io/en/latest/hazmat/primitives/asymmetric/ed25519/
- 最新版 **50.0.1**（PyPI，Python ≥3.9，排除 3.9.0/3.9.1）。
- `Ed25519PrivateKey.generate()` / `.sign(data)` → 64 字节签名；`Ed25519PublicKey.verify(signature, data)` 失败抛 `InvalidSignature`。
- 序列化：raw 32 字节（`private_bytes_raw()`/`public_bytes_raw()`，v40+）、PEM/DER、PKCS8、OpenSSH。Ed25519 自 2.6 版就有，极成熟。
- 官方推荐语："If you do not have legacy interoperability concerns then you should strongly consider using this signature algorithm."

**2. PyNaCl（备选）** — https://pypi.org/project/PyNaCl/
- 1.6.2（2026-01-01），Python ≥3.8，libsodium 绑定，含签名。维护活跃但 API 面窄、发版慢于 cryptography。

**3. rfc8785（JCS 规范化）** — https://pypi.org/project/rfc8785/
- **存在且可用**：Trail of Bits 维护，最新 **0.1.4（2024-09-27）**，Python ≥3.8，**纯 Python、零依赖**。
- API：`rfc8785.dumps(obj) -> bytes`（规范化 UTF-8）、`dump()`；失败抛 `CanonicalizationError`。
- 成熟度：分类器为 Beta（Development Status 4），自述"behaviorally comparable to Andrew Rundgren's reference implementation"（RFC 8785 作者的参考实现）。注意：dict key 必须已是 str（不做隐式转换）；无 pretty-print。
- 评估 **[综合]**：出自专业安全审计公司、零依赖、对齐参考实现——用于"签名前规范化 JSON"这种窄场景足够可信；版本久未更新反映的是 RFC 8785 本身已冻结、库功能面极小，而非失修。风险缓解：脚手架里放一组 RFC 8785 附录测试向量做回归。

**[综合] 签名方案**：`rfc8785.dumps(payload)` → `Ed25519PrivateKey.sign(canonical_bytes)`（cryptography 库），公钥/签名 base64url 存审计记录。两库均纯依赖友好，uv 安装无编译负担（cryptography 有预编译轮子覆盖 macOS arm64）。

---

## G. Policy engine（OPA/Cedar）vs 类型化代码内 policy

### 事实与引用
- **OPA**：Rego 通用、生态最大，但学习曲线陡、缺应用级授权原语、需要 sidecar/独立部署与数据同步机制，高吞吐场景有性能与扩展顾虑（https://www.permit.io/blog/policy-engine-showdown-opa-vs-openfga-vs-cedar ；https://www.osohq.com/learn/opa-vs-cedar-vs-zanzibar）。
- **Cedar**：为应用级授权设计、可读性与形式化验证（verification-guided development）是卖点；但社区小、工具链少，核心是 Rust 实现，Python 绑定非官方（osohq/permit.io 对比文）。
- 小团队结论（osohq/permit.io 综述）：两者都引入**新语言 + 新运行时 + policy 数据同步**三重成本；规模小时这三样都摊不薄。
- 来源：https://www.permit.io/blog/policy-engine-showdown-opa-vs-openfga-vs-cedar ；https://www.osohq.com/learn/opa-vs-cedar-vs-zanzibar ；https://goteleport.com/blog/benchmarking-policy-languages/

**[综合] 首期结论**：用**类型化代码内 policy**——纯函数 `evaluate(ctx: PolicyContext) -> PolicyDecision`（Pydantic 模型进出，decision 含 allow/deny + 结构化 reasons + policy_version），policy 与业务代码同库同测试同 review，git 即版本化。关键投资在**接口形状**：把 PolicyContext/PolicyDecision 定义成可序列化、可落审计表——这正是未来若迁 OPA/Cedar 时的 input/output 文档。触发外置的信号：多服务要共享同一套 policy、非工程角色要改 policy、或合规要求 policy 生命周期与代码解耦。

---

## 脚手架选型建议表

| 层 | 选型 | 版本 | 理由 | 本机可用性 |
|---|---|---|---|---|
| 语言 | Python (CPython) | **3.12**（3.13 进 CI 矩阵） | 生态轮子最全；本机已装 | ✅ 已验证（uv 管理 3.12.13） |
| 项目管理 | uv | 0.11.3 | init/add/run/python 管理一体；锁文件 | ✅ 已验证（`uv --version`、`uv init/run --help`） |
| Web/API | FastAPI | 0.141.1 | Pydantic v2 原生、OpenAPI 自动化 | ⬜ 未装（uv add 即得，纯轮子） |
| 校验/序列化 | Pydantic | 2.13.4 | Decimal JSON 序列化为字符串（金额安全）；条件校验齐 | ⬜ 未装 |
| ORM | SQLAlchemy (async) | 2.0.52 | 2.0 风格 + asyncio；行业标准 | ⬜ 未装 |
| 迁移 | Alembic | 1.19.1 | SQLAlchemy 官配 | ⬜ 未装 |
| PG 驱动 | asyncpg | 0.31.0 | 性能最好；支持 PG 18 | ⬜ 未装 |
| 数据库 | PostgreSQL | 18.4（本机）/ 生产 ≥16 | RLS、SKIP LOCKED、NUMERIC、事务性 outbox 全在一库 | ✅ 已验证（Postgres.app，psql 18.4；无 Docker 也能跑） |
| 可靠提交 | Transactional Outbox + PG job 表 | — | microservices.io 标准模式；at-most-once 见 A.4 | —（模式，无依赖） |
| 多租户 | PG RLS：SET LOCAL + set_config + FORCE RLS + 非 owner 角色 + NULLIF fail-closed | — | 官方语义 + 生产实践收敛一致 | —（模式） |
| 编排 | 首期不用 Temporal；PG job 表，activity 形状预留 | — | 见 E；量级远低于 PG 上限 | — |
| MCP 基座 | 官方 `mcp` SDK（MCPServer） | 2.1.1 | streamable HTTP + TokenVerifier 认证钩子 + ASGI 挂载 | ⬜ 未装 |
| 签名 | cryptography (Ed25519) | 50.0.1 | pyca 标准库级地位；官方推荐算法 | ⬜ 未装 |
| JSON 规范化 | rfc8785 | 0.1.4 | Trail of Bits；零依赖；对齐参考实现 | ⬜ 未装 |
| Policy | 类型化代码内 policy（不引 OPA/Cedar） | — | 首期规模下三重成本摊不薄；接口留外置通道 | — |
| 测试 | pytest（`uv run pytest`） | 最新 | uv 原生驱动 | ⬜ pytest 未装，uv run 链路已验证 |

"⬜ 未装"均为纯 pip/uv 轮子安装，macOS arm64 全有预编译 wheel，无系统依赖风险。

---

## 风险与备选

| # | 风险 | 等级 | 缓解 / 备选 |
|---|---|---|---|
| 1 | **RLS 上下文泄漏**：谁写了一句裸 `SET`、或某条查询跑在事务外，隔离即破 | 高 | 封装唯一的 `tenant_session()` 入口（内部 begin + `set_config(...,true)`），禁止直接拿 session；加"未设 tenant → 0 行"回归测试；代码评审清单项。备选：极端敏感租户走 schema-per-tenant |
| 2 | **at-most-once 的代价是"卡单"**：崩溃后停在 `submitting` 的行需要对账才能推进 | 高 | 必须同期交付 reconciliation worker（拉平台侧状态回填）+ `submitting` 超时告警；广告平台支持 idempotency key 的接口优先用 key，降级风险为零 |
| 3 | **SQLAlchemy 2.1 临近**（2.0.52 现役、2.1.0b 已出）：未来升级有 API 变动 | 低 | 2.0 风格代码就是 2.1 兼容路径；锁 `>=2.0.52,<2.1` 待 2.1 stable 后再评估 |
| 4 | **rfc8785 库更新慢（2024-09 后无版本）** | 低 | RFC 已冻结、库零依赖面极小；脚手架内置 RFC 8785 测试向量回归。备选：自实现 JCS（规范仅数页）或 PyNaCl 侧生态 |
| 5 | **MCP SDK v2 仍在快速迭代**（2.x 大版本刚落地，挂载 FastAPI 有已知 lifespan 坑） | 中 | Internal MCP server 独立进程部署（不与 API 同进程挂载），锁 `mcp>=2.1,<3`；SDK 只做协议壳，业务逻辑全在可独立测试的 service 层 |
| 6 | **PG job 表在高并发下的 vacuum/CPU 问题**（Microsoft 博文） | 低（首期） | 首期量级差几个数量级；监控 dead tuple 与轮询 QPS；预留 Temporal 迁移边界（E 节），或备选 procrastinate/pgqueuer 这类现成 PG 队列库 |
| 7 | **本机无 Docker**：CI/他人环境若依赖容器化 PG 会与本机开发路径分叉 | 中 | 本机用 Postgres.app 18.4（已验证）；CI 用官方 postgres 镜像；Alembic 迁移保证两边 schema 一致；README 写清两条启动路径 |
| 8 | **Decimal 被谁在边界上转成 float**（前端/报表/第三方 SDK） | 中 | JSON 边界统一字符串（Pydantic 默认已如此）；lint/评审禁止 `PlainSerializer(float)` 用于金额字段；DB 列一律 NUMERIC |
| 9 | 代码内 policy 日后要求外置 | 低 | G 节接口形状即迁移契约；PolicyContext/Decision 已可序列化，可平移为 OPA input / Cedar entities |

---

## 附：本机验证命令记录（全部只读）
- `uv --version` → `uv 0.11.3 (45da18ac3 2026-04-01 aarch64-apple-darwin)`
- `uv python list` → cpython **3.12.13 已安装**；3.13.12 / 3.14.3 可下载
- `uv init --help` / `uv run --help` → 子命令存在
- `python3 --version` → 3.11.15（browser-use venv，勿用）；`/usr/bin/python3` → 3.9.6
- `psql` / `docker` → 不在 PATH；`~/Applications/Postgres.app/.../latest/bin/psql --version` → **psql (PostgreSQL) 18.4 (Postgres.app)**
- `git --version` → 2.50.1
