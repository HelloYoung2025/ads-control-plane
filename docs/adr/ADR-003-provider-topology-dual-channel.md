# ADR-003：Provider 拓扑——双通道三源（修订原设计的单通道假设）

日期：2026-08-28 ｜ 状态：ACCEPTED（写通道最终选型仍需 Owner 决策，见 DEC-010）

## 背景

设计输入（handoff）把领星 MCP 同时当作读与写 Provider。2026-08-28 调研证实：官方个人版 MCP 广告工具全部只读（QPS=1/工具，X-Mcp-Key 绑定个人并继承其 ERP 权限）；有文档背书的广告写路径是**领星开放平台 REST API**（仅 SP，企业 AppId/AppSecret，逐条 apiResult，无幂等键，写接口令牌桶容量 1）；开放平台另有"操作日志（新）"API（user_name/前后值/ERP 与亚马逊来源二分）。handoff 会话曾实测的 MCP 写工具与当前官方文档冲突（DEC-009 核实中）。

## 决定

```
读（即席/AI 探索） ：领星 MCP，专用低权限子账号 X-Mcp-Key
读（批量/权威回读）：开放平台 REST（SP/SB/SD 报表 + 小时级 + 基础数据）
对账/归因         ：操作日志 API（查询窗口 ≤1 个月，需自建轮询采集）
```

- 端口不动、实现换绑：Read / AuthoritativeRead / Write 三协议分离保持原样（`providers/base.py`）。
- MCP 写工具（若存在）在 Capability Registry 全部标 DISCOVERED/Disabled——不是断言其不存在，而是不把产品合同押在无文档背书、无变更公告义务的表面上。
- SB/SD 写：两通道均无文档化能力，明文移出范围。
- 新增隔离域 **Provider Credential Proxy**：唯一持有 AppId/AppSecret，内置 Token Custodian 单飞续约（refresh_token 一次性，多实例并发续约会互相打翻），按调用方身份做接口路径白名单；Executor 独占 IP 白名单出口。
- 三级拓扑（平台→领星缓存→Amazon）意味着写前重读读到的是二手缓存：TOCTOU 窗口 = 领星同步滞后（分钟到一天）。每字段回读源合同必须记录通道与实测收敛延迟分布（Gate 2 合同测试），残余风险书面接受时写明实测滞后。

## 增补（2026-08-28 晚，业务 Owner 证词）

密钥持有者当面证词：本人电脑接入 LX-MCP 实测**可调整广告预算与 CPC**【OBSERVED——用户口头，未复现】。

## 后果

- 操作日志采集管道提前为 Gate 1 交付物（外部变更基线报表，晚接一天基线薄一天）。
