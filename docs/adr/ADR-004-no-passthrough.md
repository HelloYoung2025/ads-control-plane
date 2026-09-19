# ADR-004：禁止 Provider Passthrough 工具

日期：2026-08-28 ｜ 状态：ACCEPTED

## 决定

不存在 `call_lingxing_anything(tool_name, arbitrary_json)` 形态的工具/端点。

## 依据

- 实证：handoff 会话（≤2026-08-27）观察到的领星 MCP 有 ~15 个广告写工具，当前官方文档广告工具全部只读——Provider 工具面三个月内真实漂移过。Passthrough 会让这种漂移静默扩权或静默破坏。
