# R1 调研报告：领星(LingXing) MCP 服务与开放平台 API

- 调研日期：2026-08-28
- 方法：仅 WebFetch/WebSearch 公开网页，未连接任何 MCP 服务器、未使用任何凭据、未登录任何系统。
- 说明：apidoc.lingxing.com 为 docsify 风格 SPA，正文通过直接抓取其 markdown 源文件（如 `/_sidebar.md`、`/docs/**/*.md`）获得，内容为官方文档原文。

---

## 1. 领星 MCP 服务（官方）

### 1.1 接入方式与协议
- 传输方式为 Streamable HTTP（HTTP Streaming），服务器 URL 使用 https 协议。[https://www.lingxing.com/help/article/mcp, 2026-08-28]
- 官方服务器 URL：`https://openmcp.lingxing.com/mcp-servers/lingxing-mcp`。[https://www.lingxing.com/help/article/mcp, 2026-08-28]
- 官方配置示例（可直接用于 Cursor/Claude 等支持自定义 MCP 的客户端）：

```json
{
  "mcpServers": {
    "lingxing-mcp": {
      "type": "streamableHttp",
      "url": "https://openmcp.lingxing.com/mcp-servers/lingxing-mcp",
      "headers": { "X-Mcp-Key": "<鉴权密钥>" }
    }
  }
}
```
[https://www.lingxing.com/help/article/mcp, 2026-08-28]
- 支持接入任何支持自定义 MCP 工具的 AI 平台（Cursor、ChatGPT、Claude、Gemini、Trae、通义千问等），配置方法通用。[https://www.lingxing.com/help/article/mcp, 2026-08-28]
- 开通前提：管理员在【业务配置】-【开放接口】-【MCP】中启用 MCP 功能，仅限付费用户。[https://www.lingxing.com/help/article/mcp, 2026-08-28]

### 1.2 凭据模式
- 鉴权方式：请求头 `X-Mcp-Key` 携带鉴权密钥。[https://www.lingxing.com/help/article/mcp, 2026-08-28]
- 绑定对象：原文"鉴权密钥与当前领星帐号绑定"——密钥绑定**个人用户帐号**（非企业级应用凭据），不同帐号获取的密钥可查询范围不同。[https://www.lingxing.com/help/article/mcp, 2026-08-28]
- 权限继承：原文"该链接仅有当前领星帐号的数据权限，即通过该URL查询到的数据范围与当前登录帐号在ERP中的权限一致"——完全继承该用户在 ERP 中的店铺/功能权限，无独立的密钥级权限配置。[https://www.lingxing.com/help/article/mcp, 2026-08-28]
- 轮换/吊销：原文"如果密钥泄露，可在【管理MCP】页面重新生成"——支持重新生成（即轮换，旧密钥随之作废）。文档未提及独立的"吊销但不重发"操作。[https://www.lingxing.com/help/article/mcp, 2026-08-28]
- 文档**未提及**：一个帐号创建多个密钥、只读密钥、按店铺/Profile 限权的密钥、密钥级访问日志。（见 OPEN 清单）

### 1.3 工具清单（官方帮助页列出）
读工具（约 13 类），广告相关全部为只读报告查询：
- 店铺：`get_my_sids`；库存：`get_fba_stock_list`；Listing：`erp_listing`；销售：`query_product_performance_asin_lists`；利润：`get_profit_report_msku`、`query_order_profit_list_gross_profit`；关键词排名：`query_erp_keyword_ranking_keyword`、`query_erp_keyword_ranking_asin`；竞品/跟卖/店铺监控：`query_erp_competitive_monitor`、`query_erp_follow_sale_monitor`、`query_erp_new_monitor`；广告：`ad_campaign_report`、`ad_campaign_keyword_report` 等共 8 个广告报告工具；自定义报表/指标：`get_custom_report_list` 等。[https://www.lingxing.com/help/article/mcp, 2026-08-28]

写工具（5 个，均与广告无关）：
- `create_erp_keyword`（关键词排名监控）、`create_erp_competitive_monitor`（竞品监控）、`create_erp_follow_sale_monitor`（跟卖监控）、`create_erp_new_monitor`（店铺监控）、`add_custom_indicator` / `update_custom_indicator`（自定义指标）。[https://www.lingxing.com/help/article/mcp, 2026-08-28]
- **结论：官方 MCP 当前不提供任何广告写操作工具（无改预算/出价/状态工具），也未提及 dry-run/preview 机制。**[https://www.lingxing.com/help/article/mcp, 2026-08-28]

### 1.4 限流与数据
- 原文"每个Tool的调用频率限制（QPS）为 1"；"目前无查询次数限制"（仅限频率）。[https://www.lingxing.com/help/article/mcp, 2026-08-28]
- 数据实时性：原文"数据与ERP页面数据一致，实时同步"。[https://www.lingxing.com/help/article/mcp, 2026-08-28]
- 文档未提及 MCP 侧访问日志。

### 1.5 第三方开源 MCP（旁证，非官方）
- GitHub 项目 zach22-1999/lingxing-mcp（MIT），自述为第三方；基于领星 OpenAPI 凭据（IP 白名单 + API 帐号 + AppId + AppSecret），提供 `lingxing_ad_accounts`、`lingxing_ads_sp_*`、`lingxing_ads_sd_*`、`lingxing_ads_sb_*`、小时级广告报表等工具；原文"当前公开版聚焦只读能力"。[https://github.com/zach22-1999/lingxing-mcp, 2026-08-28]
- 含义：开放平台 API 足以支撑自建 MCP/网关层，官方 MCP 并非唯一路径。

---

## 2. 领星开放平台 REST API（apidoc.lingxing.com / openapi.lingxing.com）

### 2.1 申请与凭据
- 网关域名：`https://openapi.lingxing.com`。[https://apidoc.lingxing.com/docs/Guidance/newInstructions.md, 2026-08-28]（注：openapi.lingxing.com 根路径直接访问返回 403，属正常——它是 API 网关而非门户页。[https://openapi.lingxing.com/, 2026-08-28]）
- AppID/AppSecret 查看：需超级管理员帐号登录 ERP，【设置】>【业务配置】>【全局】>【开放接口】进入开放后台，【API信息】中查看。[https://apidoc.lingxing.com/docs/Guidance/AppID.md, 2026-08-28]
- **权限范围为企业全量**：原文"获取该AppID和AppSecret将有权限访问企业全部数据"——不支持按接口/模块/店铺细分授权。[https://apidoc.lingxing.com/docs/Guidance/AppID.md, 2026-08-28]
- AppSecret 可【重置】："生成新的AppSecret后，原AppSecret将立即失效"，需超级管理员操作（硬切换，无双密钥灰度期）。[https://apidoc.lingxing.com/docs/Guidance/AppID.md, 2026-08-28]
- 必须配置外网 IP 白名单，"IP白名单不支持域名形式"；凭据与白名单"二者缺一不可"。[https://apidoc.lingxing.com/docs/Guidance/AppID.md; /docs/Guidance/newInstructions.md, 2026-08-28]
- 申请通道：邮件 openapi@lingxing.com，2-5 个工作日审核（第三方集成商记录，非官方文档原文）。[https://www.qeasy.cloud/article/lingxing-erp-authorizations-522, 2026-08-28]

### 2.2 鉴权与签名
- 获取 token：POST `https://openapi.lingxing.com/api/auth-server/oauth/access-token`，参数 appId/appSecret；响应含 `access_token`、`refresh_token`、`expires_in: 7199`（约 2 小时）。[https://apidoc.lingxing.com/docs/Authorization/GetToken.md, 2026-08-28]
- 续约：原文"refresh_token的有效期为2个小时，一个refresh_token只能被使用一次"，续约接口每次返回新 refresh_token。[https://apidoc.lingxing.com/docs/Guidance/newInstructions.md, 2026-08-28]
- 每次业务请求需带签名 sign：参数按 ASCII 排序拼接 `key1=value1&key2=...`（空值不参与、null 参与）→ "用MD5(32位)加密后转大写" → "用AES/ECB/PKCS5PADDING对生成的MD5值加密，其中AES加密的密钥为appId" → URL 编码；"sign的有效期为2分钟"。[https://apidoc.lingxing.com/docs/Guidance/newInstructions.md, 2026-08-28]
- URL 仅可携带 appId、token、timestamp、sign 四参数，业务参数放 body；时间戳须 10 位。[https://apidoc.lingxing.com/docs/Guidance/QA.md, 2026-08-28]

### 2.3 限流
- 采用"改进的令牌桶算法"，限流维度为 "appId + 接口url"；令牌不足返回错误码 3001008。[https://apidoc.lingxing.com/docs/Guidance/newInstructions.md, 2026-08-28]
- 每个接口文档标注自己的令牌桶容量，实测样例：读接口 SP广告活动 `/pb/openapi/newad/spCampaigns` 容量为 10；写接口 修改SP关键词 `basicOpen/adReport/manage/putSpKeyword` 容量为 1。[https://apidoc.lingxing.com/docs/newAd/baseData/spCampaigns.md; /docs/newAd/adReportManagePutSpKeyword.md, 2026-08-28]

### 2.4 全局错误码（官方全表）
| 错误码 | 含义 |
|---|---|
| 2001001 | app not exist |
| 2001002 | app secret not correct |
| 2001003 | access token is missing or expire |
| 2001004 | the api not authorized, please grant first |
| 2001005 | access token not match |
| 2001006 | api sign not correct |
| 2001007 | api sign has expired |
| 2001008 | refresh token expired. please get access token again |
| 2001009 | refresh token is invalid |
| 3001001 | missing query param(access_token,sign,timestamp,app_key) |
| 3001002 | ip not permit, please add ip to white list first |
| 3001008 | requests too frequently. please request later |

[https://apidoc.lingxing.com/docs/Guidance/ErrorCode.md, 2026-08-28]
- 注意 2001004 表明存在**接口级授权开关**（"联系平台工作人员授权"），即企业 App 可被平台侧按接口授权/未授权——与 2.1 的"企业全部数据"并存：数据范围全量，但接口可用集合需平台开通。

### 2.5 广告 API 覆盖（来自官方 _sidebar.md 完整目录）
**读接口（Amazon）非常全**：SP/SB/SD 三类的活动/广告位/广告组/商品/关键词/投放/搜索词/已购商品等 20+ 报表接口；SP/SB/SD 活动、广告组、投放等小时级数据接口（来源标注 Amazon Marketing Stream："小时数据来源于Amazon Marketing Stream…因亚马逊会修正数据，小时和天数据可能会存在差异"）；DSP 广告主与订单报告；广告基础数据（组合 portfolios、SP/SB/SD 活动、广告组、商品、关键词、投放、各类否定）；ABA 搜索词周报；另有 TikTok/Walmart/Shopee/Lazada 多平台广告查询。[https://apidoc.lingxing.com/_sidebar.md; /docs/newAd/baseData/spCampaigns.md, 2026-08-28]

**写接口（"广告管理"模块）——仅覆盖 SP**：
- 修改SP广告活动和广告位（adReportManagePutSpCampaign）
- 修改SP广告组（adReportManagePutSpAdGroup）
- 修改SP关键词（adReportManagePutSpKeyword）
- 修改SP商品投放（adReportManagePutSpTarget）
- 修改广告商品状态（adReportManagePutSpProductAds）
- 添加SP关键词（SpAddKeywords）、添加SP否定关键词（SpAddNegativeKeywords）、添加SP否定商品（SpAddNegativeTargets）、归档SP否定投放（SpArchiveNegatives）
- 目录中**无 SB/SD 写接口**。[https://apidoc.lingxing.com/_sidebar.md, 2026-08-28]

写接口语义样例（修改SP关键词）：POST `basicOpen/adReport/manage/putSpKeyword`；可改 `state`（启用/暂停）、`bid`（竞价）及基准值参数（isBaseValue/baseType/baseValue）；`keywords` 为数组支持批量；响应 `apiResult` 逐条返回 code 与 keywordId（SUCCESS / entityStateError 等）；**文档无幂等键、无异步任务号、无操作人参数的说明**。[https://apidoc.lingxing.com/docs/newAd/adReportManagePutSpKeyword.md, 2026-08-28]

### 2.6 广告操作日志 API（对账关键）
- "操作日志（新）"：POST `/pb/openapi/newad/apiLogStandard`；必填 sid、sponsored_type(sp/sb/sd)、operate_type（如 campaigns）、start_date/end_date（"日期间隔不能超过一个月"）；响应含 `user_name`、`change_type`（create/update）、`operate_before`、`operate_after`、`operate_time`，并区分操作来源（ERP 或亚马逊后台）。[https://apidoc.lingxing.com/docs/newAd/apiLogStandard.md, 2026-08-28]
- 另有运营侧日志接口：查询运营日志/查询运营日志(新)（docs/Statistics/operateLogList、operateLogV2List）。[https://apidoc.lingxing.com/_sidebar.md, 2026-08-28]

### 2.7 版本公告与废弃机制
- 官方"接口更新日志"页按日期倒序持续更新（最近条目 2026-08-21）；含专门【下线通知】版块，例如 2025-11-26 预告"利润报表-订单(旧版)"于 2026-01-01 下线；广告相关示例：2026-08-07 SP广告报告 `creative_type` 字段作废，建议改用"SB广告创意"接口。[https://apidoc.lingxing.com/docs/ApiUpdateLog.md, 2026-08-28]
- 开放平台通知邮箱：开通时需预留接收领星开放平台通知的邮箱。[https://www.qeasy.cloud/article/lingxing-erp-authorizations-522, 2026-08-28]

---

## 3. ERP 内广告功能与用户权限（帮助中心）

### 3.1 "全部广告"功能页
- 页面覆盖 SP、SD 广告类型及活动/广告组/关键词/搜索词/投放等层级（该帮助页未提及 SB）。[https://www.lingxing.com/help/article/allAds, 2026-08-28]
- 支持单个与批量"直接对广告进行修改编辑"（含预算、竞价信息）及创建（添加 MSKU 到广告组）。[https://www.lingxing.com/help/article/allAds, 2026-08-28]
- 数据时效：新授权店铺 SP/SD 基础数据"5分钟内"，报告获取"5分钟~2小时"；日常更新从近期数据"10分钟/次"递减到历史数据"1天/次"；"接收到亚马逊最新数据后1分钟内显示"。[https://www.lingxing.com/help/article/allAds, 2026-08-28]

### 3.2 用户权限体系
- 角色在【设置>角色管理>功能权限】配置；帮助页未列预设角色清单。[https://www.lingxing.com/help/article/userRule, 2026-08-28]
- 店铺/ASIN 授权："选择店铺、负责人，设置该负责人可管理的ASIN"；部门主管可查看所属部门内用户的权限。[https://www.lingxing.com/help/article/userRule, 2026-08-28]
- 广告权限粒度：可按 ASIN、按广告活动单独授权；授权 ASIN 即"将拥有该ASIN对应的所有MSKU的权限"；"在应用策略时，只能选择自己有权限的广告活动"。该页未说明出价/预算等操作是否可独立开关。[https://www.lingxing.com/help/article/userRule, 2026-08-28]
- 超级管理员：开放接口凭据与 MCP 开关均要求管理员/超级管理员操作（见 1.1、2.1）。

---

## 4. 关键问题逐条回答（对应设计文档 P0）

**A. MCP 凭据模式**——已答：`X-Mcp-Key` 请求头；密钥绑定**个人领星帐号**（非企业/应用），数据范围完全继承该帐号在 ERP 中的店铺与功能权限。[help/article/mcp, 2026-08-28]

**B. 密钥管理能力**——部分答：支持在【管理MCP】页面重新生成（轮换）；**多密钥、按店铺/Profile 限权密钥、只读密钥、密钥级访问日志均无公开文档记载 → OPEN**。间接推论（非官方声明）：因密钥绑定个人帐号且权限继承 ERP，可通过"专用低权限子帐号 + 其密钥"模拟限权只读密钥。

**C. MCP 工具清单**——已答：官方列出约 13 类读工具（含 8 个广告报告读工具）+ 5 个写工具（监控/自定义指标类）；**广告全部只读，无写工具，无 dry-run/preview**。[help/article/mcp, 2026-08-28]

**D. 开放平台 REST API**——已答：广告读接口覆盖 SP/SB/SD 报表+小时数据+DSP+基础数据，写接口**仅 SP**（改活动/广告位/广告组/关键词/投放/商品状态，加关键词/否定，归档否定）；完全可作为 MCP 之外的第二通道，且"操作日志（新）"接口（operate_before/after、user_name、区分 ERP/亚马逊来源）是现成的对账数据源。鉴权：appId/appSecret → access_token(7199s)+refresh_token(2h,一次性)，每请求 MD5+AES(ECB) 签名（2 分钟有效）；限流：令牌桶按 appId+接口URL（样例：读接口容量 10、写接口容量 1），超限 3001008；**幂等语义未见文档 → OPEN**（写接口响应为逐条 code，无幂等键/请求去重说明）。

**E. 用户权限体系**——部分答：角色管理+功能权限；店铺与 ASIN 级授权、广告活动级授权；操作级（出价/预算单独开关）粒度未见公开文档 → 部分 OPEN。

**F. SLA/公告/沙箱**——部分答：无公开 SLA 承诺（未检索到）；版本公告渠道为 apidoc"接口更新日志"页（含下线通知版块）+ 开通时预留的通知邮箱；**沙箱/测试环境无公开文档记载（QA 页未提及）→ OPEN**。

**G. MCP 接入方式**——已答：Streamable HTTP；URL `https://openmcp.lingxing.com/mcp-servers/lingxing-mcp`；`X-Mcp-Key` header；官方给出 mcpServers JSON 配置示例（见 1.1）。

---

## OPEN 清单（公开文档无法验证，不得推断不存在）

1. MCP 一个帐号能否创建多个密钥；有无只读密钥、按店铺/Profile 限权密钥。[help/article/mcp 未提及]
2. MCP 密钥级访问日志/审计（谁在何时调了哪个工具）。[help/article/mcp 未提及]
3. MCP 工具的完整机读清单（官方帮助页为示例性列举，未声明穷尽；实际 tools/list 需连接后才知，本次禁止连接）。
4. 开放平台写接口的幂等语义（重复提交同一修改的行为、有无幂等键/去重窗口）。[adReportManagePutSpKeyword.md 未提及]
5. 开放平台能否为一个企业签发多套 AppId/AppSecret（多应用隔离）。[Guidance/AppID.md 未提及]
6. SB/SD 广告写接口是否存在于未公开目录或规划中（当前 _sidebar.md 无）。
7. 领星 ERP 广告模块操作级权限（出价/预算修改能否独立于查看权限单独开关）。[help/article/userRule 未提及]
8. 官方 SLA、可用性承诺、沙箱/测试环境。[QA.md 与公开检索均未见]
9. MCP 每工具 QPS=1 之外是否有企业级总量限流。
10. openapi.lingxing.com 门户页内容（根路径 403，无法访问，标记 OPEN；不影响 apidoc 文档与网关可用性结论）。

---

## 对控制平面设计的含义（简评）

领星官方 MCP 是"个人帐号只读数据面"：密钥绑定个人、权限继承 ERP、广告工具全只读、每工具 QPS=1、无 dry-run 与密钥级审计。它适合做分析/问答的低速读通道，不能作为广告写路径，也不宜作为对账主通道。控制平面的写路径应走开放平台 REST API：但其凭据是"超级管理员签发、企业全量数据"的单一 AppId/AppSecret，配 IP 白名单与 MD5+AES 签名，写接口仅覆盖 SP 且令牌桶容量 1、无幂等键——因此控制平面必须自建：凭据集中托管与轮换（重置即旧密立即失效）、店铺/操作级授权、写队列限速与重试去重、以及用"操作日志（新）"接口（含 operate_before/after、操作来源）+ 报表读接口做双通道对账。SB/SD 写、沙箱、SLA 均缺失，需在设计中按 OPEN 风险项处理，并订阅接口更新日志页跟踪废弃公告。
