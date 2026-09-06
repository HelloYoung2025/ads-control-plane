# ADR-004：禁止 Provider Passthrough 工具

日期：2026-08-28 ｜ 状态：ACCEPTED

## 决定

不存在 `call_lingxing_anything(tool_name, arbitrary_json)` 形态的工具/端点。每个可用动作都在 Action Catalog 中显式建模（schema、风险级、字段白名单、回读定义）；Provider 新工具默认 DISCOVERED=禁用（Capability Registry）。

## 依据

- 实证：handoff 会话（≤2026-08-27）观察到的领星 MCP 有 ~15 个广告写工具，当前官方文档广告工具全部只读——Provider 工具面三个月内真实漂移过。Passthrough 会让这种漂移静默扩权或静默破坏。
- 2025-2026 公开研究（tool poisoning、rug-pull CVE-2025-54136、MCPTox）的共识缓解就是 tool definition hash pinning + allowlist——即本仓库 `capabilities/registry.py`。

## 后果

- 新增 Provider 能力的路径只有一条：登记 → 合同测试 → 逐级晋级 → ACTIVE。
- 运行时写路径校验 schema hash 与登记一致（`assert_write_usable`）。
