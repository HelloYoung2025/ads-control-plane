# 广告控制逻辑：LX-MCP 实测能力与四控制面设计

2026-08-28 实测（Owner 提供 key 并指示本会话直连调研；全程只读，证据见 `docs/evidence/lx-*-20260828*.json`）。
本文档是控制逻辑的权威设计；策略参数口径以 decision register 为准。

## 1. 通道实测事实

**网关形态**：官方端点只暴露 3 个元工具——`help`（业务目录，分页/搜索）→ `search`（按 toolId 取完整入参 Schema + catalogVersion + schemaVersion）→ `action`（执行，信封必须携带 catalogVersion + schemaVersion + paramsJson）。handoff 会话所见"59 工具直暴"形态已不存在——DEC-009 判定为**能力漂移（网关化改版）**。

- 目录 `basic-open-online-20260825-v3`：**137 业务工具 = 116 读 + 21 写**；Amazon 广告写工具 15 个（SP 6 / SB 4 / SD 5），覆盖 SP/SB/SD × campaign/adGroup/target 全矩阵。
- **版本钉扎原生支持**：action 强制带 catalogVersion/schemaVersion——Capability Registry 的合同钉扎直接映射，版本漂移天然可检测（目录三天一版，漂移是常态不是意外）。
- **写入归因**：写工具信封强制 `module_name="Mcp"` 作为日志来源——领星操作日志可按来源区分 MCP 写入（DEC-005 对账利好）。
- **双层信封**：外层网关 `code` + 内层业务 `code/traceId`——错误分类要拆两层。
- **账户规模**：当前 key 可见 74 个已授权广告店铺、14 国（US 13 / CA·MX·DE·FR·IT·UK 各 6 / 其余欧澳巴）。广域 key 实锤（DEC-002/103 模式 3 证实），profile 级白名单授权不可省。
- **ID 双轨**：读工具用领星 `sid`，写工具用 Amazon `profile_id`；`ad_auth_shops` 返回两者映射。
- Schema 描述内嵌大量 LLM 护栏文案（"禁止传 null / 严禁猜测 / 仅当用户明确要求"）——领星预设调用方是 AI，护栏语义必须进我们的合同测试。

## 2. 多控制器现实（最重要的系统约束）

读侧 `ads_strategy` 字段暴露领星自有三个自动化工具与我们并存：**RuleEngine（自动规则）/ StepBudget（递增预算）/ TimingTactics（分时策略）**。其中分时策略会按时段**覆盖**竞价与预算——写工具因此带"基准值同步"语义（`is_base_value/is_base_price/uuid` 反推），且 schema 明文要求"仅当用户明确要求同时更新分时策略基准值才允许传入"。

**互斥原则（DEC-119 待 Owner 签）**：平台默认只接管**无领星策略托管**的对象（写前必查 `ads_strategy` 与 `is_apply_time`）；已托管对象上的写入会被领星策略引擎在下一周期覆盖或打架，属白写+振荡。接管带分时实体 = Owner 显式授权基准值同步，另立参数包。

## 3. 四控制面（实测旋钮 → 控制设计）

### 3.1 Campaign 控制

| 实测旋钮 | 工具 | 说明 |
|---|---|---|
| 状态 enabled/paused | `put_campaigns_sp/sb/sd` | 批量 1-1000 条/次 |
| 每日预算 `budget.budget` | 同上 | daily/lifetime 读侧可辨 |
| 广告位竞价加成 | `dynamicBidding.placementBidding[]` | 搜索顶部/其余位置/商品页/企业购；SPV 视频独立走 `creativeBidAdjustments` |
| 结束日期/站点限制/站外预算策略 | 同上 | endDate 空串=清除；站外 MAXIMIZE_REACH/MINIMIZE_SPEND |
| 读侧 | `ad_campaign_report` | 全指标区间筛选 + `only_over_budget` + `bidding_type`(动态双向/只降/固定) + `ads_strategy` 托管筛选 |

控制设计：策略动作限 **暂停失血 campaign / 预算再分配 / 广告位加成调整** 三类；每类绑定授权书参数包（单次幅度上限、日累计上限、批量条数上限——API 许 1000，授权书默认限 50）。

### 3.2 CPC / 竞价控制

竞价是**三层叠加**：campaign 层竞价策略与广告位加成 → 广告组默认竞价（`put_adGroups_sp`）→ target/keyword 独立竞价（`put_targets_sp`：`bid` 或 `use_default_bid=1` 回归继承）。实际扣费 CPC = bid × 动态竞价 × placement 加成，可放大数倍——**bid 不是 CPC 的直接旋钮，是它的下界杠杆**。

控制设计：调价自 target 层做起（粒度最细、爆炸半径最小）；步长上限（默认 ±15%/次）+ 冷却期（≥归因成熟窗，待实测校准）+ bid→CPC 放大率监控入目标函数报表。分时托管实体冻结不碰（§2）。

### 3.3 每日预算控制

旋钮即 3.1 的 budget 字段；控制设计三件套：
1. **超预算监控**：`only_over_budget=1` 定时轮询 → 告警/提案（读侧，最先上线）；
2. **预算再分配**：目标函数驱动（表现分位靠前的加、失血的减），授权书限定单次 ±20%、日总变动额上限、profile 白名单；
3. **月度包干**：授权书级总额约束由平台自行记账（领星无此概念），触顶自动停策略回到人。

### 3.4 关键词管理（加词 / 否词 / 类别）

| 动作 | 工具 | 实测要点 |
|---|---|---|
| 加词 | `post_keywords` | campaignId+adGroupId+keywordText+matchType(broad/phrase/exact) 必填；bid 省略=继承组默认；批量 1-1000 |
| 否词 | `post_targets_sp`（`negative=true`） | **广告组级**：KEYWORD/PRODUCT/PRODUCT_CATEGORY；**Campaign 级**：KEYWORD/PRODUCT（只传 campaignId）；否定不得带 bid |
| 词状态/竞价 | `put_targets_sp` | enabled/paused + bid |
| 类别投放 | `post_targets_sp/sb/sd` | targetType 五类（KEYWORD/LOCATION/PRODUCT/PRODUCT_CATEGORY/THEME）+ 自动投放四表达式 + SB 主题组 |
| 读侧 | `ad_campaign_keyword_report` / `search_term_report` / `targeting_report` | 搜索词→关键词→投放三报表齐备 |

控制设计：**已建的 NEG_EXACT 纵切与实测通道完全对齐**——证据门/集合冻结/Hash 审批/核验全部复用，执行从"CSV 人工"升级为 `post_targets_sp negative=true`（Gate 3 后）。第二策略=**搜索词收割闭环**：搜索词报告高转化词 → `post_keywords`（exact）+ 原组内否定同词防内竞——一次审批出两组写入，同一集合原子核验。类别投放（PRODUCT/CATEGORY）后置。

## 4. 落地序列（并入既有阶梯，不另起炉灶）

1. **P1 读侧**（现在可开工）：`ad_auth_shops` 种 profile 白名单 → 四报表读适配器 + 录制 fixture 合同测试 → PG 历史库入库（DEC-117）→ 超预算监控第一个上线（纯读+告警）。
2. **P2 提案**：预算再分配与调价以提案形态跑（人批 + 人执行/CSV），攒采纳率。
3. **P3 写 Canary**（Gate 3 授权后）：单对象否词写入全链路证据包（action→内层 code→操作日志 module=Mcp 归因→读回对账）；写合同测试补 DEC-004 空白（幂等/部分成功/CAS 无原生支持，以"读回验证"补偿）。
4. **P4 有界自动**：授权书扩执行边界，策略逐类解锁。

依赖与风险登记：批量部分成功语义未实测（DEC-004）；目录为 "basic" 版暗示存在更高权限目录；写限流/QPS 未实测；15 个广告写工具中 SB/SD 变体的护栏差异未逐一核对（仅 SP 全查）。

## 5. 任务层（DEC-120/121，2026-08-28 Owner 四条需求，内核已实施 `tasks/` + `authorization/custody.py`）

### 5.1 任务初始化（"优化 HX02"如何进系统）

任务（TaskEngagement）是一等域对象，生命周期只进不跳，每步产物入库可回溯：

```
DRAFT（建任务：产品焦点 focus="HX02"）
→ DIAGNOSED（诊断报告：读全部相关广告 + 窗口表现 → 每对象一条建议
   KEEP/PAUSE/ENABLE/ADJUST_BUDGET/ADJUST_BID/NEGATE_TERMS/HARVEST_TERMS，
   建议必附证据陈述；报告声明覆盖数与数据时刻，禁止静默截断）
→ PLANNED（人从建议中圈选成计划——计划条目必须能在报告找到出处，
   ACTION_NOT_RECOMMENDED 拒绝凭空动作；AI 不能批计划）
→ RUNNING（一次性动作走提案；持续策略签授权书绑定 engagement_id；
   每次调整挂 engagement_id 入历史）
→ CLOSED（人关闭，附原因）
```

诊断报告生成器依赖真实读通道，P1 接入后由四报表 + `erp_listing`（按 SKU/ASIN 检索）实现 focus 展开。

### 5.2 中途介入（AdjustmentDirective）

"选对象、说改什么、看预览、再批准"：ObjectSelector（显式 ID 列表或名称/指标筛选，二选一）+ AdjustmentIntent（PAUSE/ENABLE/SET·SCALE_DAILY_BUDGET/SET·SCALE_BID 白名单，相对幅度 ≤±50%）→ 展开为逐对象"现值 → 新值"预览（空命中= SELECTOR_MATCHED_NOTHING 显式报错；>200 对象 = SELECTOR_TOO_BROAD 要求收窄）→ 预览冻结后走既有 Hash 审批链。

### 5.3 数据回顾与存储（Owner 问"有没有数据库/向量数据库"）

**有，是 PostgreSQL，且已裁决为 P1 首工单（DEC-117）**：append-only 快照 + 双时间戳（采集时刻/数据源时刻）——"5 天前预算是多少"就是一条 SQL。调整历史由 Control Ledger 承担（每笔带 engagement_id、前值→新值、操作者、时刻——本来就是安全内核的预写审计）。任务/诊断报告/计划/指令同库入表。**向量数据库判定：当前不需要**——Owner 要的查询（历史值、调整记录、按产品聚合）全部是结构化时序查询，PG 是正确工具；向量库解决的是语义相似检索，此需求不存在，引入只添运维面。若未来做"相似投放案例参考"再评估（登记为非需求，不预建）。

### 5.4 人机互斥（ObjectCustody，四层控制权）

优先级：**TOOL_MANAGED（领星策略托管）> HUMAN_PRIORITY（人工优先）> AI_MANAGED（任务托管）> UNMANAGED（无主）**。

- 对 AI **硬约束**：任何 AI 动作前查 custody——非本任务托管一律 fail-closed（OBJECT_NOT_CLAIMED / OBJECT_CLAIMED_ELSEWHERE / OBJECT_HUMAN_PRIORITY / OBJECT_TOOL_MANAGED）。托管（claim）只能由人发起。
- 对人 **诚实的软约束**：人在领星/Amazon 后台的操作无法被平台实时阻止（那不是我们的界面）——只能经操作日志滞后检测；检测到带外人工变更即自动让位（HUMAN_PRIORITY，默认冷却 72h，DEC-121 待 Owner 定值），AI 停手；人显式接管则无限期直到归还。平台自己的界面上，AI 托管对象要求人先接管再改。
- 冷却过期不自动恢复 AI——必须由人重新托管（不存在"AI 等风头过了继续"）。

## 6. 对象工作台与本地镜像（V3 需求，2026-08-28 第二轮实测支撑）

Owner V3 需求：类领星清单界面全量展示广告对象、勾选布置任务、对象下载到本地并同步、策略包（预算+词+时段+解绑）。第二轮只读实测（证据 `docs/evidence/lx-v3-feasibility-20260828.md`）判定：

- **镜像数据源 = 四报表**（目录无独立对象接口）：出参实测携带对象现值——campaign 层 budget/bidding/state，组层 default_bid，target/keyword 层 bid/keyword_text/match_type/expression；`is_apply_time` 四层可得 → TOOL_MANAGED 打标随同步完成。
- **同步形态**：campaign_id 为可选筛选、profile_ids 收列表 → 按店铺分页全量拉，无需按 campaign 循环。量级（8 店 7 天：campaign 约 4.0 万 / keyword 约 8.5 万行）+ QPS=1 → **白名单店铺先行 + 只同步 enabled/paused + 增量同步**；每页末尾汇总行按 campaign_id 非空过滤。
- **新鲜度合同**：镜像只服务浏览与选择（行级 source_as_of 徽章）；执行判定永远以写前实时重读为准（既有 expected_before + 执行器门 3，不新增机制）。
- **勾选后端已在**：勾选清单 = `ObjectSelector.external_ids`；预览→冻结→Hash 审批链复用。缺的是层级清单 API/UI、跨层勾选容器（SelectionSet）与 engagement 的对象集入口（DEC-122）。
- **报价参数**：`erp_listing` 出参含售价/到手价/FBA 费/佣金/库存/销量窗口（三层信封 `data.data.data`，适配器按工具族区分）→ 保本 ACOS 可算，作策略包基准线。
- **策略包（StrategyBundle）**：编排层，引用 N 张授权书 + 共享退出条件（推荐形态，不动 DEC-113/114 的"一授权书一参数包"合同，DEC-124）；解绑三型：到期退出已有承载（valid_days≤30），止损/达标退出需新建 ExitGuard（DEC-125），拉排名方向被 DEC-118 挡；时段组件连带 DEC-119 二选一（平台自任时段控制器 vs 委托领星分时）。
- **合同测试实证**：同名入参跨工具类型漂移（with_ring 三态、length 两态）——参数编码逐工具按 schemaVersion 钉扎，禁止共享序列化。
