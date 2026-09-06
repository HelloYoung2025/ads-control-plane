# ADR-002：隔离写执行器 + 平台级"至多一次提交"

日期：2026-08-28 ｜ 状态：ACCEPTED

## 决定

生产写凭据只存在于隔离执行器；每个 Execution 的 Provider 网络提交次数由数据层 CAS 强制 ≤ 1（`submission_phase` 条件转换 + `provider_submit_count <= 1` 约束）；超时/模糊响应进入 UNKNOWN，只读对账，永不自动重发。

## 依据

- Amazon Ads API 已证实（2026-08-28 公开文档核查）：写操作是 last-write-wins 的 partial update，无幂等键、无 ETag/版本号、无条件写——远端不能去重，平台自身就是最后一道防重复防线。
- 领星开放平台写接口同样无公开幂等键说明（OPEN，按无处理）。
- exactly-once 网络投递不存在；"未收到响应"≠"未发送"。

## 后果

- 实现于 `safety/execution.py`（CAS）与 `safety/protocol.py`（门序：Kill → 审计预写 → 写前重读 → CAS → 一次提交 → 回读）。
- 所有中间层（HTTP client/代理/SDK）写自动重试、透明重放、redirect-follow 必须关闭——接入真实 Provider 时列为合同测试项。
- 明确未应用（如校验拒绝）的 Intent 也随 CAS 消费而终结：重试 = 新 Proposal 新审批，不是重放。
