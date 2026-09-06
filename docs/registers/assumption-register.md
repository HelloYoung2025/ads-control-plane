# Assumption Register

每条假设：内容 + 证据等级 + 若为假的后果 + 验证方式。证据等级沿用设计输入：VERIFIED / OBSERVED-PROVISIONAL / PROPOSED / OPEN。

| ID | 假设 | 证据等级（2026-08-28） | 若为假 | 非生产验证方式 |
|---|---|---|---|---|
| ASM-01 | 领星 MCP 可作为 Provider **Read** Adapter 候选；**Write 通道候选是领星开放平台 REST API**（官方 MCP 当前广告工具全部只读，与 handoff 快照冲突，见 ASM-10） | 读侧 VERIFIED（窄）；写通道改判 OBSERVED-PROVISIONAL（公开 API 目录：SP 写接口存在，SB/SD 无） | 换用 Amazon Ads API 直连 | 授权测试账户合同测试 |
| ASM-02 | Provider 无服务器端幂等键、无条件写(CAS)：Amazon Ads API 侧已证实（partial update、last-write-wins、无 ETag/If-Match/幂等键，官方文档系统性缺席）；领星开放平台写接口无幂等键说明 | Amazon 侧 VERIFIED（公开文档核查）；领星侧 OPEN | 单提交协议可放宽；补偿可自动化 | 领星测试账户实测 |
| ASM-03 | 归因确认路径：Amazon change history（beta）与 Marketing Stream 均**不含操作者身份** → Amazon 侧归因不可行；领星开放平台"操作日志（新）"API 含 user_name/change_type/before/after/操作来源 → 经领星写入的归因**有现实路径**（依赖平台使用专属服务身份可区分，待实测） | Amazon 侧 VERIFIED；领星侧 OBSERVED-PROVISIONAL | EXECUTION_ATTRIBUTION_CONFIRMED 成为死状态，L2 最高证据即 DESIRED_STATE_OBSERVED | 领星测试账户实测操作日志的身份/延迟 |
| ASM-04 | 凭据现实：开放平台 AppId/AppSecret 为超管签发的**企业全量单密钥**（不可按店铺/模块细分，强制 IP 白名单）；MCP X-Mcp-Key 绑定个人账号并完全继承其权限 | VERIFIED（公开官方文档） | 授权设计可升级为模式 1/2 | 领星后台核实是否有企业版细分能力 |
| ASM-05 | 即时回读与 ERP UI 一致不代表 Amazon 权威最终一致；Amazon 官方对写后读一致性无任何承诺 | VERIFIED（Amazon 文档缺席即证据） | 回读分级可简化 | 测试账户观察传播延迟分布 |
| ASM-06 | 限流现实：MCP 每工具 QPS=1；开放平台写接口令牌桶容量 1（按 appId+接口）；access_token 7199s + 一次性 refresh_token(2h) | VERIFIED（公开文档） | — | 实测验证令牌桶补充速率 |
| ASM-07 | 员工将继续拥有领星后台直接操作权 → 平台外修改常态存在 | PROPOSED（组织现实） | Controller 冲突检测可简化 | 与业务 Owner 确认 |
| ASM-08 | Paused 状态不是写锁（暂停 Campaign 的预算/子级 bid 仍可被修改） | VERIFIED（当时对象） | 无（更安全） | — |
| ASM-09 | MVP 团队规模下双人审批不可运转 | PROPOSED | 恢复双审设计 | 业务 Owner 确认审批人数 |
| ASM-10 | **快照冲突**：handoff 会话（≤2026-08-27）观察到 59 工具/≈15 广告写工具并实测写成功；当前官方 MCP 文档（2026-08-28 查）广告工具全部只读。两种解释：当时接入的不是当前官方个人版 MCP，或 Provider 已漂移 | 冲突本身 VERIFIED；解释 OPEN | — | 由密钥持有者在授权环境核对当时连接的 URL 与当前工具目录 |
| ASM-11 | Amazon 官方口径：bid 是"maximum bid"，up-and-down 动态竞价下实际 CPC 可达 configured bid 的 2 倍，placement 调整另可 +900% → "Bid ≠ Actual CPC"有硬证据 | VERIFIED（官方文档） | — | — |
