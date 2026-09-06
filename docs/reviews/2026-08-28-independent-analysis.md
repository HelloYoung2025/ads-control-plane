# 主智能体独立辩证分析（初稿，供多视角评审引用）

来源：完整通读 LINGXING_MCP_AD_MANAGEMENT_PLATFORM_HANDOFF.md（Owner 提供，不在本仓库；4768 行, v1.0, 2026-08-28）。
本文是"正题/反题"素材，不是最终结论。

## 一、正题：文档做对了什么（应保留的核心裁决）

1. 控制平面而非裸 MCP 分发：直接把带写权限的领星 MCP key 分发给员工+AI = 无审计、无细分权限、prompt injection 直通生产写。裁决成立。
2. 隔离 Write Executor + 读写凭据分离：最小特权正确。
3. Proposal(绝对目标值+expected_before) → 冻结 Hash → Approval 绑定 Hash → 签名 Intent → 一次提交 → 回读：anti-drift 设计正确。
4. UNKNOWN 状态 + 禁止盲重试：分布式正确性上无可辩驳（exactly-once 不存在）。
5. 证据等级体系（VERIFIED/…/PROHIBITED）与"配置成功≠策略有效≠业务成功"三分：认知诚实，罕见的好。
6. 旧密钥视为已暴露必须轮换：无条件成立，是 Gate 0 第一项。
7. 分阶段 Gate + Initial-Deny + 当前最多 Gate 0/Gate 1：与证据相称。
8. 名称不可信、ID 全链 String、Decimal+币种、Effective Status 与配置状态分离：数据建模正确。

## 二、反题：问题、风险与过度设计

### T1. 体量与阶段错配（最大问题）
4768 行、17 个工作包、18 个角色、30+ 红队场景，服务的现实是：一个连接、两次 0.01 美元级窄写、0 行生产代码、0 个已确定的业务 Owner。文档自己判定当前只支持 Gate 0-1，但约 80% 篇幅在规定 Gate 3-5 的细节。风险：
- 分析瘫痪：团队被 spec 压垮，连 Gate 1 都到不了；
- 过早冻结：Intent 30+ 字段、Ed25519+JCS 签名等在 Gate 3 前没有真实约束校准，届时必重写，文档反成变更负担；
- 与"Simplicity First"原则冲突。
辩证修正：垂直切三层——(a) ~15 条安全公理（进代码与测试，永不妥协）；(b) Gate 0-1 可执行包（现在做）；(c) Gate 2+ 设计参考（冻结存档，进 Gate 前重审）。

### T2. 18 角色不现实
对典型跨境电商广告团队（几人到几十人）完全不成比例。文档 §9.5 自己给了正确答案："小团队允许一人多角色，但交易级冲突必须阻断"。
修正：角色塌缩为 ~6 个（Viewer/Analyst/Operator/Approver/Admin/Auditor），SoD 用请求级冲突矩阵按 human_person_id 实现，而不是 18 个角色对象。

### T3. 凭据现实可能反转授权设计的默认
领星 MCP 密钥很可能只有"绑定某领星账号的一把 key"（待 R1 证实）。若如此，§8.5 模式 1（每用户委托 OAuth）与模式 2（窄服务凭据）都不存在，只剩模式 3（广域共享 key）。则"领星原生权限 ∩ 公司 ACL"在 Provider 侧退化为"连接可见即全可见"，公司 ACL 是唯一真实边界。文档承认此模式但按例外处理；现实应反转默认：按模式 3 设计，把模式 1/2 当作未来升级。Blast radius 必须书面接受。

### T4. EXECUTION_ATTRIBUTION_CONFIRMED 可能是死状态
领星 MCP 大概率无 provider operation ID/审计日志（待 R1/R2 证实）。若无，归因确认永远达不到，DESIRED_STATE_OBSERVED 就是 L2 现实最高证据。诚实设计：把这一点显式写进 Gate 3 验收（接受"观察到目标值"为终态之一），保留归因状态但标注"依赖 Provider 能力，当前不可达"。

### T5. 平台不是唯一写路径——价值主张需重述
员工永远可以直接登录领星后台改广告；控制平面无法阻止 out-of-band 修改，只能观察（Controller Registry）。因此价值主张不是"唯一写路径"，而是：
(1) 统一只读事实与报表；(2) AI 分析与结构化提案；(3) AI 参与的写的唯一安全通道；(4) 全量观察与对账（含人工修改检测）。
推论：Gate 1（只读）价值独立成立，即使写通道永不上线，平台仍有正回报。这是 MVP 叙事的支点。
死亡模式警告：若审批摩擦 > 领星后台直改的成本，运营会绕过平台（shadow ops 回流），平台沦为摆设。审批 UX 与延迟是采用率的生死线，文档几乎没讨论采用激励。

### T6. 双人审批在小团队会卡死
若公司只有 1-2 个广告运营，R3 双审=永久阻塞=全部回流领星后台。修正：MVP 只开 R2 单审；R3+ 保持禁止而不是设计一套用不了的双审流程。双审机制推迟到 Gate 4。

### T7. §16-17 策略/因果框架在 MVP 无用武之地
D2-D5 数据成熟度（单位经济、库存、归因成熟、实验）在成本数据 Owner 都未定的公司短期达不到。冻结为参考；MVP 报表只做 L0 描述性 + 明确的"缺什么才能到 D2"清单。

### T8. 内部小矛盾
- §5.3 用例说 AI 生成提案并"发起审批"，§10.3 说 AI 不得对自己 draft 调 submit_proposal。应统一：AI 只能 draft，human submit。
- 写前重读 + 提交 + 立即回读 = 每写 3x Provider 调用，领星限流未知（OPEN），MVP 单对象低频可接受，需标注。

### T9. 未回答的价值问题：build vs buy vs read-only
文档从安全出发论证"怎么建"，未论证"值不值得建"。备选：
(a) 永久 Gate 1：AI 只读分析+人工领星后台执行——零写风险，保留大部分 AI 价值；
(b) 商用工具（Pacvue/Perpetua/Quartile/领星自带自动化规则）；
(c) 自建控制平面。
诚实路径：先落地 (a)（也是文档的 Gate 1），写通道只有当"AI 提案质量经人工验证有价值"后才有理由建。把 Gate 2→3 晋级从纯安全决策改为安全+价值双门。

### T10. 工作包顺序应纵切
WP-00→17 是水平分层（先全部地基再执行）。修正：先立 walking skeleton——一条端到端薄片（只读同步→提案→审批→Mock 执行→回读→审计）贯穿全部安全公理，再横向加厚。风险更早暴露，且每周都有可演示物。

## 三、安全公理清单（进代码的 ~15 条，从 4768 行中提炼）

1. 外部 ID 全链 String；金额 Decimal+币种；日期带 marketplace 时区。
2. 客户端自报身份/角色/组织一律不可信；ActorContext 由服务端签发。
3. EffectiveAllow = 各层 AND；任一 Unknown/Stale/Unavailable → Deny（fail closed）；Explicit Deny 优先。
4. 授权匹配禁止 Grant 拼接：单一 Grant 必须独立匹配完整 request tuple。
5. AI 客户端：无 Provider 密钥、无裸写工具、不能审批、不能对自己 draft 提交。
6. 每个生产写：绑定精确对象+父链+expected_before+绝对目标值+币种；名称永不作为定位依据。
7. Proposal 冻结即 Hash；Approval 绑定 Hash；任何变化使审批失效；审批人不能编辑。
8. SoD 按 human_person_id 在请求级阻断（creator≠approver 等）。
9. 每写一次预写 Intent+审计（同事务）；audit 不可用 → 停写。
10. provider_submit_count ≤ 1（数据库约束）；所有中间层写重试/重放/redirect-follow 关闭。
11. 超时/模糊响应 → UNKNOWN：冻结对象字段、只读对账、绝不自动重发。
12. 回读区分 DESIRED_STATE_OBSERVED / ATTRIBUTION_CONFIRMED / NOT_APPLIED / AMBIGUOUS，不得合并成"成功"。
13. Kill：单调 epoch，提交前最后检查；恢复需独立双人+新 epoch，不自动补执行。
14. 补偿是新 Proposal（重新审批），当前值≠平台写入值时禁止自动补偿。
15. 外部文本（Campaign 名等）永远是数据；LLM 输出只能是候选，服务端重新解析验证。
16. 组织/资源行级隔离在服务端强制（RLS 或等价），"不存在"与"无权"响应不可区分。
17. Shadow 阶段 Provider Write Count 必须 = 0（可测试）。

## 四、修订 MVP 草案（待评审挑战）

范围：Gate 0 + Gate 1 全部 + Gate 2 薄片 + Gate 3 的 Mock 演示（不接真实 Provider 写）。
- M0 治理最小集：Decision Register、Assumption Register、旧密钥轮换 checklist、4 份规范文件骨架。
- M1 平台基座：uv+FastAPI+Pydantic v2+SQLAlchemy+Alembic+PostgreSQL(容器)/SQLite(单测)；模块化单体；executor 独立包。
- M2 安全内核（薄片核心）：ActorContext、EffectiveAllow(简化 6 角色+冲突矩阵)、Proposal/Approval/Intent/Execution 状态机、单提交约束、审计追加表、kill epoch。
- M3 Canonical 只读：实体/快照/指标模型（String ID/Decimal/币种/时区/双时间）、Mock Provider Read Adapter、领星 Adapter 目录占位（BLOCKED_MISSING_SANITIZED_CONTRACT）。
- M4 接口：Internal MCP（只读工具 + create_draft_proposal）+ REST 等价 + 极简审批页（或 CLI）。
- M5 测试证据：安全公理全部有测试；红队子集 RT-01/02/05/07/08/09/10/21 在 Mock 下可复现；Go/No-Go 报告。
明确不做（本次）：真实领星连接、真实凭据、Web 富前端、策略引擎 DSL 完整实现、回测/实验、WORM 副本、Temporal、OPA、微服务。

## 五、R1 领星调研后的重大修正（2026-08-28）

R1（公开官方文档，见 research/r1_lingxing.md）与 handoff 的 VERIFIED 快照存在 P0 级冲突：

1. **官方 MCP（openmcp.lingxing.com）广告工具全部只读**（8 个报告工具；5 个写工具均非广告），而 handoff 记录"59 工具、~15 广告写工具、实测改预算/bid 成功"。两种可能：handoff 会话接的不是当前官方个人版 MCP（第三方 wrapper / 企业内测版），或 Provider 已漂移。→ 无论哪种，都是 Capability Registry + Schema Drift 检测"第一天就需要"的现实证据；此冲突必须进 Decision Register P0，实测消解。
2. **生产写通道的现实候选是领星开放平台 REST API**（仅覆盖 SP 写；批量逐条结果；无公开幂等键说明）——不是 MCP。修正架构拓扑：
   - 读：MCP（个人只读 key，QPS=1，AI 探索）+ 开放平台 REST（企业 key，同步/报表批量）
   - 写：开放平台 REST（Write Adapter 唯一通道候选）
   - 对账：开放平台"操作日志（新）"API（user_name/change_type/before/after/操作来源）+ 报表
3. **归因确认有现实路径**：操作日志 API 含操作者与前后值 → ASM-03（归因不可达）部分推翻；EXECUTION_ATTRIBUTION_CONFIRMED 不再是死状态，但依赖"平台写入使用专属服务身份可在日志中区分"，需实测。
4. **凭据现实证实 ASM-04 且更糟**：开放平台 AppId/AppSecret 由超级管理员签发、企业全量数据权限、不可按店铺细分 → 模式 3（广域 key）是唯一现实；公司 ACL 是唯一真实边界；IP 白名单利好隔离执行器（固定出口 IP）。MCP 侧 X-Mcp-Key 绑定个人账号继承其权限 → 读通道天然是"每用户委托"。
5. **限流现实**：MCP 每工具 QPS=1；REST 写接口令牌桶容量 1 → "写前重读+提交+回读"3x 调用在低频单对象可行，批量需节流设计；ASM-06 有了量化输入。
6. 无公开沙箱/SLA → 合同测试必须走"另获授权的测试账户"路径（handoff 已预见）。
