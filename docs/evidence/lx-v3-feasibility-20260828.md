# V3 引擎可行性只读实测（2026-08-28 第二轮）

对象工作台/本地镜像/策略包（Owner V3 需求）的通道可行性实测。全程只读；调用工具：`ad_auth_shops`、四报表、`erp_listing`。本文件零业务数值纪律：只记字段名、聚合计数与类型行为，不含店铺/产品/金额等业务数据。

## 1. 四报表出参携带对象现值（镜像数据源实锤）

跨 8 个 US 店铺、`report_date=2026-08-20 - 2026-08-27`、每报表抽样 1 页。逐行统计对象字段非空率（`非空行/出现行`；每页末尾含一行汇总行，对象字段为 null，属正常结构，同步器须按 `campaign_id` 非空过滤）：

| 字段 | campaign 报表 | group 报表 | targeting 报表 | keyword 报表 |
|---|---|---|---|---|
| campaign_id / campaign_name | 5/6 | 5/6 | 5/6 | 5/6 |
| state | 5/6 | 5/6 | 5/6 | 5/6 |
| budget（日预算现值） | **6/6** | 0/6 | 0/6 | 0/6 |
| bidding（竞价策略） | 5/6 | — | 4/6 | 5/6 |
| targeting_type | 5/6 | 5/6 | — | 5/6 |
| ad_group_id / ad_group_name | — | 5/6 | 5/6 | 5/6 |
| default_bid（组默认竞价） | 0/6 | 5/6 | 5/6 | 5/6 |
| bid（独立竞价现值） | 0/6 | 0/6 | **5/6** | **5/6** |
| keyword_id / keyword_text / match_type | — | — | — | **5/6** |
| expression（投放表达式） | — | — | 5/6 | 0/6 |
| is_apply_time（分时托管标志） | 5/6 | 5/6 | 5/6 | 5/6 |

结论：**本地对象镜像的数据源就是四报表**——预算在 campaign 层、组默认竞价在 group 层、独立竞价与词文本在 targeting/keyword 层齐备；`is_apply_time`（及入参侧 `ads_strategy` 筛选）四层可得 → custody 的 TOOL_MANAGED 打标可在同步时完成。目录中不存在独立"对象列表/详情"读工具，报表即唯一对象数据源。

出参侧另见：`timing_base_value`、`is_ad_group_apply_time`、`optimization_rule_id`、`entity_level_hash`、`serving_status`、`portfolio_id` 等字段存在于 keyword 报表行结构中（本次抽样为 null，语义待 P1 实连核实）。

## 2. 全量拉取形态与量级

- 四报表 `campaign_id` 均为**可选筛选**（required 仅 report_date/profile_ids/分页/排序），`profile_ids` 收列表（跨店铺）→ 全量下载按店铺分页拉，**无需按 campaign 逐个循环**。
- 量级（上述 8 店 7 天窗口的 total）：campaign 39,815 / group 40,113 / targeting 18,137 / keyword 84,742。`length` 默认 20、schema 未标 max。
- 推论：74 店全量周期同步不可行（QPS=1）；**白名单店铺先行 + state 筛选（enabled/paused，不同步 archived）+ 增量同步**是数据物理决定的设计，不是偏好。

## 3. 同名入参跨工具类型漂移（合同测试实证）

| 参数 | 工具 | 网关要求 | 实测报错 |
|---|---|---|---|
| with_ring | ad_campaign_group_report | integer | `boolean found, integer expected` |
| with_ring | ad_campaign_targeting_report | number | `boolean found, number expected` |
| length | ad_campaign_targeting_report | string | `integer found, string expected` |
| length | ad_campaign_report / keyword_report | integer | （int 通过） |

结论：**参数编码必须逐工具按其 schemaVersion 钉扎，禁止共享参数序列化逻辑**。网关校验严格（code=102 参数不合法，error_details 指明 JSON Pointer），fail-fast 行为良好。

## 4. erp_listing：报价与利润参数可得（三层信封）

- 信封结构与广告报表不同：`data.data.data`（外层网关 → 中层 open api `{msg,code,data,request_id}` → 内层 `{total, list}`）——**适配器须按工具族区分信封深度**。
- 必填 `offset/length/pvi_ids`，`pvi_ids` 收空串。
- 出参行字段（实测非空）：`listing_price`/`landed_price`/`price`/`regular_price` + 币种、`fba_fee`/`referral_fee`（FBA 费与佣金）、FBA 库存全套（`afn_fulfillable_quantity` 等 6 态 + reserved 3 态）、销量/花费窗口（`seven/fourteen/thirty_spend` 等）、`asin/parent_asin/msku/fnsku/rank/category_rank/status`。
- 结论：Owner 所称"现在的报价和其他参数"**可得**；且 FBA 费+佣金+售价齐备 → **保本 ACOS（毛利率基准线）可计算**，可作为策略包预算/竞价的目标函数基准。

## 5. 对 V3 三组件的判定

| 组件 | 判定 | 依据 |
|---|---|---|
| 类领星清单界面（数据源） | **可行** | §1 四报表现值齐备；custody 打标数据在报表内 |
| 本地下载+同步更新 | **可行，带边界** | §2 按店铺分页全量可拉；量级强制白名单+增量；镜像只服务浏览与选择，执行判定以实时读回为准（既有 expected_before+写前重读门） |
| 报价等 ERP 参数 | **可行** | §4 erp_listing 价格/费用/库存/销量齐备 |
| 策略包之时段组件 | **待裁决** | 撞领星 TimingTactics（DEC-119）；`is_apply_time` 可检测但控制器归属需 Owner 二选一 |
| 策略包之拉排名方向 | **被挡** | LAUNCH_RAMP/SALES_GROWTH 数据就绪条件未满足（DEC-118） |
