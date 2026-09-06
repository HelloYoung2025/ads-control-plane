# 领星广告控制平面 Handoff 辩证评审报告

- 评审对象：`LINGXING_MCP_AD_MANAGEMENT_PLATFORM_HANDOFF.md`（Owner 提供，不在本仓库；v1.0，4768 行，证据截止 2026-08-27）
- 评审方法：五视角独立评审（security-redteam / distributed-systems / lean-product / ads-strategy-data / platform-engineering）→ 对抗验证（逐条 CONFIRMED/DOWNGRADED 并复核原文行号）→ 与主智能体独立分析（`analysis/main_agent_review.md`）合并 → 本合成。外部事实基线：`research/r1_lingxing.md`（领星 MCP 与开放平台）、`r2_amazon_ads_api.md`、`r3_mcp_spec_security.md`、`r4_engineering_stack.md`。
- 证据等级标注沿用文档 §2.1：【VERIFIED】【OBSERVED-PROVISIONAL】【PROPOSED】【OPEN】。

---

## 一、总判定：CONDITIONAL-GO，最高可进入 Gate 1（只读）

**CONDITIONAL-GO。** 文档的安全内核与方法论是真金：控制平面包裹、交集授权、一次提交协议、UNKNOWN 语义、无远端 CAS 诚实边界、证据等级封顶——五个视角的调研全部独立证实这些裁决正确且剂量得当【VERIFIED】。因此不是 NO-GO：Gate 0 应立即开工，Gate 1 读路径可并行启动。但三个 P0 使"无条件 GO"不成立：(1) 配套项目书 L 级同名反义 + §26.6 悬空引用，使 Gate 0"契约冻结"名不副实【VERIFIED，r5】；(2) 领星现实中不存在只读/短期委托凭据，Gate 1"无生产 Write Credential"退出条件会被虚假满足【VERIFIED，r1 §1.2/§2.1】；(3) 写通道画在无公开文档背书的 MCP 写工具上，有文档背书的写路径是开放平台 REST（仅 SP），全文对后者零覆盖【VERIFIED，r1 §1.3/§2.5；全文 grep 零命中】。条件：Gate 0 冻结范围增补三项（L 级裁决、个人 MCP key 治理、写通道选型 Decision Register）并完成后方可宣布 Gate 0 退出；Gate 1 退出条件按凭据现实重写；Gate 2+ 与全部写机件冻结，直至写通道选型与 §23.1 合同答案落地。任何提前放宽都失去证据基础【VERIFIED：无沙箱、无 SLA、无幂等键、凭据企业全量，r1】。

---

## 二、正题——确认保留的核心裁决

五视角 keep 清单合并去重（每条一句为什么）：

| # | 裁决（条款） | 为什么保留 | 证据等级 |
|---|---|---|---|
| 1 | 控制平面包裹 + 交集授权（领星原生权限∩公司 RBAC/ABAC∩Allowlist∩审批）（§1、§9.1） | r3 证实三家 AI 客户端审批语义互不对齐，服务端交集授权是唯一公共分母；r1 证实 Provider 侧控制粗糙，公司侧是唯一可落点 | VERIFIED |
| 2 | 工具注解/客户端写确认只是 UX，不是服务端授权边界；禁 Passthrough（§4.5、§10.2、§10.4） | MCP 规范原文定注解为 untrusted hints、访问控制为服务端 MUST；Codex writes 模式恰在消费不可信注解 | VERIFIED |
| 3 | Capability Registry：schema hash pinning、TTL、行为探针、新工具默认禁用（§10.7、§12.4） | 与 rug-pull（CVE-2025-54136）/tool poisoning（实测成功率 60%+）的共识缓解精确对齐；本次"MCP 写能力快照冲突"正是其首日用例 | VERIFIED |
| 4 | 一次提交协议：job_delivery_count 与 provider_submit_count 分账、同事务 Outbox、网络调用前 CAS、UNKNOWN 后永不盲重发（§13.6、§13.8） | 与业界共识同构且更严谨（outbox 天然 at-least-once，exactly-once 不存在）；r1 证实领星写无幂等键，这是唯一诚实姿势 | VERIFIED |
| 5 | 无远端 CAS 诚实边界 + 自动补偿 Initial-Deny + ABA 分析（§13.7、§13.9） | 两级 Provider 均无版本令牌/条件写（r1/r2），拒绝宣称 Exactly-Once 是准确建模而非保守 | VERIFIED |
| 6 | 回读证据分级：DESIRED_STATE_OBSERVED 与 EXECUTION_ATTRIBUTION_CONFIRMED 强制拆分、分开统计（§12.5、§13.1） | r2 证实 Amazon 全生态无 actor 归因；且领星 apiLogStandard 恰好可填设计预留的插槽 | VERIFIED |
| 7 | absolute_target 唯一化，相对调整仅供人读（§11.7） | Amazon 为绝对值 partial update（重放收敛同值）；直接屏蔽领星 isBaseValue 相对语义下重放不幂等的整类风险 | VERIFIED |
| 8 | Bid≠Actual CPC 全链纪律 + NO_OP_BID_MAPPING_UNKNOWN（§4.2、§11.4、§16.7） | r2 定量证实：up-and-down 下 CPC 可达 bid 2 倍、placement +900%、官方无映射函数 | VERIFIED |
| 9 | 三类成功 + 结论证据等级封顶 + 十条伪策略反驳（§17.3、§17.4、§16.12） | 封死"配置回读成功=优化成功"的行业假胜利模式；是报表与问数产品可信度的地基 | VERIFIED 对齐 |
| 10 | extraction/attribution/finance 三种成熟度分离 + revision 不覆盖历史（§11.5） | Amazon 指标事后重述、领星自认小时/天数据可能存在差异；无此建模早期"改善"必是幻觉 | VERIFIED |
| 11 | 模块化单体 + PostgreSQL 全家桶；Temporal/Kafka/OPA/微服务/K8s 全部不上（§20.1、§20.2、§23.4） | r4 全面验证（生产案例、量级余量、迁移通道经纯函数边界保留）；选型无一处需推翻 | VERIFIED |
| 12 | RLS fail-closed + SET LOCAL 服务端可信值 + Executor 窄视图/存储过程 + 8 角色库权限矩阵（§20.5） | 与 PG 官方语义和多租户生产实践的四大 footgun 逐条对齐，超出常见实践水准 | VERIFIED |
| 13 | Initial-Deny 清单 + 数值阈值仅作 Canary 候选（§6.3、§6.4） | 直接免疫项目书未经验证默认阈值（±15%、clicks≥30）流入生产的路径（r5 冲突 3 反向印证） | VERIFIED |
| 14 | Kill 三层结构 + KILL_PROPAGATION_RACE 诚实分类（§15.1、§15.2） | 不伪称轮询 Worker 能瞬时阻断，凭据/网络层独立演练弥补软件极限（SLI 度量定义另修，见 P2） | VERIFIED |
| 15 | 端口抽象：Read / AuthoritativeRead / Write 三协议分离，Write 拆合同编译与单次提交（§12.2、§12.3） | 通道假设被推翻而端口形状完好——双通道修订只换实现不改接口，是本次调研对设计最有力的正面检验 | VERIFIED |
| 16 | 证据等级体系 + §4.4"不能证明"清单 + Action Evidence Matrix（§2.1、§4.6） | 把"一次窄写成功"与"生产能力"严格分离；本次评审能高效进行正因为它 | 方法论资产 |
| 17 | 多控制器：人工优先、不接管既有规则、振荡即对象级暂停（§14.2、§16.9） | r2 证实远端字段级 last-write-wins 无冲突信号，这是唯一不打竞价内战的并发姿态 | VERIFIED |
| 18 | 成熟度只设上限不授予权限；Strategy Maturity 与 Execution Authorization 双字段分离（§16.5、§16.11） | 阻断"跑了一段时间所以可以自动化"的滑坡 | 设计正确 |

---

## 三、反题——验证后缺陷表

仅列对抗验证 CONFIRMED/DOWNGRADED 条目，按修订后严重度排序。同根 finding 已合并（并入项注明）。

### P0（3 项）

| ID | 严重度 | 主张 | 证据 | 修复 | 阻塞 Gate |
|---|---|---|---|---|---|
| PS-01（security-redteam；并入 LP-07） | P0 | 配套项目书 L3=禁止自动化 vs 主文档 L3=Bounded Auto，同名反义；且 §26.6 指向不存在的文件，冲突优先规则挂在幽灵文件上管不到实际存在的项目书。自动化等级是全部 Gate/Grant/审批语义的脊柱，脊柱符号二义则"契约冻结"不成立 | 【VERIFIED】主文档 L4713 悬空文件名；项目书 L516/741/743（L 级倒置亲手复核）、L398-399（requires_approval:false）；r5 §1/§2 | 主文档声明为 L0-L4 唯一权威；§26.6 改指真实路径并附 supersession：项目书降级为策略素材源，其 L 级/审批标记/数值默认值一律无效，L 级重命名后方可引用；Gate 0 退出条件增加"配套文档 L 级冲突已裁决并落 Decision Register" | Gate 0 |
| SEC-01（security-redteam；并入 ds/pe 同名条） | P0 | 领星两条通道均不存在只读/短期委托凭据（MCP key 绑定个人、继承全部 ERP 权限；OpenAPI 为超管签发的企业全量单凭据），§8.5 首选模式虚构、§8.6"读写分离"不可行、Gate 1"无生产 Write Credential"退出条件（L3805）会被虚假满足——团队宣布"只做只读"通过 Gate 1，而所持凭据事实具备企业级写能力却按低标准托管 | 【VERIFIED】r1 §1.2/§2.1/§2.4/§4B；文档 §8.5/§8.6/§20.4/§7.5/§21 | 重写 §8.5 为两个真实选项（低权限子账号 MCP key 模拟窄凭据；企业 AppId+平台自建 ACL 即原第三档）；Gate 1 退出条件改为"凭据写能力已被证实性禁用（2001004 接口级授权只开读，或子账号无广告写权限）并通过写拒绝测试"，否则凭据托管自 Gate 1 起按 Executor 级标准；新增隔离域 Provider Credential Proxy（唯一持 AppId/AppSecret、按调用方身份的接口路径白名单）；"多 AppId""接口级授权能否剥离写"升为 §23.1 第一优先 | Gate 1 |
| ENG-01（platform-engineering；并入 ds ENG-01、LP-02） | P0 | 写通道画在无公开文档背书的 MCP 写工具面上（§7.2 唯一写边 EXEC→领星 MCP），而官方帮助页广告工具全只读、有文档背书的写路径是开放平台 REST（仅 SP、逐条 apiResult、无幂等键、配 apiLogStandard 对账接口）——全文 4767 行零处提及开放平台；§23.1"写工具合同"十问全按 MCP 工具面提问，无"写通道选型"先决问题。按文档自身原则（§12.4：运行时发现的工具是供应链事件不是产品合同），写路径不能押在它上面 | 【VERIFIED】grep 全文零命中"开放平台/apidoc"；r1 §1.3/§2.5/§2.6；文档 §7.2/§12.4/§23.1/§26.4。注意：MCP 写工具"存在与否"本身是快照冲突【OPEN，见六】 | 双通道 Adapter 修订（端口不动、实现换绑，详见四(a)）；写通道选型（REST SP vs Amazon Ads API 直连 vs 不建）列入 Gate 0 Decision Register P0 条目，未决则 Gate 2 合同调查与 WP-11 不得开工；§23.1 十问改写为 REST 版，r1 已答项落 Decision Register；MCP 写工具在 Capability Registry 全部标 DISCOVERED/Disabled | Gate 2/3（WP-05/06/11 合同冻结） |

### P1（16 项）

| ID | 严重度 | 主张 | 证据 | 修复 | 阻塞 Gate |
|---|---|---|---|---|---|
| SEC-02 | P1 | 员工个人 MCP key 是绕过控制平面的影子外泄通道（自助生成、接任意 AI 平台、继承全部 ERP 权限、无密钥级审计记载），Gate 0 冻结与 32 项红队表均未覆盖——控制平面沦为马奇诺防线 | 【VERIFIED】r1 §1.1/§1.2、OPEN 1/2；§18.4/§18.9/§21 复核 | Gate 0 增补：盘点组织内 MCP 开通与已发放密钥；Owner 签字决定开关策略（关闭/限名单/书面接受残余风险）；正式询问领星密钥级审计与多密钥；§18.9 增补对应 RT 条目 | Gate 0 |
| SEC-04 | P1 | 凭据运维现实未覆盖：AppSecret 重置硬切换（轮换=停写窗口）、refresh_token 一次性（多实例并发续约互相打翻）、IP 白名单使出口 IP 成为未识别的环境权威、MD5+AES/ECB（密钥=URL 明文携带的 appId）签名不构成安全控制 | 【VERIFIED】r1 §2.1/§2.2/§2.4；文档零提及（grep 复核） | Token Custodian 单飞服务（全局互斥续约、版本化、退避）；轮换 Runbook 按 mini-Kill 编排入 §19.8；Executor 独占白名单出口 IP；威胁模型明记"领星签名不计为安全控制"；2001008/2001009/3001002 进 §12.6 映射 | Gate 1 |
| ENG-02（ds） | P1 | 三级拓扑（平台→领星缓存→Amazon）使写前重读与回读读到二手缓存：TOCTOU 窗口=领星同步滞后（5 分钟~2 小时至 1 天），单次 Delta 护栏与冲突检测在滞后窗内对 Amazon 侧外部变更失明；"Provider Authoritative"档位在仅领星阶段结构性空缺 | 【VERIFIED】r1 §3.1/§2.6；文档按单跳 Provider 建模（复核确认无滞后模型） | §12.5 增"领星读=Amazon 缓存"滞后模型，每字段回读源合同记录通道+实测收敛延迟分布（Gate 2 合同测试）；§13.5 写前检查增加操作日志窗口查询作独立证据；§13.7 残余风险书面接受须写明实测滞后即 TOCTOU 窗口 | Gate 3 |
| ENG-03（ds） | P1 | 写令牌桶容量=1 与一次提交协议组合自我死锁：3001008 发生于 CAS 之后→按 §12.6/§13.6 该 Intent 报废、重走完整 Proposal+Approval；且状态机 NOT_STARTED/READY 无 EXPIRED/CANCELLED 出边、CONFLICT 无出边，与 EXE-09/Kill 语义矛盾 | 【VERIFIED】r1 §2.3/§2.4；§13.6 规则 4/9、§12.6 逐条复核 | Executor 内建按 (connection, endpoint) 的桶感知节流器置于 CAS 之前；增设 PROVIDER_REJECTED_NOT_EXECUTED 类（经合同测试证明业务层未触达的拒绝），允许审计转换下有界重投同一 Intent；§13.1 补齐出边 | Gate 3 |
| SEC-05 | P1 | 请求级 SoD 矩阵不闭合：缺 Credential Custodian≠Executor Administrator（合一即获单人无审计裸写路径且不违反任何已写规则）；Policy Admin"审批受自己政策影响的变更"禁令无可计算判据 | 【VERIFIED】§9.4/§9.5 逐行复核（矩阵恰七条） | 矩阵增补两条；写凭据明文读取双人授权或短租约+告警；Policy Bundle 携带 author，审批人∩命中政策 author 非空即 SOD_VIOLATION；§19.2 加测试 | Gate 3 |
| SEC-06 | P1 | 红队模型缺以人类审批人为目标的攻击面：Provider 可控文本渲染进审批 UI 的展示层注入、审批端点 CSRF/clickjacking/会话劫持零覆盖（审批是 L2 唯一人类安全门） | 【VERIFIED】全文 CSRF/劫持零命中；§18.9/§19.2/§19.3 复核。注：§13.4 已有部分显示合同（见七） | §18.9 增补三条 RT；显示合同补强为 MUST（canonical ID 优先、provider 来源文本降权+不可信标记、hash 摘要可见）；高风险审批逐决定 Step-up 绑定 payload hash；§19 加审批通道渗透测试 | Gate 3 |
| LP-01 | P1 | 阶梯缺 L1.5"人执行、平台核验"台阶：L1 提案无合法出口，带外人工写全文只以敌对冲突源身份出现；采用率会先于安全问题杀死平台 | 【VERIFIED】§6.1/§13.1/§14.2/WP-13 复核；r1 §2.6 证实核验闭环技术可行；r5 项目书第二阶段即此台阶 | 正式加入 L1.5：结构化 Proposal→人工复核 Bulk Sheet→运营 ERP 手工应用→平台经操作日志自动回读核验闭环（复用 §13.8 词汇）；零写凭据零 Executor；L1.5 使用量与闭环率作 Gate 3 进入条件 | Gate 3 |
| LP-04 | P1 | 成功定义缺"经平台治理的变更占比"：五类报表/KPI/SLO/各 Gate 退出条件均不度量采用率与旁路，控制价值随治理覆盖率线性衰减而无任何 Gate 迫使面对 | 【VERIFIED】全文 grep 采用/覆盖/adoption 复核；r1 §2.6 证实当天可度量 | WP-08 加采用率/覆盖率类报表（治理内变更占比按操作日志来源归因、提案被应用率、L1.5 闭环率）；"治理占比达 Owner 阈值"列为 Gate 3 进入条件 | Gate 3 |
| LP-05 | P1 | 17 角色+R2 逐笔独立人审是为不存在的组织做的剂量；从未推导不变量隐含最小人数；审批疲劳/橡皮图章作为控制失效模式全文缺席 | 【VERIFIED】§9.4/§6.2/§9.5/§19.6 复核（疲劳/橡皮零命中）；r5 冲突 2 佐证频率压力 | 机制保留（请求级矩阵+SEC-05 补齐），17 角色降为标签目录不强制人头；WP-00 写明最小人员配置模型并由 Owner 确认真实存在；R2 以复核过的 Bulk Sheet 集合粒度批量审批（§11.8/§13.10 已有集合冻结机制）；权限报表加审批时延分布与拒绝率作橡皮图章探测器 | Gate 3 |
| LP-06 | P1 | 无 build-vs-buy-vs-read-only-forever 分析："尽量自动化"被当既定终点，写通道建设（WP-09~17 大头）没有正当性条件；领星自动规则从未被评估为受控执行后端 | 【VERIFIED】全文 grep 复核；r1 §2.5（自建写通道天花板仅 SP） | WP-00 增建/买/永久只读决策备忘录，写明写通道立项正当性条件（治理占比达标、量化事故成本、§23.1 可行、人工应用成本超 TCO）；显式承认"永久 Gate 1+L1.5"为合法终态；评估"小额自动化由领星原生规则承载+平台管规则配置提案与审计"作 L3 替代 | Gate 3 |
| ASD-01 | P1 | D2（单位经济）无任何 Gate 验收：Gate 2 的 100 个提案全为 ACOS 效率类或 PROFIT 全量 ABSTAIN 照样过门，PROFIT 主目标策略无被强制演练的路径——恰与 §16.12.1 要反驳的心智相悖 | 【VERIFIED】§21/§16.5/§11.6/WP-08 复核；r1 证实利润/库存读 API 现成，瓶颈是口径冻结与财务签核 | Gate 1 退出加"目标 ASIN 单位经济覆盖率≥X%（财务签核）+库存快照日更"；Gate 2 退出加"含 PROFIT 类提案且 MISSING_ECONOMICS ABSTAIN 率可解释"；按 D0→D2 最短清单执行（归因窗口冻结、mature_through 实测、MSKU 利润→offer_unit_economics、FBA→cover_days、Bridge join_coverage） | Gate 2 |
| ASD-05 | P1 | StrategySpec 表达式 DSL 是 Gate 2 关键路径上的剂量超配：语法/解析器/静态检查/重放确定性数周级工程，收益（非工程师不发版改规则）在 Canary 前不存在 | 【VERIFIED】§16.6/WP-09 复核；r5 项目书策略内容可折叠为约 5 个规则类 | decision 块降级为 rule_class 闭集枚举（HARVEST/BID_DOWN/BID_UP/NEG_EXACT_CANDIDATE/BUDGET_FLAG）+白名单参数包；治理外壳完整保留；DSL 移 §23.4 P2，触发条件"≥2 团队需要不发版改规则" | Gate 2 |
| ASD-02 | P1 | 回测未区分"决策重放"与"结果反事实"：bid 类结果回测在 bid→CPC 映射未验证时结构性不可校验，却以固定晋级站身份进入证据链——§16.7 只挡 Proposal 没挡回测报告，晋级材料可被伪造 | 【VERIFIED】§16.10 要求清单复核（无结果可得性条款）；r2 §G | §16.10 明文二分：MVP 回测=决策重放（触发正确性/ABSTAIN/振荡/与人工历史对比），结果栏强制 INCONCLUSIVE；结果型回测仅在 bid_mapping_model_version 经 Canary 实测校验后解锁 | Gate 4 |
| ASD-03 | P1 | 单个 Negative Exact 被四处不一致条文（§6.3/§4.6/§16.8/§21 Gate 4）实际冻结出整个执行面，而它有证据门、搜索词级爆炸半径与 SpArchiveNegatives 现成撤销通道 | 【VERIFIED】四处口径逐一复核；r1 §2.5（写接口存在）；"风险剖面优于小幅 bid"含可辩论判断（误杀损失不可观测） | 统一四处措辞并裁定：Phrase/批量维持 Initial-Deny；单个 Negative Exact（搜索词证据门+双人审批+撤销登记）列为 Gate 3/4 R3 白名单候选，由 Owner 决定 | 无（候选路径缺失） |
| ASD-04（并入 ds ENG-04 残差、SEC-03 残差） | P1 | 领星"操作日志（新）"apiLogStandard 是多控制器检测与写归因的唯一现成证据源（user_name/前后值/区分 ERP 与亚马逊来源；Amazon 生态无 actor），却未列 Gate 1 交付物；查询窗口≤1 个月、保留期未知，基线晚接一天薄一天 | 【VERIFIED】r1 §2.6、r2 §D；Gate 1/WP-05 清单复核 | Gate 1 交付新增操作日志采集管道+外部变更基线报表；§12.5 登记为候选证据源并做合同测试（OpenAPI 写可见性/user_name 可辨识/延迟/完整性）；据结果增设 CORROBORATED_SINGLE_CONTROLLER 中间档（窗口内恰一条匹配且身份为平台专属账号才可升 CONFIRMED），否则停 DESIRED_STATE_OBSERVED 并明文接受为 L2/L3 标准生产终态 | Gate 3（采集应在 Gate 1） |
| PS-01（platform-engineering） | P1 | 工作包依赖图强制横向完备（WP-07 依赖 WP-02-06 全部），无最小垂直切片：首个可演示物与 Gate 1 四周采集时钟被人为推迟，最不确定的集成最后才接触 | 【VERIFIED】§22.2/WP-03/04/08 复核；r4（工具链全预编译，慢不在基建） | 按 walking skeleton 重排 M0-M5（见四(b)），每步有可演示物+测试证据；采集时钟自 M2 起跑 | 无（计划层） |

### P2 简表（择要；含降级项残差）

| ID | 残差/内容 | 处置 |
|---|---|---|
| SEC-03（降级） | 归因联合匹配仅作 corroborating、平台写用专属子账号保证 user_name 可辨识、尊重一个月窗口 | 并入 ASD-04 落地；文档 §12.5/§13.8/§23.1 已大部处理 |
| ds ENG-04（降级） | 灾难链误读（见七），建设性残差并入 ASD-04 | 合并 |
| LP-03（降级） | §13-16/WP-09~15 标注"冻结参考，待通道选型与 §23.1 答案后重推导"；安全内核 Provider 无关部分不受影响 | 文档已有 Go/No-Go 与 PROPOSED 定级；补标注即可 |
| pe ENG-02（降级） | 单 pyproject 布局下隔离断言不可日常证伪 → uv workspace 多包 + import-linter + 隔离安装测试三层 CI 断言 | 吸纳为 WP-01 修订 |
| sec ENG-01 / pe ENG-04 | MCP 规范已到 2026-07-28 不兼容大改：§26.2 更新引用；Internal MCP 以 2026-07-28 为目标+双版本握手验收；锁 mcp>=2.1,<3；session_id 永不承载授权；LB 透传 Mcp-* 头 | WP-07 修订 |
| ds ENG-05 | Control Ledger RPO=0 与未规定同步复制矛盾：synchronous_commit+同步备库；故障切换后固定协议（全局 Kill→非终态置 UNKNOWN→对账→Resume）入 Runbook 与 §19.10 演练 | WP-10A/17 修订 |
| ds ENG-06 | 禁 SQLite/内存库作触库测试替身：RLS/SKIP LOCKED/advisory lock/SET LOCAL 为 PG 专有语义，SQLite 全绿无证明力 | WP-01 修订（主智能体草案"SQLite 单测"被否，见四(b)冲突 3） |
| ds ENG-07 | Kill 传播 p99<60s 与机制错配：拆为闸门不变量审计（KILL_ACK_VIOLATION）、在途暴露 SLI、非闸门组件心跳陈旧度三件 | §15.2/§19.1 Metric Contract 修订 |
| pe ENG-03 | CI"仅 Mock"验不了 PG 语义验收项：CI 加真实 PG（GitHub Actions services: postgres:18；本机 Postgres.app 18.4） | §20.4 修订 |
| pe ENG-05 | 读容量规划缺失（MCP 每工具 QPS=1，REST 读桶约 10）：M2 交付吞吐实测报告，读通道分级入 Decision Register | WP-05 修订 |
| ASD-06 | §16.3 目标表加"首 Provider 可执行性"列：SB/SD 依赖目标标 L1-only（REST 无 SB/SD 写） | 文档修订 |
| ASD-07 | D3 拆 D3a（数据侧，Gate 2 Shadow 门槛）/D3b（写侧，Gate 4 Canary 门槛），消除循环依赖歧义 | §16.5 修订 |

---

## 四、合题——修订方案

### (a) Provider 拓扑修订：双通道三源

原拓扑（单通道）：`EXEC → 领星 MCP（读+写合一）`。修订为【PROPOSED，基于 r1 VERIFIED 事实】：

```
读（即席/个人）   ：领星 MCP，专用低权限子账号 X-Mcp-Key（QPS=1，AI 探索问数）
读（批量/权威回读）：开放平台 REST（企业 AppId，令牌桶约 10；SP/SB/SD 报表+小时级+基础数据）
写               ：开放平台 REST，仅 SP（putSp*，逐条 apiResult，无幂等键→平台侧 at-most-once 兜底）
对账/归因        ：开放平台"操作日志（新）"apiLogStandard（user_name/前后值/ERP 与亚马逊来源二分；窗口≤1 个月，需自建轮询）
```

配套裁决：
1. **端口不动、实现换绑**（§12.2/§12.3 三协议分离原样保留）；§12.4 Manifest 增 REST 维度字段（endpoint、签名、令牌桶、逐项 apiResult 映射），allowed fields 明令禁用 isBaseValue/baseType/baseValue 相对参数。
2. **凭据**：新增隔离域 Provider Credential Proxy——唯一持有 AppId/AppSecret，内置 Token Custodian 单飞续约，按调用方身份做接口路径白名单（控制平面仅读端点、写端点仅 Executor 身份）；Executor 独占白名单出口 IP。WP-00 落三选一 ADR：(a) 全部 OpenAPI 收进 Executor 信任区 /(b) 多 AppId（待领星核实）/(c) 按 §8.5 模式三书面接受 blast radius。
3. **MCP 写工具**：Capability Registry 全部标 DISCOVERED/Disabled——不是断言其不存在（本租户 §4.2 有实测写入【OBSERVED-PROVISIONAL】），而是按 §12.4 原则不把产品合同押在无文档背书、无变更公告义务的表面上；快照冲突走六节的核实项【OPEN】。
4. **SB/SD 写**：两通道均无文档化能力，明文移出范围；Amazon Ads API 直连从"泛泛未来 Provider"升格为路线图命名的战略解锁项（带 test accounts、change history、Marketing Stream、SB/SD 写）——defer，不是 cut。

### (b) MVP 修订：垂直三层 + walking skeleton M0-M5

**垂直三层**（吸收 lean-product 框架，剂量按验证结论校正）：
- **A 层——安全公理（约 17 条，主智能体已提炼）**：自第一行代码起以类型系统与 CI 测试强制，与通道选型正交，终身有效。
- **B 层——Gate 0-1 + L1.5 可执行规范**：WP-00~08 瘦身版 + 操作日志采集 + L1.5 核验闭环。
- **C 层——§13-16/WP-09~15 写机件**：标注"冻结参考，待写通道选型与 §23.1 答案后重推导"；但其中 **Provider 无关的安全内核（Outbox/CAS/Kill Epoch/风险预留）例外，随 M4 以 Mock 建成**。

**分歧选边**（Expose Conflicts, Don't Reconcile）：

1. **"Mock 也不建"（LP-03）vs M4 Mock 安全内核（ds/pe）→ 选 ds/pe。** 理由：对抗验证确认安全内核与 Provider 无关（Control Ledger/Kill/Outbox/风险预留不受通道更换打击），r4 验证实现路径成熟；一次提交协议的故障注入证据（双 Worker 竞争仅一次提交、kill -9 卡单进 UNKNOWN 不重发）是全文最值钱的工程资产，推迟只会把最难的正确性问题挤到 Gate 3 高压期。LP-03 的合理内核（Manifest/Adapter 等 Provider 绑定件不预建）已由通道选型前置吸收。
2. **主智能体草案"MVP 不连真实领星" vs pe M2 真实只读纵切 → 选 pe。** 理由：Gate 1 退出条件的四周采集时钟不连真实 Provider 就永不起跑；QPS=1、分页、限流分类是最不确定的集成，应最早接触；主智能体自己在 R1 后的第五节修正已转向双通道真实读。
3. **主智能体草案"SQLite 单测" vs ds ENG-06 禁 SQLite → 选 ENG-06。** 理由：本设计的安全不变量全部绑定 PG 专有语义（RLS fail-closed、SKIP LOCked、advisory lock、SET LOCAL），SQLite 上的全绿对 §13.6/§20.5 的安全声明没有证明力，恰好击穿两条 0 容忍 SLI 的假绿风险不可接受；纯业务逻辑以不触库纯函数编写。
4. **ds"归因率移出 Gate 3 门" vs 对抗验证"Gate 3 本无阈值" → 选验证结论。** 理由：Gate 3/§19.1 只要求分开报告，恒近零不阻塞验收（ds 的灾难链系误读）；保留分开报告，增设 CORROBORATED_SINGLE_CONTROLLER 中间档，并把 DESIRED_STATE_OBSERVED 明文接受为 L2/L3 标准生产终态——这吸收了 ds 裁定的合理部分而不删除有价值的度量。
5. **lean"塌缩为 5-6 角色" vs security"17 角色标签目录+请求级矩阵" → 机制选 security、人头选 lean。** 理由：这是两层不同的问题——强制机制必须是按 human_person_id 的请求级冲突矩阵（补 SEC-05 两缺口后闭合），17 角色仅作标签目录不强制人头；而"这些人真实存在"由 WP-00 最小人员模型与 Owner 确认解决。逐笔 R2 与审批疲劳的两难，选 Bulk Sheet 集合粒度批量审批（§11.8/§13.10 已有集合冻结机制，非新发明）。
6. **WORM 完整基建 vs 首期 append-only 异地导出+完整性 Hash → 选后者**（security 自提的剂量调整，无反对）；完整 WORM defer 至 Gate 4 准入前。Control Ledger 同步预写不可妥协。

**M0-M5 里程碑**（每步=可演示物+测试证据）【PROPOSED】：

| 里程碑 | 内容 | 可演示物 / 证据 |
|---|---|---|
| M0 基座 | uv workspace 多包（core-domain / provider-read / provider-write / app / executor）+ Alembic 基线 + CI（真实 PG service） | CI 绿灯；RLS fail-closed 回归、包隔离三层断言、rfc8785 官方向量 |
| M1 身份+只读 MCP 骨架 | 独立进程 Internal MCP（mcp>=2.1,<3）+ TokenVerifier；whoami / list_authorized_scopes | Codex 实连返回 ActorContext；401/越权/新旧协议握手矩阵 |
| M2 领星只读纵切 | 子账号 X-Mcp-Key→Read Adapter→canonical→query_ad_metrics；**apiLogStandard 采集管道+外部变更基线报表**；Gate 1 四周采集时钟起跑 | Codex 查单 Profile 结构与指标；录制 fixture 合同测试、限流分类、QPS=1 吞吐实测报告 |
| M3 提案审批+L1.5 纵切 | draft→submit→最小审批页→freeze hash；**Bulk Sheet 导出→ERP 人工应用→操作日志自动核验闭环** | 自批拒绝、hash 失效、跨组织 RLS 拒绝；L1.5 闭环率报表 |
| M4 安全内核+Mock 执行 | Intent+Outbox 同事务、SKIP LOCKED、SUBMITTING claim、provider_submit_count≤1 约束、kill epoch、桶感知节流器 | 故障注入：双 Worker 竞争仅一次提交；kill -9 卡单进 UNKNOWN 不重发 |
| M5 真实写 Canary | 仅在 Gate 3 授权 + LP-06 正当性条件满足后：REST 企业凭据+Credential Proxy+签名对拍+putSp* 单对象+操作日志对账 | 一次 12.00→12.01 全链路证据包 |

### (c) 17 个工作包最终裁定

| WP | 名称 | 裁定 | 要点 |
|---|---|---|---|
| WP-00 | 决策与威胁模型 | **modify** | 增补：写通道选型 ADR、凭据三选一 ADR、build/买/永久只读备忘录、最小人员配置模型、MCP 快照冲突核实项、L 级裁决与 supersession |
| WP-01 | Foundation | **modify** | uv workspace 多包+import-linter 三层断言；CI 加真实 PG；禁 SQLite 触库测试；纯函数测试不触库 |
| WP-02 | Identity & ActorContext | **keep** | M1 承载 |
| WP-03 | Account Mapping & Authorization | **modify** | 首期薄层（org/RLS/角色标签/Allowlist/Explicit Deny/含 SEC-05 增补的 SoD 矩阵）；JML/Temporary Grant/导出权限 defer 至 Gate 3 准入前 |
| WP-04 | Canonical Data | **modify** | Gate 1 只需 append-only revision + recorded_at/source_as_of + raw 快照；双时间投影与 Feature Snapshot defer 至 Gate 2 |
| WP-05 | Provider Read Adapter | **modify** | 双读通道（MCP 子账号+REST）；新增操作日志采集与外部变更基线报表（Gate 1 交付）；吞吐实测 |
| WP-06 | Capability Registry | **keep** | MCP 写工具全标 DISCOVERED/Disabled；Manifest 增 REST 维度；领星连接加协议版本漂移告警 |
| WP-07 | Internal MCP & REST Read | **modify** | 目标协议 2026-07-28+双版本握手验收；锁 mcp>=2.1,<3；独立进程；LB 透传 Mcp-* 头 |
| WP-08 | Data Quality & 报表 | **modify** | Gate 1 子集（数据质量/权限审计/结构设置/管理层效率版+外部变更基线）；新增采用率/覆盖率类；策略绩效推 Gate 2、操作执行推 Gate 3；D2 验收接口 |
| WP-09 | Strategy & Proposal | **modify** | DSL 降级为 rule_class 闭集+参数包白名单；治理外壳保留；回测重定义为决策重放 |
| WP-10 | Policy & Approval | **modify** | 审批显示合同 MUST+审批通道渗透测试；Bulk Sheet 集合审批粒度；Policy Admin 可计算判据 |
| WP-10A | Safety Kernel | **keep** | M4 核心；增 PG 同步复制与故障切换协议、Kill SLI 三拆 |
| WP-11 | Isolated Executor | **keep**（边界改） | Mock 先行维持；真实 Write Adapter 目标改 REST SP；PROVIDER_REJECTED_NOT_EXECUTED+节流器；状态机补出边；接入前提=通道选型已决 |
| WP-12 | Readback & Reconciliation | **modify** | 回读源合同记录通道+实测收敛延迟；CORROBORATED_SINGLE_CONTROLLER 中间档；写前检查加操作日志窗口查询 |
| WP-13 | Audit、Kill、Controller | **modify** | WORM 首期=异地 append-only 导出+完整性 Hash，完整 WORM defer Gate 4；Kill/Resume 与 Controller Registry 保留 |
| WP-14 | Operator UI | **modify** | M3 最小审批/核对页先行；完整 UI 随 Gate 3 后真实流量 |
| WP-15 | Backtest/Shadow/Experiment | **modify+defer** | Shadow 保留（Gate 2 硬门）；回测=决策重放；Holdout/实验注册表/Bid→CPC 响应模型 defer |
| WP-16 | Red Team & Fault Injection | **modify** | 增补 RT：个人 key 外泄、审批 UI 注入、CSRF/会话劫持；SEC-05 两用例 |
| WP-17 | Production Readiness | **modify** | 增 Runbook：轮换=停写窗口、故障切换后对账协议；Metric Contract 修订 |
| — | Temporal/Kafka/OPA/微服务/K8s | **cut（维持不引入）** | r4 验证；纯函数边界保留迁移通道 |

---

## 五、与《AmazonAds-Codex-Skills-MCP-自动化项目书》6 处冲突裁定

（项目书由 Owner 本机提供，不在本仓库）

| # | 冲突 | 裁定 | 为什么 |
|---|---|---|---|
| 1 | L 级语义倒置（项目书 L3=禁止 vs 主文档 L3=Bounded Auto） | **主文档胜** | L0-L4 是全部 Gate/Grant/审批语义的脊柱，唯一权威必须归一；项目书 L 级重命名（如 S1-S3）后方可被引用，§26.6 改指真实路径并附 supersession【VERIFIED 冲突，PS-01】 |
| 2 | 小幅 bid requires_approval:false vs R2 独立人工审批 | **主文档胜** | bid→CPC 映射未验证、远端无 CAS、多控制器基线为零——"小幅"≠低风险【VERIFIED，r2 §G】；项目书的真实诉求（操作频率）由 L1.5 Bulk Sheet 集合审批承接，而非放开免审 |
| 3 | 硬阈值当设计默认（±15%/clicks≥30/cover_days） | **主文档胜，项目书资产降级复用** | §6.4 正确：固定常数忽略价格带与 CVR 异质性；阈值降为具名 Canary 候选参数包，按 Marketplace×币种×价位带分层校准并叠加最低花费门；cover_days 三档可直接作候选（FBA 库存 API 现成） |
| 4 | 否词：项目书低风险自动候选 vs 主文档 Initial-Deny | **按对象拆分选边，非折中** | Phrase/批量否定维持 Deny（误杀面不可控，主文档胜）；单个 Negative Exact 是不同风险对象（搜索词级爆炸半径+SpArchiveNegatives 撤销通道），项目书方向部分正确——但入口是 Gate 3/4 R3 双审白名单候选，绝非"自动候选"【ASD-03】 |
| 5 | 领星 MCP 角色：项目书=只读问数、写走 Amazon Ads 官方 MCP vs 主文档=首个写 Provider | **两者都不采纳，按 r1 证据裁定第三选项** | 项目书"领星 MCP 只读"方向被 r1 证实；但写通道既非 Amazon Ads MCP（项目书）也非领星 MCP（主文档），而是今日唯一有文档背书的开放平台 REST SP【VERIFIED】；Amazon Ads API 直连列为命名战略解锁项（defer） |
| 6 | rollback_value 裸回滚 vs UNKNOWN/Control Ledger 体系 | **主文档胜，裸回滚废止** | 无 CAS/LWW 下按存值盲回滚=第二次未经验证的写，会踩掉并发人工修改且无冲突信号【VERIFIED，r2 §C】；补偿一律生成新 Proposal 走原审批门（§13.9） |

可复用项目书资产：ActionSpec 字段集→Proposal、store_bindings 复合键→§8、cover_days 护栏候选、Workbench 工具清单→Internal MCP 工具面、5 个 Skills→rule_class 内容素材。

---

## 六、P0 决策清单（按 Owner）

### 业务 Owner
1. 批准 L0-L4 唯一权威裁决与项目书 supersession（PS-01，Gate 0 退出条件）。
2. 签署 build/买/永久只读备忘录：写通道立项正当性条件；是否承认"永久 Gate 1+L1.5"为合法终态（LP-06）。
3. 设定 Gate 3 进入的治理占比阈值与 L1.5 采用指标（LP-01/LP-04）。
4. 确认审批人与最小人员配置模型真实存在（§23.1 Q5、LP-05）；单个 Negative Exact 是否进白名单候选（ASD-03）。
5. business_goal_assignment 填充（§23.2.3）。

### 技术 Owner
1. **写通道选型**：开放平台 REST（仅 SP）vs Amazon Ads API 直连 vs 不建——Gate 0 Decision Register P0 条目，未决则 Gate 2 合同调查与 WP-11 不得开工（ENG-01）。
2. **MCP 写能力快照冲突核实（新发现）**：本租户实测约 15 个广告写工具且窄写成功（handoff §4.1/§4.2【OBSERVED-PROVISIONAL】）vs 官方帮助页广告工具全只读（r1 §1.3【VERIFIED，帮助页非穷尽 OPEN#3】）。实测 tools/list 对比官方口径，判定属企业版差异/第三方 wrapper/能力漂移三者之一；无论结论如何，该冲突本身是 Capability Registry+Schema Drift 检测"第一天就需要"的现实证据，结论落 Decision Register。
3. 读通道分级（MCP 子账号即席 vs REST 批量）与吞吐实测（QPS=1/桶约 10，ENG-05 pe）。
4. 向领星核实：企业多 AppId（r1 OPEN#5）、2001004 接口级授权能否剥离写接口（SEC-01）、SB/SD 写是否在规划（OPEN#6）。
5. Internal MCP 目标协议版本 2026-07-28 与双版本握手矩阵（WP-07）。

### 安全 Owner
1. 个人 MCP key 治理：盘点+开关策略签字（关闭/限名单/书面接受残余风险）（SEC-02，Gate 0）。
2. Gate 1 凭据方案：低权限子账号或接口级只读授权+写拒绝测试；不可行则凭据托管自 Gate 1 起按 Executor 级标准（SEC-01）。
3. 审定 Provider Credential Proxy/Token Custodian、轮换=停写窗口 Runbook、Executor 独占出口 IP（SEC-04）。
4. SoD 矩阵增补两条+Policy Admin 可计算判据（SEC-05）；审批显示合同与审批通道渗透测试范围（SEC-06）。
5. 正式询问领星：密钥级审计与多密钥能力（r1 OPEN 1/2）。

### 数据 Owner
1. 操作日志合同测试：OpenAPI 写在日志中的可见性、user_name 可辨识性、延迟分布、完整性、保留期（ASD-04/SEC-03 残差）——决定归因证据档位可达性。
2. D2 单位经济口径冻结+财务签核流程；Gate 1/2 的 D2 验收阈值（ASD-01）。
3. 归因窗口冻结与 mature_through 实测校准（ASD-01 最短清单）。
4. 读面同步滞后基线测量（=TOCTOU 窗口量化，ENG-02 ds）。

---

## 七、被拒绝的 findings 简表

以下子主张经对抗验证被否或判定文档已处理，**后续评审不得重复提出**：

| 来源 | 被拒内容 | 处置 | 一句理由 |
|---|---|---|---|
| ds ENG-01 子主张 | "SB/SD 策略承诺了无法执行的写路径" | REFUTED | 文档从未承诺 SB/SD 写（Gate 3/4 明示仅 SP，全文无 SB/SD 写表述） |
| ds ENG-01/ds SEC-01 子前提 | "官方 MCP 今日无广告写工具/写能力被推翻/MCP 广告面只读" | REFUTED（表述过强） | r1 仅证明官方帮助页未文档化（OPEN#3 自认非穷尽），handoff §4.2 有实测写入为直接反证；准确表述="无公开合同背书的不稳定表面"，存废走六.技 2 核实项 |
| SEC-04 子主张 | "2001008/2001009 造成写中段伪 UNKNOWN 风暴" | REFUTED | 该两码产生于续约端点；业务写持失效 token 返回 2001003，属提交前可判定拒绝——真实后果是认证风暴/可用性，非 UNKNOWN |
| ds ENG-04 灾难链 | "归因率恒近零→Gate 3 无法退出；UNKNOWN 无限积压→系统自我冻结" | REFUTED | Gate 3/§19.1 只要求分开报告、无阈值；NOT_APPLIED_CONFIRMED 有不依赖日志完整性的第二路径（§13.8.5）；已确认写入不进 UNKNOWN |
| SEC-06 子主张 (c) | "文档未规定任何审批显示合同" | ALREADY_HANDLED_IN_DOC（部分） | §13.4 已规定完整对象路径/前后值/批量数/预计 Spend；残差仅为不可信标记、canonical ID 优先与 hash 对应物 |
| LP-02 子主张 | "Gate 1 采集会因 QPS=1 失败" | REFUTED（过强） | Gate 1 范围=单 Profile、SP、有限 ASIN，QPS=1 未必击穿；规模化容量属 Gate 2+ 规划（ENG-05 pe P2） |
| LP-02 子主张 | "凭据交集坍缩为单边控制未被文档处理" | ALREADY_HANDLED_IN_DOC | §8.5 已给广域共享 Key 的降级语义与显式残余风险要求，§23.1 已列 P0 OPEN；真正缺口是"默认应反转"（已并入 SEC-01） |
| LP-03 子主张 | "Go/No-Go 只闸生产执行，不闸工作推进" | REFUTED | §22.1.6 要求每个阶段 Go/No-Go、不自动继续；WP-00 已强制把本文物化为四份规范文件 |
| pe ENG-02 子主张 | "隔离断言永不会失败、直到事故才发现" | REFUTED（过强） | §12.3/WP-01 已明令构建时验证 MUST；真实残差是单包布局下不可日常证伪（P2 吸纳为 uv workspace） |
| SEC-03 整条 | "归因可被混淆伪造属 P1 缺陷" | ALREADY_HANDLED_IN_DOC（大部）→ P2 | §12.5 不可歧义门槛+§13.8"若有"+§23.1 P0 OPEN 已封住不安全路径；失败模式安全（停留 DESIRED_STATE_OBSERVED），残差为落地细化 |
| lean 隐含主张 | "17 角色/SoD 机制本身应推翻" | REFUTED | 请求级 human_person_id 冲突矩阵机制被 security 视角验证成立且优于强制人头；缺陷仅在矩阵不闭合（SEC-05）与人头模型缺失（LP-05） |

---

## 附：最终缺陷统计

- P0：3（PS-01 治理、SEC-01 凭据、ENG-01 写通道基线）——全部为"必须立即修的 P0"，修复成本低（文档修订+决策），不是架构性推翻。
- P1：16（合并后）；P2：12（含 4 项降级残差）。
- 核心架构（控制平面、交集授权、一次提交、证据分级、单体+PG）零推翻：所有修订均为剂量、时序与 Provider 基线校正。
