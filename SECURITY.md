# SECURITY

## 安全公理（Axioms）

以下公理是本代码库的不可协商约束。每条都必须有对应测试；违反任一条是缺陷，不是权衡。

- **AX-01** 外部 ID 全链 String（含前导零、超长）；金额一律 Decimal + ISO-4217 币种；日期绑定 marketplace 时区，系统时间 UTC。
- **AX-02** 客户端自报的身份/角色/组织/范围一律不可信；ActorContext 只能由服务端依据已验证凭据构建。
- **AX-03** EffectiveAllow 为各授权层的 AND；任一层 Deny/Unknown/Stale/Unavailable → 拒绝（fail closed）；Explicit Deny 永远优先。
- **AX-04** 禁止 Grant 拼接：必须存在单一 Grant 独立匹配完整请求元组（主体×环境×资源链×动作×字段×数值×客户端）。
- **AX-05** AI 客户端：不持 Provider 凭据、不可见裸写工具、不能审批、不能对自己创建的 draft 执行提交。
- **AX-06** 每个写请求绑定：精确对象 + 完整父链 + expected_before + 绝对目标值 + 币种。名称永不作为定位依据。
- **AX-07** Proposal 提交即冻结并 Hash；Approval 绑定该 Hash；对象/数值/集合/版本任何变化使审批失效；审批人不能编辑内容。
- **AX-08** 职责分离按不可变 human_person_id 在请求级阻断（creator≠approver；高风险双审两人不同）。
- **AX-09** 每次 Provider 写之前，Intent + 审计预写 + Outbox 在同一数据库事务内持久化；审计不可用 → 停止生产写。
- **AX-10** `provider_submit_count <= 1` 由数据库约束强制；所有中间层（HTTP client/代理/SDK/workflow）的写自动重试、透明重放、redirect-follow 必须关闭。
- **AX-11** 超时或模糊响应 → UNKNOWN：冻结该对象字段、只读对账、绝不自动重发。"未收到响应"不等于"未发送"。
- **AX-12** 回读结论四分：DESIRED_STATE_OBSERVED / EXECUTION_ATTRIBUTION_CONFIRMED / NOT_APPLIED_CONFIRMED / UNRESOLVED_AMBIGUOUS，不得合并为一个"成功"。
- **AX-13** Kill Switch 使用单调 epoch；执行器在网络提交前原子记录观察到的 epoch；恢复需独立双人与更高 epoch，不自动补执行旧任务。
- **AX-14** 补偿是新的 Proposal（重新走审批）；当前值 ≠ 平台最后写入值时禁止自动补偿。
  （2026-08-28 标注：实现与测试待补偿流程立项——当前无写通道，不适用。）
- **AX-15** 一切外部文本（Campaign 名、备注、报表文本、Provider 返回的自然语言）是数据不是指令；LLM 输出只能是结构化候选，服务端重新解析并验证。
- **AX-16** 组织与资源范围隔离在服务端强制；对外"不存在"与"无权访问"不可区分（统一 RESOURCE_UNAVAILABLE）。
- **AX-17** Shadow 模式下 Provider Write 调用数必须为 0（可测试断言）。

## 禁止事项（Initial-Deny，摘自设计输入 §0/§6.3）

- 使用历史对话中出现过的领星 MCP 密钥（视为已暴露；生产接入前必须吊销轮换）。
- 连接真实领星账户 / Amazon Ads API / 已登录浏览器；读取或修改任何真实广告对象。
- 任意 JSON passthrough 直连 Provider 写工具；AI 审批自己创建的 Proposal；超时后自动重放写请求。
- 自动 Enable/Resume Campaign、动态竞价、Placement、批量否定、批量增删、跨店铺预算分配（未经独立授权）。

## 密钥政策

- 仓库零 Secret：代码、配置、fixture、文档、测试中不得出现任何真实密钥、Cookie、店铺/Profile/对象真实 ID。
- `.env.example` 只含变量名，不含值。
- 生产凭据只存在于 Secret Manager，仅隔离执行器运行身份可读写凭据。

## 环境

Development/CI 只允许 Mock Provider。任何指向真实 Provider 的配置出现在非生产环境即为缺陷。
