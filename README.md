# ads-control-plane

公司内部广告管理控制平面。把领星 MCP（及未来其他 Provider）包在公司身份、权限、提案、审批与审计之后，供 Web 运营台与 Codex 等 AI 客户端共用一套业务核心。

**当前实际能做的**：人在 Web 台签一份目标授权书 → AI 客户端按这份授权生成否定词候选 →
人对着冻结指纹批准 → 导出 CSV，**由人拿去领星后台手工执行**。系统自己不改任何广告，
也不会自己到点运行：每一次运行都由人在 AI 客户端里发起（仓库里没有调度器）。

设计输入：`LINGXING_MCP_AD_MANAGEMENT_PLATFORM_HANDOFF.md`（v1.0）。该文档是**待验证的设计输入，不是生产授权**。本仓库当前只实现其中经辩证评审后保留的 MVP 范围：只读数据面 + 提案/审批/执行安全内核（全部 Mock Provider）。

## 架构（一句话）

已落地的链路（写侧尚未接通，见「生产授权」）：

```
客户端(MCP/REST/UI) → 身份网关 → 只读查询 / 结构化提案 → 权限与风险门 → 审批 → 导出 CSV(人工执行)
```

设计中的完整链路，`executor/` 已按此形状建包但未接入任何真实写凭据：

```
… → 审批 → 签名 Intent → 隔离写执行器(一次提交) → Provider Adapter → 回读对账 → 审计
```

## 模块地图

```
src/ads_control_plane/          # core 域（无写凭据、无执行协议）
  identity/        ActorContext、principal（自报身份一律不可信）
  authorization/   EffectiveAllow 交集判定、SoD 冲突矩阵、fail-closed
  canonical/       String 外部 ID、Decimal+币种、完整父链
  capabilities/    Provider 工具登记：新工具默认禁用、Schema 漂移即冻结
  proposals/       绝对目标值 + expected_before + 冻结 Hash
  approvals/       审批绑定 Hash；内容变化即失效
  safety/          执行状态机、单提交 CAS 存储、kill epoch
  audit/           追加式审计账本（预写失败即停写）
  providers/       Read/Write Adapter 协议 + mock/；lingxing/ 只读搜索词源(见其 README)
  strategies/      目标授权书、运行闸（时段/间隔/配额）、否定词候选与导出 CSV
  mirror/          领星只读镜像同步（分页续拉、覆盖率如实上报），只活在进程内存
  tasks/           勾选 → 「现值 → 新值」预览（只产预览，不产生任何执行）
  adapters/        lx_read：结构性防写白名单，只放行 9 个只读工具
  api/mcp_tools/   Internal MCP 只读工具面（mcp SDK ≥2.1）
  api/ui_static/   Web 运营台（原生 HTML/JS/CSS，无框架无构建）
executor/                        # uv workspace 独立包：唯一允许执行协议与写适配器的部署单元
  src/ads_write_executor/        # 执行协议（门序+一次提交）、桶感知节流
```

依赖方向单向 executor → core，由测试断言（[test_package_isolation.py](tests/unit/test_package_isolation.py)）。

## 文档索引

- [docs/runbook-local-demo.md](docs/runbook-local-demo.md) — 十分钟走查：起服务、签授权书、批准、导出 CSV
- [docs/codex-connect.md](docs/codex-connect.md) — 把 Codex 等 AI 客户端接到本服务的 /mcp
- [SECURITY.md](SECURITY.md) — 安全公理 AX-01..17 与禁止事项
- [docs/roadmap.md](docs/roadmap.md) — 垂直三层与 M0-M5 里程碑
- [docs/adr/](docs/adr/) — 6 份架构决策记录
- [docs/registers/](docs/registers/) — Decision / Assumption Register（Owner 决策队列）
- [docs/reviews/2026-08-28-dialectic-review.md](docs/reviews/2026-08-28-dialectic-review.md) — 对设计输入的多智能体辩证评审（CONDITIONAL-GO）
- [docs/research/](docs/research/) — 5 路外部调研存档（领星/Amazon Ads/MCP 规范/工程栈/本地文档）
- [docs/evidence/](docs/evidence/) — Gate 阶段报告

## 开发

```bash
uv sync
uv run pytest
uv run ruff check .
```

起一个纯 Mock 的本地实例（Web 台在 <http://127.0.0.1:8791/ui/>，MCP 面在 `/mcp`）：

```bash
uv run python scripts/serve_local_demo.py --port 8791
```

默认启动不需要任何生产凭据；集成测试只连接 Mock Provider。数据是不是真的由环境变量
决定，不由「本地演示」这四个字决定——启动横幅按实际通道状态逐条播报。

## 生产授权

默认：**无**。任何真实 Provider 连接、真实凭据、真实广告写入都需要业务/技术/安全 Owner 按 Gate 另行书面授权（见 `docs/registers/decision-register.md`）。本仓库的测试通过不构成生产权限。
