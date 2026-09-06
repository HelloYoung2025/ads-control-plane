# R2: Amazon Ads API（Sponsored Products）写操作语义调研

调研日期：2026-08-28。方法：仅 WebFetch/WebSearch 公开文档，无凭据。
说明：advertising.amazon.com/API/docs 是客户端渲染 SPA，直接抓取只返回页面壳；本调研经 r.jina.ai 渲染代理读取官方文档页，另以 amzn/ads-advanced-tools-docs 官方 GitHub 仓库（迁移 skill、Unified API spec、官方 Postman 集合）交叉验证。凡经渲染代理取得的内容均已注明；个别页面（campaign-management 数据集页）多次渲染失败，相关条目标 OPEN 或以次级来源补充。

重要背景：Amazon 已推出 Unified Ads API（`/adsApi/v1/*`，又称 unified Campaign Management API），2025-08-01 beta、2025-12-01 GA，宣称与既有 API 功能对等，把近 200 个端点变体收敛为 16 个操作，覆盖 SP/SB/DSP（SD/STV 后续）[https://advertising.amazon.com/API/docs/en-us/release-notes/ads-api, 2026-08-28]。SP v3 仍在服务，官方迁移材料未给出 SP v3 下线时间表（标注"Sunset deadline"为待定优先级）[https://github.com/amzn/ads-advanced-tools-docs/tree/main/unified-campaign-management-migration-skills, 2026-08-28]。控制平面设计需同时考虑两套 API。

---

## A. 写操作 API 形态（预算更新 / bid 更新 / batch 语义）

**SP v3（现行）**
- 更新 campaign：`PUT /sp/campaigns`；更新 keyword：`PUT /sp/keywords`；更新 target：`PUT /sp/targets`；list 用 `POST /sp/campaigns/list`，删除用 `POST /sp/campaigns/delete`。请求体为对象数组（wrapper 字段 `campaigns` / `keywords`）[https://github.com/amzn/ads-advanced-tools-docs/blob/main/unified-campaign-management-migration-skills/skills/unified-sp-migration/SKILL.md, 2026-08-28]。
- 头部：按资源版本化的 vendor media type（如 `Content-Type/Accept: application/vnd.spCampaign.v3+json`），可选 `Prefer` 头（迁移对照表列明 SP v3 有 `Prefer`、Unified API 无）[同上; media type 亦见官方 Postman 集合 https://github.com/amzn/ads-advanced-tools-docs/blob/main/postman/Amazon_Ads_API.postman_collection.json, 2026-08-28]。
- **partial update：官方明确支持**。开发者笔记：更新实体唯一必填属性是 ID，允许只提供属性子集，其余属性保持不变（paraphrase）[https://advertising.amazon.com/API/docs/en-us/reference/concepts/developer-notes, 2026-08-28]。即：只改 budget 或只改 bid 的最小请求是一等公民。
- **batch 语义**：官方明确"批量 update/create 操作是非原子的"（non-atomic）[developer-notes, 同上]。SP v3 响应为逐项 multi-status：`{"campaigns": {"success": [{campaignId, index}], "error": [{index, errors:[...]}]}}`，以 `index` 对应请求数组位置 [unified-sp-migration SKILL.md, 2026-08-28]。
- 批量还有隐式语义（风险点）：批量 update 中无 ID 且含创建必需属性的条目会被解释为"创建新实体"；含 ID 且 state=archived 即归档（paraphrase）[developer-notes, 同上]。→ 更新请求若丢失 ID 字段，可能变成意外创建。
- SP v3 单请求最大对象数：OPEN（普遍认为 1000/请求，但本次未能从可引用的公开镜像验证 SP v3 spec 的 maxItems；全量 OpenAPI JSON https://dtrnk0o2zy01c.cloudfront.net/openapi/en-us/dest/SponsoredProducts_prod_3p.json 体积过大，工具截断）。旁证：Unified SP spec 中 multi-status 响应 success/error 数组 maxItems=1000、index 取值 0–999 [https://github.com/amzn/ads-advanced-tools-docs/blob/main/unified-campaign-management-migration-skills/api-specs/unified-api-sp.json, 2026-08-28]。

**Unified API（/adsApi/v1，GA）**
- 全部写操作为 POST：`POST /adsApi/v1/update/campaigns`、`POST /adsApi/v1/update/targets`（keyword 并入 target，`targetType: KEYWORD`）、create/delete/query 同构；不再有 PUT [unified-sp-migration SKILL.md, 2026-08-28]。
- 头部：`Content-Type: application/json`（放弃 vendor media type）、`Amazon-Ads-ClientId`、可选 `Amazon-Ads-AccountId` 与 `Amazon-Advertising-API-Scope` [同上; 官方 Unified Postman 集合, https://github.com/amzn/ads-advanced-tools-docs/blob/main/postman/Amazon_Ads_Unified_API.postman_collection.json, 2026-08-28]。
- 更新请求：`SPCampaignUpdate` 仅 `campaignId` 必填，其余（budgets、state、name、optimizations…）可选 → 同样是 partial update [unified-api-sp.json, 2026-08-28]。bid 更新：`{"targets":[{"targetId":"...","bid":{...}}]}` [Unified Postman 集合, 2026-08-28]。
- 响应：HTTP **207**，顶层 `success[]` / `error[]`（Postman 示例另含 `partialSuccess[]`），逐项带 `index`；错误项 `ErrorsIndex = {index, errors:[{code(ErrorCode 枚举), message, fieldLocation}]}` [unified-api-sp.json + Unified Postman 集合, 2026-08-28]。
- 批量上限（官方迁移 skill 给出的操作限额）：campaigns/adGroups/ads 每请求 create/update/delete 各 **10**；targets 每请求 **1000**；query maxResults：campaigns 100 / targets 5000 [unified-sp-migration SKILL.md, 2026-08-28]。注意与 spec 数组 maxItems=1000 的张力：schema 上限≠操作限额，以迁移 skill 的限额为准（OPEN：官方 docs 页面的权威数字未能直接读取）。

## B. 服务器端幂等键与重试建议

- **不存在幂等键**。在 Unified SP spec、官方 Postman 集合（两套）、迁移 skill、developer-notes、common model 中均未发现 idempotency key / clientRequestToken / requestToken 类字段或头 [unified-api-sp.json; Amazon_Ads_Unified_API.postman_collection.json; developer-notes, 均 2026-08-28]。Postman 分析明确：无 `Idempotency-Key` 类头部。
- 官方对幂等的表述（developer-notes，paraphrase）：所有操作在"最终服务器状态"意义上幂等——同一操作重复执行，最终状态不变；但**重复创建会在第二次请求返回错误**；报表请求会被记忆化，重复请求返回相同 report ID [developer-notes, 2026-08-28]。
  - 解读：update 的"幂等"来自**绝对值写入语义**（set budget=X，而非 increment），不是服务端去重。对 update 而言重放安全（收敛到同值）；对 create 而言重放不安全但会报错（错误即信号）；**超时后盲重试 create 需以命名唯一性/错误码做对账**。
- 重试建议（官方）：429 响应含 `Retry-After` 头（值为应等待的秒数，paraphrase）；建议指数退避（2s→4s→8s…），设最大重试次数与最大间隔 [https://advertising.amazon.com/API/docs/en-us/reference/concepts/rate-limiting, 2026-08-28]。已知例外：`POST /reporting/reports` 被限流时**不带** Retry-After（官方仓库 issue 报告，文档与实测不符）[https://github.com/amzn/ads-advanced-tools-docs/issues/344, 2026-08-28]。

## C. 版本令牌 / ETag / 乐观并发（CAS）

- **未发现任何并发控制机制**：
  - developer-notes 通篇不提 conflict / concurrent write / versioning / ETag / last-write-wins（专门提问核查过）[developer-notes, 2026-08-28]。
  - campaign 公共模型无 version/revision 字段 [https://advertising.amazon.com/API/docs/en-us/reference/common-models/campaigns, 2026-08-28]。
  - Unified SP spec 无 If-Match/ETag/version 参数或头 [unified-api-sp.json, 2026-08-28]。
  - 两套官方 Postman 集合的请求头中无条件请求头 [postman 目录, 2026-08-28]。
- 结合"仅 ID 必填的 partial update + 绝对值语义 + 批量非原子"，实际行为只能是 **last-write-wins（字段级）**：两个并发调用者各改不同字段可互不覆盖，改同一字段则后写胜，且无任何冲突信号返回。
- OPEN：官方从未用"last-write-wins"字样描述；无 CAS 是"文档与规范中彻底缺席"的强推断，非官方明示。另 SP v3 全量 spec 未能逐行排查（体积截断），存在极小的遗漏可能。

## D. 写后读取一致性 & 变更历史 / 归因证据

- 同步性：官方 limits 页给出 Synchronous CRUD operations 的 P99 保证为 30s（指调用完成延迟，非读一致性承诺）[https://advertising.amazon.com/API/docs/en-us/reference/concepts/limits, 2026-08-28]。
- **写后读一致性无官方承诺**：developer-notes 与 rate-limiting 页均不涉及 entity 读取的一致性/缓存；仅警告分页期间数据变化会导致重复/漏读（说明 list 读的是活动数据集）[developer-notes, 2026-08-28]。OPEN：GET/list 是否即时反映刚完成的写。工程上应以**写响应本身**（SP v3 `Prefer: return=representation` 取回实体 / Unified 207 success 内含 campaign 对象）作为第一手回读，而非依赖随后的 list。
- 注意区分：报表/指标链路明确是最终一致（数据可 72h 内补齐、无效点击事后重述）[https://docs.openbridge.com/en/articles/5575078-amazon-advertising-console-vs-advertising-api（次级）, 2026-08-28]——这是 metrics，不是 entity 配置读取。
- **Change history API（beta）**：`POST` 查询式接口；参数 `fromDate`/`toDate`（UTC epoch，最多 90 天）、`eventTypes`（带 filters/parents）、`count`（50–200）、`nextToken`/`pageOffset`（≤10000 条）；返回记录含 `entityId`、`entityType`（如 AD）、`changeType`（如 BID_AMOUNT）、`previousValue`、`newValue`、`timestamp`、`metadata`（campaignId/adGroupId 等）。**官方明确：不返回是谁做的变更**；仅覆盖 SP 与 SB（不含 SD）；仅能查询已授权 advertiser 自身事件 [https://advertising.amazon.com/API/docs/en-us/change-history, 2026-08-28]。
- **Amazon Marketing Stream**（push 到订阅方 AWS：SQS/Firehose）：
  - 实体变更数据集：`campaigns`、`adgroups`、`ads`、`targets`（campaign management datasets），近实时推送，**只推送发生变更的实体**（非全量快照流）[官方页 https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-stream/datasets/campaign-management（存在但本次渲染失败）；数据集清单旁证：官方参考实现 https://github.com/amzn/amazon-marketing-stream-examples；次级：https://docs.openbridge.com/en/articles/8065598-amazon-marketing-stream-connector、https://tinuiti.com/blog/amazon/amazon-marketing-stream/，均 2026-08-28]。消息是否含 audit/actor 字段：所查来源均未记载 → OPEN（倾向无）。
  - `budget-usage` 数据集：字段含 `budget_scope_id`、`budget_scope_type`（CAMPAIGN/PORTFOLIO）、`budget`、`budget_usage_percentage`、`usage_updated_timestamp`；每消耗 5% 推一次，近实时，可能因流量校验回退产生重复消息 [https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-stream/datasets/budget-usage, 2026-08-28]。
  - 另有 `sp-traffic`/`sp-conversion`（小时级）、`sponsored-ads-campaign-diagnostics-recommendations` 等 [amazon-marketing-stream-examples, 2026-08-28]。
- 附：docs 中的 "Events API (beta)"（`POST /adsApi/v1/create/events`）是**转化事件上报**接口，与变更审计无关，勿混淆 [https://advertising.amazon.com/API/docs/en-us/guides/events/events, 2026-08-28]。
- **归因结论：全生态没有任何官方通道能把一次实体变更归因到具体 API 调用者/用户**（change history 明示不含 actor；stream 实体消息未记载 actor 字段）。

## E. 限流

- 动态限流：官方不公布固定 TPS；限流随系统整体负载动态发生；报表另按区域分层、依报表队列长度调节（paraphrase）[rate-limiting, 2026-08-28]。
- 429 + `Retry-After`（秒）；建议指数退避 + 最大重试上限 [rate-limiting, 2026-08-28]。`POST /reporting/reports` 的 429 无 Retry-After（社区实测，官方 issue 在案）[issues/344, 2026-08-28]。
- 维度：文档仅明确"按区域"（报表）与全局动态；**未见 per-profile/per-account 的官方承诺** → OPEN（社区经验普遍按 advertiser/profile 维度观测到差异，未见官方背书）。
- 成本权重：extended data 操作为标准调用 5 倍权重；官方建议避免全量拉取实体、错峰报表 [rate-limiting, 2026-08-28]。

## F. 测试环境

- 现行方案是 **test accounts**（不是 sandbox）：专用测试广告账户；不投放广告、无有效计费；SP 创建的广告不会展示给购物者，因此无 clicks/impressions/spend 等表现数据，报表能生成但无数据；创建需额外 scope `advertising::test:create_account`；不支持 Stores/Posts/锁屏广告，素材仅 IMAGE [https://advertising.amazon.com/API/docs/en-us/guides/account-management/test-accounts/overview, 2026-08-28]。配套页：create-test-accounts、use-test-accounts [搜索结果确认存在, 2026-08-28]。
- 旧 v2 sandbox（advertising-api-test 端点、registerProfile 注册测试 profile）只存在于已弃用的 SDK 文档中（PHP SDK 仓库整体标注 DEPRECATED）[https://github.com/amzn/amazon-advertising-api-php-sdk, 2026-08-28]。OPEN：官方未见"v3 sandbox 已移除"的正式声明，但现行 v3/Unified 文档体系中 sandbox 不再出现，测试路径即 test accounts。
- 含义：**无法在测试环境验证任何与真实竞价/花费相关的行为**（预算耗尽、CPC、投放），只能验证 CRUD 语义与错误码。

## G. Bid 语义（configured bid / dynamic bidding / placement adjustment / 实际 CPC）

- 官方对 bid 字段的定义（Unified SP spec 原文短语）："The maximum bid for a target" [unified-api-sp.json, 2026-08-28]。即 configured bid 是竞价上限输入，非成交价。
- Dynamic bidding（campaign 级策略，API 中位于 campaign 的 bidding/optimizations 设置；SP v3 枚举如 LEGACY_FOR_SALES，Unified 改名如 SALES_DOWN_ONLY [unified-sp-migration SKILL.md, 2026-08-28]）：
  - up-and-down：Amazon 对更可能转化的展示实时提价、更不可能转化的降价；现行幅度为**所有 placement 最高 ±100%**（官方举例：$1 bid 最高可加到 $2）[https://advertising.amazon.com/library/guides/dynamic-bidding-sponsored-products, 2026-08-28]。（历史口径"top-of-search 最高 +100%、其余 +50%"仍流传于次级来源 [perpetua.io 等, 2026-08-28]，官方 library 指南已是"全部 placement 100%"。）
  - down-only：只降不升（降幅至多 100%）[次级来源汇总, 2026-08-28]；fixed：不调整。
- Placement adjustment（"Adjust bids by placement"）：top-of-search / rest-of-search / product pages 三个 placement 均可上调**至多 +900%**；适用于所有 targeting 类型与所有 bidding 策略 [https://advertising.amazon.com/resources/whats-new/improve-campaign-performance, 2026-08-28]。
- 叠加顺序：次级来源一致表述为"先对 base bid 应用 placement 百分比，再在其上应用 dynamic bidding 调整"[scaleinsights/sellerlabs 等, 2026-08-28]；官方页未给出公式 → 顺序与联合公式标 OPEN（官方未明示）。按此口径理论最大有效出价 = bid × (1+900%) × (1+100%) = bid × 20。
- bid 与 average CPC：官方文档**未**声明第二价拍卖；"实际支付比次高出价高 $0.01"之说全部来自第三方 [搜索核查：无 Amazon 官方来源, 2026-08-28]。工程上唯一可依赖的官方语义：bid 是上限，但在 up-and-down 下**实际 CPC 可超过 configured bid（至其 2 倍）**；报表中的 avg CPC 与 configured bid 之间不存在官方给定的函数关系 → 用 CPC 反推 bid 生效与否不可靠，标 OPEN。

## H. OAuth / 权限模型（profiles 粒度）

- 授权采用 Login with Amazon（LWA）OAuth 2.0 授权码流程。scope 粗粒度：`advertising::campaign_management` 一个 scope 覆盖 SP/SB/SD/DSP/Amazon Attribution 的 API 访问；`advertising::audiences` 用于 Data Provider API；`advertising::test:create_account` 用于建测试账户；2020-10 之前的旧 client 用 `cpc_advertising:campaign_management`。**没有按功能/按实体的细粒度 scope** [https://advertising.amazon.com/API/docs/en-us/guides/get-started/create-authorization-grant, 2026-08-28]。
- token 属于**用户**（advertiser 登录身份）：同一登录可管理多个账户；几乎所有请求必须以 `Amazon-Advertising-API-Scope` 头传 profile id 选定作用账户，缺失/错误则 401/400（paraphrase）[https://advertising.amazon.com/API/docs/en-us/guides/account-management/authorization/profiles, 2026-08-28]。
- profile = 某 marketplace 中的一个 advertiser account（对应 console 里的 account）；类型 seller/vendor/agency（agency 限 DSP/Data Provider）[同上]。
- Unified API 增加 `Amazon-Ads-AccountId` 头（全局 account 体系），`Amazon-Advertising-API-Scope` 变为可选 [unified-sp-migration SKILL.md; Unified Postman 集合, 2026-08-28]。
- 粒度结论：**授权粒度 = 用户 × 粗 scope；请求粒度 = profile（账户/marketplace）**。无法为单个 token 限定到 campaign/portfolio 级；API 侧的实际权限取决于该用户在 ads console 的账户角色（另有 user-permissions 管理 API 管理控制台用户权限 [https://advertising.amazon.com/API/docs/en-us/user-permissions, 2026-08-28]）。多租户控制平面的最小隔离单元是 profile/account，campaign 级隔离必须自建。

---

## 对"远端无 CAS"假设的裁决

**裁决：支持（强）。**
证据：(1) 官方 developer-notes 系统性描述了幂等（最终状态意义）、partial update（仅 ID 必填、未提供字段不变）、批量非原子，却对并发冲突、版本、ETag 只字未提 [developer-notes, 2026-08-28]；(2) campaign 公共模型无版本字段 [common-models/campaigns]；(3) Unified SP spec 与两套官方 Postman 集合中无 If-Match/ETag/幂等键/条件请求头 [unified-api-sp.json; postman]；(4) 写接口是"按 ID 的字段级绝对值覆盖 + 逐项 multi-status"，无任何冲突信号通道。因此远端语义为字段级 last-write-wins，无服务端 CAS、无服务端请求去重（create 例外：重复创建报错，可当弱幂等信号）。
保留：SP v3 全量 spec 因体积未逐行排查；"LWW"是官方缺席下的推断而非官方措辞；Unified API 仍在迭代，未来可能引入并发原语。当前把"远端无 CAS/无幂等键"作为设计公理是安全的。

## 对权威回读与归因设计的含义

回读：写后一致性无官方承诺，权威回读的第一凭证应是**写响应本身**（SP v3 用 `Prefer: return=representation`；Unified 207 的 success[index] 内含实体），随后 GET/list 只做对账不做判定；分页 list 有重复/漏读风险，须按 ID 去重。归因：**没有任何官方通道给出变更 actor**——change history API 明示不返回操作者（且仅 SP/SB、90 天、beta），Marketing Stream 实体数据集只近实时推送"变了什么"（变更实体、前后值可由 change history 补充），不含调用方。因此"把某次变更归因到某调用者"只能靠自建 write-ahead log（记录 request、index、响应、时间戳）与 change-history/stream 记录做时间窗 + 字段前后值匹配的相关性归因；无法匹配到自有写日志的变更即判为外部写（console/其他工具），这是检测并发覆盖的唯一手段。budget 消耗监听用 budget-usage（5% 步进、可能重复投递，需幂等消费）。

## 关键来源清单

- developer-notes（幂等/partial update/非原子）: https://advertising.amazon.com/API/docs/en-us/reference/concepts/developer-notes [2026-08-28]
- rate-limiting: https://advertising.amazon.com/API/docs/en-us/reference/concepts/rate-limiting [2026-08-28]
- limits: https://advertising.amazon.com/API/docs/en-us/reference/concepts/limits [2026-08-28]
- campaign 公共模型: https://advertising.amazon.com/API/docs/en-us/reference/common-models/campaigns [2026-08-28]
- change history (beta): https://advertising.amazon.com/API/docs/en-us/change-history [2026-08-28]
- test accounts: https://advertising.amazon.com/API/docs/en-us/guides/account-management/test-accounts/overview [2026-08-28]
- profiles: https://advertising.amazon.com/API/docs/en-us/guides/account-management/authorization/profiles [2026-08-28]
- authorization grant/scopes: https://advertising.amazon.com/API/docs/en-us/guides/get-started/create-authorization-grant [2026-08-28]
- Marketing Stream budget-usage: https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-stream/datasets/budget-usage [2026-08-28]
- Marketing Stream campaign-management（页面存在，未能渲染）: https://advertising.amazon.com/API/docs/en-us/guides/amazon-marketing-stream/datasets/campaign-management [2026-08-28]
- Unified API v1 release notes: https://advertising.amazon.com/API/docs/en-us/release-notes/ads-api [2026-08-28]
- 官方迁移 skill（SP v3↔Unified 对照）: https://github.com/amzn/ads-advanced-tools-docs/blob/main/unified-campaign-management-migration-skills/skills/unified-sp-migration/SKILL.md [2026-08-28]
- Unified SP OpenAPI spec: https://github.com/amzn/ads-advanced-tools-docs/blob/main/unified-campaign-management-migration-skills/api-specs/unified-api-sp.json [2026-08-28]
- 官方 Postman 集合（v3 与 Unified）: https://github.com/amzn/ads-advanced-tools-docs/tree/main/postman [2026-08-28]
- Retry-After 缺失 issue: https://github.com/amzn/ads-advanced-tools-docs/issues/344 [2026-08-28]
- Stream 参考实现（数据集清单）: https://github.com/amzn/amazon-marketing-stream-examples [2026-08-28]
- dynamic bidding 官方指南: https://advertising.amazon.com/library/guides/dynamic-bidding-sponsored-products [2026-08-28]
- rest-of-search placement 官方公告: https://advertising.amazon.com/resources/whats-new/improve-campaign-performance [2026-08-28]
- Events API（转化事件，非审计）: https://advertising.amazon.com/API/docs/en-us/guides/events/events [2026-08-28]
- 次级来源（标注为次级）: tinuiti.com、docs.openbridge.com、perpetua.io、scaleinsights.com [2026-08-28]

## OPEN 汇总

1. SP v3 单请求批量上限（推测 1000，未获可引用官方原文）。
2. Unified API 各操作批量限额的官方 docs 原文（现引官方迁移 skill：campaigns 10 / targets 1000）。
3. entity GET/list 的写后读一致性（无官方表述）。
4. Marketing Stream 实体数据集消息是否含 actor/audit 字段（未见记载，倾向无）。
5. 限流是否存在 per-profile/per-account 维度（官方只说动态+区域）。
6. placement 调整与 dynamic bidding 的官方叠加公式（次级来源：先 placement 后 dynamic）。
7. 实际 CPC 与 bid 的关系（官方无第二价表述；up-and-down 下 CPC 可达 bid×2）。
8. SP v3 正式弃用时间表（未公布）。
