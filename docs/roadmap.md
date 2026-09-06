# 路线图：垂直三层 + Walking Skeleton M0-M5

来源：辩证评审合成报告（2026-08-28，CONDITIONAL-GO，最高可进入 Gate 1）。
本文件是计划，不是进度快照；完成证据以测试与 Gate 证据包为准。

## 垂直三层

- **A 层——安全公理（SECURITY.md AX-01..17）**：自第一行代码起由类型系统与 CI 测试强制，与 Provider 通道选型正交，终身有效。
- **B 层——Gate 0-1 + L1.5 可执行规范**：治理文件 + 只读数据面 + 操作日志采集 + L1.5 核验闭环。
- **C 层——写机件设计参考**（handoff §13-16 / WP-09~15）：冻结参考，待写通道选型（DEC-010）与 §23.1 合同答案后重推导。例外：Provider 无关的安全内核（状态机/单提交/Kill/审计预写）随 M4 以 Mock 建成。

## 自动化阶梯（含评审新增的 L1.5）

```
L0 只读 → L1 提案 → L1.5 人执行+平台核验（Bulk Sheet + 操作日志闭环，零写凭据）
→ L2 人批+平台执行 → L3 有界自动 → L4 高级
```

L1.5 是合法长期终态（ADR-006）；进入 L2 需满足写通道立项正当性条件。

## M0-M5 里程碑（每步 = 可演示物 + 测试证据）

| 里程碑 | 内容 | 可演示物 / 证据 | 本仓库状态（2026-08-28） |
|---|---|---|---|
| M0 基座 | uv workspace 多包（core / executor）+ CI（真实 PG service）+ 包隔离断言 | CI 绿灯；隔离断言测试 | 已落地（PG service 待接入 CI） |
| M1 身份 + 只读 MCP 骨架 | Internal MCP（mcp>=2.1,<3）+ TokenVerifier；whoami / list_authorized_scopes | 工具面测试；越权/过期拒绝 | 已落地（进程内测试级）；本地 streamable HTTP 组合根与演示 UI 已落地（2026-08-28） |
| M2 领星只读纵切 | **PG 历史数据库先行**（append-only 快照，DEC-117）；子账号 X-Mcp-Key → Read Adapter → canonical → query_ad_metrics；**操作日志采集管道 + 外部变更基线报表**；Gate 1 四周采集时钟起跑 | 实连查询；录制 fixture 合同测试、限流分类、QPS=1 吞吐实测 | 未开始——需要 DEC-013 凭据方案（本仓库禁止使用任何真实凭据） |
| M3 提案审批 + L1.5 纵切 | draft→submit→最小审批页→freeze hash；Bulk Sheet 导出→ERP 人工应用→操作日志自动核验闭环 | SoD/hash 失效测试；L1.5 闭环率报表 | 域内核已落地（提案/审批/SoD 全测试）；NEG_EXACT 候选纵切（证据门→冻结审批→导出行→日志核验）已落地（DEC-112）；最小审批页已落地（本地演示级，2026-08-28） |
| M4 安全内核 + Mock 执行 | Intent+Outbox 同事务（PG）、SKIP LOCKED、单提交约束、kill epoch、桶感知节流 | 故障注入：双 Worker 仅一次提交；kill -9 卡单进 UNKNOWN 不重发 | 进程内版本已落地（80 测试含故障注入）；PG 持久化版未开始 |
| M5 真实写 Canary | 仅在 Gate 3 授权 + ADR-006 正当性条件满足后：REST 企业凭据 + Credential Proxy + putSp* 单对象 + 操作日志对账 | 一次 12.00→12.01 全链路证据包 | 被阻塞（设计输入 §0 授权边界 + DEC-010/013） |

## 当前 Gate 判定

CONDITIONAL-GO：Gate 0 立即开工（三项 P0 增补后方可宣布退出）；Gate 1 读路径可并行启动；Gate 2+ 与全部真实写机件冻结至 DEC-010 与 §23.1 合同答案落地。
