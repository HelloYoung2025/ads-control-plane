# 本地演示 Runbook（Owner / 运营）

日期：2026-08-28 ｜ 面向：业务 Owner 与运营，无需读代码 ｜ 全流程已于当日实测

一句话：在你的电脑上起一个纯 Mock 的广告控制平面，走通「人签目标授权书 → AI 生成否定词候选 → 人对着冻结指纹批准 → 导出执行 CSV」，并现场看三类越权/越界被服务端拒绝。全程零真实凭据、零真实店铺、零外部连接。

## 启动

```bash
cd <仓库根目录>
uv run python scripts/serve_local_demo.py    # 可加 --port 改端口，默认 8788
```

启动横幅打印全部入口，随后是两个身份的 token 表（与下节一致）：

```
========================================================================
  ads-control-plane LOCAL DEMO · MOCK DATA ONLY · 无真实凭据 / 无真实店铺
========================================================================
  UI   http://127.0.0.1:8788/ui/
  API  http://127.0.0.1:8788/candidate-sets  /mandates  (Authorization: Bearer <token>)
  MCP  http://127.0.0.1:8788/mcp  (streamable HTTP, 同一套 Bearer token)
  DEV  http://127.0.0.1:8788/dev/identities  (demo token 清单)
========================================================================
```

浏览器打开 http://127.0.0.1:8788/ui/（访问根路径 / 会自动跳转过去）。Ctrl+C 停止。

要点：

- 服务只绑 127.0.0.1，外部机器连不上；
- 全部状态在内存：重启即清零。纯 Mock 演示这是便利；**接了真实通道后这意味着
  同步下来的镜像、签的授权书、待批 / 已批 / 已拒候选与运行记录在重启后全部消失**，
  需要重新同步/重签，并把新的授权书 ID 交给 AI 客户端。之前批过什么，以本机
  已下载的 CSV 为准（文件名带集合 ID）；「这批词不是第一次出现」的提示只在当前
  清单内比对，重启前批过 / 拒过的比不出来（2026-09-06 核对，对应界面上的同色提示）；
- demo token 有效期覆盖整个进程寿命（2026-08-29 起；此前 8 小时会让过夜的服务
  次日全员 401）；
- 服务端终端只用来看 uvicorn 日志：MCP 的拒绝码随错误文本一并返回给客户端
  （2026-09-06 实测，见第⑤步），不必切到终端去找。

## 真实领星通道（可选）

默认不配任何环境变量 = 纯 Mock，「同步镜像」会明确拒绝。配齐以下变量后启动，
横幅与界面徽章会切换为「真实通道」警示态（2026-08-29 起两态区分）。身份始终是
演示 token；**待批队列里的数据是不是 Mock，由下表最后一行那道开关决定**——
打开 `ADS_CP_STRATEGY_LX_ENABLED` 之后，待批的词来自真实店铺，批准后导出的 CSV
拿去领星执行会否掉真实关键词。

**对领星只读拉取的链路有两条**：同步镜像，以及——只在额外打开
`ADS_CP_STRATEGY_LX_ENABLED` 时——否定词候选的搜索词数据源。

| 环境变量 | 含义 |
|---|---|
| `LX_MCP_KEY` | 领星 MCP 网关密钥（只经环境变量传递，绝不落盘/入库） |
| `LX_MCP_URL` | 领星 MCP 网关地址 |
| `ADS_CP_SYNC_PROFILES` | 同步白名单：允许拉取的店铺 profile id，逗号分隔。缺省空 = 全拒（fail-closed） |
| `ADS_CP_SYNC_MAX_PAGES` | 每张报表单轮最多拉几页（每页 100 行），缺省 3。到达上限即截断——2026-08-29 起截断会在同步结果与界面上如实标注，并给「继续拉取」入口续拉，不再静默把部分样本当全店 |
| `ADS_CP_STRATEGY_LX_ENABLED` | **独立于同步 key 的显式开关**：`1` 才让否定词候选走真实搜索词源。不配 = 策略面仍用 Mock。单列一道开关是因为生成候选是 AI 可调用工具，且即席模式没有配额；配了 key 就顺带打开，等于让 AI 无人值守地反复读领星生产 API |

同步走领星生产 API（约 1.1 秒/次限流）；一轮四张报表，大店铺拉全需要多轮续拉。

## 两个演示身份

| Token | 身份 | 能做 | 不能做 |
|---|---|---|---|
| `demo-owner-token` | 运营负责人（HUMAN owner-1，OPERATOR+APPROVER） | 签发/撤销目标授权书；批准/拒绝候选集合；下载 CSV；触发同步 | SoD：不能批准自己生成的集合（`CREATOR_CANNOT_APPROVE`） |
| `demo-codex-token` | AI 助手（AI codex-1，ANALYST，委托人 owner-1） | 经 MCP 生成候选集合、只读查询 | 批准、签发、撤销、触发同步（服务端 403） |

2026-08-28 Owner：审批者与运营人员在本演示里合二为一，因此只有一个「人」身份。域层的 6 角色目录与请求级 SoD 冲突矩阵未变（DEC-101：SoD 靠请求级冲突矩阵而不是角色数量）——多人时把上表第一行拆成两行、各持一个角色即可，无需改代码。上表只描述**本演示**给了哪两个身份，不是说系统只有人和 AI 两种主体类型。

身份只由服务端从 Bearer token 解析——UI 右上角切换的就是页面请求所用的 token；请求参数里自报的身份一律无效。机器可读清单：`curl -s http://127.0.0.1:8788/dev/identities`。

## 演示数据

Mock 店铺档案 `profile-A` 预置 10 条搜索词绩效：3 条达标（零转化 + ≥25 点击 + ≥20.00 USD + 数据新鲜）、1 条数据过旧、1 条 ASIN 型（两者均触发 ABSTAIN 显式上报）、2 条有转化（规则上永不候选）、3 条证据不足（正常排除）。按下文参数生成，结果恒为 3 条候选 + 2 条 ABSTAIN——**仅 Mock 通道如此**。接了真实搜索词源（`ADS_CP_STRATEGY_LX_ENABLED`）之后，`profile-A` 会返回 `profile_has_data_source=false`（它确实没接），候选数由真实数据决定。

## 十分钟演示

### ① Owner 签发目标授权书（WASTED_SPEND_REMOVED，1440 分钟频次）

UI 路径：右上角切到 **运营负责人**，展开「签发新授权书」。表单默认值即演示参数（目标 WASTED_SPEND_REMOVED，频次 1440 分钟/每天）；**展开表单底部的「参数细则」，把「每天最多跑几次」从 1 改成 2**（界面上没有叫「每日运行上限」的字段，且这一项收在折叠区里）——第⑤步要演示"间隔未到"，每日上限为 1 时配额会先于间隔被拒，演示出的就成了 RUN_BUDGET_EXCEEDED。**改完那一栏下面会出现一条琥珀提示**，说 1440 分钟的间隔下第 2 次跑不成——它说得对，本演示要的正是让间隔而不是配额成为那道闸；真实使用时看到这条提示，该改的是「多久检查一次」。提交后表格出现 ACTIVE 授权书。**ID 别照抄表格上那串**——表格只印前 8 位，服务端按完整 UUID 精确解析，抄缩写第②步必得 `MANDATE_UNKNOWN`。取完整 ID 有两条路：点该行的「复制指令」（复制的是一句可直接粘给 AI 客户端的话，内含完整 ID），或把鼠标停在 ID 上看悬停原文。

curl 等价（响应回显全部合同内容，含 mandate_id 与 parameter_pack_hash——参数列表合同的指纹）：

```bash
curl -s -X POST http://127.0.0.1:8788/mandates \
  -H "Authorization: Bearer demo-owner-token" \
  -H "Content-Type: application/json" \
  -d '{
    "profile_external_id": "profile-A",
    "objective": "WASTED_SPEND_REMOVED",
    "statement": "清除近 30 天零转化高花费搜索词造成的广告浪费",
    "lookback_days": 30,
    "min_spend_amount": "20.00",
    "currency": "USD",
    "min_clicks": 25,
    "max_data_staleness_hours": 24,
    "max_runs_per_day": 2,
    "max_candidates_per_run": 50,
    "valid_days": 7,
    "run_interval_minutes": 1440
  }'
```

讲解点：授权书 = 「目标 → 参数列表 → 有界配额」的合同（DEC-114）。AI 试图签发会得到 403 AI_CANNOT_ISSUE_MANDATE；授权最长 30 天，到期必须由人重签，不存在长生不老的自动化授权。

### ② 以 Codex（AI 身份）经 MCP 生成候选

方式 A——真用 Codex CLI（先按 [codex-connect.md](codex-connect.md) 配好连接），进入 `codex` 会话后说：

> 调用 ads-control-plane 的 generate_negation_candidate_set，参数 profile_external_id=profile-A，mandate_id=<第①步的授权书 ID>

方式 B——冒烟命令（不依赖 Codex，效果相同）：

```bash
MANDATE_ID=<第①步的授权书 ID> uv run python - <<'PY'
import asyncio, os
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    async with (
        httpx2.AsyncClient(headers={"Authorization": "Bearer demo-codex-token"}) as http,
        streamable_http_client("http://127.0.0.1:8788/mcp", http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool(
            "generate_negation_candidate_set",
            {"profile_external_id": "profile-A", "mandate_id": os.environ["MANDATE_ID"]},
        )
        print("is_error:", result.is_error)
        for item in result.content:
            print(item.text)

asyncio.run(main())
PY
```

预期输出（**仅 Mock 通道**）：`evaluated_ad_group_terms: 10`、`distinct_search_terms: 10`、`candidate_count: 3`、`asin_abstain_count: 1`、`abstains` **2** 条、`set_id` 与 `set_hash`（下一步要用），以及一句 note：审批需要人类会话。UI 的「待批」页签同步出现该集合（来源 AI）。

两条弃权是两种完全不同的处境，演示专门各埋了一条：

| 搜索词 | reason | 意思 | 人该做什么 |
|---|---|---|---|
| `b0demo0001` | `ASIN_NOT_A_KEYWORD` | 花了钱、零转化，但它是个 ASIN——本系统开出的否定**精确关键词**挡不住 ASIN 型来源 | 去领星「否定投放」页签单独否定它。批准下面那份候选清单**不覆盖**它（见 §④「拿到 CSV 之后」） |
| `vintage widget manual` | `STALE_DATA` | 数据太旧，判断不了 | 等新数据，或重签一份放宽时效上限的授权 |

两者都必须显式上报："无法判断"和"判断得出、但否不掉"都不等于"没有候选"。

讲解点：授权模式下参数只来自签发合同——调用里附带任何参数覆盖（比如 min_clicks）都会被拒（MANDATE_PARAMS_FORBIDDEN），结构上不存在参数漂移。不带 mandate_id 也可即席生成（参数走白名单校验），但即席运行不消耗、也不受授权书频次约束。

### ③ 切 Owner 批准（看 hash 绑定）

UI 路径：切回 **运营负责人**，「否定词候选集合 → 待批」展开集合卡片：完整候选表（搜索词 / 广告组 / 广告活动 / 花费 / 曝光 / 点击 / 点击率 / 广告订单）与 **冻结指纹 set_hash** 同屏（界面上就印着「冻结指纹」这四个字；不叫「内容指纹」是刻意的——指纹不同不等于内容不同）。点「批准」——按钮把页面显示的这个 hash 作为 expected_hash 一并提交：**批的必须是看到的内容**（AX-07）。

curl 等价：

```bash
# 列出待批集合，取 set_id 与 set_hash
curl -s "http://127.0.0.1:8788/candidate-sets?state=FROZEN" \
  -H "Authorization: Bearer demo-owner-token"

curl -s -X POST http://127.0.0.1:8788/candidate-sets/<set_id>/approve \
  -H "Authorization: Bearer demo-owner-token" -H "Content-Type: application/json" \
  -d '{"expected_hash": "<set_hash>"}'
```

顺手看反例——hash 不对时批准无效：

```bash
curl -s -w "\nHTTP %{http_code}\n" -X POST http://127.0.0.1:8788/candidate-sets/<set_id>/approve \
  -H "Authorization: Bearer demo-owner-token" -H "Content-Type: application/json" \
  -d '{"expected_hash": "deadbeef"}'
# → {"detail":"HASH_MISMATCH"}  HTTP 409
```

同族防线：集合生成超过 72 小时 → SET_EXPIRED（基于旧数据的候选不允许迟到批准）；冻结后内容漂移 → CONTENT_DRIFT。

### ④ 下载执行 CSV

UI：「已批」页签 → 「下载 CSV」，落盘名形如 `negation-<店铺>-<生成日 UTC>-<集合 ID 前 8 位>.csv`。curl：

```bash
curl -OJ "http://127.0.0.1:8788/candidate-sets/<set_id>/export.csv" \
  -H "Authorization: Bearer demo-owner-token"
```

内容（2026-09-06 本演示数据实测，UTF-8 带 BOM，Excel 可直接双击打开）：

```csv
profile_external_id,shop_external_id,campaign_external_id,ad_group_external_id,search_term,match_type,campaign_name,ad_group_name
profile-A,shop-1,c-1,ag-1,cheap widget holder,NEGATIVE_EXACT,HX02-Auto-US,ag-core
profile-A,shop-1,c-1,ag-1,widget free shipping,NEGATIVE_EXACT,HX02-Auto-US,ag-core
profile-A,shop-1,c-2,ag-2,wobbly widget hack,NEGATIVE_EXACT,HX02-Exact-US,ag-brand
```

讲解点：这就是 L1.5 的人工执行物——人拿它去 ERP/平台后台应用（平台零写凭据）。未批准的集合导不出（409 NOT_APPROVED）。

**拿到 CSV 之后**（这一段此前没有，人拿着文件不知道下一步）：

- 每行 = 在 `ad_group_external_id` 那个广告组里加一条否定精确词（`match_type` 恒为
  `NEGATIVE_EXACT`）。`campaign_name` / `ad_group_name` 两列是给你在领星后台按名称
  找到对象用的，不是导入模板的字段。2026-08-29 只读实测到的位置：领星左侧「广告」→
  「全部活动」→ 点活动名下钻到广告组，SP 三级页签里有「否定词」
  （`docs/evidence/lx-ads-ia-20260829.md` §1）。添加否定词的具体操作步骤、以及领星
  是否支持批量导入，均未实测，不要假定有。文件按「活动 → 广告组」归好组（同一个广告组
  的词连在一起），照着从上往下做即可，每个广告组只需下钻一次。**组内不排序**——
  上面那份实测输出里 ag-1 的两行就是花费低的在前（`cheap widget holder` 35.40 USD
  排在 `widget free shipping` 48.90 USD 之前）。要先做贵的，看界面证据表（它按花费
  排序）：表是给你判断用的，文件是给你照着敲的。
- **这份 CSV 不一定是那一轮浪费的全部。** 集合卡片上若出现「另有 N 个 ASIN 否不掉」
  的黄色提示，说明同一轮里还有 N 个搜索词花了钱、零转化，但它们是 ASIN 不是关键词。
  本系统只开否定精确关键词，加上去挡不住 ASIN 型来源，所以它们没有进这个文件。
  这几个 ASIN 要在领星「否定投放」页签里单独处理（页签位置与「否定词」并列，
  见 §④ 上面那条导航路径；该页签**里面**的操作步骤未实测），把这份 CSV
  执行完不等于处理完。
  具体是哪几个，就写在那条黄色提示末尾（「要否定的是：…」，2026-09-06 实测）——
  照着抄进领星即可，不必再去问 AI。
- **以 `=` `+` `-` `@` 开头的搜索词，CSV 里会多一个前置单引号**。这是防表格软件把顾客
  搜索词当公式执行（搜索词是站外真实输入），必要且不会撤。代价是那几行的 E 列与
  界面证据表上显示的不是同一个字符串——**按界面上的原词输入，不要带引号**，
  否则加进去的是一条永远命中不了的否定词。集合卡片上会点名是哪几个词。
- 执行完不需要回本系统做任何操作，也没有地方可以标记：本系统不记录谁下载了、
  是否执行了。要留痕请在系统外按文件名记录谁执行了哪一份。
- 系统唯一能「感知」执行的方式是取数时下推 `targeted_type=not_negatived`（领星释义
  「未否定」）——按此设计，已在领星否定掉的词下一轮不应再被提名。该筛选的粒度尚未
  实测（见 `src/ads_control_plane/providers/lingxing/README.md` 的已知未验证项），
  所以「同一批词又出现在待批」应先当作「那份还没执行」来查，而不是当作系统出错。

### ⑤ 三类拒绝（安全边界现场验证）

**1）AI 点批准 → 403 AI_CANNOT_APPROVE**

需要一个仍在 FROZEN 的集合。若③已批掉唯一集合，先以 codex 身份即席再生成一个：把方式 B 命令中 `"mandate_id": ...` 一项删掉重跑即可（即席模式不受授权书频次限制）。然后用**正确的 set_hash**、换 **demo-codex-token** 发起批准：

```bash
curl -s -w "\nHTTP %{http_code}\n" -X POST http://127.0.0.1:8788/candidate-sets/<set_id>/approve \
  -H "Authorization: Bearer demo-codex-token" -H "Content-Type: application/json" \
  -d '{"expected_hash": "<set_hash>"}'
# → {"detail":"AI_CANNOT_APPROVE"}  HTTP 403
```

两个实测细节：hash 校验在 SoD 之前，错 hash 会先得到 409 HASH_MISMATCH；已批准的集合会先得到 409 NOT_FROZEN——所以这条要拿 FROZEN 集合 + 正确 hash 演示。UI 侧切到 AI 身份时「批准/拒绝/签发/撤销」按钮本就置灰（同步按钮直接隐藏），但那只是预告；用 curl 直接打 API 才演示出真正的强制在服务端。否决同样只属人类会话，AI 打 `/reject` 得 403 AI_CANNOT_REJECT。

**2）签发未就绪目标 → 403 OBJECTIVE_NOT_READY**

本条只能用 curl 演示：界面上未就绪的目标在下拉里显示为「清仓出货速度（缺数据，暂不可选）」并被置灰，**选不中**，签发按钮也随之禁用——这是有意为之（fail-closed 前移到界面），所以 UI 走不到这个 403。把①的 curl 里 objective 换成 `"CLEARANCE_VELOCITY"`。响应：

```
{"detail":"OBJECTIVE_NOT_READY"}  HTTP 403
```

这是 fail-closed 设计而非故障：目标已定义但数据地基未就绪，系统拒绝"先签着、数据以后再说"。各目标缺什么（DEC-116）：

| 目标 | 状态 | 缺失的数据地基 |
|---|---|---|
| WASTED_SPEND_REMOVED 降无效花费 | 可签发 | — |
| CLEARANCE_VELOCITY 清仓 | 未就绪 | fba_inventory_feed、clearance_unit_loss_cap |
| LAUNCH_RAMP 打新品 | 未就绪 | ramp_definition、attribution_maturity_calibration |
| SALES_GROWTH 推高销量 | 未就绪 | unit_economics_baseline、breakeven_acos_definition |

**3）间隔未到重复生成 → RUN_TOO_SOON**

立刻原样重跑第②步（同一 mandate_id）。合同频次 1440 分钟未到，服务端拒绝第二次运行。

实测（2026-09-06，对本机 8791 Mock 实例）：拒绝码**直接回到 MCP 客户端**，不必切终端。`is_error: True`，文本逐字为

```
Error executing tool generate_negation_candidate_set: RUN_TOO_SOON: last run was 0:00:00.018987 ago; contract interval is 1440 minutes
```

码在 `Error executing tool <name>: ` 之后、冒号之前（`server.py` 的 `_coded` 把 `ToolDenied` 翻成 `ToolError(f"{code}: {detail}")`）。若授权书每日上限是 1，则先触发配额拒绝 `RUN_BUDGET_EXCEEDED`——同样是"越界即回到人"，只是演示的不是间隔。

## 常见错误码中文对照

审批/授权 API（HTTP；错误码在响应体 `{"detail": "..."}`）：

| HTTP | 错误码 | 含义 |
|---|---|---|
| 401 | AUTHENTICATION_REQUIRED | 缺少或无效的 Bearer Token |
| 403 | AI_CANNOT_APPROVE | AI 身份不能批准（SoD，服务端强制） |
| 403 | CREATOR_CANNOT_APPROVE | 不能批准自己创建的集合（SoD） |
| 403 | AI_CANNOT_ISSUE_MANDATE | 授权书只能由人类签发 |
| 403 | HUMAN_REQUIRED | 撤销授权书只能由人类执行 |
| 403 | OBJECTIVE_NOT_READY | 目标数据地基未就绪，拒绝签发（fail-closed） |
| 404 | RESOURCE_UNAVAILABLE | 不存在或无权访问（二者刻意不可区分） |
| 409 | NOT_FROZEN | 集合不在待批（FROZEN）状态 |
| 409 | HASH_MISMATCH | expected_hash 与冻结指纹不符：批的不是看到的内容 |
| 409 | CONTENT_DRIFT | 冻结后内容发生漂移 |
| 409 | SET_EXPIRED | 集合超过 72 小时时效，需基于新数据重新生成 |
| 409 | NOT_APPROVED | 仅已批准的集合可导出 CSV |
| 422 | PARAMETER_REJECTED | 参数超出白名单范围 |
| 422 | CANDIDATE_STATE_INVALID | 状态筛选值不认识（可用 FROZEN/APPROVED/REJECTED/GENERATED，大小写均可；2026-08-29 前误报 500） |
| 403 | SCOPE_PROFILE_MISMATCH | 作用域勾选的对象属于另一个店铺——一份授权只管一个店铺（2026-08-29 起签发期即拒，此前被静默改挂） |

MCP 工具面：Bearer 无效时 `/mcp` 在 HTTP 层直接 401，不会进入工具；进入工具后的拒绝，码以 `<CODE>: <说明>` 的形式随错误文本一并返回客户端（2026-09-06 实测），不必看服务端终端：

| 错误码 | 含义 |
|---|---|
| AUTH_SCOPE_DENIED | 无该动作的授权（EffectiveAllow 不通过） |
| PARAMETER_REJECTED | 即席参数超出白名单 |
| MANDATE_PARAMS_FORBIDDEN | 授权模式下不允许参数覆盖 |
| MANDATE_UNKNOWN | 授权书不存在或 ID 非法 |
| MANDATE_NOT_ACTIVE | 授权书已撤销 |
| MANDATE_EXPIRED | 授权书已过期 |
| SCOPE_MISMATCH | 授权书不覆盖该组织或店铺档案 |
| RUN_BUDGET_EXCEEDED | 当日运行配额已用尽 |
| RUN_TOO_SOON | 距上次运行未达合同间隔 |
| CURRENCY_MISMATCH | 授权书结算币种与数据币种不一致——按币种分开跑（2026-08-29 前这条会裸崩成无码的 Error executing tool） |

## 安全注記

- 未配置领星环境变量时，本演示一切数据为 Mock：Provider.MOCK、占位店铺/广告组 ID、十条虚构搜索词；与真实领星/Amazon 零连接，进程不发任何外部请求。配置后对领星只读拉取的链路有两条：「同步镜像」，以及额外打开 `ADS_CP_STRATEGY_LX_ENABLED` 后的否定词搜索词数据源（两条都走结构性防写白名单，见 adapters/lx_read.py）；其余仍为 Mock。此处此前写的是「仅『同步镜像』这一条链路」，2026-08-30 起不再成立。
- 两个 demo token 固定可读，仅因服务只绑 127.0.0.1 且身份全为 Mock 才可接受；固定 token 与 /dev/identities 端点只存在于本地演示组合根（`src/ads_control_plane/api/local_demo.py`），生产 app factory 不含两者。
- 不要把本演示映射到任何生产语义：生产身份将来自公司 OIDC introspection，凭据边界见 SECURITY.md 与 decision register。
