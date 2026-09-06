/* 广告控制平面 · 本地演示界面。
   纯浏览器原生实现：无框架、无构建、无外部依赖。
   页面挂载在 /ui/，因此所有 API 调用使用根相对路径（同源）。
   所有权限约束（SoD、HUMAN-only、hash 绑定、TTL、参数白名单）由服务端强制，
   本界面只做预告式禁用、客户端预检与错误码翻译。

   客户端预检的纪律（D8）：预检只在本地拦住"必然被拒"的输入，并且**绝不伪造错误码**——
   带 SCREAMING_SNAKE 码的红条只在服务端真的拒绝时出现。看到码 = 服务端说的。 */
"use strict";

(() => {
  // ---------- 错误码 → 中文说明（detail 为服务端错误码字符串） ----------
  const ERROR_TEXT = {
    AUTHENTICATION_REQUIRED: "未认证：缺少或无效的 Bearer Token",
    //: 这个码在审批面上最常见的成因不是「无权」，是服务重启过——授权书与候选集合
    //  只存在进程内存里。旧标签页上点批准/拒绝/下载都会撞到它，而「资源不存在或
    //  无权访问」会把人引去查权限。措辞保持条件式：界面无法区分「首次启动」与
    //  「重启过」，写成「服务重启过」就是把猜测说成事实。
    RESOURCE_UNAVAILABLE:
      "资源不存在或无权访问。若这张卡片是页面早前加载的：授权书与候选集合只保存在" +
      "服务进程内存里，服务重启即清空——需要重新签发 / 重新生成（已下载的 CSV 不受影响）",
    // SoD / 身份约束
    AI_CANNOT_APPROVE: "AI 身份不能批准候选集合——职责分离由服务端强制",
    AI_CANNOT_REJECT: "AI 身份不能否决候选集合——否决同样是审批意思表示，只属于人",
    CREATOR_CANNOT_APPROVE: "你就是这个集合的生成者，不能自己批自己生成的内容（职责分离，服务端强制）",
    SUBMITTER_CANNOT_APPROVE: "提交者不能批准自己提交的内容（职责分离）",
    APPROVER_IDENTITY_UNKNOWN: "审批人缺少人员身份标识，无法记录审批责任",
    AI_CANNOT_ISSUE_MANDATE: "AI 身份不能签发授权书——授权扩大只能来自人类",
    HUMAN_REQUIRED: "该操作仅限人类身份执行",
    // 授权书生命周期
    OBJECTIVE_NOT_READY: "该目标的数据地基未就绪，服务端拒绝签发（fail-closed 设计，非故障）",
    MANDATE_NOT_ACTIVE: "授权书已不在生效状态",
    MANDATE_EXPIRED: "授权书已过期，需重新签发",
    SCOPE_MISMATCH: "授权书与目标组织或店铺档案不匹配",
    RUN_BUDGET_EXCEEDED: "当日运行次数已用完",
    RUN_TOO_SOON: "距上次运行未达到授权书约定的间隔",
    MANDATE_PARAMS_FORBIDDEN: "按授权书运行时不允许覆盖参数",
    //: 2026-08-30：撤销授权书此前不管它已经生出来的待批集合——那些集合与好集合
    //: 逐字同形，「批准」按钮照样可点，而人刚在撤销框里读到「AI 立即停止按它运行」。
    MANDATE_REVOKED: "这批候选来自一份已被撤销的授权书",
    //: 2026-08-30：镜像是纯内存的，重启即清空，而 demo token 是固定串——浏览器里
    //: 的旧断点重启后照样 POST 得进去，服务端此前原样采信，于是标着「已拉全」的
    //: 那一层一页都不拉，界面却平铺直叙地说「该店铺镜像里没有广告组对象」。
    SYNC_CURSOR_STALE: "服务重启过，内存镜像与同步断点一起清空了——请点「同步镜像」重新完整拉取",
    //: 服务端现在会把「哪个参数、允许范围是什么」放进 detail.message，由 api()
    //: 拼在这句后面（见 readErrorDetail）。所以这句不再列举可能性——猜三种可能
    //: 的提示，在确切原因就在后面时是纯噪音。
    PARAMETER_REJECTED: "参数被服务端白名单拒绝",
    MIN_SPEND_NOT_A_NUMBER:
      "「至少花了多少钱才处理」不是一个数：只填数字和小数点，不要千分位逗号、货币符号或单位",
    //: 2026-08-30：这句原本写死「当前数据源为 USD」。自「币种按站点推出」之后
    //: 它对任何非美国站都是假话——德国站的人照它改回 USD，再签一次、再失败一次，
    //: 循环不会终止。实际币种由服务端在 message 里点名（见 readErrorDetail）。
    CURRENCY_MISMATCH: "授权书的结算币种与这个店铺数据的币种不一致",
    //: 「已撤销」与「不属于本组织」这两种成因其实报的是 MANDATE_NOT_ACTIVE 与
    //  SCOPE_MISMATCH，走不到这个码。真正会走到的是：服务重启清空了授权书，
    //  或者 ID 抄的是界面上那 8 位缩写。
    MANDATE_UNKNOWN:
      "找不到这份授权书：服务重启会清空重启前签发的授权书（只存在进程内存里）；" +
      "也可能是 ID 抄成了界面上的 8 位缩写——用授权书行的「复制指令」取完整 ID",
    AUTH_SCOPE_DENIED: "当前身份没有这个店铺或这个动作的授权范围",
    // 授权作用域 / 运行时段（2026-08-28 新增；逐条对照服务端实际抛出的码，不留同义别名）
    SCOPE_KIND_INVALID: "作用域类型不合法：只能是「整店所有广告」或「只管我勾选的对象」",
    SCOPE_LEVEL_INVALID: "作用域对象层级不合法：仅支持 campaign / ad_group / target",
    SCOPE_SELECTION_REQUIRED:
      "选了「只管勾选的对象」却没带入任何对象——在对象工作台勾选活动或广告组后，" +
      "还要点「用选中对象签授权书」把它们带进这份授权书",
    MANDATE_SCOPE_CONFLICT: "选了「整店所有广告」却又带了勾选清单——两者只能选一个",
    SCOPE_PROFILE_MISMATCH: "勾选的对象不属于这份授权指定的店铺——一份授权只管一个店铺",
    MANDATE_SCOPE_LEVEL_UNSUPPORTED:
      "这个目标的作用域只能是活动或广告组：否定词按广告组落位，投放层无法界定范围",
    INVALID_TIMEZONE:
      "时区无法识别：请填写标准时区名，例如 Asia/Kuala_Lumpur。系统不会拿服务器所在地的钟点替你解释时间",
    RUN_WINDOW_INVALID: "运行时段不合法：起止钟点都必须在 0–23 之间",
    RUN_WINDOW_INCOMPATIBLE:
      "这个检查频次和限定时段不相容：只有能整除或整倍于 24 小时的频次，才能保证每天都落在时段内；" +
      "否则运行时刻会逐日漂移，直到再也进不了时段而永远不再运行",
    NAIVE_DATETIME_REJECTED: "时刻缺少时区信息，系统拒绝按服务器本地时区替你解释（fail-closed）",
    OUTSIDE_RUN_WINDOW: "现在不在这份授权允许的运行时段内",
    // 候选集合生命周期
    //: 名字要与卡片上印的一致（见 2367 的注释：刻意不叫「内容指纹」）。这条红条原来
    //  用的正是那个被废掉的名字，人在同一屏上看到两个称呼，只会以为是两样东西。
    HASH_MISMATCH: "冻结指纹不一致：页面显示的版本与服务端冻结版本不符，请刷新后重批",
    CONTENT_DRIFT: "集合内容在冻结后发生漂移，服务端拒绝批准",
    SET_EXPIRED: "候选集合已超过 72 小时时效，请基于新数据重新生成",
    NOT_FROZEN: "集合不在待批（FROZEN）状态",
    NOT_GENERATED: "集合不在可冻结（GENERATED）状态",
    NOT_APPROVED: "仅已批准（APPROVED）的集合可以导出",
    //: 2026-08-29 排查（ui-6）：列表按状态筛选时传错值会被拒，词典此前没有这条。
    CANDIDATE_STATE_INVALID: "状态筛选值不认识——可用：FROZEN / APPROVED / REJECTED / GENERATED（大小写均可）",
    MISSING_PARENT: "广告组引用缺失所属广告活动，无法导出",
    // 对象工作台：镜像浏览 / 勾选预览
    LEVEL_INVALID: "对象层级不合法：仅支持 campaign / ad_group / target",
    SORT_FIELD_INVALID: "排序字段或方向不在白名单内，服务端已拒绝（不会按更宽的默认序悄悄返回）",
    SORT_DIR_INVALID: "排序方向不合法：只能是 asc 或 desc，服务端已拒绝",
    SELECTION_EMPTY: "勾选集为空：先在表格中勾选至少一个对象",
    SELECTION_MIXED_PROFILE: "勾选集跨店铺：一次预览只能覆盖一个 profile",
    SELECTION_TOO_BROAD: "勾选对象过多：单次上限 200 个，请分批操作",
    SELECTOR_MATCHED_NOTHING: "选择器没有命中任何对象，请检查勾选或先同步镜像",
    SELECTOR_TOO_BROAD: "选择器命中对象超过 200 个上限，请缩小范围",
    PREVIEW_OBJECT_NOT_IN_MIRROR: "勾选对象不在镜像中（可能已过期或未同步）——请先同步镜像再预览",
    //: 2026-08-29 排查（workbench-3）：动作栏的层级门控已保证层级不会错；真正会触发
    //  这个拒绝的是"个别对象在镜像里缺现值"。旧文案指去换层级，是把人引向死路——
    //  服务端 message 会点名是哪个对象（api() 已拼进红条），照它移出勾选即可。
    PREVIEW_VALUE_UNAVAILABLE: "勾选里有对象在镜像里没有这个动作要改的现值——看提示里点名的对象，把它移出勾选",
    // 对象工作台：同步链路
    LX_KEY_ABSENT: "服务端未配置 LX_MCP_KEY，同步通道关闭（fail-closed）——请在服务进程环境配置后重启",
    LX_URL_ABSENT: "服务端未配置 LX_MCP_URL，同步通道关闭（fail-closed）",
    SYNC_NO_ALLOWED_PROFILES: "同步白名单为空（ADS_CP_SYNC_PROFILES 未配置）：默认全拒，不会顺手多拉",
    SYNC_PROFILE_NOT_ALLOWED: "该店铺不在同步白名单（ADS_CP_SYNC_PROFILES）内，已拒绝拉取",
    SYNC_CONFIG_INVALID: "同步配置不合法（如 ADS_CP_SYNC_MAX_PAGES 不是正整数）",
    LX_GATEWAY_ERROR: "领星网关拒绝了本次调用（外层信封报错）",
    LX_BUSINESS_ERROR: "领星业务侧返回错误（内层信封报错）",
    //: 2026-08-29 排查（lxchannel-1）：网络层失败与网关拒绝是两回事——前者可重试，
    //  词典此前缺这条，人只能看到裸码猜含义。
    LX_TRANSPORT_ERROR: "没连上领星网关（超时/断网/网关异常）——这类失败可以直接重试；反复失败请检查服务端的领星配置",
    LX_TOOL_NOT_ALLOWED: "工具不在只读白名单内——写工具在结构上不可达",
    LX_ENVELOPE_SHAPE: "领星返回的信封形态不符合钉扎合同，已显式失败而非猜测解析",
    LX_PARAM_NOT_ENCODABLE: "同步参数无法按工具合同编码",
    LX_CONFIG_INVALID: "领星客户端配置不合法",
    //: 搜索词取数的失败码。此前只在 MCP 面出现过，人在界面上一次都看不到；
    //  自授权书卡片开始回显「上次跑成什么样」（#23），它们会被人直接读到，
    //  裸码等于没说。逐条只写「这是什么、该怎么办」。
    SEARCH_TERM_PROFILE_NOT_BOUND: "这家店没接搜索词数据源（缺店铺 ID 或币种声明），这份授权跑不出任何东西",
    SEARCH_TERM_UPSTREAM_ERROR: "取搜索词报表时上游报错——超时可等下一轮，参数类错误重试永远不会成功",
    SEARCH_TERM_TOTAL_ABSENT: "上游没给总行数，无法断言拉全了——宁可失败也不返回可能残缺的数据",
    SEARCH_TERM_RESULT_TOO_LARGE: "这家店该窗口的搜索词行数超出单次上限——缩短回看天数重签",
    SEARCH_TERM_PAGE_BUDGET_EXCEEDED: "翻页次数超出上限仍未拉全——缩短回看天数重签",
    SEARCH_TERM_ROWS_UNUSABLE: "拿回的行几乎全都解析不了，本次不做判断（数据形态异常，别拿它下结论）",
    SEARCH_TERM_METRIC_SHAPE: "上游指标字段的形态与钉扎合同不符，已显式失败而非猜着解析",
    SEARCH_TERM_WINDOW_IN_FUTURE: "推算出的数据窗口落在未来——检查服务端时钟",
    SEARCH_TERM_CLOCK_NAIVE: "取数时刻缺时区信息，系统拒绝按服务器本地时区替你解释（fail-closed）",
    DUPLICATE_SEARCH_TERM_ROW: "同一个（广告组，搜索词）出现了多行未合并——数据源侧的聚合没做到位，本次拒绝出结论",
  };

  const OBJECTIVE_TEXT = {
    WASTED_SPEND_REMOVED: "清除浪费花费",
    CLEARANCE_VELOCITY: "清仓出货速度",
    LAUNCH_RAMP: "新品爬坡",
    SALES_GROWTH: "销量增长",
  };

  //: 目标就绪度依赖键 → 人话。**事实**（缺哪几项）由服务端 /mandates/objectives 提供，
  //  这里只负责措辞；键名对不上时原样显示键名，不编造。
  const REQUIREMENT_TEXT = {
    fba_inventory_feed: "库存与周转天数的数据源还没接进来",
    clearance_unit_loss_cap: "「单件最多能亏多少」的口径还没冻结",
    ramp_definition: "「爬坡」到底怎么算，还没有可计算的定义",
    attribution_maturity_calibration: "归因成熟期还没做实测校准",
    unit_economics_baseline: "单位经济口径还没过财务签核",
    breakeven_acos_definition: "保本 ACOS 的定义还没冻结",
  };

  //: 常用时区的中文别名，仅用于回显措辞；IANA 名始终一并显示，不做隐式替换。
  const TZ_TEXT = {
    UTC: "协调世界时",
    "Asia/Shanghai": "北京时间",
    "Asia/Kuala_Lumpur": "吉隆坡时间",
    "Asia/Singapore": "新加坡时间",
    "Asia/Tokyo": "东京时间",
    "Europe/London": "伦敦时间",
    "Europe/Berlin": "柏林时间",
    "America/New_York": "纽约时间",
    "America/Los_Angeles": "洛杉矶时间",
    "Australia/Sydney": "悉尼时间",
  };

  //: 场景预设——**前端常量，仅预填，不约束**（D1）。服务端参数白名单仍是唯一裁判；
  //  提交上传的永远是展开后的全部参数，服务端不认识"预设"这个概念。
  //  取值待 Owner 按真实数据校准（无实测依据，见 risks R4）。
  const MANDATE_PRESETS = {
    CONSERVATIVE: {
      title: "保守试水",
      sub: "只动铁证，先看看系统怎么干活",
      lookback_days: 60,
      min_spend_amount: "50.00",
      min_clicks: 40,
      max_data_staleness_hours: 24,
      run_interval_minutes: 1440,
      max_candidates_per_run: 20,
      valid_days: 3,
      statement: "先小范围试手：只清最铁的零转化高花费搜索词",
    },
    STEADY: {
      title: "稳健清理",
      sub: "每天一次，长期挂着的常规打法",
      recommended: true,
      lookback_days: 30,
      min_spend_amount: "20.00",
      min_clicks: 25,
      max_data_staleness_hours: 24,
      run_interval_minutes: 1440,
      max_candidates_per_run: 50,
      valid_days: 7,
      statement: "清除零转化高花费搜索词造成的广告浪费",
    },
    AGGRESSIVE: {
      title: "激进清理",
      sub: "抓得更早更多，误伤概率也更高",
      lookback_days: 14,
      min_spend_amount: "8.00",
      min_clicks: 15,
      max_data_staleness_hours: 12,
      run_interval_minutes: 720,
      max_candidates_per_run: 120,
      valid_days: 7,
      statement: "尽早止损：短窗口、低门槛，让刚开始烧钱的词尽快进否定候选",
    },
    //: 下面四个打法的阈值取自 2026-08 检索的行业常见做法（LandingCube/Ad Badger/
    //  AmazonGrowthLab/BellaVix 等），出处见 docs/evidence/ppc-playbook-research-20260829.md。
    HONEYMOON: {
      title: "新品保护期",
      sub: "蜜月期手别太快：攒够 30 次点击再判，防误伤潜力词",
      lookback_days: 14,
      min_spend_amount: "30.00",
      min_clicks: 30,
      max_data_staleness_hours: 24,
      run_interval_minutes: 4320,
      max_candidates_per_run: 20,
      valid_days: 14,
      statement: "新品蜜月期保守清理：证据攒够再否，防误伤还没跑出成绩的潜力词",
    },
    STOP_LOSS: {
      title: "新品大额止血",
      sub: "一个词烧到设定的高花费门槛还没单就是纯放血，每天掐一遍",
      lookback_days: 7,
      min_spend_amount: "100.00",
      min_clicks: 50,
      max_data_staleness_hours: 24,
      run_interval_minutes: 1440,
      max_candidates_per_run: 30,
      valid_days: 7,
      statement: "新品期大额止血：只否最铁的高花费零出单词，其余先留着看",
    },
    PRE_EVENT: {
      title: "大促前清扫",
      sub: "开赛前 2-3 周动手，把预算留给大促当天能出单的词",
      lookback_days: 60,
      min_spend_amount: "20.00",
      min_clicks: 20,
      max_data_staleness_hours: 24,
      run_interval_minutes: 1440,
      max_candidates_per_run: 150,
      valid_days: 14,
      statement: "大促备战大扫除：把长期零出单的废词清进否定词，不让它们在大促里偷预算",
    },
    WEEKLY: {
      title: "稳健周清",
      sub: "每周一次的行业标准节奏，避开归因延迟的过度优化",
      lookback_days: 30,
      min_spend_amount: "20.00",
      min_clicks: 15,
      max_data_staleness_hours: 48,
      run_interval_minutes: 10080,
      max_candidates_per_run: 50,
      valid_days: 30,
      statement: "每周清一次：零转化搜索词的例行维护，与搜索词报告的更新节奏同频",
    },
  };

  //: 频次下拉：主语是人类节奏，机器值只作为 value 携带（Owner 第 4 点）。
  //  六个值全部满足 1440 % iv == 0 或 iv % 1440 == 0，故经 UI 签发永不触发 RUN_WINDOW_INCOMPATIBLE。
  const INTERVAL_OPTIONS = [
    { minutes: 10080, label: "每周检查一次", help: "系统允许的最慢节奏。再慢就没意义了——授权书本身最长 30 天就到期。" },
    { minutes: 4320, label: "每 3 天检查一次", help: "攒够证据再动手，候选更少但更准。" },
    { minutes: 1440, label: "每天检查一次（推荐）", help: "否定词依据的搜索词报告按天出，这是与数据同频的节奏。" },
    { minutes: 720, label: "每 12 小时检查一次", help: "早晚各一次。同一天的两次有可能读到同一份报告。" },
    { minutes: 360, label: "每 6 小时检查一次", help: "一天四次。多数站点的数据不会更新得这么快。" },
    { minutes: 60, label: "每小时检查一次（最快）", help: "系统允许的最快节奏。只有在确认数据源真的按小时刷新时才有意义。" },
  ];

  //: 授权书列表里的频次短文案（不带「推荐/最快」尾巴）。
  const INTERVAL_SHORT = {
    10080: "每周检查一次", 4320: "每 3 天检查一次", 1440: "每天检查一次",
    720: "每 12 小时检查一次", 360: "每 6 小时检查一次", 60: "每小时检查一次",
  };

  //: 高级区的 6 个数值参数（预设覆盖它们，"已微调 N 项"按它们比对）。
  const ADV_FIELDS = [
    ["lookback_days", "f-lookback"],
    ["min_spend_amount", "f-min-spend"],
    ["min_clicks", "f-min-clicks"],
    ["max_data_staleness_hours", "f-staleness"],
    ["max_runs_per_day", "f-runs-per-day"],
    ["max_candidates_per_run", "f-max-candidates"],
  ];

  //: 审计 #2（2026-08-29）：旧 FROZEN 空态是写给开发者的指令（MCP/Bearer/函数名），
  //  运营唯一能照做的「切到 AI 助手」会走进全按钮禁用的死胡同。改成运营视角的
  //  流程话术；开发者的手动触发方式收进 runbook，不在主界面指路。
  const SET_TAB_EMPTY = {
    FROZEN:
      "还没有待审的否定词。流程：先在上方签发一份授权书；再由人把授权书行上的「复制指令」交给 " +
      "Codex 等 AI 客户端，让它按这份授权生成候选——本系统不会自己定时运行，没有人发起就不会有候选。" +
      "生成后候选出现在这里等你审批；本页不会自己隔一会儿更新一次，点右上「刷新列表」查看。",
    APPROVED: "还没有已批集合——在「待批」页签核对证据后批准。服务重启会清空已批集合" +
      "（已下载到本机的 CSV 不受影响）：之前批过什么，以那些 CSV 文件为准。",
    REJECTED: "还没有已拒集合。",
  };

  const LEVEL_TEXT = { CAMPAIGN: "广告活动", AD_GROUP: "广告组", AD: "广告", TARGET: "投放" };
  const LEVEL_TEXT_SHORT = { campaign: "活动", ad_group: "广告组", ad: "广告", target: "投放" };
  //: 二轮审计：变更清单 CSV 是发给执行的人看的，「要做什么」一列不能是 SCALE_DAILY_BUDGET
  //  这种机器枚举。词表与动作下拉（index.html #wb-action）逐字同源。
  const ACTION_TEXT = {
    PAUSE: "暂停", ENABLE: "启用",
    SET_DAILY_BUDGET: "设日预算（绝对值）", SCALE_DAILY_BUDGET: "日预算按百分比增减",
    SET_BID: "设竞价（绝对值）", SCALE_BID: "竞价按百分比增减",
  };

  //: 2026-08-29 领星 IA 实测（docs/evidence/lx-ads-ia-20260829.md §5）：领星对运营说
  //  「投放中/已暂停」，不说 enabled/paused。显示中文、title 保留源侧原文；词表外的
  //  值原样显示，不猜翻译。
  const STATE_TEXT = { enabled: "投放中", paused: "已暂停", archived: "已归档" };
  function stateZh(s) { return (s && STATE_TEXT[s]) || s; }

  //: 投放方式徽标（领星行内 SP[手动]/SP[自动] 同款维度，§2）；值来自报表 targeting_type。
  const TARGETING_TEXT = { manual: "手动", auto: "自动" };

  //: 匹配方式中文（领星词汇）；词表外原样显示。
  const MATCH_TYPE_TEXT = { exact: "精确", phrase: "词组", broad: "宽泛" };

  //: 投放 expression 说人话（2026-08-29 真实镜像实测 7 种类型；领星「投放」页同款词汇）。
  //  镜像存源侧原文（单引号 repr 风格字符串），翻译只在展示层，悬停仍见原文。
  const EXPRESSION_TEXT = {
    queryHighRelMatches: "自动 · 紧密匹配",
    queryBroadRelMatches: "自动 · 宽泛匹配",
    asinSubstituteRelated: "自动 · 同类商品",
    asinAccessoryRelated: "自动 · 关联商品",
    asinSameAs: "商品定投",
    asinExpandedFrom: "拓展商品",
    asinCategorySameAs: "类目定投",
  };
  function friendlyTargetName(raw) {
    if (!raw || raw[0] !== "[") return null;
    // 宽容解析：逐段抓 type/value（单双引号都认），不做完整反序列化——
    // 解析不出就返回 null，让调用方显示原文，绝不编内容。
    // 二轮审计：值的引号按开头那只闭合（值内含另一种引号不再被截断成假名称），
    // 无引号数字值也认；段里有 value 键却抓不出值时整体放弃——宁给原文不给半截。
    const s = String(raw);
    const typeRe = /(["'])type\1\s*:\s*(["'])([^"']+)\2/g;
    const segs = [];
    let m;
    while ((m = typeRe.exec(s))) segs.push({ type: m[3], start: m.index });
    if (!segs.length) return null;
    const parts = [];
    for (let i = 0; i < segs.length; i++) {
      const seg = s.slice(segs[i].start, i + 1 < segs.length ? segs[i + 1].start : s.length);
      const label = EXPRESSION_TEXT[segs[i].type] || segs[i].type;
      const v = /(["'])value\1\s*:\s*(?:(["'])((?:(?!\2).)*)\2|(-?\d+(?:\.\d+)?))/.exec(seg);
      if (!v) {
        if (/(["'])value\1\s*:/.test(seg)) return null; // 有 value 却解析不出：放弃
        parts.push(label);
        continue;
      }
      const val = v[3] != null ? v[3] : v[4];
      parts.push(val ? label + " " + val : label);
    }
    return parts.join(" + ");
  }

  //: 绩效分桶快捷条（§3）：与服务端 PERF_BUCKETS 同名同序；"" = 未分桶（all）。
  //  四桶按漏斗互斥；判定指标缺失的行不落桶但仍算「全部」，所以四桶合计可以小于全部。
  const PERF_BUCKET_LABELS = [
    ["", "全部"],
    ["has_orders", "有成交"],
    ["clicks_no_orders", "有点击无成交"],
    ["impressions_no_clicks", "有曝光无点击"],
    ["no_impressions", "无曝光"],
  ];

  // ---------- 状态 ----------
  const state = {
    identities: [],
    token: null,
    sets: [],
    mandates: [],
    //: ui-5（2026-08-29 排查结论）：区分「拉取失败」与「确实为空」。失败若照渲染空态，
    //  界面与"系统里本来就没有"逐字相同，唯一线索是 15 秒后自动消失的红条——
    //  用户会以为授权书丢了而重签一份，造出重复授权。取值 loading | ok | error，
    //  与工作台 wb.status 同一套约定。
    setsStatus: "loading",
    setsError: null,
    mandatesStatus: "loading",
    mandatesError: null,
    tab: "FROZEN",
    //: 这一页不轮询。开着昨天的标签页回来的人，看到的「待批 0」是昨晚的快照，
    //  而他会把它读成「AI 昨晚没产出」。数字旁边写明读取时刻，最少让他能自己判断。
    loadedAt: null,
    //: 搜索词通道取值（LINGXING / MOCK*）。渲染徽章时顺手存下——「同一批词又回来了」
    //  在两个通道下的含义完全相反：Mock 数据不随时间变，每次生成都原样回来；
    //  真实通道取数只向领星要「未否定」的词，同一批还能回来说明那份 CSV 还没执行。
    termSource: null,
    expandedSets: new Set(),
    objectives: null,   // null = 服务端未提供就绪度清单（不编造快照）
  };

  //: 签发表单草案：作用域对象是从工作台**拷贝**过来的快照，
  //  拷贝之后工作台再改勾选不影响这份草案（不共享引用）。
  //  scopeItems 每项的 profile 记录带入时对象实际所属的店铺（ui-3/mandate-2，
  //  2026-08-29 排查结论）：签发时随每项上传，店铺填错时服务端据此拒绝，
  //  而不是把 A 店的对象静默塞进 B 店的授权书。
  const mandateDraft = {
    preset: "STEADY",
    scopeItems: [],     // [{level:"campaign"|"ad_group"|"target", external_id, name, profile}]
    scopeProfile: null,
  };

  const $ = (id) => document.getElementById(id);

  function currentIdentity() {
    return state.identities.find((i) => i.token === state.token) || null;
  }
  function isHuman() {
    const id = currentIdentity();
    return !!id && id.principal_type === "HUMAN"; // 非 HUMAN 一律按 AI 提示处理（fail-closed）
  }

  // ---------- DOM 构造（只用 textContent，不拼 HTML） ----------
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v == null) continue;
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k === "dataset") Object.assign(node.dataset, v);
        else if (k in node && typeof v === "boolean") node[k] = v;
        else node.setAttribute(k, v);
      }
    }
    for (const child of children) {
      if (child == null) continue;
      node.append(child);
    }
    return node;
  }

  // 时刻一律带时区。同一屏上「运行时段」显式写着 IANA 时区（Asia/Shanghai），
  // 而其余时刻是浏览器本地时间且不标注——人无从判断两者是不是同一把尺子。
  const TZ_LABEL = (() => {
    try {
      const parts = new Intl.DateTimeFormat("zh-CN", { timeZoneName: "short" }).formatToParts(new Date());
      const name = parts.find((p) => p.type === "timeZoneName");
      return name ? name.value : "";
    } catch { return ""; }
  })();

  function fmtTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    const text = d.toLocaleString("zh-CN", { hour12: false });
    return TZ_LABEL ? text + " " + TZ_LABEL : text;
  }

  //: 表格窄列用的短时刻「8/30 12:25」。完整值（含年份与时区）由调用点放进 title——
  //  短是为了让一列字排得开，不是为了少说；两者同时在，人不会因此少知道任何事。
  function fmtShortTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
  }

  // 报表窗口是 UTC 的整日边界，必须按 UTC 渲染日期：换算成本地时区会让日期整体
  // 漂一天，而人会拿这个日期去领星后台核对。window_end 是**排他**右端，
  // 显示时减一天还原成人话里的「最后一天」。
  function fmtUtcDate(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toISOString().slice(0, 10);
  }

  function fmtUtcLastDay(exclusiveEndIso) {
    if (!exclusiveEndIso) return "—";
    const d = new Date(exclusiveEndIso);
    if (Number.isNaN(d.getTime())) return String(exclusiveEndIso);
    return new Date(d.getTime() - 86400000).toISOString().slice(0, 10);
  }

  function shortId(id) {
    const s = String(id || "");
    return s.length > 8 ? s.slice(0, 8) : s;
  }

  function shortHash(h) {
    const s = String(h || "");
    return s.length > 22 ? s.slice(0, 16) + "…" + s.slice(-6) : s;
  }

  const pad2 = (n) => String(n).padStart(2, "0");

  // ---------- 提示条（非模态） ----------
  function showAlert(kind, message, code) {
    const region = $("alert-region");
    // warn 介于成功与错误之间：请求成功但结果需要人警觉（如同步被截断）——
    // 绿条=「全部办妥」，这种场合给绿条等于替服务端撒谎（2026-08-29 排查 workbench-1）。
    const cls = kind === "error" ? "alert-error"
      : kind === "warn" ? "alert-warn"
      : kind === "info" ? "alert-info" : "alert-success";
    // 同码错误合并计数，而不是叠加新条（否则连点会堆满屏幕）。
    if (code) {
      const same = [...region.children].find((n) => n.dataset.code === code);
      if (same) {
        const counter = same.querySelector(".alert-count");
        const n = (parseInt(counter.dataset.n || "1", 10) || 1) + 1;
        counter.dataset.n = String(n);
        counter.textContent = "×" + n;
        counter.hidden = false;
        return;
      }
    }
    const box = el(
      "div",
      { class: "alert " + cls, role: "status", dataset: { code: code || "" } },
      el("span", { text: message }),
      code ? el("span", { class: "alert-code", text: code }) : null,
      el("span", { class: "alert-count", hidden: true }),
      el("button", { class: "alert-close", type: "button", "aria-label": "关闭", text: "×" })
    );
    box.querySelector(".alert-close").addEventListener("click", () => box.remove());
    region.append(box);
    while (region.children.length > 3) region.firstChild.remove();
    // 错误条 15 秒后淡出（旧版永不消失，实测会连堆三条挡住内容）；警示条同错误条时长。
    setTimeout(() => box.remove(), kind === "error" || kind === "warn" ? 15000 : 6000);
  }

  function describeError(err) {
    if (err && err.code) {
      const zh = ERROR_TEXT[err.code];
      // 服务端如果把 detail 做成 {code, message}，把 message 拼在中文后面——
      // 「下一个窗口几点开」正是人最想知道的那一句，丢掉它等于把 fail-closed 做成哑巴。
      const tail = err.serverMessage ? "（服务端：" + err.serverMessage + "）" : "";
      return {
        message: (zh || "服务端返回错误：" + err.code) + tail,
        code: err.code + (err.status ? " · HTTP " + err.status : ""),
      };
    }
    return { message: "网络错误：无法连接本地服务（127.0.0.1）", code: err && err.message ? err.message : "" };
  }

  function alertError(err) {
    const d = describeError(err);
    showAlert("error", d.message, d.code);
  }

  // 错误体解析只此一处。此前 api() 与 exportSetCsv() 各写了一份，而只有前者认识
  // {code, message}——同一个服务端回复，走导出那条路的人只会看到一个 HTTP_422。
  async function readErrorDetail(res) {
    let code = "HTTP_" + res.status;
    let serverMessage = null;
    try {
      const data = await res.json();
      if (typeof data.detail === "string") code = data.detail;
      else if (data.detail && typeof data.detail === "object" && typeof data.detail.code === "string") {
        code = data.detail.code;
        if (typeof data.detail.message === "string") serverMessage = data.detail.message;
      } else if (data.detail != null) code = JSON.stringify(data.detail);
    } catch { /* 非 JSON 错误体，保留 HTTP_ 状态码 */ }
    return { code, serverMessage };
  }

  // ---------- API ----------
  function authHeaders() {
    return state.token ? { Authorization: "Bearer " + state.token } : {};
  }

  async function api(path, options) {
    const opts = options || {};
    const headers = Object.assign({}, authHeaders(), opts.body ? { "Content-Type": "application/json" } : {});
    const res = await fetch(path, {
      method: opts.method || "GET",
      headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (!res.ok) {
      const parsed = await readErrorDetail(res);
      const err = new Error(parsed.code);
      err.code = parsed.code;
      err.status = res.status;
      err.serverMessage = parsed.serverMessage;
      throw err;
    }
    return res.json();
  }

  // ---------- 数据通道状态（MOCK / 真实通道 / 未知三态徽章与页脚） ----------
  //
  // 2026-08-29 排查结论（ui-4/runtime-1）：徽章与页脚曾硬编码「全部是 Mock」，而
  // env 配齐 LX_MCP_KEY/LX_MCP_URL/ADS_CP_SYNC_PROFILES 时同一进程挂着真实领星
  // 通道，「同步镜像」真的调用领星生产 API——两种部署状态的界面输出逐字相同。
  // 现按 GET /dev/runtime-config（只回布尔与计数，不含 key/URL/店铺 ID）切三态。
  // 身份、审批与种子数据在任何状态下都仍是演示 Mock；「身份是演示的」与「同步
  // 通道是真实的」是两件独立的事，文案里分开说，不合并成一句。
  // 2026-08-30 补：此前这句话把「真实的」逐条列了出来——「同步与镜像数据是真实的」
  // ——而清单恰好漏掉了最要紧的一条：否定词候选的搜索词源。服务端算好的
  // search_term_source / search_term_profile_count 一直返回着，前端从不读。
  // 于是打开 ADS_CP_STRATEGY_LX_ENABLED 的人看到的是一份**看起来完整的清单**，
  // 而他正要批准的候选，花的是真店里的真钱。含糊的「已连接真实通道」只是没说全，
  // 一份点着名的清单停在半路，读起来就是说全了——这比含糊更坏。
  // 与 ui-4/runtime-1 是同一个病在下一层复发。
  //: lead 是这句话的**开头**，必须跟着候选的真假走。此前它写死「身份与审批数据为
  //  演示 Mock」，而同一句话往后两个分句就是「否定词候选也读真实搜索词」——自相
  //  矛盾，且人读到第一句就停了。审批数据就是待批队列里那些候选：策略通道打开时
  //  它们来自真实店铺，批准导出的 CSV 拿去领星执行会否掉真实关键词。
  //  （2026-08-30 排查 #11 修的是启动横幅里同样这句话；页脚是它的第二个出口。）
  //: 候选仍是演示数据的成因 → 徽章与页脚措辞。表里没有的取值不落这里，走末尾的
  //  「无法确认」——宁可说确认不了，也不替服务端挑一个成因说给人听。
  const MOCK_REASON_TEXT = {
    MOCK: {
      badge: "真实通道 · 同步",
      short: "否定词候选仍用演示数据（未开策略开关）",
    },
    MOCK_NO_CREDENTIALS: {
      badge: "真实通道 · 候选未接",
      short: "否定词候选仍用演示数据：策略开关已开，但 LX_MCP_KEY/LX_MCP_URL 缺失",
    },
    MOCK_NO_PROFILES: {
      badge: "真实通道 · 候选未接",
      short: "否定词候选仍用演示数据：策略开关已开，但同步白名单 ADS_CP_SYNC_PROFILES " +
        "是空的（空 = 一个店都不许碰）",
    },
    MOCK_LOOKUP_FAILED: {
      badge: "真实通道 · 名录取不到",
      short: "否定词候选仍用演示数据：策略开关已开，但取店铺名录时网关调用失败" +
        "（key/URL/网络）——不是绑定配错了",
    },
    MOCK_NO_BINDINGS: {
      badge: "真实通道 · 候选绑定失败",
      short: "否定词候选仍用演示数据：策略开关已开、名录也取到了，但白名单内没有一个店" +
        "同时拿得到 sid 与币种",
    },
  };

  function termChannelText(termSource, termProfileCount) {
    if (termSource === "LINGXING") {
      return {
        badge: "真实通道 · 含候选",
        lead: "身份（demo token）是演示 Mock，但待批候选来自真实店铺",
        short:
          "否定词候选也读真实搜索词（" + termProfileCount +
          " 个店铺已绑定），批准导出的 CSV 会否掉真实关键词",
      };
    }
    //: 挂 Mock 的成因逐个说（2026-08-30 排查两轮）。说错了人就去改一个本来就是对的
    //  设置：第一轮只分了「开关没开」与「没绑上」，而「没绑上」自己还有三个成因，
    //  文案只好点名其中一个——点的偏偏不是最常见的那个。ADS_CP_SYNC_PROFILES 在
    //  .env.example 里缺省为空，首次运行必然落在 MOCK_NO_PROFILES，而文案叫人去查
    //  店铺 sid 和币种，两件都白做，真正要设的那个变量一个字都没提。
    const mockReason = MOCK_REASON_TEXT[termSource];
    if (mockReason) {
      return { badge: mockReason.badge, lead: "身份与审批数据为演示 Mock", short: mockReason.short };
    }
    // 服务端没报这个字段：不猜。宁可说确认不了，也不替它宣布候选是假的。
    return {
      badge: "真实通道",
      lead: "身份为演示 Mock；待批候选是真是假无法确认",
      short: "否定词候选的数据源无法确认",
    };
  }

  function channelState(mode, profileCount, termSource, termProfileCount) {
    const sod = "批准 / 拒绝 / 签发 / 撤销的权限约束由服务端强制，界面提示仅为预告。";
    if (mode === "real") {
      const scope = "（" + profileCount + " 个店铺在同步白名单内）";
      const term = termChannelText(termSource, termProfileCount);
      return {
        badgeClass: "badge badge-real-channel",
        badgeText: term.badge,
        title:
          term.lead + "。已连接领星真实通道" + scope +
          "，同步与镜像数据是真实的；" + term.short,
        footer:
          term.lead + "。本服务已连接领星真实通道" + scope +
          "，「同步镜像」会调用领星生产 API，同步与镜像数据是真实的；" + term.short +
          "。服务仅绑定 127.0.0.1。" + sod,
      };
    }
    if (mode === "mock") {
      return {
        badgeClass: "badge badge-mock",
        badgeText: "演示数据",
        title: "全部数据为演示假数据（Mock），无真实凭据、无真实平台连接",
        footer: "全部数据为演示假数据（Mock），服务仅绑定 127.0.0.1。" + sod,
      };
    }
    // 未知态 = index.html 的静态初始态：确认不了就说确认不了，两边都不冒认。
    return {
      badgeClass: "badge badge-channel-unknown",
      badgeText: "通道未知",
      title: "无法确认数据通道状态",
      footer:
        "无法确认数据通道状态：请勿假定数据为 Mock，也勿假定已连接真实通道。" +
        "服务仅绑定 127.0.0.1。" + sod,
    };
  }

  function renderChannelState(mode, profileCount, termSource, termProfileCount) {
    state.termSource = termSource || null;
    //: 同步按钮的禁用理由要按这个值分支——「不在白名单」与「这台服务根本没配通道」
    //  指向两个完全不同的动作，而后者时前者是白做（见 renderWbSyncButton）。
    state.channelMode = mode;
    const s = channelState(mode, profileCount, termSource, termProfileCount);
    const badge = $("channel-badge");
    badge.className = s.badgeClass;
    badge.textContent = s.badgeText;
    badge.title = s.title;
    $("app-footer").textContent = s.footer;
  }

  async function loadRuntimeConfig() {
    // 不 throw：通道状态确认失败不拦整个界面，但**绝不**在失败时谎称 Mock——
    // 维持「未知」灰态。响应形状不对（布尔缺失）同样按未知处理，不猜。
    try {
      const cfg = await api("/dev/runtime-config");
      if (cfg && typeof cfg.lx_channel_configured === "boolean") {
        renderChannelState(
          cfg.lx_channel_configured ? "real" : "mock",
          Number(cfg.sync_profile_count) || 0,
          typeof cfg.search_term_source === "string" ? cfg.search_term_source : null,
          Number(cfg.search_term_profile_count) || 0
        );
        return;
      }
    } catch { /* 网络或服务错误 → 落到未知态 */ }
    renderChannelState("unknown", 0, null, 0);
  }

  // ---------- 身份（人 / AI 二态切换器） ----------
  async function loadIdentities() {
    // 兼容两种响应形状：裸数组，或 {identities:[...]}（本地演示组合根实测为后者）。
    const raw = await api("/dev/identities");
    const list = Array.isArray(raw) ? raw : raw && Array.isArray(raw.identities) ? raw.identities : [];
    state.identities = list.filter((i) => i && i.token);
    renderConnectHelp();   // 名录到手才知道 AI 身份的 token 是哪一个
    const box = $("identity-switch");
    box.textContent = "";
    for (const id of state.identities) {
      box.append(el("button", {
        type: "button",
        class: "identity-opt",
        role: "radio",
        "aria-checked": "false",
        dataset: { token: id.token },
        text: id.display_name || id.identity || id.token,
      }));
    }
    if (state.identities.length > 0) state.token = state.identities[0].token; // 人在前
    renderIdentityMeta();
  }

  function renderIdentityMeta() {
    const meta = $("identity-meta");
    meta.textContent = "";
    const id = currentIdentity();
    for (const btn of $("identity-switch").querySelectorAll(".identity-opt")) {
      const on = btn.dataset.token === state.token;
      btn.setAttribute("aria-checked", on ? "true" : "false");
      btn.classList.toggle("is-active", on);
    }
    //: 量顶栏必须排在早退之前。身份名录取不回来时（端点失败、纯 Mock 部署下为空）
    //  这里原本直接 return，--topbar-h 于是永不写入，抽屉与滚动目标全按 116px 的
    //  回退值定位——而实测顶栏 117px 起、窄屏换行后更高。顶栏的高度和名录取没取到
    //  没有关系，它就在那儿。
    syncTopbarHeight();
    syncDrawerFit();
    if (!id) return;
    const human = id.principal_type === "HUMAN";
    const roles = Array.isArray(id.roles) ? id.roles.join(",") : String(id.roles || "");
    meta.append(
      el("span", {
        class: "badge " + (human ? "badge-human" : "badge-ai"),
        text: human ? "人" : "AI",
        title: "principal_type=" + String(id.principal_type) + " · roles=" + roles,
      }),
      el("span", { class: "identity-cap", text: id.capabilities || "" })
    );
    // 徽章挂上去之后栏可能变高（窄屏会换行），再量一次。
    syncTopbarHeight();
    syncDrawerFit();
  }

  // ---------- 数据刷新 ----------
  //: ui-5（2026-08-29 排查结论）：两个端点各自拉取、各自记错。原先 Promise.all 一损俱损，
  //  任一失败就把两份数据一起清空渲染成空态；拆开后失败面板显示失败态 + 重试，
  //  重试只重刷自己，不连累另一个面板。
  async function loadMandates() {
    state.mandatesStatus = "loading";
    state.mandatesError = null;
    try {
      const res = await api("/mandates");
      state.mandates = res.mandates || [];
      state.mandatesStatus = "ok";
    } catch (err) {
      // 拉取失败**不得**渲染成"还没有授权书"——那是把错误说成事实（与工作台同一纪律）。
      state.mandates = [];
      state.mandatesStatus = "error";
      state.mandatesError = describeError(err);
      alertError(err); // 红条照旧弹（15 秒自动消失）；面板内的失败态不随红条消失
    }
  }

  async function loadSets() {
    state.setsStatus = "loading";
    state.setsError = null;
    try {
      const res = await api("/candidate-sets");
      state.sets = res.candidate_sets || [];
      state.setsStatus = "ok";
    } catch (err) {
      state.sets = [];
      state.setsStatus = "error";
      state.setsError = describeError(err);
      alertError(err);
    }
  }

  //: 面板级重试入口：只重刷对应面板；KPI 依赖两份数据，跟着重算。
  async function refreshMandates() {
    await loadMandates();
    renderProfileFilter();
    renderKpis();
    renderMandates();
    renderSodUi();
  }

  async function refreshSets() {
    await loadSets();
    renderProfileFilter();
    renderKpis();
    renderSets();
  }

  async function refreshAll() {
    await Promise.all([loadMandates(), loadSets()]);
    state.loadedAt = new Date();
    renderProfileFilter();
    renderKpis();
    renderMandates();
    renderSets();
    renderSodUi();
  }

  //: 「这些数字是几点读的」。两个消费者：KPI 条与「刷新列表」按钮旁。
  function renderLoadedAt() {
    const node = $("loaded-at");
    if (!node) return;
    node.textContent = state.loadedAt
      ? "读取于 " + state.loadedAt.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })
      : "";
    //: 「本页不会自动更新」这句话在同一个文件里是假的（2026-09-06 排查）：
    //  下面那个 visibilitychange 监听器会在切走再回来、且距上次读取超过一分钟时
    //  自动重读一次。那个行为本身是对的（开着昨天的标签页回来的人会把「待批 0」
    //  读成「AI 昨晚没产出」），说谎的是这句文案。留着它，人会拿一个自己不知道
    //  发生过的重读当成"我上次看到的那一屏"，而两屏之间可能已经差了一整晚。
    node.title = state.loadedAt
      ? "数字是这个时刻从本地服务读到的。本页不轮询——只在你切走再回来（距上次读取超过一分钟）" +
        "时自动重读一次，其余时候点「刷新列表」。"
      : "";
  }

  //: ui-5（2026-08-29 排查结论）：面板级失败态。结构与样式照抄工作台 renderWbTable 的
  //  失败分支（.error-state），不另发明一套；retryAction 由全局委派点击分发。
  function loadErrorState(error, retryAction) {
    const e = error || { message: "未知错误", code: "" };
    return el("div", { class: "error-state" },
      el("div", null, el("strong", { text: "加载失败：" }), el("span", { text: e.message })),
      e.code ? el("span", { class: "chip mono", text: e.code }) : null,
      el("button", { class: "btn btn-sm", type: "button", text: "重试", dataset: { action: retryAction } })
    );
  }

  // ---------- KPI ----------
  function renderKpis() {
    const now = Date.now();
    // ui-5（2026-08-29 排查结论）：拉取失败时显示「—」而不是 0——
    // 「0 份活跃授权书」是会驱动人去重签的事实断言，而失败时系统并不知道这个事实。
    const setsOk = state.setsStatus === "ok";
    const mandatesOk = state.mandatesStatus === "ok";
    //: ui-6（2026-08-30 排查结论）：同屏两个 KPI 曾用两套口径——「活跃授权书」把到期的
    //  排除在外，「待批集合」却把过期的一并算进去。过期的 FROZEN 集合批准必被服务端
    //  拒绝（只剩「拒绝后重新生成」一条路），把它计进「等待你审批」是在派一件办不成的活。
    //  过期数不隐藏，单列进副文案——「3 待批」和「3 待批 + 5 已过期」是两种不同的处境。
    //: 同一条纪律往下推一格（2026-08-30 排查 #12）：来源授权书已被撤销的集合，
    //  批准必被服务端拒（409 MANDATE_REVOKED），与过期集合处境相同——撤销时人
    //  刚被告知「AI 立即停止按它运行」，它生出来的东西却还在催人审批。
    //: KPI 与下面的列表必须口径一致。筛了某家店却仍给全店合计，人读到的是
    //  「待批 3」配着一张卡片——他会以为另外两张藏在哪儿没找到。
    const scoped = state.sets.filter(matchesProfileFilter);
    const frozen = scoped.filter((s) => s.state === "FROZEN");
    const expiredPending = frozen.filter((s) => s.expired === true).length;
    const revokedPending = frozen.filter(
      (s) => s.expired !== true && s.mandate_state === "REVOKED"
    ).length;
    //: 内容相同的重复不从计数里剔除——它们确实是各自独立、各自可批的集合，
    //  剔除就是替人做主。但「3 份待批」里有 2 份是同一批词，这件事必须说出来。
    const pendingIds = new Set(
      frozen.filter((s) => s.expired !== true && s.mandate_state !== "REVOKED").map((s) => s.set_id)
    );
    //: 只数**待批之间**的重复。孪生若已批准或已拒绝，那不是「这一屏有重复要处理」，
    //  是另一回事（卡片上单独说），混进这个数会让人以为待批里还有一份要处理。
    const duplicatePending = [...pendingIds].filter((id) => {
      const s = frozen.find((x) => x.set_id === id);
      return Array.isArray(s.same_content_as) && s.same_content_as.some((o) => pendingIds.has(o));
    }).length;
    const pending = frozen.length - expiredPending - revokedPending;
    const approved = scoped.filter((s) => s.state === "APPROVED").length;

    //: 「生效中」不等于「跑得通」。#23 之前这两件事在界面上无法区分，人看到
    //  「2 份活跃授权书」就以为系统在替他干活，而它们可能每一次运行都在失败。
    //: 「没跑通」与「跑通了但没判断完」是两件事，副文案不能都说成前者。
    //  服务端把 needs_attention 放宽到也覆盖后者之后（2026-08-30），这句仍写死
    //  「上次运行没跑通」——而那份授权刚刚送来 7 条待批。同屏正下方的琥珀行写着
    //  「这份授权上次跑通了，但没判断完」，两句话互相否定：信 KPI 的人会去撤销
    //  一份正在正常工作的授权，不信的人会认定整块 KPI 数字不可信。
    const live = state.mandates.filter(
      (m) => matchesProfileFilter(m) && m.state === "ACTIVE" && new Date(m.expires_at).getTime() > now
    );
    const needy = live.filter((m) => m.needs_attention === true);
    const broken = needy.filter((m) => !runOutcomeIsOk(m.last_outcome)).length;
    //: 第三种处境：跑通了、也判断完了，但查出来的浪费本系统否不掉（ALL_ASIN）。
    //  并进「没跑通」是假话，并进「没判断完」也是假话，而它要人做的事最具体——
    //  去领星手工否定几个 ASIN。三种处境三句话，合并任意两种都会让人拿不准该干什么。
    const asinOnly = needy.filter((m) => m.last_outcome === "ALL_ASIN").length;
    const incomplete = needy.filter(
      (m) => runOutcomeIsOk(m.last_outcome) && m.last_outcome !== "ALL_ASIN"
    ).length;
    const active = live.length;
    const mandateNote = $("kpi-mandates-note");
    if (mandateNote) {
      const parts = [];
      if (broken > 0) parts.push("其中 " + broken + " 份上次运行没跑通");
      if (incomplete > 0) parts.push(incomplete + " 份跑通了但没判断完");
      if (asinOnly > 0) parts.push(asinOnly + " 份查出的浪费要去领星手工否定");
      mandateNote.textContent = mandatesOk && parts.length
        ? "生效中且未到期的授权；" + parts.join("，")
        : "生效中且未到期的授权";
    }
    const pendingNote = $("kpi-pending-note");
    if (pendingNote) {
      const notes = [];
      if (expiredPending > 0) notes.push("另有 " + expiredPending + " 份已过期，只能拒绝后重新生成");
      if (revokedPending > 0) notes.push(revokedPending + " 份来自已撤销的授权书，批准会被拒");
      if (duplicatePending > 0) notes.push("其中 " + duplicatePending + " 份与另一份内容相同");
      pendingNote.textContent = setsOk && notes.length
        ? "等待你审批的否定词；" + notes.join("；")
        : "等待你审批的否定词";
    }
    $("kpi-pending").textContent = setsOk ? String(pending) : "—";
    $("kpi-approved").textContent = setsOk ? String(approved) : "—";
    $("kpi-mandates").textContent = mandatesOk ? String(active) : "—";
    //: 页签上带数字：人现在要看的是「待批」，而三个页签在没有数字时长得一模一样，
    //  非当前页签里有没有东西，只能一个个点过去才知道。
    //: 页签上的数必须等于点进去看到的行数（2026-09-06 排查两条）。
    //  一，「已拒」此前数的是 state.sets 全集，没跟店铺筛选走：筛到 B 店时页签
    //  写着全店的 3，点进去只有 1 张卡，人会去找另外两张。
    //  二，「待批」此前用的是 KPI 那个数（扣掉过期与来源授权书已撤销的），而列表
    //  照样把它们画出来——页签写 1、屏幕上 3 张卡。两个数干的是两件事：KPI 回答
    //  「你手上有多少真能干的活」（所以扣，并在副文案里单列），页签回答「这个标签
    //  底下有多少行」。合并任意一边都会让另一边说谎，所以分开算，并在两者不等时
    //  由页签的 title 说清差在哪。
    const tabRows = { FROZEN: frozen.length,
      APPROVED: approved,
      REJECTED: scoped.filter((s) => s.state === "REJECTED").length };
    const tabNames = { FROZEN: "待批", APPROVED: "已批", REJECTED: "已拒" };
    const notActionable = expiredPending + revokedPending;
    for (const t of $("set-tabs").querySelectorAll(".tab")) {
      const n = tabRows[t.dataset.state];
      t.textContent = tabNames[t.dataset.state] + (setsOk ? "（" + n + "）" : "");
      t.title = t.dataset.state === "FROZEN" && setsOk && notActionable > 0
        ? "这里有 " + n + " 张卡片，其中 " + notActionable +
          " 张已经批不了（过期，或来源授权书已撤销）——上面 KPI 的「" + pending +
          "」只数还能批的那些"
        : "";
    }
    renderLoadedAt();
  }

  // ---------- 运行窗口 / 作用域的人话合成 ----------
  function tzPhrase(tz) {
    const alias = TZ_TEXT[tz];
    return alias ? alias + " · " + tz : tz + " 当地时间";
  }

  //: 「每天 02:00–18:00（吉隆坡时间 · Asia/Kuala_Lumpur）」。
  //  end=0（或 24）表示当日午夜：那不是跨午夜，是「跑到今天结束」，措辞必须区分开。
  function windowPhrase(startHour, endHour, tz) {
    const endIsMidnight = endHour === 0 || endHour === 24;
    const endLabel = endIsMidnight ? "24:00" : pad2(endHour) + ":00";
    const body = !endIsMidnight && startHour > endHour
      ? "每天 " + pad2(startHour) + ":00 至次日 " + endLabel
      : "每天 " + pad2(startHour) + ":00–" + endLabel;
    return body + "（" + tzPhrase(tz) + "）";
  }

  //: 服务端如果直接给了人话摘要（run_window_summary / scope_summary）就用它；
  //  只给了事实对象就在前端合成。两种形状都接受，缺失即"全天 / 整店"。
  function mandateWindowText(m) {
    if (typeof m.run_window_summary === "string" && m.run_window_summary.trim()) {
      return m.run_window_summary;
    }
    const w = m.run_window;
    if (!w || w.start_hour == null) return "不限时段";
    return windowPhrase(Number(w.start_hour), Number(w.end_hour), String(w.timezone || ""));
  }

  function mandateScopeText(m) {
    if (typeof m.scope_summary === "string" && m.scope_summary.trim()) return m.scope_summary;
    const s = m.scope;
    if (!s || !s.kind) return "整店";
    if (s.kind === "PROFILE" || s.kind === "ENTIRE_PROFILE") return "整店";
    const items = Array.isArray(s.items) ? s.items : [];
    const n = s.object_count != null ? Number(s.object_count) : items.length;
    return "勾选 " + n + " 个对象";
  }

  //: 审计 #9（2026-08-29）：签完授权书列表里只剩「勾选 N 个对象」计数，一周后
  //  没人知道管的是哪几条——服务端现在随摘要回 scope_items（含镜像名称），
  //  悬停徽章即见名单；名称缺失退回 ID，不编造。
  function mandateScopeTitle(m) {
    const items = Array.isArray(m.scope_items) ? m.scope_items : [];
    if (items.length === 0) return null;
    const head = items.slice(0, 8).map((i) =>
      (LEVEL_TEXT_SHORT[String(i.level).toLowerCase()] || i.level) + " " + (i.name || i.external_id));
    return head.join("\n") + (items.length > 8 ? "\n… 共 " + items.length + " 个" : "");
  }

  // ---------- 授权书列表 ----------
  // 审计 #31/#42：状态词全站一套（领星词汇），同列不再中英混排；原文进悬停。
  function mandateStateBadge(m) {
    if (m.state === "REVOKED") {
      return el("span", { class: "state-badge state-revoked", text: "已撤销", title: "REVOKED" });
    }
    const expired = new Date(m.expires_at).getTime() <= Date.now();
    if (expired) return el("span", { class: "state-badge state-expired", text: "已过期" });
    return el("span", { class: "state-badge state-active", text: "生效中", title: "ACTIVE" });
  }

  //: 一次运行的结局 → 人话 + 该干什么。
  //  ui-6（2026-08-30 排查 #23）：签发之后这张卡片再也不会变化。币种签错、店铺没接
  //  数据源、作用域把对象全挡掉、整批数据太旧，四种「这份授权根本跑不通」与「一切
  //  正常、这段时间确实没有该否的词」在界面上逐字同形：徽章「生效中」+ 待批空态。
  //  人得到的唯一信号是「没有新东西要批」，读出来是好消息。
  //  next 一栏是**下一步动作**，不是原因复述——只说原因等于把排查工作原样丢回给人。
  const RUN_OUTCOME_TEXT = {
    CANDIDATES: { ok: true, text: "产出候选", next: "去「待批」页签审批" },
    //: 「这段窗口确实干净」只在一条都没弃权时才是真话。有弃权时改口由
    //  runOutcomeSpec 负责（部分弃权也会落到这个码上）。
    NO_CANDIDATES: { ok: true, text: "没有该否的词", next: "不用做什么，这段窗口确实干净" },
    ALL_ABSTAINED: { text: "数据太旧，全部弃权", next: "等新数据，或重签一份放宽时效上限的授权" },
    //: 与 NO_CANDIDATES 合并是这里最贵的一种合并：那句「这段窗口确实干净」会让人
    //  安心地什么都不做，而这一批每一条都是花了钱、零转化的浪费，只是本系统开出的
    //  否定精准关键词挡不住 ASIN 型来源。
    //: ok:true 是因为这一轮**确实跑通了**：取到了行、判断完了、结论也对——只是本系统
    //  的手段（否定精准关键词）对这批浪费无效。写成 false 会让 KPI 说「上次运行没跑通」，
    //  人去查一份工作正常的授权。而 alert 是因为另一句默认文案「跑通了但没判断完」
    //  同样是假话：它判断完了。两句现成的话对它都不成立，所以自带一句。
    ALL_ASIN: {
      ok: true,
      alert: "这份授权上次跑通了，但查出的浪费本系统否不掉",
      text: "浪费的词全是 ASIN，本系统否不掉",
      next: "去领星「否定投放」手工否定这些 ASIN——加否定关键词对它们无效",
    },
    SCOPE_EMPTY: { text: "圈的对象没有数据", next: "这份授权圈的广告在这段数据里一条都没出现——改作用域后重签" },
    NO_ROWS: { text: "这段窗口一行数据都没有", next: "确认这家店这段时间在不在投放，或换更长的回看天数重签" },
    //: 与 NO_ROWS 是两回事，合并会把人指向两个注定无效的动作（拉长窗口、确认投放）。
    NO_USABLE_ROWS: { text: "取回了数据，但一条都读不出来", next: "拉长窗口和确认投放都没用——这是源侧行的形状问题，去看那批行缺了什么" },
    NO_DATA_SOURCE: { text: "这家店没接数据源", next: "这份授权现在跑不出任何东西——先把该店的搜索词数据源接上" },
    SOURCE_ERROR: { text: "取数失败", next: "看错误码：超时类可以再发起一次，参数类错误重试永远不会成功" },
    REJECTED: { text: "数据口径对不上", next: "多半是币种签错了——按该店实际币种重签一份" },
  };

  //: 最近一次运行「有多少东西没被判断」。两个单位不同、互不重叠，分开说不相加：
  //  一个数 (广告组,词) 组，一个数连归属都读不出来的行。加起来是个无意义的数。
  function unjudgedText(m) {
    const latest = (m.recent_runs || [])[0];
    if (!latest) return "";
    const parts = [];
    if (latest.unjudged_ad_group_terms > 0) {
      parts.push("有 " + latest.unjudged_ad_group_terms + " 组(广告组×搜索词)没有被判断过");
    }
    //: 弃权也是「没判断完」的一种（2026-09-06 排查）。全量弃权有 ALL_ABSTAINED /
    //  ALL_ASIN 自己的文案兜着，**部分**弃权此前一路落到 NO_CANDIDATES，
    //  而那句「不用做什么，这段窗口确实干净」正好是它在否定的话——同一轮里可能
    //  躺着这个店最大的一笔零转化花费，只是那行数据太旧、判不了。判不了不是干净。
    if (
      latest.abstain_count > 0 &&
      m.last_outcome !== "ALL_ABSTAINED" &&
      m.last_outcome !== "ALL_ASIN"
    ) {
      parts.push("有 " + latest.abstain_count +
        " 条没判成（数据太旧，或是本系统否不掉的 ASIN），它们不在这一轮的结论里");
    }
    if (latest.unattributable_rows > 0) {
      //: 这一项恒为全店口径——连广告组是谁都读不出来的行，说不出它落在哪份授权的
      //  范围里。不写明的话，人会以为这些行就在自己圈的那几个活动下面。
      parts.push("有 " + latest.unattributable_rows +
        " 行连属于哪个广告组都读不出来（全店口径，说不出是否落在本授权范围内）");
    }
    if (!parts.length) return "";
    //: 尾句要跟着这一轮**实际**的结局说，不能写死一句。此前恒为「『没有该否的词』
    //  只对读得懂的那部分成立」——可这一轮若产出了候选，那句结论根本没发生过，
    //  人读到的是一句针对别的运行的话，只会疑惑「谁说没有该否的词了」。
    //  三种结局要提醒的不是同一件事：空手而归时要防「这个店很干净」被读成事实，
    //  产出候选时要防「这 7 条就是全部」被读成事实，而**没跑通**的那几种结局
    //  （数据太旧全部弃权 / 作用域全挡掉 / 币种对不上）压根没得出任何结论——
    //  对它们说「『没有该否的词』只对读得懂的那部分成立」是在引用一句没人说过的话。
    //  那几种要提醒的是另一件事：把上面那个问题修好，这些组仍然不会被判断。
    const tail = m.last_outcome === "CANDIDATES"
      ? "这一轮产出的候选只覆盖读得懂的那部分，没判断的那些里可能还有该否的词。"
      : m.last_outcome === "NO_CANDIDATES"
        ? "「没有该否的词」只对读得懂的那部分成立，不等于这个店这段窗口没有浪费。"
        : "它们没进入任何结论，而上面那个问题修好之后它们仍然不会被判断，是另一件事。";
    return "这一轮" + parts.join("，另") + "——" + tail;
  }

  //: 这个结局算不算「跑通了」。RUN_OUTCOME_TEXT 里标了 ok 的两项（产出候选 /
  //  没有该否的词）才算；未知结局按「没跑通」算——不认识的东西不许当好消息。
  function runOutcomeIsOk(outcome) {
    const spec = RUN_OUTCOME_TEXT[outcome];
    return !!(spec && spec.ok);
  }

  //: 人 → AI 的交接物（2026-09-06 审核）。授权书行是整个流程里唯一同时握着
  //  profile 与完整 mandate_id 的地方，而表格只印得下 8 位前缀——服务端做的是
  //  uuid.UUID() 精确解析（strategy_service.py），把那 8 位贴给 AI 必得 MANDATE_UNKNOWN。
  //  复制的是整句指令而不只是 ID：交接断在「不知道该跟 AI 说什么」的次数，
  //  不比断在「抄错 ID」少。与 docs/runbook-local-demo.md §② 同源，改一处要改两处。
  //: 第②步的接入信息。界面反复把人指向「Codex 等 AI 客户端」，却从没说过怎么接
  //  （2026-09-06 排查）。地址取当前实际 origin，token 取身份名录里的 AI 那条——
  //  两者都不写死，换端口、换部署都不会变成假话。
  function renderConnectHelp() {
    const box = $("connect-body");
    const wrap = $("connect-help");
    if (!box || !wrap) return;
    const ai = (state.identities || []).find((i) => i.principal_type === "AI_CLIENT");
    if (!ai) { wrap.hidden = true; return; }
    const endpoint = location.origin + "/mcp";
    const cmd =
      "export ADS_CP_MCP_TOKEN=" + ai.token + "\n" +
      "codex mcp add ads-control-plane --url " + endpoint +
      " --bearer-token-env-var ADS_CP_MCP_TOKEN";
    box.textContent = "";
    box.append(
      el("p", { text: "本服务的 MCP 面就在这台机器上。把下面两条粘进终端，Codex 会话里就能调用它。" }),
      el("pre", { class: "mono", text: cmd }),
      //: 这两条要一字不差地进终端，而它比屏幕宽（窄屏上要横着推着看才能全选中）。
      //  「复制指令」按钮在授权书那一行早就有了，这里却要人手选——手选正是最容易
      //  漏掉尾巴的地方，而漏掉 --bearer-token-env-var 的后果是接上了、调不通。
      el("button", { class: "btn btn-sm", type: "button", text: "复制这两条",
        dataset: { action: "copy-hash", hash: cmd } }),
      el("p", { class: "field-note", text:
        "接好之后，在授权书那一行点「复制指令」，把复制到的那句话说给 Codex 即可——" +
        "它内含完整授权书 ID（表格只显示前 8 位，服务端不认前缀）。" }),
      el("p", { class: "field-note", text:
        "这是本地演示的固定 token（" + (ai.display_name || ai.identity) +
        "），只在这台机器上有效，与真实领星/Amazon 零连接。别的客户端只要支持 " +
        "streamable HTTP MCP，用同一个地址和 Bearer 头也能接；细节见仓库里的 " +
        "docs/codex-connect.md。" })
    );
    wrap.hidden = false;
  }

  //: 参数一句话。授权书行与即席集合卡片必须逐字同源——同一组阈值在两处写成两句话，
  //  人会以为是两回事（2026-09-06）。
  function packSummaryText(pack) {
    if (!pack) return "";
    const spend = pack.min_spend || {};
    return "回看 " + pack.lookback_days + " 天 · ≥" + pack.min_clicks + " 点击 · ≥" +
      (spend.amount || "?") + " " + (spend.currency || "") +
      " · 时效 ≤" + pack.max_data_staleness_hours + "h";
  }

  function mandateRunCommand(m) {
    return "调用 ads-control-plane 的 generate_negation_candidate_set，参数 profile_external_id=" +
      m.profile_external_id + "，mandate_id=" + m.mandate_id;
  }

  //: 「签发已多久」。只说时长不下判断——「超期」要看这份授权自己的间隔与时段，
  //  阈值判断留给下面的琥珀行，这一格只给事实。
  function mandateAgeText(m) {
    const ms = Date.now() - new Date(m.issued_at).getTime();
    if (!Number.isFinite(ms) || ms < 0) return "已签发";
    const hours = Math.floor(ms / 3600000);
    if (hours < 1) return "刚签发";
    if (hours < 48) return "签发已 " + hours + " 小时";
    return "签发已 " + Math.floor(hours / 24) + " 天";
  }

  function mandateRunLine(m) {
    // 「从没跑过」与「跑过但没结果」是两件事，不合成一句。
    if (!m.run_count_known) {
      //: 签了 5 分钟与签了 4 天在这一格里此前逐字相同，而两者的含义完全相反：
      //  前者正常，后者说明根本没人去发起过。手机上没有悬停，所以这句要可见。
      return el("span", {
        class: "cell-sub",
        text: mandateAgeText(m) + "，还没跑过",
        title: "签发后还没有过一次运行。本系统不会自己到点运行——要有人拿这一行的「复制指令」" +
          "去 Codex 等 AI 客户端里发起。被时段、最小间隔或配额挡回的尝试不会记在这里。",
      });
    }
    const spec = RUN_OUTCOME_TEXT[m.last_outcome] || { text: m.last_outcome || "未知结果", next: "" };
    // 这一列只有 8 列表格里的一小条宽度：完整时刻（带年份与时区）会被拆成竖排
    // 单字。短形式够人回答「是刚跑的还是几天前跑的」，完整值进 title 不丢。
    return el("span", { class: "cell-sub run-line", title: "上次运行 " + fmtTime(m.last_run_at) },
      el("span", { class: "num", text: fmtShortTime(m.last_run_at) }),
      el("span", { text: spec.text })
    );
  }

  //: 「现在能不能跑」——两个数都由服务端算好（runs_remaining_today / next_run_allowed_at），
  //  与 MCP 面给 AI 的是同一对。已撤销/已过期的授权不画：它不会再跑了，配额还剩几次
  //  不是待办，只会跟真正要处理的那几份抢注意力。
  function quotaNowLine(m) {
    if (m.state !== "ACTIVE" || new Date(m.expires_at).getTime() <= Date.now()) return null;
    if (typeof m.runs_remaining_today !== "number") return null;
    const next = m.next_run_allowed_at ? new Date(m.next_run_allowed_at).getTime() : 0;
    const gated = next > Date.now();
    //: 间隔比配额紧、且闸门此刻关着时，「今天还能跑 N 次」是一笔花不掉的钱：
    //  闸门要到下一个配额日才开，那时今天剩的 N 次早已作废。它读起来是邀请，
    //  而人照着它去叫 AI，换回来的是 RUN_TOO_SOON（2026-09-07 排查）。
    //  「今天的次数已用完」不在此列——那句是真的，且正是人要知道的。
    const bd = m.bounds || {};
    const unspendable =
      gated &&
      m.runs_remaining_today > 0 &&
      intervalCapsRunsAt(bd.run_interval_minutes, bd.max_runs_per_day) !== null;
    const parts = [];
    if (!unspendable) {
      parts.push(m.runs_remaining_today > 0
        ? "今天还能跑 " + m.runs_remaining_today + " 次"
        : "今天的次数已用完");
    }
    if (gated) parts.push("最早 " + fmtShortTime(m.next_run_allowed_at) + " 可再发起");
    const blocked = m.runs_remaining_today <= 0 || gated;
    return el("span", {
      class: "cell-sub" + (blocked ? " quota-blocked" : ""),
      text: parts.join(" · "),
      title: blocked
        ? "现在发起会被服务端拒绝（RUN_BUDGET_EXCEEDED / RUN_TOO_SOON）。被拒的尝试不会记进上面的运行记录——记进去会消耗配额，把授权锁死。"
        //: 列名要跟表头逐字对上。这张表的那一列叫「节奏与时段」，从来没有叫
        //  「运行时段」的列——人照着找会以为是自己没找到（2026-09-07 第五轮排查）。
        : "现在发起不会被配额或最短间隔挡回。运行时段另算，见「节奏与时段」列。",
    });
  }

  //: 要人动手的那句单独占一整行。塞进「状态」单元格里的话，表格 8 列会把它挤成
  //  一条竖着排字的细长条——话说全了却没人读得下去，等于没说。
  function mandateAlertRow(m) {
    //: 服务端的 needs_attention 只看**最近一次运行**，从没运行过的恒为 false
    //  （approval_api：latest is None → false）。于是一份签下去就没人管的授权，
    //  在 KPI 与卡片上与一份每天正常产出的授权逐字同形——而这恰恰是本轮最常见的
    //  失败：人以为签完就有人跑了。这一支只在前端判定：数据（issued_at、间隔）
    //  现成就在响应里，服务端不必为此多一个字段。
    //  下限取一天：22:00–06:00 这类时段型授权，要等一个完整窗口过去才说得出话。
    //: 「跑过一次然后停了」与「一次都没跑过」是同一个失败的两种形态，界面上此前
    //  只说了后者。一份时段 22:00–06:00 的授权，5 天前跑通过一次，之后 AI 每天
    //  09:00 敲一次、次次被 OUTSIDE_RUN_WINDOW 挡回——这一行印着「09-01 产出候选」，
    //  没有琥珀条、needs_attention 为 false，与「这 5 天根本没人再叫过 AI」逐字同形。
    //  被拒的尝试**故意**不进 run_log（记进去会让 count_on_day 自耗配额、把授权锁死，
    //  见 strategy_service._record_run），所以这件事只能靠「上次运行有多久了」推。
    if (m.state === "ACTIVE" && new Date(m.expires_at).getTime() > Date.now()) {
      const interval = (m.bounds || {}).run_interval_minutes || 1440;
      const due = Math.max(interval, 1440) * 60000;
      const never = !m.run_count_known;
      const sinceRef = never ? m.issued_at : m.last_run_at;
      const idleMs = sinceRef ? Date.now() - new Date(sinceRef).getTime() : 0;
      if (idleMs >= due) {
        const days = Math.floor(idleMs / 86400000);
        const idleText = days >= 1 ? "已 " + days + " 天" : "已超过一个完整间隔";
        return el("tr", { class: "run-alert-row" }, el("td", { colspan: "8" },
          el("div", { class: "run-alert" },
            el("strong", {
              text: never
                ? "这份授权签下来之后一次都没运行过"
                : "这份授权距上次运行" + idleText + "，比它自己的间隔还长",
            }),
            el("span", {
              text: "本系统不会自己到点运行——把这一行的「复制指令」交给 Codex 等 AI 客户端，" +
                "由它按这份授权发起一次。若已经发起过却仍是这样，说明每次都被时段、" +
                "最短间隔或配额挡回了（那些尝试不会记在这里），AI 侧能看到具体错误码。",
            }))));
      }
    }
    if (!m.needs_attention) return null;
    // 已撤销 / 已过期的授权不会再跑了，它上次为什么没跑通不再是待办——挂着琥珀条
    // 只会跟真正要处理的那几份抢注意力。运行历史仍在（状态列那行短的），只是不催人。
    if (m.state === "REVOKED" || new Date(m.expires_at).getTime() <= Date.now()) return null;
    const spec = RUN_OUTCOME_TEXT[m.last_outcome] || { text: m.last_outcome || "未知结果", next: "" };
    //: 「跑通了但没判断完」不是「没跑通」（2026-08-30 排查 #8/#16）：源侧有一批行
    //  读不出来时，那些 (广告组, 词) 这一轮根本没被判断过，而结局仍是「产出候选」
    //  或「没有该否的词」——后者会被读成「这个店干净」，而真相是「在看得懂的那部分里
    //  干净」。两句话对人的意思完全相反，界面上此前一个字都没有。
    const gap = unjudgedText(m);
    //: 下一步动作**永远**要在。此前 spec.ok 且有缺口时，gap 整个顶掉了 spec.next，
    //  于是「产出候选 + 没判断完」这条最需要人动手的琥珀行，恰恰丢掉了唯一那句
    //  「去『待批』页签审批」——只剩一段关于覆盖率的陈述，人读完不知道该去哪。
    //  但也不能无脑把 spec.next 印回来：NO_CANDIDATES 的下一步是「不用做什么，
    //  这段窗口确实干净」，正是缺口在否定的那句话，照印等于用大字重复一句假话。
    //  所以只替换这一种结局的下一步，其余原样保留；缺口作为补充说明跟在后面。
    //: 替换文案只能许诺界面真的做得到的事。「先弄清这批没判断的是怎么回事」听着
    //  像有个地方能查——查不到：运行记录只存计数（mandate_run.py），源侧账目
    //  （source_accounting）只在 MCP 响应里，一路没有传到界面。把人指向一个不存在的
    //  下一步，比不给下一步更坏：他会找一圈，然后认定是自己没找到。
    //  说得出口的动作只有一个：跟下一轮比。偶发与每轮都有，结论完全不同。
    //: 「去『待批』页签审批」只在那里**真的还有东西**时才是真话（2026-08-30 排查）。
    //  这一批被批准、被拒绝、或过了 72 小时时效之后，那个页签对这份授权是空的，
    //  而琥珀行还在催人过去——他切过去看到「还没有待审的否定词」，只能怀疑自己
    //  点错了。待批列表与授权书列表在同一次刷新里加载，这个事实现成就在手里。
    //: 「已经处理掉了」这句只在**真的看过**待批列表之后才说得出口
    //  （2026-09-07 第四次排查）。取集合那一次请求失败时 state.sets 被清成空数组
    //  （loadSets 的 catch），stillPending 跟着为 false，于是界面对着一批可能正
    //  躺在待批里的候选说「不用再过去」——而这一刻恰恰是待批页签自己也打不开、
    //  只剩这一行在说话的时刻。人照做不去，那批候选 72 小时后自己作废。
    //  不知道就别断言：退回去说「去『待批』页签审批」。白跑一趟可以回头，不去不行。
    const setsKnown = state.setsStatus === "ok";
    const stillPending = (state.sets || []).some(
      (s) => s.mandate_id === m.mandate_id && s.state === "FROZEN" && s.expired !== true
    );
    //: 「等下一轮 / 查源侧数据」这条建议只对**数据读不出来**那一类弃权成立。
    //  ASIN 型弃权完全不是这么回事：这一轮源侧一行没坏、组全判完了，这几个词就是
    //  查清楚了、就是在烧钱，而本系统开的否定精准关键词挡不住它们——唯一有效的
    //  动作是去领星「否定投放」手工否定，等多久都不会变。
    //: 所以它**不挂在任何一个结局分支上**（2026-09-07 第二次排查）。上一版写死
    //  NO_CANDIDATES，漏掉了同样可达的另一格：一轮里全部弃权、而弃权里只有一部分
    //  是 ASIN——那一格落 ALL_ABSTAINED，nextLine 只说「等新数据」，那几个 ASIN
    //  在整个界面上一个字都不会出现（不产候选集合，只跑过一次时连历史条都不画）。
    //  ALL_ASIN 除外：它自己的 spec.next 说的就是这句，不重复一遍。
    const latestRun = (m.recent_runs || [])[0] || {};
    const asinAbstains = Number(latestRun.asin_abstain_count) || 0;
    const otherAbstains = Math.max(0, (Number(latestRun.abstain_count) || 0) - asinAbstains);
    //: 得说得出**是哪几个**。只给数字，人拿着「3 个」去领星后台什么也做不了，
    //  而 ALL_ASIN 那一路不创建候选集合，词表在别处没有第二个出处（2026-09-07 排查）。
    //  服务端已随运行流水记下（MandateRunRecord.asin_abstain_terms），照列即可。
    const asinTerms = Array.isArray(latestRun.asin_abstain_terms)
      ? latestRun.asin_abstain_terms : [];
    //: 这个计数数的是 (广告组 × 搜索词) 行，不是搜索词个数——同一个 ASIN 投在两个
    //  广告组里就是两行（negation.py 的 evaluated_count 注释把这个区分写死了）。
    //  照印成「2 个 ASIN 型搜索词」并把词表原样列出来，人读到的是
    //  「B08XYZ1234、B08XYZ1234」——同一个 ASIN 念了两遍（2026-09-07 实测）。
    //  要否定的是那 1 个 ASIN，要动手的地方是那 2 个广告组：两个数都得说，
    //  而且都不能叫「个词」。
    const asinUnique = Array.from(new Set(asinTerms));
    const asinWhich = asinUnique.length ? "——" + asinUnique.join("、") : "";
    //: 拿不到词表时（老运行流水只记了计数）只说得出行数，那就如实只说行数。
    const asinHow = asinUnique.length
      ? asinUnique.length + " 个 ASIN"
      : asinAbstains + " 条 ASIN 型搜索词(广告组×词)";
    const asinSpread = asinUnique.length && asinAbstains > asinUnique.length
      ? "它们落在 " + asinAbstains + " 条(广告组×词)上，每个广告组都要单独否定。"
      : "";
    const asinLine = asinAbstains > 0 && m.last_outcome !== "ALL_ASIN"
      ? "这一轮有 " + asinHow + " 花了钱、零转化，本系统否不掉" + asinWhich +
        "。" + asinSpread + "去领星「否定投放」手工否定；加否定关键词无效，等下一轮也不会变。"
      : "";
    //: 「跟下一轮比」是派给人的一件跨轮对比的活。弃权全是 ASIN 时对比对象是空集，
    //  派出去纯属白跑，还顺带暗示「可能是源侧数据的问题」这个本轮毫无证据的方向。
    const restToCompare =
      otherAbstains > 0 ||
      Number(latestRun.unjudged_ad_group_terms) > 0 ||
      Number(latestRun.unattributable_rows) > 0;
    const baseLine = gap && m.last_outcome === "NO_CANDIDATES" && restToCompare
      ? (asinLine ? "其余没判成的" : "界面查不到这批的明细——") +
        "跟下一轮比：偶发就等，每轮都有就是源侧数据的问题。在那之前别把「干净」当结论"
      : m.last_outcome === "CANDIDATES" && setsKnown && !stillPending
        ? "这一批已经处理掉了（批准 / 拒绝 / 或过了 72 小时时效）——待批里没有它了，不用再过去"
        //: ASIN 那句已经说完了要做的事时，不必再补一句同样内容的 spec.next。
        //  「同样内容」只有 NO_CANDIDATES 一个：它的 next 是「不用做什么，这段
        //  窗口确实干净」，正是 ASIN 那句在否定的话，照印等于用大字重复一句假话。
        //: 判据此前写的是 spec.ok（2026-09-07 第四次排查）。ok:true 的结局有三个，
        //  ALL_ASIN 的 asinLine 恒空走不到这里，于是它连坐的是 CANDIDATES——
        //  而它的 next 是「去「待批」页签审批」，与 ASIN 那句毫无重叠，是这一行
        //  唯一一句把人送到待批的话。一轮里既产出候选、又有 ASIN 弃权、还有读不出
        //  归属的行（三者可同时发生）时，人读完只知道去领星否 ASIN，那批候选
        //  72 小时后自己过期。**没跑通**的结局同理要照给：一个讲这份授权为什么
        //  跑不通，一个讲已经查实的那几个 ASIN，两件事都得做。
        : asinLine && m.last_outcome === "NO_CANDIDATES"
          ? ""
          //: ALL_ASIN 走的是静态的 spec.next（「去领星手工否定这些 ASIN」），
          //  它说得出要做什么、说不出对哪几个下手——而这一路恰恰不创建候选集合，
          //  词表没有第二个出处。把运行流水里记下的那几个词接上去。
          : m.last_outcome === "ALL_ASIN" && asinUnique.length
            ? spec.next + "。这几个：" + asinUnique.join("、")
            : spec.next;
    const nextLine = [asinLine, baseLine].filter(Boolean).join(" ");
    const body = el("div", { class: "run-alert" },
      el("strong", {
        text: spec.alert || (spec.ok ? "这份授权上次跑通了，但没判断完" : "这份授权上次没跑通：" + spec.text),
      }),
      el("span", { text: nextLine })
    );
    if (gap) body.append(el("span", { text: gap }));
    if (m.last_error_code) {
      body.append(el("span", {
        class: "chip mono",
        text: m.last_error_code,
        title: ERROR_TEXT[m.last_error_code] || m.last_error_code,
      }));
    }
    return el("tr", { class: "run-alert-row" }, el("td", { colspan: "8" }, body));
  }

  //: 最近几次运行连起来看。只有一次时不画——「状态/上次运行」那格已经说全了，
  //  再画一遍是噪音。两次以上才有它能回答、而单次结局回答不了的问题：
  //  这是偶发（一次超时）还是一直如此（每次都圈不中对象），两者的下一步完全不同。
  function mandateRunsRow(m) {
    const runs = m.recent_runs || [];
    if (runs.length < 2) return null;
    const strip = el("div", { class: "run-history" }, el("span", { class: "cell-sub", text: "最近几次：" }));
    runs.forEach((r) => {
      const spec = RUN_OUTCOME_TEXT[r.outcome] || { text: r.outcome, next: "" };
      //: chip 说的是**这一次运行的事实**（这一轮有没有漏判），琥珀行说的是
      //  **现在要不要人动手**——两者判据不必相同，也不该相同：服务端刻意不为
      //  「产出了候选 + 有弃权」染琥珀（人手上本来就有活要做），但那一轮确实有几条
      //  没判成，历史条上如实标记是对的。不同的是**解释权**：⚠ 一旦出现，
      //  这枚 chip 自己的悬停就得说清它指的是什么（2026-09-07 排查：上一版只改了
      //  判据，没改依赖它的悬停三元支，于是 unjudged 为 0、弃权为 3 时印出
      //  「未判断 0 组」——与 ⚠ 和琥珀行的「有 3 条没判成」照旧互相否定）。
      const partlyAbstained =
        Number(r.abstain_count) > 0 && r.outcome !== "ALL_ABSTAINED" && r.outcome !== "ALL_ASIN";
      const unjudged = r.unjudged_ad_group_terms > 0 || r.unattributable_rows > 0;
      const incomplete = unjudged || partlyAbstained;
      strip.append(el("span", {
        class: "chip" + (spec.ok && !incomplete ? "" : " chip-warn"),
        text: fmtShortTime(r.ran_at) + " " + spec.text + (incomplete ? " ⚠" : ""),
        // 数字进 title：这一行的用处是看趋势，不是逐次核对。
        //: 单位必须逐个说对。evaluated_ad_group_terms 数的是 (广告组 × 搜索词) 组合，
        //  这里此前印成「取到 40 行」，而下一行的未判断数印成「260 组」——同一个单位
        //  两个名字，人只能把「组」当成另一种东西，读出「40 行都读到了、覆盖完整」。
        //  单位统一是对的，动词错了：evaluated_ad_group_terms 是**送进策略的行数**，
        //  弃权就在这个数里面——而弃权的定义正是「没能给出可执行的结论」
        //  （negation.py 的 AbstainRecord）。于是整批弃权的那一轮，同一段悬停上下
        //  两行写着「已判断 40 组」和「弃权 40」，直接互相否定（2026-09-07 第五轮
        //  排查）。「读到」是真话，且不动那个刚统一好的单位；判没判出结论，
        //  下一行的「候选 X · 弃权 Y」自己会说。
        title: [
          fmtTime(r.ran_at),
          "读到 " + r.evaluated_ad_group_terms + " 组(广告组×搜索词) · 去重后 " +
            r.distinct_search_terms + " 个词",
          "候选 " + r.candidate_count + " · 弃权 " + r.abstain_count + " · 作用域挡掉 " + r.scope_filtered_out + " 组",
        //: 「弃权 N」不说明人要不要动手。两类弃权指向完全相反的下一步：数据太旧是
        //  等，ASIN 型是现在就得去领星手工否定。混成一个数，等于把这件事藏起来。
          r.asin_abstain_count
            ? "其中 " + r.asin_abstain_count + " 条是 ASIN 型搜索词(广告组×词)：花了钱、零转化，但否定关键词挡不住——要去领星「否定投放」处理"
            : "",
        //: 「全部判断完毕」只有在这一轮真的判断过东西时才是真话。取数失败 / 没接
        //  数据源 / 一行都没有，这三种结局的未判断计数天然是 0（根本没读到行，
        //  谈不上「有几组没判断」），旧写法据此落进 else 支，于是一次一行都没读过的
        //  失败运行，悬停里印着「全部判断完毕」——把「什么都没做」说成「做全了」，
        //  方向正好相反。不是好消息的结局就不给这句话，chip 上的结局文本已经说清了。
          //: ⚠ 的两个来源分开说，不能拿一个去印另一个的数——「未判断 0 组」
          //  在人眼里就是「没漏判」，正好否定了旁边那个 ⚠。
          unjudged
            ? "未判断 " + r.unjudged_ad_group_terms + " 组(广告组×搜索词)" +
              (r.unattributable_rows ? " · 另有 " + r.unattributable_rows + " 行连归属都读不出来" : "")
            : "",
          !unjudged && partlyAbstained
            ? "⚠ 指的是上面那 " + r.abstain_count + " 条弃权：这一轮判不了它们，它们不在这次的结论里"
            : "",
          !incomplete && spec.ok ? "全部判断完毕" : "",
          //: 同一个码，上方那条琥珀行已经用 ERROR_TEXT 译过一遍了（chip 的 title）。
          //  这里留裸英文，等于同一屏上同一件事说两种语言，而这一份恰恰是人翻
          //  历史时唯一看得到的解释（2026-09-07 第五轮排查）。码要留着——它是人
          //  转给别人时唯一能对上的东西——但后面得跟上人话。
          r.error_code
            ? "错误码 " + r.error_code +
              (ERROR_TEXT[r.error_code] ? "：" + ERROR_TEXT[r.error_code] : "")
            : "",
        ].filter(Boolean).join("\n"),
      }));
    });
    return el("tr", { class: "run-history-row" }, el("td", { colspan: "8" }, strip));
  }

  function renderMandates() {
    const region = $("mandate-list");
    region.textContent = "";
    // ui-5（2026-08-29 排查结论）：失败态优先于空态——
    // 「还没有授权书」只在确认拉取成功且确实为空时才许出现。
    if (state.mandatesStatus === "error") {
      region.append(loadErrorState(state.mandatesError, "retry-mandates"));
      return;
    }
    if (state.mandates.length === 0) {
      // 审计 #14：旧文案叫人「切到运营负责人身份」——而默认身份就是它，照做等于
      // 原地转圈；还顺带展示了 token 字符串。按当前身份给一条能走的路。
      region.append(el("div", { class: "empty-state", text: isHuman()
        ? "还没有授权书——展开下方「签发新授权书」，为一家店铺设定目标与边界。" +
          "（授权书、候选与运行记录都只保存在服务进程内存里，服务重启即清空：" +
          "之前签过的话需要重签，并把新的授权书 ID 交给 AI 客户端。）"
        : "还没有授权书。签发需要人类身份：先在右上角切回「运营负责人」，再展开下方「签发新授权书」。" }));
      return;
    }

    const table = el("table", { class: "data" },
      el("thead", null, el("tr", null,
        el("th", { text: "目标 / 作用域" }),
        el("th", { text: "状态 / 上次运行" }),
        el("th", { text: "店铺" }),
        el("th", { text: "节奏与时段" }),
        el("th", { text: "配额" }),
        el("th", { text: "有效期" }),
        el("th", { text: "签发人" }),
        el("th", { text: "操作" }),
      )),
      el("tbody", null, ...state.mandates.filter(matchesProfileFilter).flatMap(
        (m) => [mandateRow(m), mandateAlertRow(m), mandateRunsRow(m)].filter(Boolean)))
    );
    if (!state.mandates.some(matchesProfileFilter)) {
      region.append(el("div", { class: "empty-state", text:
        "这家店铺（" + profileLabel(state.profileFilter) + "）还没有授权书。" +
        "上方店铺下拉选「全部店铺」可以看到其余 " + state.mandates.length + " 份。" }));
      return;
    }
    region.append(el("div", { class: "table-wrap" }, table));
  }

  function mandateRow(m) {
    const bounds = m.bounds || {};
    const interval = bounds.run_interval_minutes;
    const revoked = m.state === "REVOKED";

    const packText = packSummaryText(m.parameter_pack);

    const scopeTitle = mandateScopeTitle(m);
    const objectiveCell = el("td", null,
      el("div", null,
        el("span", { text: OBJECTIVE_TEXT[m.objective] || m.objective }),
        " ",
        el("span", {
          class: "scope-badge",
          text: mandateScopeText(m),
          title: scopeTitle || "这份授权覆盖的对象范围",
        })),
      el("span", { class: "cell-sub", text: m.statement || "" }),
      // 审计 #33：行内不再印英文枚举（objective 中文已在上行），机器 ID 缩短、原文进悬停。
      el("span", { class: "cell-sub id-line" },
        el("span", { class: "mono", title: m.mandate_id + "\nobjective=" + m.objective, text: "ID " + shortId(m.mandate_id) }),
        revoked || new Date(m.expires_at).getTime() <= Date.now() ? null : el("button", {
          class: "btn btn-xs", type: "button", text: "复制指令",
          title: "复制一句可以直接粘给 Codex 等 AI 客户端的话，内含完整授权书 ID" +
            "（表格只显示前 8 位，服务端不认前缀）",
          dataset: { action: "copy-hash", hash: mandateRunCommand(m) },
        }))
    );

    const actionCell = el("td", null);
    //: 到期重签是这套设计里**必然**会发生的动作（授权最长 30 天、默认 7 天，
    //  「不存在长生不老的自动化授权」是写进 runbook 的），而界面此前对它零支持：
    //  过期行连「复制指令」都收起来，动作列只剩一个对死授权毫无意义的「撤销」，
    //  全站没有任何克隆入口。打法卡片只带参数，不带店铺、作用域与运行时段——
    //  人得凭记忆把一份自己一周前签的合同重新填一遍（2026-09-06 排查）。
    const dead = revoked || new Date(m.expires_at).getTime() <= Date.now();
    if (dead) {
      actionCell.append(el("button", {
        class: "btn btn-sm", type: "button", text: "照这份再签一份",
        title: "把这份授权书的全部合同内容填回签发表单：店铺、目标、说明、四个阈值、" +
          "配额与频次、作用域清单、运行时段。填完你再核对一遍，签发仍然要你点。",
        dataset: { action: "clone-mandate", mandateId: m.mandate_id },
      }));
    }
    if (!revoked) {
      const btn = el("button", {
        class: "btn btn-sm btn-danger-ghost", type: "button",
        text: "撤销",
        dataset: { action: "revoke-mandate", mandateId: m.mandate_id },
      });
      if (!isHuman()) { btn.disabled = true; btn.title = "AI 无批准/拒绝/签发/撤销权——服务端强制，此处仅提示"; }
      actionCell.append(btn);
    } else {
      actionCell.append(el("span", { class: "set-meta", text: "—" }));
    }

    // 店铺列显示店名（审计 #5 同族：16 位数字认不出店），ID 降为小字。
    const profileCell = el("td", null,
      el("div", { text: profileLabel(m.profile_external_id) }));
    if (profileLabel(m.profile_external_id) !== m.profile_external_id) {
      profileCell.append(el("span", { class: "cell-sub mono", text: m.profile_external_id }));
    }
    return el("tr", { class: revoked ? "row-muted" : null },
      objectiveCell,
      el("td", null, mandateStateBadge(m), mandateRunLine(m)),
      profileCell,
      el("td", null,
        el("span", {
          text: INTERVAL_SHORT[interval] || "每 " + interval + " 分钟检查一次",
          title: "两次运行至少间隔 " + String(interval) + " 分钟（run_interval_minutes=" + String(interval) +
            "）——这是闸门不是排程，运行由人发起",
        }),
        el("span", { class: "cell-sub", text: mandateWindowText(m) })),
      el("td", null,
        el("span", {
          //: 卡住时把实际次数写进**常驻可见**的那一句。放进 title 等于重犯
          //  runsVsIntervalNote 注释里点名的那个错：会引起误解的常驻可见，
          //  能解释它的要悬停才看得到。
          text: String(bounds.max_runs_per_day) + " 次/日" +
            (intervalCapsRunsAt(interval, bounds.max_runs_per_day) !== null
              ? "（间隔下实际 " + intervalCapsRunsAt(interval, bounds.max_runs_per_day) + " 次）"
              : "") +
            " · ≤" + String(bounds.max_candidates_per_run) + " 词/次",
          //: 「日」的边界写进悬停（2026-08-30 排查 #2/#9）。配额此前按 UTC 日切、
          //  运行时段按当地钟点，两个「天」差一个时区偏移，当地同一天里能跑到双倍。
          //  现在两者共用同一个「天」。这句话由服务端合成（quota_day_summary）：
          //  跨午夜时段的日界不是当地 0 点而是窗口起点，只报时区会把人指回旧行为。
          //  旧字段 quota_timezone 仍在响应里，是这句话之外的原始值，此处不再拼装。
          title: m.quota_day_summary ||
            (m.quota_timezone ? "「日」按 " + m.quota_timezone + " 的当地日切换" : ""),
        }),
        el("span", { class: "cell-sub", text: packText }),
        //: 合同值回答不了人此刻唯一的问题：「我现在叫 AI 跑，会不会被拒？」
        //  他手上只有静态的「1 次/日」和「上次运行 14:32」，要自己减出间隔、还要
        //  读懂跨午夜日界那句散文。而被拒的尝试**不进流水**（进了会自耗配额），
        //  卡片纹丝不动——他连刚才那次有没有打到服务端都判断不出。
        quotaNowLine(m)),
      el("td", null,
        el("span", { class: "num", text: fmtTime(m.issued_at) }),
        el("span", { class: "cell-sub num", text: "→ " + fmtTime(m.expires_at) })),
      el("td", { text: m.issued_by_person_id || "—" }),
      actionCell
    );
  }

  //: 照一份旧授权书重填表单。**只填，不签**——签发仍然要人点，且服务端仍会完整
  //  校验（币种、就绪度、作用域归属）。填不回去的东西一个都不假装：作用域清单里
  //  的对象若已从镜像里消失，带回来的仍是签发那一刻冻结的 external_id 与名字。
  //: 两个 select 只有固定档位，而服务端接受的区间比档位宽（API/别的客户端签的授权
  //  可能落在档外）。克隆时**不许收敛到最近档位**——那会把一份合同悄悄换成另一份；
  //  也不许留空（实测 480 分钟 / 5 天两处就是这么空掉的）。补一个带标注的档位，
  //  让原值原样回到表单，并且看得出它是沿用来的。
  function ensureOption(selectId, value, label) {
    const sel = $(selectId);
    const v = String(value);
    if (![...sel.options].some((o) => o.value === v)) {
      sel.append(el("option", { value: v, text: label + "（沿用原授权书）" }));
    }
    sel.value = v;
  }

  function cloneMandate(mandateId) {
    const m = (state.mandates || []).find((x) => x.mandate_id === mandateId);
    if (!m) { showAlert("error", "找不到这份授权书，可能列表已过期——点「刷新列表」再试"); return; }
    const pack = m.parameter_pack || {};
    const spend = pack.min_spend || {};
    const bounds = m.bounds || {};
    $("issue-wrap").open = true;
    $("f-objective").value = m.objective;
    $("f-statement").value = m.statement || "";
    $("f-lookback").value = String(pack.lookback_days);
    $("f-min-spend").value = String(spend.amount || "");
    $("f-min-clicks").value = String(pack.min_clicks);
    $("f-staleness").value = String(pack.max_data_staleness_hours);
    ensureOption("f-interval", bounds.run_interval_minutes,
      INTERVAL_SHORT[bounds.run_interval_minutes] ||
        "每 " + bounds.run_interval_minutes + " 分钟检查一次");
    ensureOption("f-valid-days", bounds.valid_days, bounds.valid_days + " 天");
    $("f-max-candidates").value = String(bounds.max_candidates_per_run);
    $("f-runs-per-day").value = String(bounds.max_runs_per_day);
    //: 配额是从旧合同原样带回来的，不是频次推的——所以标记为「人定的」，
    //  免得人随手动一下频次就把它顶掉。
    $("f-runs-per-day").dataset.touched = "1";
    $("f-currency").dataset.touched = "1";
    $("f-currency").value = String(spend.currency || "");

    const win = m.run_window;
    const winRadio = document.querySelector(
      "input[name=run_window_kind][value=" + (win ? "WINDOW" : "ALL_DAY") + "]");
    if (winRadio) winRadio.checked = true;
    if (win) {
      $("f-tz").value = win.timezone;
      $("f-start-hour").value = String(win.start_hour);
      $("f-end-hour").value = String(win.end_hour);
    } else if (m.quota_timezone) {
      $("f-tz").value = m.quota_timezone;
    }

    mandateDraft.scopeItems = (m.scope_items || []).map((s) => ({
      level: String(s.level).toLowerCase(),
      external_id: s.external_id,
      name: s.name,
      profile: m.profile_external_id,
    }));
    mandateDraft.scopeProfile = m.profile_external_id;
    const scopeRadio = document.querySelector(
      "input[name=scope_kind][value=" + (m.scope_kind === "OBJECTS" ? "OBJECTS" : "PROFILE") + "]");
    if (scopeRadio) scopeRadio.checked = true;

    setFormProfile(m.profile_external_id);
    mandateDraft.preset = null;
    for (const btn of $("preset-cards").querySelectorAll("input[name=preset]")) btn.checked = false;
    renderScope();
    renderIntervalHelp();
    renderWindow();
    renderPresetSummary();
    renderAdvBadge();
    renderRunsWarn();
    renderObjectiveReadiness();
    renderIssueGate();
    showAlert("info", "已按 " + shortId(mandateId) +
      " 填好表单（含作用域与运行时段）。核对一遍再点「签发授权书」——签发仍然要你点。");
    $("issue-wrap").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  //: 撤销后焦点回到那一行（与 focusSetTab 同一条理由，只是「它去哪了」的答案不同：
  //  被撤销的授权书还留在原位，只是变灰、动作格换成「照这份再签一份」）。
  function focusMandateRow(mandateId) {
    const a = document.activeElement;
    const mine =
      !a ||
      a === document.body ||
      a === document.documentElement ||
      $("mandate-list").contains(a);
    if (!mine) return;
    const anchor = document.querySelector(
      '#mandate-list [data-mandate-id="' + mandateId + '"]');
    const row = anchor ? anchor.closest("tr") : null;
    const target = row ? row.querySelector("button:not([disabled])") : null;
    if (target && target.offsetParent !== null) target.focus();
  }

  //: 撤销会连坐：这份授权名下还停在「待批」的集合，撤销后批准必被服务端 409
  //  （MANDATE_REVOKED）拒。此前确认框只说「AI 立即停止按它运行」——人读到的是
  //  「以后不再自动跑了」，读不出「我现在正要审的这几份，一并作废」。卡片上那个
  //  已撤销标记是**事后**才看得到的，而这是个不可逆的决定，话要在按下之前说。
  function pendingSetsOfMandate(mandateId) {
    return state.sets.filter(
      (s) => s.state === "FROZEN" && s.expired !== true && s.mandate_id === mandateId
    ).length;
  }

  async function revokeMandate(mandateId) {
    // 审计 #24：撤销单击立即生效且不可逆——加一道确认，误触不再直接毁一份授权。
    const pending = pendingSetsOfMandate(mandateId);
    const also = pending > 0
      ? "「待批」里还有 " + pending + " 份来自这份授权的候选，撤销后它们一并批不了" +
        "（只能拒绝掉）。"
      : "";
    if (!window.confirm(
      "撤销这份授权书？撤销后 AI 立即停止按它运行，且无法恢复（需要重新签发）。" + also
    )) {
      return;
    }
    try {
      await api("/mandates/" + encodeURIComponent(mandateId) + "/revoke", { method: "POST" });
      showAlert("success", "授权书已撤销：" + shortId(mandateId));
    } catch (err) {
      alertError(err);
    }
    await refreshAll();
    focusMandateRow(mandateId);
  }

  // ---------- 签发表单 ----------
  function derivedRunsPerDay(interval) {
    if (!interval || interval > 1440) return 1;
    return Math.min(24, Math.floor(1440 / interval));
  }

  //: 自定义打法存浏览器本地（localStorage）：这是"帮你填表"的快捷方式，不是服务端合同，
  //  换浏览器不带走。损坏/禁用 storage 时静默回退为只有内置打法。
  const CUSTOM_PRESETS_KEY = "mandate-custom-presets";

  function loadCustomPresets() {
    try {
      const raw = JSON.parse(localStorage.getItem(CUSTOM_PRESETS_KEY) || "{}");
      return raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
    } catch {
      return {};
    }
  }

  function saveCustomPresets(map) {
    try {
      localStorage.setItem(CUSTOM_PRESETS_KEY, JSON.stringify(map));
    } catch {
      showAlert("warn", "浏览器不允许本地存储，这个打法只在本页有效，刷新就没了");
    }
  }

  function allPresets() {
    return { ...MANDATE_PRESETS, ...loadCustomPresets() };
  }

  //: max_runs_per_day 必须在这张表里（2026-09-06 排查）。它此前存不下：
  //  「存为新打法 / 更新 / 下载 / 导入」四个入口一起丢这一项，而 applyPreset 又拿
  //  derivedRunsPerDay 覆写它。于是照 runbook 把日上限从 1 改成 2 再存成打法，
  //  下次用这张打法签出来的是 1，当天第二次运行被 RUN_BUDGET_EXCEEDED 拒；
  //  反方向（每小时频次下把 24 压到 4）则是一条人为收窄的边界被悄悄放宽 6 倍。
  //  两个方向界面都不出声：摘要里没有这一项，警告条因 v===k 沉默，徽章归零。
  const PRESET_NUM_FIELDS = [
    "lookback_days", "min_clicks", "max_data_staleness_hours",
    "run_interval_minutes", "max_candidates_per_run", "valid_days", "max_runs_per_day",
  ];

  //: 导入校验：字段齐、数值是正数、频次落在 UI 下拉允许的档位上（否则 select 会静默不生效）。
  //  返回 null 表示不合格，由调用方给人话报错。
  //: allowMissingRunsPerDay 只给导入用。表单永远有这个字段，空值不是「旧格式」
  //  而是「人填错了」——把它一起吞掉，就成了四个数值框里唯独这一项的非法值不出声，
  //  而其余每一项都会明确报「表单参数有非法值，没有覆盖」（2026-09-07 排查）。
  function sanitizePreset(obj, { allowMissingRunsPerDay = false } = {}) {
    if (!obj || typeof obj !== "object") return null;
    const title = String(obj.title || "").trim();
    if (!title) return null;
    const p = {
      title: title.slice(0, 20),
      sub: String(obj.sub || "").slice(0, 60),
      statement: String(obj.statement || "").slice(0, 200),
      custom: true,
    };
    for (const k of PRESET_NUM_FIELDS) {
      const v = Number(obj[k]);
      if (!Number.isFinite(v) || v <= 0) {
        //: 2026-09-06 之前存下的打法文件没有 max_runs_per_day。缺它不算不合格——
        //  回落到频次推导值（正是那之前的行为），别把同事发来的旧文件判成坏文件。
        if (k === "max_runs_per_day" && allowMissingRunsPerDay) continue;
        return null;
      }
      p[k] = Math.round(v);
    }
    if (p.max_runs_per_day !== undefined) {
      p.max_runs_per_day = Math.min(24, Math.max(1, p.max_runs_per_day));
    }
    if (!INTERVAL_OPTIONS.some((o) => o.minutes === p.run_interval_minutes)) return null;
    //: 两个 select 只有固定档位，落不进档的值会静默丢失；数值输入框有 min/max，
    //  超界会卡在客户端预检。导入时统一收敛到允许区间，别让人对着填好的表单猜哪里错了。
    p.valid_days = [3, 7, 14, 30].reduce((best, d) =>
      Math.abs(d - p.valid_days) < Math.abs(best - p.valid_days) ? d : best);
    p.lookback_days = Math.min(90, Math.max(7, p.lookback_days));
    p.min_clicks = Math.max(10, p.min_clicks);
    p.max_data_staleness_hours = Math.min(72, Math.max(1, p.max_data_staleness_hours));
    p.max_candidates_per_run = Math.min(200, Math.max(1, p.max_candidates_per_run));
    const spend = Number(obj.min_spend_amount);
    if (!Number.isFinite(spend) || spend <= 0) return null;
    p.min_spend_amount = spend.toFixed(2);
    return p;
  }

  function presetFromForm(title, sub) {
    return sanitizePreset({
      title,
      sub,
      statement: $("f-statement").value.trim(),
      lookback_days: $("f-lookback").value,
      min_spend_amount: $("f-min-spend").value,
      min_clicks: $("f-min-clicks").value,
      max_data_staleness_hours: $("f-staleness").value,
      run_interval_minutes: $("f-interval").value,
      max_candidates_per_run: $("f-max-candidates").value,
      valid_days: $("f-valid-days").value,
      max_runs_per_day: $("f-runs-per-day").value,
    });
  }

  function activePreset() {
    return allPresets()[mandateDraft.preset] || MANDATE_PRESETS.STEADY;
  }

  function presetValueFor(key) {
    const p = activePreset();
    //: 打法自己存了日上限就以它为准；老打法（存于 2026-09-06 之前）没有这一项，
    //  才回落到频次推导值——否则「刚把这张表存成打法卡，它却说你相对这张卡改了一项」。
    if (key === "max_runs_per_day" && p.max_runs_per_day === undefined) {
      return String(derivedRunsPerDay(Number($("f-interval").value)));
    }
    return String(p[key]);
  }

  function buildIntervalOptions() {
    const sel = $("f-interval");
    sel.textContent = "";
    for (const opt of INTERVAL_OPTIONS) {
      sel.append(el("option", { value: String(opt.minutes), text: opt.label }));
    }
  }

  function buildHourOptions() {
    const start = $("f-start-hour");
    const end = $("f-end-hour");
    start.textContent = "";
    end.textContent = "";
    for (let h = 0; h <= 23; h += 1) start.append(el("option", { value: String(h), text: pad2(h) + ":00" }));
    // 结束钟点整点粒度，域层合同同样是 [0,23]；end=0 即当日午夜（如 20:00 → 24:00），
    // 所以它排在 23:00 之后而不是最前面。start==end 是「全天」的第二种写法，由
    // 客户端预检拦掉——全天运行只保留上面那个单选项一种表达。
    for (let h = 1; h <= 23; h += 1) end.append(el("option", { value: String(h), text: pad2(h) + ":00" }));
    end.append(el("option", { value: "0", text: "24:00（次日零点）" }));
    start.value = "2";
    end.value = "18";
  }

  function buildPresetCards() {
    const box = $("preset-cards");
    box.textContent = "";
    for (const [key, p] of Object.entries(allPresets())) {
      const input = el("input", { type: "radio", name: "preset", value: key });
      input.checked = key === mandateDraft.preset;
      const card = el("label", { class: "preset-card" },
        input,
        el("span", { class: "preset-body" },
          el("span", { class: "preset-title" },
            el("span", { text: p.title }),
            p.recommended ? el("span", { class: "preset-tag", text: "推荐" }) : null,
            p.custom ? el("span", { class: "preset-tag preset-tag-custom", text: "自定义" }) : null),
          el("span", { class: "preset-sub", text: p.sub })),
        p.custom
          ? el("span", { class: "preset-ops" },
              el("button", { type: "button", class: "preset-op", "data-preset-update": key, title: "用下方表单当前参数覆盖这个打法", text: "更新" }),
              el("button", { type: "button", class: "preset-op", "data-preset-delete": key, title: "删除这个自定义打法", text: "删" }))
          : null
      );
      box.append(card);
    }
  }

  function applyPreset(key) {
    const p = allPresets()[key];
    if (!p) return;
    mandateDraft.preset = key;
    $("f-interval").value = String(p.run_interval_minutes);
    $("f-valid-days").value = String(p.valid_days);
    $("f-statement").value = p.statement;
    $("f-lookback").value = String(p.lookback_days);
    $("f-min-spend").value = p.min_spend_amount;
    $("f-min-clicks").value = String(p.min_clicks);
    $("f-staleness").value = String(p.max_data_staleness_hours);
    $("f-max-candidates").value = String(p.max_candidates_per_run);
    //: 点打法卡片是明示的「照这套填」，覆盖并清掉手改标记；下面频次下拉那条不一样。
    //  打法存了日上限就用它（老打法没存，回落到频次推导值）。
    //: touched 得跟着值的来路走，不能一律清掉（2026-09-07 排查，本轮上一版漏的）。
    //  卡里存着的那个日上限，本身就是当初有人在这张表单上手填、再按「存为新打法」
    //  钉下来的一个决定——套用这张卡把它填回来，等于把那个决定重新拿出来用，
    //  它仍然是「人定的」。清掉标记之后，接下来第一次动频次下拉就会把它无声顶掉：
    //  实测「每 12 小时 + 手填 1 次/日」存成卡 → 点回这张卡 → 改成每小时，
    //  日上限被改写成 24（人为收窄的边界被放宽 6 倍），警告条因 v===k 沉默，
    //  摘要只报新数字，徽章反过来说「已微调 1 项」——把一次不是人做的改动记在人头上。
    //  只有回落到频次推导值那条路才是真的「没人定过」，那时才该清。
    const runsFromPreset = p.max_runs_per_day !== undefined;
    $("f-runs-per-day").value = String(
      runsFromPreset ? p.max_runs_per_day : derivedRunsPerDay(p.run_interval_minutes)
    );
    if (runsFromPreset) $("f-runs-per-day").dataset.touched = "1";
    else delete $("f-runs-per-day").dataset.touched;
    for (const btn of $("preset-cards").querySelectorAll("input[name=preset]")) {
      btn.checked = btn.value === key;
    }
    renderPresetSummary();
    renderIntervalHelp();
    renderAdvBadge();
    renderRunsWarn();
  }

  function saveNewPresetFromForm() {
    const name = (window.prompt("给这个打法起个名字（比如：新品保护期）") || "").trim();
    if (!name) return;
    const p = presetFromForm(name, "自定义打法 · 存于本浏览器");
    if (!p) {
      showAlert("error", "下面的参数还没填全或有非法值，先把表单填好再保存");
      return;
    }
    const map = loadCustomPresets();
    const key = "CUSTOM_" + Date.now().toString(36);
    map[key] = p;
    saveCustomPresets(map);
    mandateDraft.preset = key;
    buildPresetCards();
    renderAdvBadge();
    showAlert("info", "已保存打法「" + p.title + "」——只存在这台电脑的浏览器里，可用「下载」带走");
  }

  function updateCustomPreset(key) {
    const map = loadCustomPresets();
    const old = map[key];
    if (!old) return;
    const p = presetFromForm(old.title, old.sub);
    if (!p) {
      showAlert("error", "表单参数有非法值，没有覆盖");
      return;
    }
    map[key] = p;
    saveCustomPresets(map);
    if (mandateDraft.preset === key) applyPreset(key);
    showAlert("info", "打法「" + p.title + "」已用当前表单参数覆盖");
  }

  function deleteCustomPreset(key) {
    const map = loadCustomPresets();
    const p = map[key];
    if (!p) return;
    if (!window.confirm("删除自定义打法「" + p.title + "」？删了就找不回来。")) return;
    delete map[key];
    saveCustomPresets(map);
    // 二轮审计：只有删的是当前选中的打法才需要回落 STEADY 并重填表单——
    // 无条件 applyPreset 会把删无关卡片的人正在填的整张表（含手工微调）静默打回存档值。
    if (mandateDraft.preset === key) {
      mandateDraft.preset = "STEADY";
      applyPreset(mandateDraft.preset);
    }
    buildPresetCards();
  }

  function downloadCurrentPreset() {
    const p = presetFromForm(activePreset().title, activePreset().sub || "");
    if (!p) {
      showAlert("error", "表单参数有非法值，先填好再下载");
      return;
    }
    const payload = { format: "ads-control-plane/mandate-preset@1", ...p };
    delete payload.custom;
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const a = el("a", {
      href: URL.createObjectURL(blob),
      download: "打法-" + p.title.replace(/[\\/:*?"<>|\s]+/g, "-") + ".json",
    });
    document.body.append(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);
  }

  function importPresetFile(file) {
    const reader = new FileReader();
    reader.onload = () => {
      let p = null;
      try {
        p = sanitizePreset(JSON.parse(String(reader.result)), { allowMissingRunsPerDay: true });
      } catch {
        p = null;
      }
      if (!p) {
        showAlert("error", "这个文件不是有效的打法文件——需要「下载当前打法」导出的那种 JSON");
        return;
      }
      p.sub = p.sub || "导入的打法";
      const map = loadCustomPresets();
      const key = "CUSTOM_" + Date.now().toString(36);
      map[key] = p;
      saveCustomPresets(map);
      applyPreset(key);
      buildPresetCards();
      showAlert("info", "已导入打法「" + p.title + "」并填入下方参数，签发前请过目");
    };
    reader.readAsText(file);
  }

  function renderPresetSummary() {
    const p = activePreset();
    const currency = ($("f-currency").value || "USD").trim().toUpperCase();
    const interval = Number($("f-interval").value) || p.run_interval_minutes;
    const summary =
      (INTERVAL_SHORT[interval] || "每 " + interval + " 分钟检查一次") +
      //: 不写「近 N 天」。真实通道的窗口右端按归因滞后往回退（lingxing 的
      //  ATTRIBUTION_LAG_DAYS=3），实际看的是 T-(N+3) 到 T-3——冲着「今天烧的钱明天就掐」
      //  选下这份打法的人，会拿不到前天那笔浪费的候选，而结局是 NO_CANDIDATES，
      //  界面对他说「这段窗口确实干净」。退几天由数据源决定（mock 与 lingxing 不同），
      //  所以这里不写死数字，只说清「不含最近几天」这件事。
      "，只处理一段 " + $("f-lookback").value + " 天的窗口（不含最近几天）里花了 " +
      $("f-min-spend").value + " " + currency +
      " 以上、点了 " + $("f-min-clicks").value + " 次以上、却一单没出的词。一次最多提 " +
      $("f-max-candidates").value + " 个，一天最多跑 " + $("f-runs-per-day").value +
      " 次" + runsVsIntervalNote() + "，授权 " + $("f-valid-days").value + " 天后自动到期。";
    $("preset-summary").textContent = summary;
  }

  //: 「每天检查一次」和「一天最多跑 2 次」并排出现在同一句里，读起来像两件事，
  //  实际前者是硬闸、后者是配额，冲突时以前者为准（2026-09-07 排查）。把这句话
  //  说清楚的那条警告在 #runs-warn，而它锁在默认折叠的「参数细则」里（index.html
  //  的 details#adv-wrap 既无 open，代码里也从没有人展开过它）——会引起误解的那句
  //  常驻可见，能解释它的那句要手动展开才看得到，正好放反。摘要里补一句短的。
  //: 「最短间隔」是硬闸，「日上限」是配额，间隔更紧时以间隔为准——于是一份写着
  //  「2 次/日 · 每天检查一次」的授权，第二次永远发起不了。这个判断此前**只有签发
  //  表单侧有**（下面的 runsVsIntervalNote），签完就再没人说过；而人每天看的是
  //  授权书卡片，不是那张早就关掉的表单（2026-09-07 排查）。抽成纯函数给两处共用。
  //  返回被卡到的实际次数，卡不住则返回 null。
  function intervalCapsRunsAt(interval, runs) {
    const k = derivedRunsPerDay(Number(interval));
    const v = Number(runs);
    return Number.isFinite(v) && v > k ? k : null;
  }

  function runsVsIntervalNote() {
    const iv = Number($("f-interval").value);
    const v = parseInt($("f-runs-per-day").value, 10);
    const capped = intervalCapsRunsAt(iv, v);
    if (capped !== null) return "（这个间隔下一天实际只发起得了 " + capped + " 次，多的用不到）";
    const k = derivedRunsPerDay(iv);
    if (Number.isNaN(v) || v === k) return "";
    return "（这个间隔一天要检查 " + k + " 次，多出的会被拒）";
  }

  //: 这个下拉是**最短间隔**，不是排程：服务端拿它拒绝来得太早的运行
  //  （mandate.py assert_run_authorized → RUN_TOO_SOON），仓库里没有任何东西会到点自己跑。
  //  选项标签用的是人类节奏语（Owner 第 4 点定的），所以这句必须每次跟在后面——
  //  否则「每天检查一次」读起来就是一句排程承诺，而签发的人会等一个永远不来的候选。
  const INTERVAL_GATE_NOTE =
    "这是两次运行之间的最短间隔，不是定时——本系统不会自己到点运行，运行由人在 AI 客户端里发起。" +
    "距上次运行不满这个时长就再发起，会被拒绝（RUN_TOO_SOON）。";

  function renderIntervalHelp() {
    const iv = Number($("f-interval").value);
    const opt = INTERVAL_OPTIONS.find((o) => o.minutes === iv);
    $("interval-help").textContent = (opt ? opt.help + " " : "") + INTERVAL_GATE_NOTE;
  }

  function renderAdvBadge() {
    //: 没有任何打法被选中时，「相对打法微调了几项」这句话没有比较对象
    //  （2026-09-06 排查）：「照这份再签一份」把 mandateDraft.preset 置为 null 并
    //  取消所有卡片选中，而 activePreset() 会静静回落到 STEADY——于是徽章拿一张
    //  人根本没选的卡当基准，报出「已微调 1 项」；旁边的「恢复为打法推荐值」
    //  走 applyPreset(null)，在函数第一行就 return，点了零反应。
    const reset = $("adv-reset");
    const badge = $("adv-badge");
    if (!mandateDraft.preset) {
      badge.hidden = true;
      reset.disabled = true;
      reset.title = "当前参数来自一份已有的授权书，不是某张打法卡——没有「推荐值」可恢复。选一张打法卡即可启用。";
      return;
    }
    reset.disabled = false;
    reset.title = "把参数细则里的数字恢复成这张打法卡的推荐值";
    let n = 0;
    for (const [key, id] of ADV_FIELDS) {
      if (String($(id).value).trim() !== presetValueFor(key)) n += 1;
    }
    badge.hidden = n === 0;
    badge.textContent = "已微调 " + n + " 项";
  }

  //: 手填过就不再被频次下拉顶掉（与 f-currency 同一套「尊重明示意图」）。
  function markRunsPerDayTouched() {
    $("f-runs-per-day").dataset.touched = "1";
    renderRunsWarn();
  }

  function renderRunsWarn() {
    const iv = Number($("f-interval").value);
    const k = derivedRunsPerDay(iv);
    const v = parseInt($("f-runs-per-day").value, 10);
    const warn = $("runs-warn");
    if (!Number.isNaN(v) && v < k) {
      warn.hidden = false;
      warn.textContent =
        "当前频次每天要检查 " + k + " 次，但日上限只有 " + v + " 次——多出的 " + (k - v) +
        " 次会被拒绝（RUN_BUDGET_EXCEEDED）。";
    } else if (!Number.isNaN(v) && v > k) {
      //: 调高的方向此前一个字都不说。最小间隔是硬闸：每 1440 分钟一次，一天就是 1 次，
      //  填 5 也只有 1 次跑得成，多出的 4 次配额结构上永远用不到。人填这个数是想
      //  「多跑几次」，而真正要改的是上面的频次。
      warn.hidden = false;
      warn.textContent =
        "当前频次每天最多只能发起 " + k + " 次，填 " + v + " 次也用不到多出的 " + (v - k) +
        " 次——最小间隔是硬闸。想多跑，改上面的「多久检查一次」。";
    } else {
      warn.hidden = true;
      warn.textContent = "";
    }
  }

  // 目标就绪度：事实从服务端读，措辞在前端（D2）。端点缺失时如实说明，不编造快照。
  async function loadObjectives() {
    try {
      const res = await api("/mandates/objectives");
      state.objectives = Array.isArray(res.objectives) ? res.objectives : null;
    } catch {
      state.objectives = null; // 端点不存在 / 不可达：不做任何就绪度断言
    }
    // 审计 #16：4 个目标 3 个不可用，选项上却无任何标记——人只能逐个选中试错。
    // 未就绪的选项直接标注并禁用（就绪度事实来自服务端，禁用是它的界面投影）。
    if (Array.isArray(state.objectives)) {
      for (const opt of $("f-objective").options) {
        const row = state.objectives.find((o) => o.objective === opt.value);
        if (row && row.ready === false) {
          if (!opt.textContent.includes("（缺数据")) {
            opt.textContent = opt.textContent + "（缺数据，暂不可选）";
          }
          opt.disabled = true;
        }
      }
    }
    renderObjectiveReadiness();
  }

  function renderObjectiveReadiness() {
    const objective = $("f-objective").value;
    const note = $("objective-note");
    const blocked = $("objective-blocked");
    const presetBlock = $("preset-block");

    if (state.objectives === null) {
      //: mandate-6 收尾（2026-08-29 排查结论）：降级分支不再报接口路径与错误码——
      //  那是说给开发者的话。读不到清单时本页不做就绪断言，提交权留给服务端裁定。
      note.textContent =
        "暂时读不到目标就绪度清单——仍可提交，未就绪的目标会被服务端明确拒绝并说明缺什么。";
      blocked.hidden = true;
      presetBlock.hidden = false;
      renderIssueGate();
      return;
    }
    // 审计 #43：签发前得知道 AI 被授权做「哪种动作」——不只是目标名。
    //: 「选到未就绪的目标时」是一句永远兑现不了的承诺（2026-09-06 核实）：未就绪的
    //  选项在上面被设成 disabled，选不中，于是 #objective-blocked 一次都不显示，
    //  服务端已经回来的 missing 清单人一眼都看不到。下拉里只写着「缺数据」，缺哪些
    //  数据无处可查。改成不依赖选中：有未就绪目标就直接把它们缺什么列出来。
    note.textContent =
      "「清除浪费花费」只会产出否定词候选（否定精确匹配），不改预算、不改竞价、不暂停广告。" +
      "下拉里标着「缺数据」的目标暂时选不了，下方列出它们各自缺什么。";

    const notReady = state.objectives.filter((o) => o.ready === false);
    const row = state.objectives.find((o) => o.objective === objective);
    if (!(row && row.ready === false) && notReady.length) {
      blocked.textContent = "";
      blocked.append(
        el("p", { text: "这些目标暂时不能签发，缺的是数据而不是权限：" }),
        el("ul", { class: "field-note" }, ...notReady.map((o) =>
          el("li", {
            text: (OBJECTIVE_TEXT[o.objective] || o.objective) + "：缺 " +
              (Array.isArray(o.missing) ? o.missing : []).map((k) => REQUIREMENT_TEXT[k] || k).join("；"),
          })
        )),
        el("p", {
          class: "field-note",
          text: "就绪与否由服务端裁定——这是设计，不是故障。数据接上后它们会自己变成可选。",
        })
      );
      blocked.hidden = false;
      presetBlock.hidden = false;   // 选中的是就绪目标，打法卡片照常给
      renderIssueGate();
      return;
    }
    if (row && row.ready === false) {
      const missing = Array.isArray(row.missing) ? row.missing : [];
      blocked.textContent = "";
      blocked.append(
        el("p", {
          text: "这个目标还不能签发。系统缺 " + missing.length + " 样东西才能算清楚：" +
            missing.map((k) => REQUIREMENT_TEXT[k] || k).join("；") +
            "。缺这些就无法判断「" + (OBJECTIVE_TEXT[objective] || objective) +
            "」到底有没有做到，系统拒绝在没有依据的情况下假装能优化。",
        }),
        el("p", {
          class: "field-note",
          // 签发按钮由 issueBlockReason 同步禁用（mandate-6），这里的措辞不能再说
          // "仍然可以点"——界面自己说的话要与自己的按钮一致。
          text: "签发按钮已随之禁用。就绪与否由服务端裁定——这是设计，不是故障。",
        })
      );
      blocked.hidden = false;
      presetBlock.hidden = true;   // 打法卡片只服务于就绪目标；未就绪时不给假的推荐值
    } else {
      blocked.hidden = true;
      presetBlock.hidden = false;
    }
    renderIssueGate();   // 目标切换会改变签发按钮的可用性，必须跟着重算
  }

  // ---------- 作用域分区 ----------
  function scopeKind() {
    const on = document.querySelector("input[name=scope_kind]:checked");
    return on ? on.value : "PROFILE";
  }

  function renderScope() {
    const kind = scopeKind();
    const items = mandateDraft.scopeItems;
    const n = items.length;
    $("scope-detail").hidden = kind !== "OBJECTS";

    // 层级漏一个，那一层的对象就在计数里凭空消失：总数 n 和括号里的分项对不上，
    // 人会以为带入丢了东西。分项覆盖全部层级，非零才显示。
    const byLevel = { campaign: 0, ad_group: 0, ad: 0, target: 0 };
    for (const it of items) if (byLevel[it.level] != null) byLevel[it.level] += 1;
    $("scope-count").textContent = n === 0
      ? "还没有带入任何对象"
      : "已带入 " + n + " 个（活动 " + byLevel.campaign + " · 广告组 " + byLevel.ad_group +
        (byLevel.ad ? " · 广告 " + byLevel.ad : "") +
        (byLevel.target ? " · 投放 " + byLevel.target : "") + "）";

    //: 2026-08-29 Owner 反馈：还没带入对象时，「展开勾选清单（0）」展开只有一片空白，
    //  「清空带入的对象」点了也没反应——两个能点但点了没用的控件。此时该做什么，
    //  下面那行红字已经说清楚（去工作台勾选再带入），空壳不必占位。
    const hasItems = n > 0;
    $("scope-list-wrap").hidden = !hasItems;
    $("scope-clear").hidden = !hasItems;
    if (!hasItems) $("scope-list-wrap").open = false;

    $("scope-list-summary").textContent = "展开勾选清单（" + n + "）";
    const list = $("scope-list");
    list.textContent = "";
    for (const it of items) {
      list.append(el("div", { class: "scope-item" },
        el("span", { class: "level-badge", text: LEVEL_TEXT_SHORT[it.level] || it.level }),
        el("span", { class: "scope-item-name", text: it.name || "（无名称）" }),
        el("span", { class: "mono scope-item-id", text: it.external_id }),
        el("button", {
          class: "btn btn-sm", type: "button", text: "移除",
          dataset: { action: "scope-remove", externalId: it.external_id, level: it.level },
        })
      ));
    }
    renderScopeProfileWarn();
    renderIssueGate();
  }

  //: ui-3/mandate-2（2026-08-29 排查结论）：带入对象后再改店铺输入，提交必被服务端
  //  拒绝——不一致要在改的当下就说，而不是等提交挨拒。只警告不禁用：服务端是最终裁判。
  function renderScopeProfileWarn() {
    const warn = $("scope-profile-warn");
    const formProfile = $("f-profile").value.trim();
    const withProfile = mandateDraft.scopeItems.filter((s) => s.profile);
    const mismatch = scopeKind() === "OBJECTS" && withProfile.length > 0 && formProfile &&
      withProfile.some((s) => s.profile !== formProfile);
    if (!mismatch) {
      warn.hidden = true;
      warn.textContent = "";
      return;
    }
    const src = withProfile[0].profile;
    warn.hidden = false;
    // 有名录时说店名（认得出），没有时退回 ID（仍精确）。
    warn.textContent =
      "带入的 " + withProfile.length + " 个对象属于店铺「" + profileLabel(src) + "」，与上方填的「" +
      profileLabel(formProfile) + "」不一致——提交会被拒绝；换回前者或清空重选。";
  }

  // ---------- 运行窗口分区 ----------
  function windowKind() {
    const on = document.querySelector("input[name=run_window_kind]:checked");
    return on ? on.value : "ALL_DAY";
  }

  function renderWindow() {
    const on = windowKind() === "WINDOW";
    $("window-detail").hidden = !on;
    const echo = $("window-echo");
    if (!on) {
      echo.textContent = "不限钟点：任何时候发起运行都不会因时段被拒（配额、最短间隔、有效期仍然生效）。" +
        "本系统不会到点自己运行——运行由人在 AI 客户端里发起。";
      renderIssueGate();
      return;
    }
    const tz = $("f-tz").value.trim();
    const sh = parseInt($("f-start-hour").value, 10);
    const eh = parseInt($("f-end-hour").value, 10);
    if (!tz) {
      echo.textContent = "请先选时区——系统不会拿服务器所在地的钟点替你解释时间。";
    } else if (sh === eh) {
      echo.textContent = "起止钟点相同，这不是一个有长度的时段。";
    } else {
      echo.textContent = "只允许在" + windowPhrase(sh, eh, tz) + "之间发起运行；其余钟点发起会被拒绝" +
        "（OUTSIDE_RUN_WINDOW）。本系统不会到点自己运行——选了时段，就要有人在这个时段里发起。";
    }
    renderIssueGate();
  }

  // ---------- 签发按钮门控（客户端预检，不伪造错误码） ----------
  function issueBlockReason() {
    if (!isHuman()) return "AI 身份不能签发授权书——服务端强制，此处仅提示";
    //: mandate-6（2026-08-29 排查结论）：就绪度清单可用且当前目标未就绪时预告式禁用——
    //  否则人填完整张表才被拒。清单不可用时**不拦**（fail-open），由服务端兜底裁定。
    if (Array.isArray(state.objectives)) {
      const row = state.objectives.find((o) => o.objective === $("f-objective").value);
      if (row && row.ready === false) {
        const n = Array.isArray(row.missing) ? row.missing.length : 0;
        return "该目标数据地基未就绪（缺 " + n + " 样），见上方说明";
      }
    }
    if (scopeKind() === "OBJECTS") {
      const items = mandateDraft.scopeItems;
      if (items.length === 0) {
        // Owner 实测（2026-08-29）：勾了 2 个对象，这里却说「还没有勾选任何对象」——
        // 对已经勾了的人这是假话，他会认定自己照做了、问题在别处，从此不会去找
        // 「带入」这第二步。勾选（工作台）与带入（这份授权书）是两件事，就照实说。
        if (wb.selected.size > 0) {
          return "你在下方「对象工作台」勾了 " + wb.selected.size +
            " 个对象，但还没把它们带进这份授权书——勾选只是工作台里的选择，" +
            "带入才算写进授权范围。";
        }
        return "还没有勾选任何对象——请先到下方「对象工作台」勾选活动或广告组，再点「用选中对象签授权书」";
      }
      if (items.length > 200) {
        return "已勾选 " + items.length + " 个，超过单次上限 200 个——请拆成多份授权书，或改为整店授权";
      }
      if (items.some((i) => i.level === "target")) {
        return "投放层对象不能作为「清除浪费花费」授权的作用域：否定词按广告组落位，投放层无法界定范围——请改勾活动或广告组";
      }
    }
    if (windowKind() === "WINDOW") {
      if (!$("f-tz").value.trim()) return "限定时段必须填时区（如 Asia/Kuala_Lumpur）";
      const sh = parseInt($("f-start-hour").value, 10);
      const eh = parseInt($("f-end-hour").value, 10);
      if (sh === eh) return "起止钟点不能相同——不按钟点限制请选上面的「不限时段」";
    }
    return null;
  }

  function renderIssueGate() {
    const reason = issueBlockReason();
    $("issue-submit").disabled = reason !== null;
    const note = $("issue-gate-note");
    note.hidden = reason === null;
    note.textContent = reason || "";
    const scopeErr = $("scope-error");
    const windowErr = $("window-error");
    scopeErr.hidden = true;
    windowErr.hidden = true;
    if (reason && scopeKind() === "OBJECTS" && /对象|作用域|投放层/.test(reason)) {
      scopeErr.hidden = false;
      scopeErr.textContent = reason;
      // 已勾未带入：把第二步就地摆出来。否则人得先滚到页面底部、在动作栏里
      // 认出「用选中对象签授权书」——而他刚被告知的是「你还没勾」。
      if (mandateDraft.scopeItems.length === 0 && wb.selected.size > 0) {
        scopeErr.append(" ", el("button", {
          class: "btn btn-sm btn-primary", type: "button",
          text: "把勾选的 " + wb.selected.size + " 个带进来",
          dataset: { action: "scope-use-selected" },
          title: "等同于工作台底部动作栏的「用选中对象签授权书」",
        }));
      }
    } else if (reason && windowKind() === "WINDOW" && /时区|钟点/.test(reason)) {
      windowErr.hidden = false;
      windowErr.textContent = reason;
    }
  }

  async function submitIssueForm(form) {
    const fd = new FormData(form);
    const num = (k) => parseInt(String(fd.get(k)), 10);
    const body = {
      profile_external_id: String(fd.get("profile_external_id")).trim(),
      objective: String(fd.get("objective")),
      statement: String(fd.get("statement")).trim(),
      lookback_days: num("lookback_days"),
      min_spend_amount: String(fd.get("min_spend_amount")).trim(),
      currency: String(fd.get("currency")).trim().toUpperCase(),
      min_clicks: num("min_clicks"),
      max_data_staleness_hours: num("max_data_staleness_hours"),
      max_runs_per_day: num("max_runs_per_day"),
      max_candidates_per_run: num("max_candidates_per_run"),
      valid_days: num("valid_days"),
      run_interval_minutes: num("run_interval_minutes"),
      // 作用域与运行窗口按冻结接口的嵌套形状上传；缺省（整店 + 全天）与今天行为一致。
      scope: scopeKind() === "OBJECTS"
        ? {
            kind: "OBJECTS",
            //: ui-3/mandate-2（2026-08-29 排查结论）：从工作台带入的对象随项上传其
            //  实际所属店铺，与授权书店铺不一致时服务端拒绝。没有 profile 的项
            //  （非工作台来源）不带该字段，行为不变。
            items: mandateDraft.scopeItems.map((s) => {
              const item = { level: s.level, external_id: s.external_id };
              if (s.profile) item.profile_external_id = s.profile;
              return item;
            }),
          }
        : { kind: "PROFILE" },
      run_window: windowKind() === "WINDOW"
        ? {
            timezone: $("f-tz").value.trim(),
            start_hour: parseInt($("f-start-hour").value, 10),
            end_hour: parseInt($("f-end-hour").value, 10),
          }
        : null,
    };
    // 二轮审计：重复签发零防护——绿条没看见的人会再点一次，签出两份一模一样的
    // 生效授权（AI 双倍频次跑、审批队列翻倍、事后分不清撤哪份）。同店同目标已有
    // 生效授权时先问一句；服务端本就允许多份（域模型如此），这里只挡「无意识重复」。
    const now = Date.now();
    const dup = state.mandates.find((m) =>
      m.state === "ACTIVE" && new Date(m.expires_at).getTime() > now &&
      m.profile_external_id === body.profile_external_id && m.objective === body.objective);
    //: 签发是这条流程里最后一个没有确认的终局动作——批准、拒绝、撤销都问一句，
    //  唯独最先发生、也最要紧的这一个不问（2026-09-07 第五轮排查）。它同时是最
    //  容易被误触的一个：表单里的单行输入按回车就是提交（原生隐式提交），而
    //  「参数细则」此刻还折叠着（index.html 的 details#adv-wrap 默认不展开）。
    //  人在「为什么签这份授权」里敲完一句话顺手回车，就把一份自己一眼都没看过
    //  数字的授权签给了 AI。
    //: 所以确认框照念那几个被折叠起来的数字。它既是「你确定吗」，也是这些参数
    //  在签下去之前唯一一次被摆到眼前——只写「确定签发吗」等于什么都没问。
    const windowText = body.run_window
      ? body.run_window.start_hour + ":00–" + body.run_window.end_hour + ":00 " +
        body.run_window.timezone
      : "全天";
    const terms = [
      profileLabel(body.profile_external_id),
      OBJECTIVE_TEXT[body.objective] || body.objective,
      body.scope.kind === "OBJECTS" ? "圈定 " + body.scope.items.length + " 个对象" : "整店",
      windowText,
      "最多 " + body.max_runs_per_day + " 次/日",
      "有效 " + body.valid_days + " 天",
    ].join(" · ");
    const gates = "判定门槛：回看 " + body.lookback_days + " 天 · 花费 ≥ " +
      body.min_spend_amount + " " + body.currency + " · 点击 ≥ " + body.min_clicks +
      " · 数据不超过 " + body.max_data_staleness_hours + " 小时 · 每轮最多 " +
      body.max_candidates_per_run + " 个候选";
    //: 重复签发那句并进同一个框——连弹两个确认，人只会连点两次「确定」。
    //  服务端本就允许多份（域模型如此），这里只挡「无意识重复」。
    const dupLine = dup
      ? "\n\n注意：这家店已有一份生效中的同目标授权（有效至 " + fmtTime(dup.expires_at) +
        "）。再签一份会两份同时生效、各自按频次运行。"
      : "";
    if (!window.confirm(
      "签发这份授权书？\n\n" + terms + "\n" + gates + dupLine +
      "\n\n签发后 AI 就能按这些参数发起运行；要停下来只能撤销。")) {
      return;
    }
    let issuedId = null;
    try {
      const m = await api("/mandates", { method: "POST", body });
      issuedId = m.mandate_id;
      showAlert("success", "授权书已签发：" + profileLabel(m.profile_external_id) + " · " +
        (OBJECTIVE_TEXT[m.objective] || m.objective) + " · " + mandateScopeText(m) + " · " +
        mandateWindowText(m) + "，有效至 " + fmtTime(m.expires_at) +
        "。下一步：点该行的「复制指令」，把它交给 Codex 等 AI 客户端运行——本系统不会自己运行。");
      // 二轮审计：签发成功即清空带入的作用域草案——留着它，下一次提交就是同一批
      // 对象的重复授权；打法参数是无害偏好，保留不清。
      mandateDraft.scopeItems = [];
      renderScope();
    } catch (err) {
      alertError(err);
    }
    await refreshAll();
    //: 绿条刚说完「点该行的『复制指令』交给 AI 客户端」，而那一行在表单之上一屏多，
    //  页面又停在签发按钮旁边一动不动。照做的第一步是往上滚过整张表单再找到表尾——
    //  一句指路，落点在视野外，等于没指。绿条 6 秒后消失，那之后视野内没有任何
    //  东西证明刚才签成了。滚过去，让人直接看到新签的那一行。
    //: 滚的必须是**新签的那一行本身**，不是列表的开头（2026-09-07 排查）。
    //  上一版滚的是 #mandate-list，落点是表头；而新行按签发时间排在表尾，
    //  行一多它就在视野之外——指路的那句话仍然落空，而这段注释还写着人已经看见了。
    //  拿不到那一行（列表还没渲染出来）才退回滚列表，聊胜于无。
    if (issuedId) {
      const anchor = document.querySelector(
        '#mandate-list [data-mandate-id="' + issuedId + '"]');
      const row = anchor ? anchor.closest("tr") : null;
      (row || $("mandate-list"))?.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }

  //: 多店场景（Owner 实测 8 家店）下两张列表都只是把全部内容一路铺开：待批页签底下
  //  混着几家店的卡片，授权书表里生效中的和已撤销/已过期的挨在一起。人想先处理某一家
  //  店，只能逐张展开去认「店铺」那一格。筛选只做一件事——按店过滤——且只在**数据里
  //  真的出现两家以上店铺**时才渲染这个下拉：单店部署（含本演示）界面一个字不变。
  //  选项从已加载数据里去重生成，不依赖同步白名单接口（那个接口在纯 Mock 下恒为空）。
  function knownProfiles() {
    const seen = new Set();
    for (const s of state.sets || []) if (s.profile_external_id) seen.add(s.profile_external_id);
    for (const m of state.mandates || []) if (m.profile_external_id) seen.add(m.profile_external_id);
    return [...seen].sort();
  }

  function matchesProfileFilter(x) {
    return !state.profileFilter || x.profile_external_id === state.profileFilter;
  }

  function renderProfileFilter() {
    const host = $("profile-filter");
    if (!host) return;
    const profiles = knownProfiles();
    host.textContent = "";
    // 只有一家店时这个控件没有任何作用，渲染它只是多一个要读的东西。
    host.hidden = profiles.length < 2;
    if (host.hidden) {
      state.profileFilter = null;
      return;
    }
    // 筛选中的店铺后来消失了（撤销 + 过期清理），不能把界面卡在一个空列表上。
    if (state.profileFilter && !profiles.includes(state.profileFilter)) state.profileFilter = null;
    const select = el("select", { class: "filter-select", "aria-label": "按店铺筛选" });
    select.append(el("option", { value: "", text: "全部店铺（" + profiles.length + "）" }));
    for (const p of profiles) select.append(el("option", { value: p, text: profileLabel(p) }));
    select.value = state.profileFilter || "";
    select.addEventListener("change", () => {
      state.profileFilter = select.value || null;
      renderKpis();
      renderMandates();
      renderSets();
    });
    host.append(
      el("span", { class: "set-meta", text: "店铺" }),
      select,
      el("span", { class: "set-meta", text: "上面的三个数字与下面两张列表都跟着这里筛" })
    );
  }

  //: 空态要按手上的事实说话，不能永远是同一段开场白。
  //  批准掉唯一一份待批之后，页面停在「待批」页签上，而这段常量把人指回流程第①步
  //  「先签一份授权书」——他刚批的那份正在「已批」等着下载 CSV，而审批与执行之间
  //  那一跳正是这个产品存在的理由。批准成功的绿条 6 秒就没了，之后屏上只剩这段
  //  指错方向的话。同一段话对「已有生效授权、只是这一轮还没跑」的人也是错的：
  //  他不需要再签一份，他需要去点「复制指令」。
  function frozenEmptyState() {
    const fallback = SET_TAB_EMPTY[state.tab] || "此状态下暂无集合。";
    if (state.tab !== "FROZEN") return document.createTextNode(fallback);
    //: 空态也要跟着店铺筛选走。不跟，筛到 B 店时它会说「1 份已批准的集合正等着
    //  下载」——而那一份属于 A 店，人切到「已批」页签会看到空的。
    const approved = (state.sets || []).filter(
      (s) => s.state === "APPROVED" && matchesProfileFilter(s)
    ).length;
    if (approved > 0) {
      const box = el("div", null,
        el("div", { text: "待批是空的——" + approved + " 份已批准的集合正等着下载 CSV 拿去领星执行。" }),
        el("div", { class: "set-meta", text: "本系统不记录你是否已下载、是否已执行，请以本机的 CSV 文件为准。" })
      );
      box.append(el("button", {
        class: "btn btn-sm", type: "button", text: "去「已批」页签",
        dataset: { action: "goto-approved" },
      }));
      return box;
    }
    const live = (state.mandates || []).filter(
      (m) => matchesProfileFilter(m) && m.state === "ACTIVE" &&
        new Date(m.expires_at).getTime() > Date.now()
    );
    //: 「还没有人发起过运行」只有在真的一次都没跑过时才是真话（2026-09-06 排查）。
    //  把唯一一批候选拒掉之后，待批空了、已批为 0，人就会读到这句——而同一屏上
    //  授权书那一行明明白白写着「上次运行 产出候选」。两句话直接打架，人只能怀疑
    //  自己看错了，或者怀疑刚才那次拒绝没生效。run_count_known 就是为了分开
    //  「从没跑过」和「跑过但没结果」而存在的，这里用上它。
    const ran = live.filter((m) => m.run_count_known && m.last_run_at);
    if (ran.length > 0) {
      const latest = ran.reduce((a, b) =>
        new Date(b.last_run_at).getTime() > new Date(a.last_run_at).getTime() ? b : a);
      const spec = RUN_OUTCOME_TEXT[latest.last_outcome] ||
        { text: latest.last_outcome || "未知结果", next: "" };
      //: 数的是**跑过的那几份**，不是全部生效授权（2026-09-07 排查）。上一版这里
      //  写的是 live.length：3 份生效、只有 1 份跑过时，这句说「3 份都跑过了」，
      //  而同一屏下面另外两行写着「签发已 N 小时，还没跑过」，各自还挂着琥珀条。
      //  run_count_known 刚被用来分开这两件事，这个数又把它们合了回去。
      const ranPhrase = ran.length === live.length
        ? "生效中的 " + live.length + " 份授权书跑过了"
        : "生效中的 " + live.length + " 份授权书里有 " + ran.length + " 份跑过";
      //: 下一步要跟着**结局**说（2026-09-07 排查）。上一版对所有结局都只说
      //  「点复制指令再跑一次」——而没跑通的那几种（币种签错、店铺没接数据源、
      //  作用域全挡掉、整批数据太旧）照原样再跑一次必然是同一个失败，且每试一次
      //  都真扣一次日配额。每个结局自己的 next 就是为这件事写的，别丢掉它。
      const nextLine = latest.last_outcome === "CANDIDATES"
        ? "那一批已经处理掉了（批准 / 拒绝 / 或过了 72 小时时效）。要再跑一次，" +
          "点授权书行上的「复制指令」——本系统不会自己到点运行。"
        : spec.ok
          ? "要再跑一次，点授权书行上的「复制指令」——本系统不会自己到点运行。"
          //: 取数失败是唯一一种**有条件**可重试的结局，它自己的 next 就写着
          //  「超时类可以再发起一次，参数类错误重试永远不会成功」。再拼上那句绝对的
          //  「照原样再跑一次会是同一个结果」，两句紧挨着直接互相否定，而后果是
          //  方向性的：超时时重试正是唯一正确的动作，界面却拿配额吓阻，人不重试，
          //  这份授权就一直空转（2026-09-07 第二次排查）。
          : latest.last_outcome === "SOURCE_ERROR"
            ? "下一步：" + spec.next + "。"
            : "下一步：" + spec.next + "。照原样再跑一次会是同一个结果，而每跑一次都要扣一次日配额。";
      return document.createTextNode(
        "待批是空的。" + ranPhrase + "，最近一次是 " +
        fmtTime(latest.last_run_at) + "，结局是「" + spec.text + "」。" + nextLine
      );
    }
    if (live.length > 0) {
      return document.createTextNode(
        "待批是空的。已有 " + live.length + " 份生效中的授权书，但还没有人发起过运行——" +
        "本系统不会自己定时运行。点授权书行上的「复制指令」，交给 Codex 等 AI 客户端跑一次；" +
        "跑完点右上「刷新列表」。"
      );
    }
    return document.createTextNode(fallback);
  }

  // ---------- 候选集合 ----------
  //: 页签切换只此一处（页签按钮与 KPI 卡片共用）。
  function selectSetTab(tabState) {
    if (!tabState) return;
    state.tab = tabState;
    for (const t of $("set-tabs").querySelectorAll(".tab")) {
      t.classList.toggle("is-active", t.dataset.state === tabState);
    }
    renderSets();
  }

  //: 数字缺失时显示「—」，绝不显示 0：源侧没给和真的是 0，人得能分开。
  function fmtCount(n) {
    return n == null ? "—" : String(n);
  }

  //: CTR 只在显示层现算，不入库、不进 hash：它是个比值，逐行求和无意义，
  //  必须用同一层级上聚合好的两个数重算。曝光为 0 时不写 0%（那会读成
  //  「点击率极低」），写「—」——没有曝光就谈不上点击率。
  function ctrText(impressions, clicks) {
    if (impressions == null || Number(impressions) <= 0) return "—";
    return ((Number(clicks || 0) / Number(impressions)) * 100).toFixed(2) + "%";
  }

  function renderSets() {
    const region = $("set-list");
    region.textContent = "";
    // ui-5（2026-08-29 排查结论）：同 renderMandates——失败绝不冒充"还没有集合"，
    // 三个页签共用同一份拉取，失败态对每个页签都成立。
    if (state.setsStatus === "error") {
      region.append(loadErrorState(state.setsError, "retry-sets"));
      return;
    }
    const visible = state.sets.filter((s) => s.state === state.tab && matchesProfileFilter(s));
    if (visible.length === 0) {
      region.append(el("div", { class: "empty-state" }, frozenEmptyState()));
      return;
    }
    // 审计 #48：待批按生成时间升序会让最新可批的沉底、过期死卡片霸榜——
    // 新的在上、过期的沉底（它们只剩「拒绝」一条路，不该挡住能办的事）。
    visible.sort((a, b) => {
      const ax = a.expired === true ? 1 : 0;
      const bx = b.expired === true ? 1 : 0;
      if (ax !== bx) return ax - bx;
      return new Date(b.generated_at).getTime() - new Date(a.generated_at).getTime();
    });
    //: 「已批」页签只会单调变长，而卡片上没有任何字段能回答「这份做过了吗」——
    //  系统确实不记录下载与执行（导出是纯 GET，状态机止于 APPROVED）。答不出来
    //  就直说答不出来，比让人在十几张同形卡片之间凭记忆猜要好。
    if (state.tab === "APPROVED") {
      region.append(el("div", { class: "set-note", text:
        "下载 CSV 后请到领星后台手工添加否定词。本系统不记录你是否已下载、是否已执行——" +
        "哪几份已经做过，请团队自己记（CSV 文件名带集合 ID）。" }));
    }
    for (const s of visible) region.append(setCard(s));
  }

  function evidenceWindowRow(s) {
    const cands = s.candidates || [];
    const starts = new Set(cands.map((c) => c.window_start).filter(Boolean));
    const ends = new Set(cands.map((c) => c.window_end).filter(Boolean));
    if (starts.size !== 1 || ends.size !== 1) {
      // 一个集合里出现多个窗口，或者服务端没给——都不编一句好听的。
      return el("div", { class: "set-note", text: cands.length ? "统计区间：服务端未提供，无法确认这批数字统计的是哪一段。" : "" });
    }
    const start = [...starts][0];
    const end = [...ends][0];
    // 按 **UTC 日历天** 数没有数据的天数，不按小时差取整：生成于 08-30 03:53Z、
    // 窗口最后一天 08-27 时，小时差取整是 2，而实际上 08-28/08-29/08-30 三天都没有
    // 数据。这一整条修的就是「把缺口说小」，自己再说小一次就没意思了。
    const lagDays = s.generated_at
      ? Math.round((Date.parse(fmtUtcDate(s.generated_at)) - Date.parse(fmtUtcLastDay(end))) / 86400000)
      : 0;
    //: 「还没有数据」说得不准：那几天的花费与点击是有的，只是订单还没归因完，
    //  所以窗口右端被刻意往回推。说成「没有数据」，人会以为是取数漏了。
    const tail = lagDays > 0
      ? "；最近 " + lagDays + " 天不计入（订单归因还没结算完，算进来会把正在出单的词判成零单）"
      : "";
    return el("div", { class: "set-note" },
      el("strong", { text: "统计区间 " }),
      el("span", { text: fmtUtcDate(start) + " 至 " + fmtUtcLastDay(end) + "（UTC）" + tail }),
      el("span", { class: "muted", text: "　下表的花费/点击/广告订单都只统计这一段；" +
        "窗口末尾几天的订单仍可能补录，表里的 0 不是最终值。" })
    );
  }

  function setStateBadge(s) {
    const cls = { FROZEN: "state-frozen", APPROVED: "state-approved", REJECTED: "state-rejected" }[s.state]
      || "state-generated";
    const zh = { FROZEN: "待批", APPROVED: "已批", REJECTED: "已拒", GENERATED: "已生成" }[s.state] || s.state;
    // 审计 #31 同族：徽章不再中英并印（「待批 FROZEN」），原文进悬停。
    return el("span", { class: "state-badge " + cls, text: zh, title: s.state });
  }

  function namedIdCell(name, externalId) {
    if (!name) return el("td", { class: "num", text: externalId });
    return el("td", null,
      el("div", { text: name }),
      el("span", { class: "cell-sub mono", text: externalId }));
  }

  function setCard(s) {
    const card = el("details", { class: "set-card" });
    if (state.expandedSets.has(s.set_id)) card.open = true;
    card.addEventListener("toggle", () => {
      if (card.open) state.expandedSets.add(s.set_id);
      else state.expandedSets.delete(s.set_id);
    });

    //: approval-3（2026-08-29 排查结论）：FROZEN 集合有 72 小时时效，过期后批准必被
    //  服务端拒绝——过期要在卡片上一眼可辨，而不是点了「批准」才发现。expired 由
    //  服务端按当下时刻判定（仅对 FROZEN 有意义），前端不自己拿 expires_at 算钟点。
    const expired = s.state === "FROZEN" && s.expired === true;

    card.append(el("summary", null,
      setStateBadge(s),
      expired ? el("span", {
        class: "chip chip-warn",
        text: "已过期",
        title: "已超过 72 小时时效，只能拒绝后重新生成",
      }) : null,
      el("span", { class: "set-id", title: s.set_id, text: shortId(s.set_id) }),
      // 这批是哪家店的。此前卡片一个字都不说，两家店的待批集合除 uuid 前 8 位外
      // 逐字同形——同一条产品线在两家店常常就是同名广告组。人挑一份批准、
      // 下载 CSV，没有任何一列告诉他该打开哪家店的后台。
      s.profile_external_id
        ? el("span", { class: "set-meta", title: s.profile_external_id, text: "店铺 " + profileLabel(s.profile_external_id) })
        : null,
      el("span", { class: "set-meta", text: candidateCountText(s) }),
      // 截断前命中多少个。签发表单承诺过「运行结果会告诉你截断前有多少个」，
      // 而那个人往往就是审批屏幕前的这个人。
      //: 上限来自哪里，要跟这批候选的出身对上（2026-08-30 排查）。即席生成不在任何
      //  授权书之下——同一张卡片的另一枚 chip 正是这么写的——它撞的是服务端写死的
      //  那道上限。此前两种出身共用「超出授权书的单次上限」一句，于是即席卡片上
      //  两句话直接打架，人去翻授权书想改这个数字，翻遍了也找不到。
      //: 「其余的怎么办」必须说得能走通（2026-08-30 排查）。此前即席那支写「只能分批」，
      //  而截断是按花费从高到低取前 N 的**确定性**取法：原样再跑一次拿到的是逐字相同
      //  的一批，界面随即把它标成重复、叫人拒掉——一个闭环。真正能推进的动作只有一个：
      //  把这批批准并在领星执行，已否定的词下一轮不会再被提名（取数下推
      //  targeted_type=not_negatived），后面的词才轮得到。
      s.truncated_from
        ? el("span", {
            class: "chip chip-warn",
            text: "本次共命中 " + s.truncated_from + " 条",
            title: (s.mandate_id
              ? "超出这份授权书的单次上限，"
              : "即席生成不在授权书之下，撞的是服务端固定的单次上限（改不了），") +
              "按花费从高到低保留了 " + s.candidate_count +
              " 条；其余 " + (s.truncated_from - s.candidate_count) + " 条本次没有进入这批候选。" +
              "原样再跑一次拿到的还是这一批（按花费取前 N，确定性）——" +
              "要轮到后面的词，先把这批批准并在领星执行" +
              (s.mandate_id ? "，或重签一份放宽单次上限的授权。" : "。"),
          })
        : null,
      //: 这份清单不是本轮浪费的全部。与上面的截断提示是同一个病的两个入口：签字的人
      //  看到的只有清单本身，读出来是「本轮的浪费都在这儿了」，于是他在领星把这几条
      //  加完否定词就收工，而那几个 ASIN 还在烧钱。放在审批屏幕上，是因为那正是他
      //  以为自己处理完了的那一刻。
      s.asin_abstain_count
        ? el("span", {
            class: "chip chip-warn",
            text: "另有 " + s.asin_abstain_count + " 个 ASIN 否不掉",
            title: "详见下方说明",
          })
        : null,
      // 来源授权书已被撤销：撤销确认框刚跟人说过「AI 立即停止按它运行」，
      // 而它今早生成的集合还原样躺在「待批」里，按钮亮着。
      s.mandate_state === "REVOKED"
        ? el("span", {
            class: "chip chip-warn",
            text: "来源授权书已撤销",
            title: "这批候选来自一份已被撤销的授权书；撤销时你要停的就是它。批准会被服务端拒绝——请拒绝这一批。",
          })
        : null,
      el("span", { class: "set-meta", text: "生成于 " + fmtTime(s.generated_at) }),
      s.state === "FROZEN" && s.expires_at
        ? el("span", { class: "set-meta", text: "时效至 " + fmtTime(s.expires_at) })
        : null,
      el("span", { class: "set-meta", text: "来源 " + String(s.source || "—") }),
      //: 这批候选是在哪份合同下跑出来的（2026-08-30 排查 #15）。「来源 AI」在两种
      //  处境下逐字相同：一种是 AI 按人签发的授权书、用合同里钉死的参数跑出来的；
      //  另一种是 AI 自己挑参数即席跑的——不在任何授权书之下，不受配额与运行时段
      //  约束。整套授权机制的意义就是这个区分，而审批屏幕上此前一个字都不说。
      s.mandate_id
        ? el("span", {
            class: "set-meta", title: "授权书 " + s.mandate_id,
            text: "按授权书 " + shortId(s.mandate_id),
          })
        : el("span", {
            class: "chip chip-warn",
            text: "即席生成",
            title: "不在任何授权书之下：参数由调用方在白名单范围内自选，不受授权书的" +
              "配额、最小间隔与运行时段约束。批准前请自己核对下面那行参数。",
          }),
      //: 参数必须看得见（2026-09-06 排查）：上面那句「自己核对」是整条即席分支唯一的
      //  安全说辞，而卡片上此前一个阈值都没有——lookback / min_spend / min_clicks /
      //  时效四个数，人找一圈找不到，只能放弃核对直接批，等于替公司在一批参数不明的
      //  否定词上签字。授权书那边早就印着同源的这一行，即席这边没有授权书行可对。
      s.mandate_id ? null : packSummaryText(s.parameter_pack)
        ? el("span", { class: "set-meta", text: packSummaryText(s.parameter_pack) })
        : null,
      s.approved_by_person_id
        ? el("span", { class: "set-meta", text: "批准人 " + s.approved_by_person_id })
        : null,
      //: 「已批」列表按生成时间倒序，而人回头问的是「哪份是我刚批的」——两者不同序。
      //  没有这一格，他只能靠「最上面那张」猜，而那个直觉在跨天审批时是错的。
      s.approved_at
        ? el("span", { class: "set-meta", text: "批准于 " + fmtTime(s.approved_at) })
        : null
    ));

    const body = el("div", { class: "set-body" });

    // 冻结指纹：批准动作绑定的就是这里显示的 hash。
    //: 不叫「内容指纹」（2026-08-30 排查 #1）：每条候选的编号都进这个 hash，于是
    //  内容逐字相同的两次生成必得两个不同的值。叫它内容指纹，就是在教人「指纹不同
    //  ⇒ 内容不同」——而重复生成的两张卡片正是这样并排躺在待批里。防篡改要它每次
    //  都变，认重复要它不变，一个值答不了两个问题；认重复的那个是下面这行。
    body.append(el("div", { class: "hash-row" },
      el("span", { class: "hash-label", text: "冻结指纹 set_hash" }),
      el("span", {
        class: "hash-value", text: shortHash(s.set_hash) || "—",
        title: (s.set_hash || "") + "\n批准与这个值绑定，内容变过就会被拒。它每次生成都不同，" +
          "两份指纹不同不代表内容不同。",
      }),
      s.set_hash ? el("button", {
        class: "btn btn-sm", type: "button", text: "复制",
        dataset: { action: "copy-hash", hash: s.set_hash },
      }) : null
    ));

    //: 同内容的重复：same_content_as 是 null 时表示服务端没查（单份查询），不说话。
    //  孪生集合处在哪个状态，决定了这句话该说什么——「批一份就够」对一份**已经批过**
    //  的孪生是假话，人照着做会再导出一份一模一样的 CSV；对一份**拒过**的孪生更是
    //  反的：他上次的判断是「不要」，而系统又把同一批词端上来了。
    const twins = dupTwins(s);
    if (twins.length) {
      const byState = { FROZEN: [], EXPIRED: [], APPROVED: [], REJECTED: [] };
      for (const t of twins) (byState[t.state] || (byState[t.state] = [])).push(t.set_id);
      const lines = [];
      if (byState.FROZEN.length) {
        lines.push("另有 " + byState.FROZEN.length + " 份待批与本份内容逐字相同（" +
          byState.FROZEN.map(shortId).join("、") + "）——同一段数据被生成了多次，批一份就够，" +
          "其余可以直接拒掉；批两份只会导出两份一样的 CSV。");
      }
      if (byState.EXPIRED.length) {
        lines.push("另有 " + byState.EXPIRED.length + " 份内容相同但已过期（" +
          byState.EXPIRED.map(shortId).join("、") + "）——那几份批不了，只能拒掉；" +
          "本份还在时效内的话，批本份即可。");
      }
      if (byState.APPROVED.length) {
        //: 「再批一次拿到的是同一批」把人指向「拒掉这份」，而在真实通道下，
        //  这批词能回来本身就是个信号：取数只向领星要了「未否定」的词。
        //  通道未知（loadRuntimeConfig 失败）时一个字都不加，不替它挑一种解释。
        const why = state.termSource === "LINGXING"
          ? "真实通道取数时只向领星要了「未否定」的词，这批还是回来了：要么那份 CSV 还没在领星执行，" +
            "要么刚执行、领星报表或本地缓存还没跟上。先确认那份已执行，再拒掉这份。"
          : state.termSource && state.termSource.startsWith("MOCK")
            ? "演示数据不随时间变化，这批词每次生成都会原样回来。"
            : "";
        lines.push("这批词你已经批准过（" + byState.APPROVED.map(shortId).join("、") +
          "）——那份的 CSV 在「已批」页签可以直接下载，再批一次拿到的是同一批否定词。" + why);
      }
      if (byState.REJECTED.length) {
        lines.push("这批词你拒绝过（" + byState.REJECTED.map(shortId).join("、") +
          "）——同一段数据又被生成了一次。若不该再提，改授权书的参数（门槛、回看天数）比反复拒更省事。");
      }
      body.append(el("div", { class: "dup-note" },
        el("strong", { text: "这批词不是第一次出现" }),
        el("span", { text: lines.join(" ") })
      ));
    }

    //: 这句必须可见，不能只挂在 chip 的 title 上。同一份代码为完全相同的理由立过两次
    //  规矩——「手机上没有悬停，所以这句要可见」、「要人动手的那句单独占一整行」——
    //  在授权书那边执行了，在这张卡片上没有。而这里比那两处更要紧：人在这张卡片上
    //  签完字就去领星，chip 上「另有 3 个 ASIN 否不掉」不说人话，他就带着
    //  「本轮浪费都在这份 CSV 里」的结论走了。
    if (s.asin_abstain_count) {
      body.append(el("div", { class: "dup-note" },
        el("strong", { text: "这份清单不是本轮浪费的全部" }),
        el("span", {
          text: "同一轮里还有 " + s.asin_abstain_count +
            " 个搜索词花了钱、零转化，但它们是 ASIN 不是关键词——本系统只开否定精准关键词，" +
            "加上去挡不住它们，所以没有进这份清单。批准这份清单不覆盖它们：" +
            "要去领星「否定投放」页签单独否定这几个 ASIN。",
        }),
        //: 词必须就在这里（2026-09-06 排查）：这句原来写「向 AI 要那次运行的
        //  abstains」，而那条路走不通——词表不进任何存储，列表工具只回计数，
        //  按同一份授权书重跑当天必撞 RUN_BUDGET_EXCEEDED。人知道该去哪个页签，
        //  唯独拿不到要否定的那个词，当天没有任何合法出路。
        (s.asin_abstain_terms || []).length
          ? el("div", { class: "asin-terms" },
              el("span", { text: "要否定的是：" }),
              el("span", { class: "mono", text: (s.asin_abstain_terms || []).join("、") }))
          : null
      ));
    }

    //: 导出时 render_bulk_csv 会给以 = + - @ 制表符 回车 开头的单元格前置一个单引号
    //  （防止表格软件把顾客搜索词当公式执行——搜索词是站外真实输入，这是必要的防线，
    //  不能撤）。代价是 CSV 里的那几个词与这张表上显示的**不是同一个字符串**：
    //  人复制过去，落到领星就是一条永远命中不了的否定精确词，而下一轮取数下推
    //  targeted_type=not_negatived，这个词原样回来，界面的重复提示会把他指回去
    //  查自己——一个闭环的误诊。屏幕上有原词，只是没人告诉他两者不一样。
    const defused = (s.candidates || []).filter(
      (c) => ["=", "+", "-", "@", "\t", "\r"].indexOf(String(c.search_term).charAt(0)) >= 0
    );
    if (defused.length) {
      body.append(el("div", { class: "set-note", text:
        "有 " + defused.length + " 个词以 = + - @ 这类字符开头（" +
        defused.map((c) => c.search_term).join("、") +
        "）。下载的 CSV 会在它们前面多一个单引号，防止表格软件把顾客搜索词当公式执行——" +
        "在领星里添加否定词时请按上表显示的原词输入，不要带那个引号。" }));
    }

    // 统计区间。没有它，卡片上的「生成于今天」与表里的「转化 0」并排出现，
    // 人读成「今天查的，这个词到今天一单没出」——而窗口右端被归因滞后刻意往回
    // 推了几天，最近那几天根本没看。两个数字各自都对，摆在一起就把一个刻意的
    // 滞后变成了不存在，而窗口有多长恰恰是「该不该否定这个词」的关键前提。
    // 「不含最近 N 天」由数据算出（generated_at 与 window_end 之差），不写死。
    body.append(evidenceWindowRow(s));

    // 证据表
    //: 行序此前是取数顺序（服务端只在超出上限时才按花费排）。真实规模下这一批是
    //  50–150 行，人要逐行扫才知道哪几条最贵——而「先看最贵的」正是他唯一的读法。
    //  排序只在显示层做：set_hash 绑定的是服务端那份，这里复制一份再排。
    const rows = [...(s.candidates || [])].sort(
      (a, b) => Number(b.spend) - Number(a.spend) || String(a.search_term).localeCompare(String(b.search_term))
    );
    //: 「这批一共值多少钱」此前一个字都没有，而它是「要不要现在处理这一批」的
    //  第一个判据。按币种分组求和：一个集合理论上只有一种币种（授权书钉死），
    //  但真出现两种时分开列，不合并成一个没有意义的数。
    const totals = new Map();
    let clickTotal = 0;
    let convTotal = 0;
    //: 只要有一行读不出曝光，这一批就说不出总曝光——少算几行会把合计 CTR 算高，
    //  而"高 CTR"读出来是"这批词其实挺相关，别急着否"，方向恰好偏向保留。
    let imprTotal = 0;
    for (const c of rows) {
      const cur = String(c.currency || "");
      totals.set(cur, (totals.get(cur) || 0) + Number(c.spend || 0));
      clickTotal += Number(c.clicks || 0);
      convTotal += Number(c.conversions || 0);
      if (imprTotal !== null) {
        imprTotal = c.impressions == null ? null : imprTotal + Number(c.impressions);
      }
    }
    const spendTotalText = [...totals.entries()]
      .map(([cur, amt]) => amt.toFixed(2) + " " + cur).join(" + ");
    const evidence = el("table", { class: "data" },
      el("thead", null, el("tr", null,
        el("th", { text: "搜索词" }),
        el("th", { text: "广告组" }),
        el("th", { text: "广告活动" }),
        el("th", { text: "花费", title: "窗口内这个词在这个广告组花掉的广告费；表按它从高到低排" }),
        //: 曝光与点击率不参与任何判定，纯粹给人看——但它们正是"该不该否掉这个词"
        //  的关键前提：42 次点击来自 300 次曝光（CTR 14%，流量高度相关，问题多半在
        //  listing 或价格，否掉是把好流量扔了）和来自 6 万次曝光（CTR 0.07%，纯粹
        //  不相关，该否）是相反的结论，而花费/点击/广告订单三列在两种情形下逐字相同。
        el("th", { text: "曝光", title: "窗口内这个词在这个广告组的曝光次数。不参与判定，只用来看这些点击是从多大的流量里来的。「—」= 源侧没给或读不出来，不猜。" }),
        el("th", { text: "点击" }),
        el("th", { text: "点击率", title: "点击 ÷ 曝光，显示时现算（比值逐行相加没有意义）。很低 = 这个词与商品不相关；不低却零单 = 相关但转化不了，问题可能在 listing 或价格，否掉它未必对。" }),
        //: 列头此前叫「转化」，同一页的工作台却叫「订单」，而两者是同一个东西：
        //  领星 orders。人对着两套词，会以为「转化」另有所指（加购？）。口径也得
        //  写出来——含间接归因，是这一条判定「零转化」时最容易踩空的地方。
        el("th", {
          text: "广告订单",
          title: "领星 orders：直接 + 间接归因的广告订单合计（不是只算直接归因）。" +
            "窗口末尾几天的订单可能仍在归因中，0 不是最终值。",
        }),
      )),
      // 审计 #5/#25：广告组/活动列裸 ID 让批准变盲签——镜像有名称就显示名称
      // （ID 降为小字），镜像缺名时只有 ID 可显示，如实裸奔。
      el("tbody", null, ...rows.map((c) => el("tr", null,
        el("td", { text: c.search_term }),
        namedIdCell(c.ad_group_name, c.ad_group_external_id),
        namedIdCell(c.campaign_name, c.campaign_external_id),
        el("td", { class: "num", text: String(c.spend) + " " + String(c.currency) }),
        el("td", { class: "num", text: fmtCount(c.impressions) }),
        el("td", { class: "num", text: String(c.clicks) }),
        el("td", { class: "num", text: ctrText(c.impressions, c.clicks) }),
        el("td", { class: "num", text: String(c.conversions) }),
      ))),
      rows.length ? el("tfoot", null, el("tr", null,
        el("td", { text: "合计 " + rows.length + " 条(广告组×词)" }),
        el("td", null), el("td", null),
        el("td", { class: "num", text: spendTotalText }),
        el("td", { class: "num", text: fmtCount(imprTotal) }),
        el("td", { class: "num", text: String(clickTotal) }),
        //: 合计行的 CTR 用合计后的两个数重算，绝不是各行 CTR 求和或求平均——
        //  比值逐行相加是个没有意义的数，而它会被人当成「这批的整体点击率」。
        el("td", { class: "num", text: ctrText(imprTotal, clickTotal) }),
        el("td", { class: "num", text: String(convTotal) }),
      )) : null
    );
    body.append(el("div", { class: "table-wrap" }, evidence));

    // 操作区
    const actions = el("div", { class: "set-actions" });
    if (s.state === "FROZEN") {
      const approveBtn = el("button", {
        class: "btn btn-primary btn-sm", type: "button", text: "批准",
        // 审计 #35：旧悬停是开发者参数说明（expected_hash）——改说后果；机制进括号。
        title: "确认这批否定词可以执行：批准后到「已批」页签下载 CSV，拿去领星后台添加否定词。" +
          "（批准与上方冻结指纹绑定，内容变过就会被拒）",
        dataset: { action: "approve-set", setId: s.set_id, hash: s.set_hash || "", count: String(s.candidate_count ?? "") },
      });
      const rejectBtn = el("button", {
        class: "btn btn-sm btn-danger-ghost", type: "button", text: "拒绝",
        dataset: { action: "reject-set", setId: s.set_id },
      });
      // 否决与批准同为审批意思表示，域层同样只放行 HUMAN（AI_CANNOT_REJECT）。
      // 少禁一个 = 把一次注定 403 的点击留给人，且与顶栏铁律行自相矛盾。
      if (!isHuman()) { approveBtn.disabled = true; rejectBtn.disabled = true; }
      // approval-3：过期集合的批准注定被拒，预告式禁用；拒绝仍可用——那正是出路。
      if (expired) {
        approveBtn.disabled = true;
        approveBtn.title = "已超过 72 小时时效，只能拒绝后重新生成";
      }
      //: 同上（2026-08-30 排查 #12）：来源授权书已撤销时批准同样注定 409。卡片上
      //  已经挂着「来源授权书已撤销」的琥珀标，按钮却还亮着——把一次注定失败的
      //  点击留给人，而这一次点击是替公司签字的那一次。
      if (s.mandate_state === "REVOKED") {
        approveBtn.disabled = true;
        approveBtn.title = "这批候选来自一份已被撤销的授权书；撤销时你要停的就是它。" +
          "只能拒绝后重新签发授权书再生成。";
      }
      actions.append(approveBtn, rejectBtn);
      //: 这一屏只有「批准」「拒绝」两个按钮，而人常遇到的是「12 个词里 1 个不能否」。
      //  两个按钮都不对：拒绝掉，另外 11 个确实在烧钱的词继续不被否，下一轮还原样回来；
      //  批准掉，界面没有一句话告诉他那一行可以不执行。而孪生提示教的「改参数重跑」
      //  在原理上办不到——参数包只有窗口/金额/点击/时效四个旋钮，没有词级旋钮，
      //  抬门槛掉的是最便宜的那几条，不是那个品牌词。
      //  真出路一直存在且零成本：CSV 每行是人到领星后台手工加的一条，跳过一行即可
      //  （runbook §④）。缺的只是这句话。不做逐词剔除——那是为一个已有出路的场景
      //  再造一套部分批准的内容物与 hash 绑定。
      if (!expired && s.mandate_state !== "REVOKED") {
        actions.append(el("span", {
          class: "sod-note",
          text: "个别词不该否？CSV 每行是你到领星后台手工加的一条，执行时跳过那一行即可，不必为一个词拒掉整批",
        }));
      }
      if (!isHuman()) {
        actions.append(el("span", {
          class: "sod-note", text: "AI 不能批准，也不能拒绝——服务端强制，此处仅提示",
        }));
      }
    }
    if (s.state === "APPROVED") {
      actions.append(el("button", {
        class: "btn btn-sm", type: "button", text: "下载 CSV",
        dataset: { action: "export-set", setId: s.set_id },
      }));
    }
    if (actions.children.length > 0) body.append(actions);

    card.append(body);
    return card;
  }

  //: 批/拒之后焦点往哪儿放（2026-09-07 实测）。委派处理器在分发前就把按钮
  //  disabled（防双击），浏览器随即把焦点丢回 <body>；refreshAll 再把整张列表
  //  重建一遍，那颗按钮连同它的位置一起消失。于是键盘用户批完一份候选，下一次
  //  Tab 是从文档最顶上重新开始——而提示恰好在说「可在「已批」页签导出 CSV」，
  //  那个页签此刻离他几十次 Tab。工作台侧早有同一套治法（wbRestoreFocus /
  //  wbToggleDrawer 的 mayTakeFocus），审批侧一直没有，而这边的动作不可逆。
  //  落点取「这份集合去了哪个页签」——那正是人此刻唯一的问题，批准时它还恰好
  //  是下一步。只在焦点确实是我们弄丢的那种情况下才接管。
  function focusSetTab(state) {
    const a = document.activeElement;
    const mine =
      !a ||
      a === document.body ||
      a === document.documentElement ||
      $("set-list").contains(a) ||
      $("set-tabs").contains(a);
    if (!mine) return;
    const tab = $("set-tabs").querySelector('[data-state="' + state + '"]');
    if (tab && !tab.disabled && tab.offsetParent !== null) tab.focus();
  }

  //: 候选数数的是 (广告组 × 搜索词) 行，不是搜索词个数：同一个词投在三个广告组里
  //  就是三条（去重键是 (ad_group_id, term)，见 negation.py）。印成「N 个候选词」，
  //  人拿这个数去领星后台数词必然对不上——而他真要录的正是 N 条，每个广告组各录
  //  一条。两数不等时把词数也说出来：那正是他的预期会落空的那一刻
  //  （2026-09-07 实测：4 条候选、只有 2 个不同的词）。
  function candidateCountText(s) {
    const n = Number(s.candidate_count) || 0;
    const words = Array.isArray(s.candidates)
      ? new Set(s.candidates.map((c) => String(c.search_term || "").toLowerCase())).size
      : 0;
    return n + " 条候选(广告组×词)" + (words && words < n ? " · " + words + " 个不同的词" : "");
  }

  async function approveSet(setId, expectedHash, count) {
    // 二轮审计：拒绝、撤销都会再问一句，唯独批准——整条流程里唯一替公司签字、
    // 产出可执行 CSV 的动作——单击即成且服务端没有反悔端点。补上同款确认。
    const n = count ? count + " 条否定(广告组×词)" : "这批候选";
    if (!window.confirm("批准" + (count ? "这 " + n : n) + "？批准后即为「可以执行」的最终决定，" +
      "可在「已批」页签导出 CSV 拿去执行，不能再改回待批。")) {
      return;
    }
    try {
      await api("/candidate-sets/" + encodeURIComponent(setId) + "/approve", {
        method: "POST",
        body: { expected_hash: expectedHash },
      });
      showAlert("success", "已批准集合 " + shortId(setId) + "，可在「已批」页签导出 CSV。");
    } catch (err) {
      alertError(err);
    }
    await refreshAll();
    focusSetTab("APPROVED");
  }

  //: 「需要重新生成」这句得先确认重新生成这条路今天走得通。默认打法是 1 次/日，
  //  而产出这批候选的那一次运行已经把它用掉了——人拒绝之后才发现今天再也生成不出
  //  东西来，而拒绝是不可逆的。配额与「最早几点可再发起」服务端已经算好挂在授权书上，
  //  按下之前照实说出来。
  function regenerateOutlook(setId) {
    const set = state.sets.find((s) => s.set_id === setId);
    const mid = set && set.mandate_id;
    if (!mid) return "";   // 即席生成不受配额约束，重跑随时可以
    const m = state.mandates.find((x) => x.mandate_id === mid);
    if (!m || typeof m.runs_remaining_today !== "number") return "";
    if (m.runs_remaining_today > 0) return "";
    return "注意：这份授权今天的次数已用完，今天重新生成不出来" +
      (m.next_run_allowed_at ? "，最早 " + fmtShortTime(m.next_run_allowed_at) + " 才能再发起" : "") +
      "。";
  }

  async function rejectSet(setId) {
    // 审计 #24：拒绝单击即生效且不可逆（集合终态）——加确认。
    if (!window.confirm(
      "拒绝这批候选？拒绝后该集合进入终态，不能再批准（需要重新生成）。" + regenerateOutlook(setId)
    )) {
      return;
    }
    try {
      await api("/candidate-sets/" + encodeURIComponent(setId) + "/reject", { method: "POST" });
      showAlert("success", "已拒绝集合 " + shortId(setId) + "。");
    } catch (err) {
      alertError(err);
    }
    await refreshAll();
    focusSetTab("REJECTED");
  }

  async function exportSetCsv(setId) {
    // 导出需带 Bearer 头，无法用裸 <a href>；用 fetch + blob 触发下载。
    try {
      const res = await fetch("/candidate-sets/" + encodeURIComponent(setId) + "/export.csv", {
        headers: authHeaders(),
      });
      if (!res.ok) {
        const parsed = await readErrorDetail(res);
        alertError({ code: parsed.code, status: res.status, serverMessage: parsed.serverMessage });
        //: 批准/拒绝/撤销三处失败后都会重拉，唯独导出不会——于是服务重启后，
        //  一张已经不存在的卡片留在屏上，人可以对着它一直点下载、一直收到同一条
        //  红条（同码只累加计数）。跟那三处对齐。
        await refreshAll();
        return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      //: 文件名此前只有一串 uuid。下载文件夹是人手里唯一的「哪几份做过了」的记录，
      //  而 negation-<uuid>.csv 既看不出是哪家店、也看不出是哪天的。店铺别名与日期
      //  都在手里（列表响应里就有），拼进去零成本。走的是 blob 下载，落盘名由这里
      //  的 download 属性决定，服务端的 Content-Disposition 在这条路径上不参与。
      const meta = (state.sets || []).find((x) => x.set_id === setId);
      const safe = (t) => String(t).replace(/[\\/:*?"<>|\s]+/g, "_");
      const day = meta && meta.generated_at ? fmtUtcDate(meta.generated_at).replace(/-/g, "") : "";
      const filename = ["negation",
        meta && meta.profile_external_id ? safe(profileLabel(meta.profile_external_id)) : "",
        day, shortId(setId)].filter(Boolean).join("-") + ".csv";
      const a = el("a", { href: url, download: filename });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
      showAlert("success", "CSV 已开始下载：" + filename +
        "——每行是一个广告组要加的一条否定精确词；到领星后台按 campaign_name / ad_group_name 找到广告组再添加。");
    } catch (err) {
      alertError(err);
    }
  }

  async function copyHash(hash, button) {
    let ok = false;
    try {
      await navigator.clipboard.writeText(hash);
      ok = true;
    } catch {
      // 剪贴板 API 被拒时的旧式回退：临时 textarea + execCommand
      const ta = el("textarea", { style: "position:fixed;opacity:0" });
      ta.value = hash;
      document.body.append(ta);
      ta.select();
      try { ok = document.execCommand("copy"); } catch { ok = false; }
      ta.remove();
    }
    if (ok) {
      const old = button.textContent;
      button.textContent = "已复制";
      setTimeout(() => { button.textContent = old; }, 1500);
    } else {
      showAlert("error", "无法访问剪贴板，请手动复制下方完整值", hash);
    }
  }

  // ---------- 对象工作台 ----------
  // 镜像只供浏览与选择；预览是读侧动作（AI 也可调），执行永远走既有审批链。
  // 筛选与排序都是**浏览端**行为（GET /objects 查询参数）：它只影响"表格显示哪些行"，
  // 离开工作台的永远是人勾出来的显式 external_id 集合，绝不是晚绑定查询。

  //: 动作 → 数值输入形态（null=无数值 / absolute=绝对值 / percent=±百分比）
  const WB_ACTION_INPUT = {
    PAUSE: null,
    ENABLE: null,
    SET_DAILY_BUDGET: "absolute",
    SCALE_DAILY_BUDGET: "percent",
    SET_BID: "absolute",
    SCALE_BID: "percent",
  };

  //: 动作 → 允许的对象层级（全称判断：已选层级必须全部落在集合内）。
  //  服务端 MirrorExpansionPort 是全有全无的，混选会 409 PREVIEW_VALUE_UNAVAILABLE。
  const WB_ACTION_LEVELS = {
    PAUSE: null,   // null = 不限层级
    ENABLE: null,
    SET_DAILY_BUDGET: ["CAMPAIGN"],
    SCALE_DAILY_BUDGET: ["CAMPAIGN"],
    SET_BID: ["AD_GROUP", "TARGET"],
    SCALE_BID: ["AD_GROUP", "TARGET"],
  };

  // 2026-08-29 排查结论 workbench-4：镜像只活在服务进程内存里，重启即空。空镜像
  // 最常见的原因是"重启后还没重新同步"，旧文案却把人指去查 LX_MCP_KEY/LX_MCP_URL
  // ——一个没坏的配置。环境变量名不进文案；真在白名单外时服务端会明确报错。
  //: 空态给的下一步必须与同屏那颗按钮的实际状态一致（2026-09-06 实测）：这段话原来
  //  恒说「点『同步镜像』拉取」，而纯 Mock 部署（默认部署）下那颗按钮是灰的，紧挨着
  //  的小字正写着「没有可同步的东西」。人换个店铺 ID 就撞上：空表格叫他去点，按钮
  //  不让点，两句话在同一屏上打架。理由从按钮那边取，不再自己编一套。
  function wbEmptyMirrorText() {
    const head = "该店铺在本进程里还没有镜像数据。";
    if (state.channelMode === "mock") {
      return head + "这台服务没有配领星只读通道，镜像里只有演示种子店铺（" +
        DEMO_PROFILE_PLACEHOLDER + "）的数据，换成别的店铺 ID 就是空的——这里没有可同步的东西。";
    }
    if (!isHuman()) {
      return head + "同步只能由运营负责人身份触发。切换身份后点「同步镜像」拉取（约 1–2 分钟）。";
    }
    if (Array.isArray(wb.syncProfiles) && !wb.syncProfiles.includes(wb.profile)) {
      return head + "这个店铺不在服务端的同步授权名单里，无法同步。";
    }
    return head + "数据只保存在服务进程内存中，服务重启后需要重新同步" +
      "——点「同步镜像」拉取（约 1–2 分钟）。";
  }

  //: 勾选上限。服务端 MAX_AFFECTED_OBJECTS 是唯一裁判；响应若回显 max_selection 则以它为准。
  const WB_MAX_SELECTION_FALLBACK = 200;

  //: 演示种子店铺。服务端配了同步白名单时会被白名单首个真实店铺顶掉，见 loadWbProfiles。
  const DEMO_PROFILE_PLACEHOLDER = "profile-A";

  const wb = {
    profile: DEMO_PROFILE_PLACEHOLDER,
    level: "campaign",     // campaign | ad_group | target（查询参数原文）
    page: 1,
    length: 25,
    rows: [],
    total: 0,
    mirrorEmpty: false,
    maxSelection: WB_MAX_SELECTION_FALLBACK,
    selected: new Map(),   // object_key → {level, external_id, name}
    filters: { name_contains: "", state: "", managed_only: "" },
    sortField: "",
    sortDir: "desc",
    status: "loading",     // loading | ok | error
    error: null,
    knownStates: new Set(),
    syncStart: 0,
    syncTimer: null,
    previewStale: false,
    //: 2026-08-29 排查新增的覆盖率/续拉状态（workbench-1 与已知项②）：
    syncProfiles: null,        // null = 白名单未知（接口失败即 fail-open，交服务端判定）
    continuation: null,        // 上一轮同步给的断点游标；null = 已拉全。只对当店当窗口有效
    lastSyncErrorCode: null,   // 上一轮同步失败的服务端错误码；null = 没失败或无码
    continuationSummary: "",   // 续拉条上的覆盖率描述（随游标一起清）
    levelCoverage: null,       // /objects 回的本层覆盖：{source_total, rows_covered, truncated} | null
    reportDate: null,          // 这批行同属的指标窗口；null = 不同属一个（见下两项）
    reportWindows: [],         // 这批行里实际出现过的窗口
    reportUnknownRows: 0,      // 说不出自己是哪个窗口的行数（如演示种子）
    //: 2026-08-29 领星 IA 借鉴（docs/evidence/lx-ads-ia-20260829.md §1/§3）：
    perfBucket: "",            // "" = 全部；其余为服务端 PERF_BUCKETS 之一
    bucketCounts: null,        // /objects 回的分桶计数（对筛选后、分桶前的全集）
    drillCampaign: null,       // 下钻中的父活动 {id, name}；null = 未下钻
    drillAdGroup: null,        // 下钻中的父广告组 {id, name}；null = 未下钻
    autoSyncOn: false,         // 自动连拉进行中（审计 #8）
    autoSyncStop: false,       // 人要求完成本轮后停止
    autoSyncRound: 0,
    //: 二轮审计（2026-08-29）：并发在途请求谁后到谁赢，旧响应会盖掉新状态。
    reqSeq: 0,                 // /objects 请求代次——响应到达时不是最新代次即整体丢弃
    syncSeq: 0,                // 同步代次——每轮同步结束 +1；跨代次的游标恢复一律不认
  };

  function wbHasFilter() {
    const f = wb.filters;
    return !!(f.name_contains || f.state || f.managed_only || wb.perfBucket);
  }

  function wbLevelOfKey(objectKey) {
    if (objectKey.startsWith("campaign:")) return "CAMPAIGN";
    if (objectKey.startsWith("ad_group:")) return "AD_GROUP";
    if (objectKey.startsWith("ad:")) return "AD";
    return "TARGET";
  }

  function wbExternalId(objectKey) {
    return objectKey.slice(objectKey.indexOf(":") + 1);
  }

  function fmtRelative(iso) {
    const t = new Date(iso).getTime();
    if (Number.isNaN(t)) return { text: String(iso || "—"), stale: false };
    const mins = Math.max(0, Math.round((Date.now() - t) / 60000));
    let text;
    if (mins < 1) text = "刚刚";
    else if (mins < 60) text = mins + " 分钟前";
    else if (mins < 2880) text = Math.round(mins / 60) + " 小时前";
    else text = Math.round(mins / 1440) + " 天前";
    return { text, stale: mins > 24 * 60 }; // 超过 24 小时按过旧警示
  }

  function lockIcon() {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 12 12");
    svg.setAttribute("width", "10");
    svg.setAttribute("height", "10");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(ns, "path");
    path.setAttribute(
      "d",
      "M3.2 5V3.6a2.8 2.8 0 0 1 5.6 0V5h.4a1 1 0 0 1 1 1v3.8a1 1 0 0 1-1 1H2.8a1 1 0 0 1" +
      " -1-1V6a1 1 0 0 1 1-1h.4zm1.2 0h3.2V3.6a1.6 1.6 0 0 0-3.2 0V5z"
    );
    path.setAttribute("fill", "currentColor");
    svg.append(path);
    return svg;
  }

  //: 2026-08-29 Owner 反馈：店铺显示 16 位数字 ID 认不出是哪家店。名录随白名单
  //  接口下发（alias/country 来自领星 ad_auth_shops）；label 缺名录时退回 ID。
  const profileDirectory = new Map();  // profile_id → {alias, country, currency}
  function profileLabel(pid) {
    const d = profileDirectory.get(pid);
    if (!d || !d.alias) return pid;
    return d.alias + (d.country ? "（" + d.country + "）" : "");
  }

  //: 授权书的店铺写入统一走这里：input#f-profile 是唯一真值来源（带 name，FormData
  //  只收它），select 只是给人看店名的选择器。白名单外的值（手输/演示种子）select
  //  表达不了，退回手输框——别让人对着一个选不中的下拉发愣。
  function setFormProfile(pid) {
    $("f-profile").value = pid;
    const sel = $("f-profile-select");
    if (!sel.hidden) {
      if ([...sel.options].some((o) => o.value === pid)) {
        sel.value = pid;
      } else {
        sel.hidden = true;
        $("f-profile").hidden = false;
      }
    }
    syncCurrencyToProfile(pid);
    renderScopeProfileWarn();
  }

  // 结算币种是系统已经知道的事实，不该让人手填。此前它是自由文本、默认写死 USD，
  // 而界面手里存着站点（店名后面就印着「（DE）」）。填错的代价不是当场报错：
  // 签发会成功、卡片显示「生效中」、「待批」页签一直空着，而每一次运行都在
  // MCP 面被 CURRENCY_MISMATCH 拒——那个码在这个界面上一次都不会出现。
  // 人手动改过就不再自动覆盖（尊重明示意图），但会就地提示不一致。
  //: 币种有两个来源，先问名录（同步白名单带的），答不出再直接问服务端。
  //  只靠名录是**错的**：那条路答的是「哪些店允许被同步」，纯 Mock 部署下恒为空，
  //  于是这个字段恒说「服务端不知道」——而服务端知道（组合根注入的 profile_currency），
  //  并且会拿它 422 拒签。接了同步通道但没开策略侧真实取数时更糟：每个真实店都落进
  //  这句「不知道」，USD 默认值原样留着，德国站签出的授权书每次运行都被
  //  CURRENCY_MISMATCH 拒，而那个码在这个界面上一次都不会出现。
  async function askServerForCurrency(pid) {
    if (!pid || currencyAsked.has(pid)) return;
    currencyAsked.add(pid);
    try {
      const res = await api("/mandates/profile-currency?profile_external_id=" + encodeURIComponent(pid));
      if (res && res.currency) {
        const prev = profileDirectory.get(pid) || {};
        profileDirectory.set(pid, { ...prev, currency: res.currency });
        if ($("f-profile") && $("f-profile").value.trim() === pid) syncCurrencyToProfile(pid);
      }
    } catch { /* 问不到就维持「不知道」——那时它是真话 */ }
  }
  const currencyAsked = new Set();

  //: ask=false 用于「人还在逐字敲店铺 ID」：note 照样重算（不能对着新店铺留着旧店铺
  //  的结论），但不向服务端发问——否则 profile-DE-9 这一串会敲出 11 次请求，
  //  每个前缀一个 pid，currencyAsked 拦不住。敲完（change/blur）再问一次。
  function syncCurrencyToProfile(rawPid, { ask = true } = {}) {
    const field = $("f-currency");
    if (!field) return;
    const pid = (rawPid || "").trim();
    const known = (profileDirectory.get(pid) || {}).currency;
    const note = $("f-currency-note");
    if (!pid) {
      if (note) note.textContent = "先填上面的店铺 ID，才能替你确认币种。";
      return;
    }
    if (!known) {
      if (note) note.textContent = "服务端不知道这个店铺的数据币种，无法替你确认——填错会让授权书每次运行都被拒。";
      if (ask) askServerForCurrency(pid);
      return;
    }
    if (!field.dataset.touched) field.value = known;
    if (note) {
      note.textContent =
        field.value.toUpperCase() === known
          ? "已按该店铺站点带出：" + known + "。搜索词数据就是以它结算的。"
          : "这个店铺的数据以 " + known + " 结算，与你填的 " + field.value.toUpperCase() + " 不一致——签发会被拒绝。";
    }
  }

  (function markCurrencyTouchedOnEdit() {
    document.addEventListener("input", (ev) => {
      if (ev.target && ev.target.id === "f-currency") {
        ev.target.dataset.touched = "1";
        syncCurrencyToProfile($("f-profile").value);
      }
    });
  })();

  async function loadWbProfiles() {
    // 同步白名单 + 店铺名录；失败不打扰（profile 仍可手输）。
    try {
      const res = await api("/api/workbench/sync-profiles");
      const entries = (res.profiles || []).map((p) =>
        typeof p === "string" ? { profile_id: p, alias: null, country: null } : p);
      // 已知项②（2026-08-29）：白名单同时喂给同步按钮做预告式禁用——名单外的店铺
      // 点同步注定被拒，等 403 才知道是把一次白挨的拒绝留给人。
      wb.syncProfiles = entries.map((p) => p.profile_id);
      profileDirectory.clear();
      for (const p of entries) {
        profileDirectory.set(p.profile_id, { alias: p.alias, country: p.country, currency: p.currency });
      }
      // datalist 供授权书 f-profile 手输建议：value 是 ID，显示文本带店名。
      const list = $("wb-profile-options");
      list.textContent = "";
      for (const p of entries) {
        const opt = el("option", { value: p.profile_id });
        if (p.alias) opt.textContent = profileLabel(p.profile_id);
        list.append(opt);
      }
      // 候选卡片与授权书表格的「店铺 …」用的都是这份名录，而它在 refreshAll()
      // 之后才到——不重画一次，两处都会一直停在裸 ID 上，而人认店靠的是别名。
      renderSets();
      renderMandates();
      // 白名单就是「这台服务被授权同步的店铺」，也就是工作台真正该打开的东西。
      // 不顶掉演示默认值的话，配了真实通道的人一进来看到的仍是 profile-A 的演示种子，
      // 得自己想到去下拉里挑——2026-08-29 Owner 实际就是这样撞上的。
      // 只覆盖仍停在演示默认值的输入框；人手输过的不动。
      if (entries.length > 0) {
        if (wb.profile === DEMO_PROFILE_PLACEHOLDER) wb.profile = entries[0].profile_id;
        for (const id of ["wb-profile", "f-profile"]) {
          const input = $(id);
          if (input && input.value === DEMO_PROFILE_PLACEHOLDER) input.value = wb.profile;
        }
        // Owner 反馈其二：店铺该是个真下拉，不是要人猜的手输框。白名单可得就渲染
        // select（显示店名，value 仍是 profile_id，悬停见 ID）；接口失败保留手输框。
        const select = $("wb-profile-select");
        select.textContent = "";
        for (const p of entries) {
          select.append(el("option", {
            value: p.profile_id,
            text: profileLabel(p.profile_id),
            title: p.profile_id,
          }));
        }
        select.value = wb.profile;
        select.hidden = false;
        $("wb-profile").hidden = true;

        // 授权书那份同样渲染：Owner 反馈其三（2026-08-29）——工作台改成了下拉，
        // 签发表单还留着 datalist 手输框，输入框里显示的仍是 16 位数字 ID
        // （datalist 的店名只在展开的建议列表里出现，框里永远是 value）。
        const fsel = $("f-profile-select");
        fsel.textContent = "";
        for (const p of entries) {
          fsel.append(el("option", {
            value: p.profile_id,
            text: profileLabel(p.profile_id),
            title: p.profile_id,
          }));
        }
        if (entries.some((p) => p.profile_id === $("f-profile").value)) {
          fsel.value = $("f-profile").value;
          fsel.hidden = false;
          $("f-profile").hidden = true;
        }
      }
      //: 名录到手、店铺可能刚被顶掉演示默认值——币种必须跟着重算一次
      //  （2026-08-30 排查 #14）。自动选店走的是直接给 input.value 赋值，
      //  不经过 setFormProfile，于是币种停在写死的 USD，提示语一片空白。
      //  这个店若是 DE 站，人签下的授权书币种就是错的：签发会被 422 拒（或更早
      //  版本里每次运行被 CURRENCY_MISMATCH 拒），而表单上没有一个字提示过。
      //  syncCurrencyToProfile 尊重 touched，人手改过的不会被覆盖。
      syncCurrencyToProfile($("f-profile").value);
    } catch {
      // 静默：白名单建议不可用不影响手输。此时**不**禁用同步按钮（fail-open）——
      // 一个建议接口挂了不该把按钮永久锁死，真越权时服务端仍会拒绝。
      wb.syncProfiles = null;
    }
    renderWbSyncButton();
  }

  //: 键盘操作排序表头 / 绩效分桶之后，焦点会掉回 <body>（2026-09-06 排查）：
  //  两个控件都在 refreshWorkbench 里被整体重建（nav.textContent = "" / 重画表头），
  //  人脚下的那个节点被删掉，浏览器无处可放焦点。症状不是"不好看"——下一次 Tab
  //  从文档最顶上重新开始，一个只用键盘的人每按一次回车排序，就要重新 Tab 穿过
  //  整个顶栏、筛选区、表格才能回到刚才那一列。审计 #50 特意给表头补了回车/空格，
  //  补完却在同一动作里把焦点丢掉，等于那条无障碍支持只做了一半。
  //  记住"他站在哪个格子上"（按语义键，不按节点），重建后站回去。
  function wbFocusKey() {
    const a = document.activeElement;
    if (!a || a === document.body || !a.closest) return null;
    const th = a.closest("th.th-sort");
    if (th) return "sort:" + (th.dataset.sortField || "");
    const bucket = a.closest(".bucket-btn");
    if (bucket) return "bucket:" + (bucket.dataset.bucket || "");
    return null;
  }

  function wbRestoreFocus(key) {
    if (!key) return;
    //: 请求在途期间人可能已经把焦点挪到别处去了——那时抢回来比丢掉更糟。
    //  只在焦点确实无处可去（掉回 body / 文档）时才补位。
    const a = document.activeElement;
    if (a && a !== document.body && a !== document.documentElement) return;
    const sep = key.indexOf(":");
    const kind = key.slice(0, sep);
    const value = key.slice(sep + 1);
    const sel = kind === "sort"
      ? 'th.th-sort[data-sort-field="' + value + '"]'
      : '.bucket-btn[data-bucket="' + value + '"]';
    const node = document.querySelector(sel);
    //: 找不到就什么都不做：那一列/那个桶这一轮真的不在了（换了层级、筛掉了），
    //  硬塞给别的节点会把人放到他没要求去的地方。
    if (node) node.focus();
  }

  async function refreshWorkbench() {
    const focusKey = wbFocusKey();
    // 二轮审计：快速切层级/翻页/筛选会产生并发在途请求，慢的那个后到会把
    // 表格覆盖成旧层级/旧页的数据，而页签、面包屑还停在新状态。发起时领代次，
    // 响应到达时代次已过期则整体丢弃——一处守卫覆盖所有入口。
    const seq = ++wb.reqSeq;
    const syncSeqAtStart = wb.syncSeq;
    const params = new URLSearchParams({
      profile_id: wb.profile,
      level: wb.level,
      page: String(wb.page),
      length: String(wb.length),
    });
    if (wb.filters.name_contains) params.set("name_contains", wb.filters.name_contains);
    if (wb.filters.state) params.set("state", wb.filters.state);
    if (wb.filters.managed_only) params.set("managed_only", wb.filters.managed_only);
    if (wb.perfBucket) params.set("perf_bucket", wb.perfBucket);
    //: 下钻导航（领星 IA §1）：点活动名看它的广告组、点组名看它的投放——父过滤
    //  是导航状态而非筛选条件，由面包屑承载与回退。
    if (wb.drillCampaign) params.set("parent_campaign_id", wb.drillCampaign.id);
    if (wb.drillAdGroup) params.set("parent_ad_group_id", wb.drillAdGroup.id);
    if (wb.sortField) {
      params.set("sort_field", wb.sortField);
      params.set("sort_dir", wb.sortDir);
    }
    wb.status = "loading";
    wb.error = null;
    renderWbTable();
    try {
      const res = await api("/api/workbench/objects?" + params.toString());
      if (seq !== wb.reqSeq) return;   // 过期响应：一个字段都不许写
      wb.rows = res.rows || [];
      wb.total = res.total || 0;
      wb.mirrorEmpty = !!res.mirror_empty;
      //: workbench-1/-5（2026-08-29 排查结论）：镜像行数 ≠ 店铺总数、指标是哪几天的
      //  合计——这两个事实由服务端随响应下发，null = 本进程没同步过该店铺（演示种子
      //  即如此），此时不得断言覆盖率与窗口。
      wb.levelCoverage = res.level_coverage || null;
      wb.reportDate = res.report_date || null;
      wb.reportWindows = Array.isArray(res.report_windows) ? res.report_windows : [];
      wb.reportUnknownRows = Number(res.report_window_unknown_rows) || 0;
      wb.bucketCounts = res.bucket_counts || null;
      // 审计 #4/#6：游标此前只活在页面内存里，刷新即丢——覆盖率行还在报缺口，
      // 「继续拉取」却永远消失。服务端随 /objects 回传有效游标，此处恢复入口。
      // 二轮审计：/objects 的游标是请求受理时刻的快照——若本请求在途期间完成过
      // 一轮同步，这个快照已被消费，恢复它会让「已同步全部」与「没拉全可续拉」
      // 同屏矛盾，照着点还会整轮重放。跨同步代次的游标一律不认。
      if (!wb.continuation && res.sync_continuation && !wb.syncTimer &&
          syncSeqAtStart === wb.syncSeq && !wb.autoSyncOn) {
        wb.continuation = res.sync_continuation;
        if (!wb.continuationSummary) {
          wb.continuationSummary = "上一轮同步没拉全（断点保存在服务端），可从断点继续。";
        }
      } else if (wb.continuation && res.sync_continuation == null && !wb.syncTimer &&
                 syncSeqAtStart === wb.syncSeq && !wb.autoSyncOn) {
        // 服务端说没有断点，本地却还握着一份——服务重启过（内存镜像与断点簿一起
        // 清空，而演示 token 是固定串，浏览器里的 Bearer 仍然有效）。此前只在
        // 本地为空时**采纳**服务端游标，从不因服务端变 null 而**清除**本地的，
        // 于是那份描述着已不存在的行的游标会被回传，服务端照单全收。
        wb.continuation = null;
        wb.continuationSummary = "服务端已没有这个店铺的同步断点（服务可能重启过）——请重新完整同步。";
      }
      if (res.max_selection != null) wb.maxSelection = Number(res.max_selection);
      for (const r of wb.rows) if (r.state) wb.knownStates.add(r.state);
      wb.status = "ok";
    } catch (err) {
      if (seq !== wb.reqSeq) return;   // 过期请求的失败同样不许碰状态
      // 拉取失败**不得**渲染成"暂无对象"——那是把错误说成事实。
      wb.rows = [];
      wb.total = 0;
      wb.mirrorEmpty = false;
      wb.levelCoverage = null;   // 失败时同样不许拿旧覆盖率做断言
      wb.bucketCounts = null;    // 失败时同样不许拿旧计数画快捷条
      wb.status = "error";
      wb.error = describeError(err);
      alertError(err);
    }
    renderWbStateOptions();
    renderWbBreadcrumb();
    renderWbBuckets();
    renderWbTable();
    renderWbPager();
    renderWbReportWindow();
    renderWbActionbar();
    renderWbChips();
    renderWbContinuation();
    wbRestoreFocus(focusKey);
  }

  //: workbench-5（2026-08-29 排查结论）：表格里的花费/ACOS 是报表窗口的合计，这个
  //  口径此前没处写——人只能猜是"昨天"还是"累计"。窗口事实来自服务端，拉取失败时
  //  隐藏本行（失败不做断言，与表格失败态同一纪律）。
  function renderWbReportWindow() {
    const box = $("wb-report-window");
    if (wb.status === "error") {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    box.className = "wb-report-window";
    //: #13（2026-08-30 排查）：窗口此前是一个 profile 级的全局值，每轮同步无条件覆写。
    //  截断的同步隔天再开一轮新的，没被重拉到的行仍带着上一个窗口的花费，表头却写着
    //  新窗口——人对着一个写死的窗口把整张表排序、比大小，行与行之间根本不可比，
    //  屏幕上没有一个字提示过。现在窗口记在每一行上，这里如实说这批行是否同属一窗。
    if (!wb.reportDate) {
      if (wb.reportWindows.length > 1) {
        box.className = "wb-report-window wb-report-window-warn";
        box.textContent = "指标窗口不一致：这批行来自 " + wb.reportWindows.length +
          " 个不同的时间窗（" + wb.reportWindows.join("、") +
          "），花费/ACOS 不可横向比较，排序结果也不可当排名读。" +
          "用「自动拉取直到拉全」把这一轮走完，仍出现在报表里的行才会对齐到同一个窗口；" +
          "再点「同步镜像」是从第一页重新开始，够不到后面那些还带着旧窗口的行。";
        return;
      }
      if (wb.reportWindows.length === 1) {
        box.className = "wb-report-window wb-report-window-warn";
        box.textContent = "指标窗口不齐：" + wb.reportUnknownRows +
          " 行说不出自己统计的是哪一段（不是本进程同步来的），其余来自 " +
          wb.reportWindows[0] + "。两者不可横向比较。";
        return;
      }
      //: #4/#13（2026-08-30 排查）：这一句此前是「筛选后没有行」时唯一会出现的话，
      //  于是筛选把行全滤掉时，界面对着一个刚同步完的店铺断言「尚未在本进程同步」。
      //  窗口清单是按**筛选后的行**算的，说不出窗口有三种完全不同的原因，
      //  只有第一种与同步有关。
      //: 0 行有两种原因，不能都算到筛选头上（2026-09-06 实测）：点「广告」页签时
      //  演示种子在该层恒为 0 行，而人一个筛选都没设过，界面却说「当前筛选下一行
      //  都没有」——他会去清一个不存在的筛选。同屏表格区说的是「这一层没有对象」，
      //  两句话对不上。
      box.textContent = wb.mirrorEmpty
        ? "指标窗口：未知（该店铺尚未在本进程同步）"
        : wb.total === 0
          ? (wbFilterActive()
              ? "指标窗口：当前筛选下一行都没有，无从说起"
              : "指标窗口：这一层一个对象都没有，无从说起")
          : "指标窗口：未知（这批行没有带窗口，不是本进程同步来的）";
      return;
    }
    // 审计 #30/#29/#27：口径按实际窗口说（不再写死「最近 7 天」——实际是 8 个
    // 自然日且含未走完的今天）；金额/ACOS 为源侧原值、无币种换算；时刻按浏览器时区。
    let span = "";
    const m = /^(\d{4}-\d{2}-\d{2}) - (\d{4}-\d{2}-\d{2})$/.exec(wb.reportDate);
    if (m) {
      const days = Math.round((Date.parse(m[2]) - Date.parse(m[1])) / 86400000) + 1;
      const todayUtc = new Date().toISOString().slice(0, 10);
      span = "（" + days + " 个自然日合计，按 UTC 日切" +
        (m[2] >= todayUtc ? "，含今天尚在累计的数据" : "") + "）";
    }
    box.textContent = "指标窗口：" + wb.reportDate + span +
      " · 金额与 ACOS 为领星源侧原值 · 时刻按你的浏览器时区显示";
  }

  function renderWbStateOptions() {
    const list = $("wb-state-options");
    list.textContent = "";
    for (const s of [...wb.knownStates].sort()) {
      const opt = el("option", { value: s });
      // datalist 显示中文说法（投放中/已暂停），value 仍是源侧原文——服务端按原文精确匹配。
      if (STATE_TEXT[s]) opt.textContent = STATE_TEXT[s];
      list.append(opt);
    }
  }

  //: 下钻面包屑（领星 IA §1「下钻即导航」）：全部活动 › 活动名 › 组名。
  //  每一段可点回退；层级页签点击 = 回到该层平铺全量（与领星顶部页签同语义）。
  function renderWbBreadcrumb() {
    const bar = $("wb-breadcrumb");
    bar.textContent = "";
    if (!wb.drillCampaign && !wb.drillAdGroup) { bar.hidden = true; return; }
    bar.hidden = false;
    bar.append(el("button", {
      class: "crumb-link", type: "button", text: "全部活动",
      title: "回到活动层平铺列表",
      dataset: { action: "wb-drill-to", target: "root" },
    }));
    if (wb.drillCampaign) {
      bar.append(el("span", { class: "crumb-sep", text: "›" }));
      if (wb.drillAdGroup) {
        bar.append(el("button", {
          class: "crumb-link", type: "button", text: wb.drillCampaign.name,
          title: "回到该活动的广告组列表",
          dataset: { action: "wb-drill-to", target: "campaign" },
        }));
      } else {
        bar.append(el("span", { class: "crumb-here", text: wb.drillCampaign.name }));
      }
    }
    if (wb.drillAdGroup) {
      bar.append(el("span", { class: "crumb-sep", text: "›" }));
      bar.append(el("span", { class: "crumb-here", text: wb.drillAdGroup.name }));
    }
    bar.append(el("span", {
      class: "crumb-note",
      text: "正在看这" + (wb.drillAdGroup ? "个广告组" : "个活动") + "下的" +
        (LEVEL_TEXT_SHORT[wb.level] || wb.level),
    }));
  }

  //: 绩效分桶快捷条（领星 IA §3）：运营找问题广告的第一反应是「花了钱没单的在哪」。
  //  计数由服务端对筛选后、分桶前的全集算——点了某桶其余数字不变；失败/镜像空不画。
  function renderWbBuckets() {
    const nav = $("wb-buckets");
    nav.textContent = "";
    if (wb.status !== "ok" || wb.mirrorEmpty) { nav.hidden = true; return; }
    const counts = wb.bucketCounts;
    nav.hidden = false;
    let bucketSum = 0;
    for (const [key, label] of PERF_BUCKET_LABELS) {
      const n = counts ? counts[key || "all"] : null;
      if (key && n != null) bucketSum += Number(n) || 0;
      nav.append(el("button", {
        type: "button",
        class: "bucket-btn" + (wb.perfBucket === key ? " is-active" : ""),
        dataset: { bucket: key },
        "aria-pressed": wb.perfBucket === key ? "true" : "false",
        text: n != null ? label + "（" + n + "）" : label,
      }));
    }
    if (counts && counts.all > bucketSum) {
      const missing = counts.all - bucketSum;
      nav.append(el("span", {
        class: "bucket-note",
        text: missing + " 行缺判定指标，不参与分桶",
        title: "分桶需要订单/点击/曝光指标；缺失≠0，这些行不冒充任何桶，但仍算在「全部」里",
      }));
    }
  }

  // 层级页签的 active 态跟随 wb.level——下钻/面包屑回退改层时也要同步，不只在点页签时。
  function syncLevelTabs() {
    for (const t of $("wb-level-tabs").querySelectorAll(".tab")) {
      t.classList.toggle("is-active", t.dataset.level === wb.level);
    }
  }

  //: 下钻（领星 IA §1）：点活动名 → 该活动的广告组；点组名 → 该组的投放，同时带上
  //  所属活动让面包屑完整。层级与父过滤一起换，页码归 1；预览随层级失效。
  async function wbDrillInto(objectKey) {
    const row = wb.rows.find((r) => r.object_key === objectKey);
    if (!row) return;
    // 审计 #38：下钻是导航不是筛选——名称/状态/分桶若静默跟进子层，会把要找的行
    // 藏掉，空态还谎报「这个活动下没有广告组」。进子层即清筛选，面包屑独自承载语境。
    wb.filters = { name_contains: "", state: "", managed_only: "" };
    wb.perfBucket = "";
    $("wb-f-name").value = "";
    $("wb-f-state").value = "";
    $("wb-f-managed").value = "";
    const id = wbExternalId(objectKey);
    if (wb.level === "campaign") {
      wb.drillCampaign = { id, name: row.name || id };
      wb.drillAdGroup = null;
      wb.level = "ad_group";
    } else if (wb.level === "ad_group") {
      wb.drillAdGroup = { id, name: row.name || id };
      if (row.parent_campaign_id) {
        wb.drillCampaign = { id: row.parent_campaign_id, name: row.parent_campaign_name || row.parent_campaign_id };
      }
      // 广告组下有「广告」与「投放」两个并列子层：默认进投放（否定词/竞价的着力点），
      // 进去后可用页签切到广告看是哪些商品在跑——父过滤由页签处理器保留。
      wb.level = "target";
    } else {
      return; // 广告层与投放层都没有子层
    }
    wb.page = 1;
    syncLevelTabs();
    markPreviewStale();
    await refreshWorkbench();
  }

  async function wbDrillBack(target) {
    // 二轮审计：#38 的「导航不带筛选」只修了进入方向——子层里新设的筛选/分桶
    // 若静默跟回父层，刚被「下钻会清筛选」训练过的人最不会怀疑它，容易把
    // 「筛剩 3 个活动」误读成「店里只有 3 个活动」。两个方向对称清空。
    wb.filters = { name_contains: "", state: "", managed_only: "" };
    wb.perfBucket = "";
    $("wb-f-name").value = "";
    $("wb-f-state").value = "";
    $("wb-f-managed").value = "";
    if (target === "campaign" && wb.drillCampaign) {
      // 回到该活动的广告组列表
      wb.drillAdGroup = null;
      wb.level = "ad_group";
    } else {
      // 回到活动层平铺全量
      wb.drillCampaign = null;
      wb.drillAdGroup = null;
      wb.level = "campaign";
    }
    wb.page = 1;
    syncLevelTabs();
    markPreviewStale();
    await refreshWorkbench();
  }

  function wbManagedBadge(row) {
    const wrap = el("span", { class: "wb-badges" });
    if (row.ads_strategy) {
      const b = el("span", {
        class: "badge badge-managed",
        title: "领星策略托管中（ads_strategy=" + row.ads_strategy + "）——本系统不与其抢方向盘，勾选已禁用",
      });
      b.append(lockIcon(), el("span", { text: row.ads_strategy }));
      wrap.append(b);
    }
    if (row.is_apply_time) {
      wrap.append(el("span", { class: "badge badge-time", text: "分时", title: "is_apply_time=true：分时策略生效中" }));
    }
    if (!wrap.children.length) wrap.append(el("span", { class: "set-meta", text: "—" }));
    return wrap;
  }

  // 领星用 99999999 表示"有花费但零销售"的无穷大 ACOS（2026-08-29 实测：某店铺
  // 100 个在投活动里 30 个是这个值，且全部订单为 0）。原样显示会让人以为是脏数据，
  // 而这恰恰是最该被看见的一类广告——花了钱一单没出。
  const ACOS_INFINITE_SENTINEL = "99999999";

  function metricCell(metrics, key) {
    const raw = metrics && metrics[key] != null ? String(metrics[key]) : null;
    if (raw === null) return el("td", { class: "num", text: "—" });
    if (key === "acos" && raw === ACOS_INFINITE_SENTINEL) {
      return el(
        "td",
        { class: "num" },
        el("span", {
          class: "chip chip-warn",
          text: "无销售",
          title: "有花费、零销售额——ACOS 无穷大（领星以 99999999 表示）",
        }),
      );
    }
    return el("td", { class: "num", text: raw });
  }

  function parentCell(name, id) {
    if (!id) return el("td", null, el("span", { class: "set-meta", text: "—" }));
    if (name) {
      //: 与 #28 同一条理由，只是落在父对象列上：真实活动名不限宽时，投放层的 15 列
      //  会被这两列推过容器宽度，最右的「操作」列（那颗「历史」按钮）落在初始视野外。
      //  macOS 的覆盖式滚动条平时不显形，读出来就是「投放层没有历史按钮」。
      return el("td", null,
        el("div", { class: "wb-parent-name", text: name, title: name }),
        el("span", { class: "cell-sub mono", text: id }));
    }
    //: workbench-1（2026-08-29 排查结论）：名称缺失的真实原因几乎都是"父对象没进
    //  本轮同步范围"（同步被截断时子表先到、父表没拉全）——旧文案说"镜像没有名称
    //  记录"，把矛头指向了一个没坏的镜像。
    return el("td", null,
      el("span", { class: "mono", text: id }),
      el("span", {
        class: "cell-sub",
        text: "父对象未在已同步范围内",
        title: "这条广告的所属上级没有被同步进镜像——用「继续拉取」补全，或它属于未同步的部分",
      }));
  }

  function wbRow(row) {
    const managed = !!row.ads_strategy;
    const check = el("input", {
      type: "checkbox",
      class: "wb-check",
      "aria-label": "勾选 " + (row.name || row.object_key),
      dataset: { objectKey: row.object_key },
    });
    check.checked = wb.selected.has(row.object_key);
    const checkCell = el("td", { class: "wb-check-cell" }, check);
    if (managed) {
      check.disabled = true;
      check.title = "托管对象：由领星策略工具管理，此处不可勾选";
      // 键盘与触屏用户看不到 tooltip，所以给一个可见的锁。
      const lock = el("span", { class: "check-lock", title: check.title, "aria-hidden": "true" });
      lock.append(lockIcon());
      checkCell.append(lock);
    }

    //: 下钻即导航（领星 IA §1）：活动名/组名可点，点开该对象的下一层；投放层没有
    //  子层，名称是纯文本。徽标 [手动]/[自动] 与领星行内投放方式徽标同款（§2）。
    const canDrill = wb.level === "campaign" || wb.level === "ad_group";
    // 投放层 expression 原文是一串 repr（[{'type': 'asinSameAs'...]）——人读不了。
    // 展示层翻译成领星词汇，悬停仍是源侧原文；解析不出就照原文显示，不编。
    const friendly = wb.level === "target" && !row.keyword_text
      ? friendlyTargetName(row.name)
      : null;
    const displayName = friendly || row.name || "（无名称）";
    const nameLine = el("div", { class: "wb-name-line" });
    nameLine.append(canDrill
      ? el("button", {
          class: "wb-drill-link", type: "button", text: displayName,
          // 长名被 CSS 截断后全名只剩悬停一条路（审计 #28）——title 先放全名再放动作。
          title: displayName + "\n" +
            (wb.level === "campaign" ? "点击查看该活动下的广告组" : "点击查看该广告组下的投放"),
          dataset: { action: "wb-drill", objectKey: row.object_key },
        })
      : el("span", { text: displayName, title: friendly ? "源侧原文：" + row.name : displayName }));
    const tt = row.targeting_type ? String(row.targeting_type).toLowerCase() : null;
    if (tt) {
      nameLine.append(el("span", {
        class: "badge badge-targeting",
        text: TARGETING_TEXT[tt] || row.targeting_type,
        title: "投放方式 targeting_type=" + row.targeting_type +
          (TARGETING_TEXT[tt] === "自动" ? "（Amazon 自动圈词跑量）" : TARGETING_TEXT[tt] === "手动" ? "（人工圈词/圈品）" : ""),
      }));
    }
    const nameCell = el("td", null,
      nameLine,
      el("span", { class: "cell-sub mono", text: row.object_key }),
      row.keyword_text
        ? el("span", {
            class: "cell-sub",
            text: "词 " + row.keyword_text + (row.match_type
              ? " · " + (MATCH_TYPE_TEXT[row.match_type] ? MATCH_TYPE_TEXT[row.match_type] + "匹配" : row.match_type)
              : ""),
          })
        : null
    );

    const bidText = row.bid != null ? row.bid : row.default_bid;
    const bidCell = el("td", { class: "num" },
      el("span", { text: bidText != null ? String(bidText) : "—" }),
      row.bid == null && row.default_bid != null
        ? el("span", { class: "cell-sub", text: "组默认" })
        : null
    );

    const rel = fmtRelative(row.source_as_of);
    const timeCell = el("td", null, el("span", {
      class: "time-badge" + (rel.stale ? " is-stale" : ""),
      text: rel.text,
      title: "source_as_of=" + String(row.source_as_of) + (rel.stale ? "（超过 24 小时，注意时效）" : ""),
    }));

    const opCell = el("td", null, el("button", {
      class: "btn btn-sm", type: "button", text: "历史",
      dataset: { action: "wb-history", objectKey: row.object_key },
    }));

    const m = row.metrics || {};
    const cells = [checkCell, nameCell];
    // 广告层（商品）与投放层一样挂在广告组下，父链两级都要显示。
    if (wb.level !== "campaign") {
      cells.push(parentCell(row.parent_campaign_name, row.parent_campaign_id));
    }
    if (wb.level === "ad" || wb.level === "target") {
      cells.push(parentCell(row.parent_ad_group_name, row.parent_ad_group_id));
    }
    // 状态说人话（领星词汇 §5）：投放中/已暂停；title 保留源侧原文，词表外原样显示。
    cells.push(el("td", { text: stateZh(row.state) || "—", title: row.state || "" }));
    cells.push(el("td", null, wbManagedBadge(row)));
    if (wb.level === "campaign") {
      cells.push(el("td", { class: "num", text: row.daily_budget != null ? String(row.daily_budget) : "—" }));
    } else {
      cells.push(bidCell);
    }
    cells.push(
      metricCell(m, "impressions"), metricCell(m, "clicks"), metricCell(m, "spends"),
      metricCell(m, "sales"), metricCell(m, "orders"), metricCell(m, "acos"),
    );
    cells.push(timeCell, opCell);
    return el("tr", null, ...cells);
  }

  function sortableTh(label, field) {
    const active = wb.sortField === field;
    const th = el("th", {
      class: "th-sort" + (active ? " is-sorted" : ""),
      dataset: { sortField: field },
      role: "button",
      tabindex: "0",
      "aria-sort": active ? (wb.sortDir === "asc" ? "ascending" : "descending") : "none",
      title: "按「" + label + "」排序（服务端排序，作用于筛选后的全集而非当前页）",
    });
    th.append(el("span", { text: label }));
    th.append(el("span", { class: "sort-mark", text: active ? (wb.sortDir === "asc" ? " ▲" : " ▼") : " ↕" }));
    return th;
  }

  function wbHeaderRow() {
    const cells = [
      el("th", { class: "wb-check-cell" }, headCheckbox()),
      sortableTh("名称", "name"),
    ];
    if (wb.level !== "campaign") cells.push(el("th", { text: "所属活动" }));
    if (wb.level === "ad" || wb.level === "target") cells.push(el("th", { text: "所属广告组" }));
    cells.push(el("th", { text: "状态" }));
    cells.push(el("th", { text: "托管方" }));
    cells.push(el("th", { text: wb.level === "campaign" ? "日预算" : "竞价" }));
    // 漏斗序（领星同款）：曝光 → 点击 → 花费 → 订单 → ACOS。分桶快捷条满口
    // 「曝光」，表里却没这列，人没法核对——列与桶必须说同一套话。
    cells.push(sortableTh("曝光", "impressions"));
    cells.push(sortableTh("点击", "clicks"));
    cells.push(sortableTh("花费", "spend"));
    cells.push(sortableTh("广告销售额", "sales"));
    cells.push(sortableTh("订单", "orders"));
    cells.push(sortableTh("ACOS", "acos"));
    cells.push(el("th", { text: "数据截至" }));
    cells.push(el("th", { text: "操作" }));
    return el("tr", null, ...cells);
  }

  function headCheckbox() {
    const selectable = wb.rows.filter((r) => !r.ads_strategy);
    const box = el("input", {
      type: "checkbox",
      id: "wb-check-all",
      "aria-label": "全选本页可勾选对象",
    });
    if (selectable.length === 0) {
      box.disabled = true;
      box.title = "本页对象均由领星策略托管，不可勾选";
      return box;
    }
    const picked = selectable.filter((r) => wb.selected.has(r.object_key)).length;
    box.checked = picked === selectable.length;
    box.indeterminate = picked > 0 && picked < selectable.length;
    return box;
  }

  function renderWbTable() {
    const region = $("wb-table-region");
    region.textContent = "";
    region.setAttribute("aria-busy", wb.status === "loading" ? "true" : "false");

    if (wb.status === "loading") {
      region.append(el("div", { class: "empty-state", text: "加载中…" }));
      return;
    }
    if (wb.status === "error") {
      // ui-5（2026-08-29 排查结论）：失败态构造收拢到 loadErrorState，三个面板不再各写一份。
      region.append(loadErrorState(wb.error, "wb-retry"));
      return;
    }
    if (wb.mirrorEmpty) {
      region.append(el("div", { class: "empty-state", text: wbEmptyMirrorText() }));
      return;
    }
    if (wb.rows.length === 0) {
      const levelZh = LEVEL_TEXT_SHORT[wb.level] || wb.level;
      if (wbHasFilter()) {
        region.append(el("div", { class: "empty-state" },
          el("div", { text: "当前筛选条件下没有" + levelZh + "对象。" }),
          el("button", { class: "btn btn-sm", type: "button", text: "清除筛选", dataset: { action: "wb-clear-filter" } })
        ));
      } else if (wb.drillCampaign || wb.drillAdGroup) {
        // 下钻后为空 ≠ 镜像为空：可能这个父对象确实没有子层，也可能子层没被同步进来
        // （截断时父表先到）。说清两种可能，并给回退出口——「清除筛选」清不掉下钻。
        const parentZh = wb.drillAdGroup ? "广告组" : "活动";
        region.append(el("div", { class: "empty-state" },
          el("div", { text: "镜像里没有这个" + parentZh + "下的" + levelZh + "——它可能确实没有，也可能未被同步进本轮范围。" }),
          el("button", { class: "btn btn-sm", type: "button", text: "返回全部活动", dataset: { action: "wb-drill-to", target: "root" } })
        ));
      } else {
        region.append(el("div", { class: "empty-state", text: "该店铺镜像里没有" + levelZh + "对象。" }));
      }
      return;
    }
    const table = el("table", { class: "data" },
      el("thead", null, wbHeaderRow()),
      el("tbody", null, ...wb.rows.map(wbRow))
    );
    region.append(el("div", { class: "table-wrap" }, table));
  }

  //: 与本份内容逐字相同的其他集合（连同它们各自的状态）。服务端只给 id——
  //  状态得从同一份清单里查回来，因为「批一份就够」这句话只对**待批**的孪生成立。
  //: 孪生集合的状态决定重复提示该说什么。FROZEN 还要再分一次：过了 72 小时时效的
  //  那份**批不了**，把它算进「另有 N 份待批、批一份就够」是把一个不能选的选项摆到
  //  人面前——他切过去只会看到「已过期，只能拒绝后重新生成」。
  function dupTwins(s) {
    if (!Array.isArray(s.same_content_as) || !s.same_content_as.length) return [];
    const byId = new Map(state.sets.map((x) => [x.set_id, x]));
    return s.same_content_as.map((id) => {
      const twin = byId.get(id) || {};
      const state = twin.state || "FROZEN";
      return { set_id: id, state: state === "FROZEN" && twin.expired === true ? "EXPIRED" : state };
    });
  }

  //: 当前是否有筛选在生效。行数说的是「筛选后」还是「全部」，这个区别一旦不说，
  //  同一个数字会被读成两件事。
  function wbFilterActive() {
    const f = wb.filters;
    return !!(f.name_contains || f.state || f.managed_only || wb.perfBucket);
  }

  function renderWbChips() {
    const box = $("wb-chips");
    box.textContent = "";
    const f = wb.filters;
    const chips = [];
    if (f.name_contains) chips.push(["name", "名称含 " + f.name_contains]);
    if (f.state) chips.push(["state", "状态 " + stateZh(f.state)]);
    if (f.managed_only === "true") chips.push(["managed", "只看领星策略托管"]);
    if (f.managed_only === "false") chips.push(["managed", "只看未托管（可勾选）"]);
    if (wb.sortField) {
      const label = {
        name: "名称", spend: "花费", acos: "ACOS", orders: "订单", clicks: "点击",
        impressions: "曝光", sales: "广告销售额",
      }[wb.sortField];
      chips.push(["sort", "按" + label + (wb.sortDir === "asc" ? " 从低到高" : " 从高到低")]);
    }
    for (const [key, text] of chips) {
      box.append(el("span", { class: "chip" },
        el("span", { text }),
        el("button", {
          class: "chip-x", type: "button", "aria-label": "移除 " + text,
          text: "×", dataset: { action: "wb-chip-drop", chip: key },
        })
      ));
    }
    //: 按钮的显隐只看筛选，不看排序（2026-09-06 实测）：clearFilters 明确不动排序
    //  （「排序不是筛选」），而这里原来按 chips.length 判，于是「只排了序、没设筛选」
    //  时按钮会亮起来，点下去屏幕上零变化——一个明确写着「清除筛选」的按钮点了没反应，
    //  人只会以为页面卡了。排序想取消，用排序 chip 自己的 ×。
    $("wb-f-clear").hidden = !wbFilterActive();
  }

  function renderWbPager() {
    const pager = $("wb-pager");
    const pages = Math.max(1, Math.ceil(wb.total / wb.length));
    //: workbench-1（2026-08-29 排查结论）：total 是镜像（筛选后）的行数，不是店铺
    //  真有多少。同步被截断时两者会差一大截，「共 N 个对象」就成了假话——截断时
    //  必须把两个数分开说，且即使只有一页也要把这行亮出来。
    const cov = wb.levelCoverage;
    const covTruncated = !!(cov && cov.truncated);
    pager.hidden = wb.total <= wb.length && !covTruncated;
    let totalText = "共 " + wb.total + " 个对象";
    if (covTruncated) {
      if (wb.drillCampaign || wb.drillAdGroup) {
        // 审计 #39：下钻后照播全层缺口，读起来像「这个活动还有 11420 个广告组
        // 未同步」——父过滤范围内说不出精确缺口，只说清单可能不完整。
        totalText = "此父对象范围内 " + wb.total + " 行 · 全层同步未拉全，此清单可能不完整";
      } else if (cov.source_total != null) {
        //: 三个数出自三个口径，必须逐个说清是谁（2026-08-30 排查 #5）：
        //  wb.total 是**当前筛选下**镜像里的行数；source_total / rows_covered 是
        //  **本轮同步窗口**的全层进度，不受筛选影响，也不含上一轮窗口留下的行。
        //  此前一句话把「镜像中 N 行」和「还有 K 个未同步」并排摆出，N + K 与
        //  source_total 对不上；加个筛选条件更是让 N 变成 3，而 K 纹丝不动。
        const missing = Math.max(0, Number(cov.source_total) - (Number(cov.rows_covered) || 0));
        const scope = wbFilterActive() ? "当前筛选下镜像中 " : "镜像中 ";
        totalText = scope + wb.total + " 行 · 本轮同步这一层拉到 " +
          (Number(cov.rows_covered) || 0) + "/" + cov.source_total +
          "，还有 " + missing + " 个没拉";
      } else {
        totalText = "镜像中 " + wb.total + " 行 · 店铺此层总数未知（本轮同步被截断）";
      }
    }
    $("wb-page-info").textContent = "第 " + wb.page + " / " + pages + " 页 · " + totalText;
    $("wb-prev").disabled = wb.page <= 1 || wb.status !== "ok";
    $("wb-next").disabled = wb.page >= pages || wb.status !== "ok";
  }

  // ---------- 动作栏：层级门控 + 客户端预检 ----------
  function selectedLevels() {
    return new Set([...wb.selected.values()].map((s) => s.level));
  }

  function actionAllowed(action) {
    const allowed = WB_ACTION_LEVELS[action];
    if (allowed === null) return true;
    const levels = selectedLevels();
    if (levels.size === 0) return true;
    for (const lv of levels) if (!allowed.includes(lv)) return false;
    return true;
  }

  function renderWbActionOptions() {
    const sel = $("wb-action");
    const levels = selectedLevels();
    for (const opt of sel.options) {
      const ok = actionAllowed(opt.value);
      opt.disabled = !ok;
      const base = opt.dataset.baseLabel || opt.textContent;
      opt.dataset.baseLabel = base;
      if (ok) {
        opt.textContent = base;
      } else if (WB_ACTION_LEVELS[opt.value].includes("CAMPAIGN")) {
        opt.textContent = base + "（已选包含广告组/投放，该层级没有日预算）";
      } else {
        opt.textContent = base + "（已选包含活动，活动层没有竞价）";
      }
    }
    const note = $("wb-action-note");
    if (!actionAllowed(sel.value)) {
      sel.value = "PAUSE";
      wbSyncValueInput();
      note.textContent = "勾选变化后原动作不再适用，已改为「暂停」";
    } else if (levels.size > 1) {
      note.textContent = "已选跨 " + levels.size + " 个层级";
    } else {
      note.textContent = "";
    }
  }

  //: 返回第一条未满足的原因（一次只说一件事），全部满足返回 null。
  function previewBlockReason() {
    const n = wb.selected.size;
    if (n === 0) return "先勾选至少一个对象";
    if (n > wb.maxSelection) return "已选 " + n + " 个，超过单次上限 " + wb.maxSelection + " 个——请分批操作";
    const action = $("wb-action").value;
    if (!actionAllowed(action)) return "当前动作不适用于已选层级";
    const mode = WB_ACTION_INPUT[action];
    const raw = $("wb-value").value.trim();
    if (mode === "absolute") {
      if (!/^\d+(\.\d+)?$/.test(raw) || Number(raw) <= 0) return "数值必须是大于 0 的十进制数字（如 12.00）";
    } else if (mode === "percent") {
      if (!/^-?\d+$/.test(raw)) return "请填 -50 到 50 之间的整数";
      const p = parseInt(raw, 10);
      if (p === 0) return "0% 没有意义，请填非 0 值";
      if (Math.abs(p) > 50) return "单次幅度上限 ±50%，请拆成多次调整";
    }
    if (!$("wb-reason").value.trim()) {
      //: 不许再说「会进审计记录」（2026-09-06 核实）：理由只随预览响应回显，服务端
      //  一处都不落地，服务重启即无。人信了这句话就不会自己留底，而这条调整日后
      //  唯一能解释它的东西，恰恰只在他下载的那份 CSV 和他自己的记性里。
      return "请写清为什么这么改——它会写进你下载的变更清单 CSV，本系统不另外留存";
    }
    return null;
  }

  //: 末尾要重量栏高：预检红字与「本动作不需要数值」这类提示会让栏长高一行，
  //  而抽屉底边、提示条坐标全钉在 --wb-actionbar-h 上。此前只有 renderWbActionbar
  //  末尾量一次，切换「动作」下拉走的是 wbSyncValueInput → renderWbPrecheck 这条路，
  //  量不到——栏长高了，抽屉还按旧数字站着，最后一行被压住。
  function renderWbPrecheck() {
    const reason = previewBlockReason();
    const btn = $("wb-preview-btn");
    btn.disabled = reason !== null;
    btn.title = reason || "生成「现值 → 新值」预览";

    // 内联错误只挂在真正出问题的那个控件下，且不带任何错误码（码=服务端说的）。
    const mode = WB_ACTION_INPUT[$("wb-action").value];
    const valueErr = $("wb-value-error");
    const reasonErr = $("wb-reason-error");
    const valueBad = reason && mode !== null && /数值|%|整数|幅度/.test(reason);
    const reasonBad = reason && /理由|为什么这么改/.test(reason);
    valueErr.hidden = !valueBad;
    valueErr.textContent = valueBad ? reason : "";
    reasonErr.hidden = !reasonBad;
    reasonErr.textContent = reasonBad ? reason : "";
    $("wb-value").classList.toggle("is-bad", !!valueBad);
    $("wb-reason").classList.toggle("is-bad", !!reasonBad);
    syncActionbarHeight();
  }

  //: 固定动作栏盖住文档底部，正文与页脚要按它的高度避让。这个高度**必须实测**：
  //  CSS 里原来写死 150px，是 2026-08 按当时约 130px 的栏高标定的；栏后来长高了，
  //  2026-09-06 实测桌面 1015px 宽下 190px、窄屏 375px 下 405px——页脚分别被盖住
  //  40px 与 255px，而被盖住的正是那段通道声明（真实通道部署时，那是屏上唯一写着
  //  「同步镜像会调用领星生产 API」的文字；徽章只有短标签，解释在 title 里，
  //  触屏没有悬停触发不了）。再猜一个数字只会再过期一次。
  //  ResizeObserver 缺席时不动，CSS 的兜底值继续生效（退化成旧行为，不报错）。
  //: 栏高会变的原因只有两个：内容变（勾选数、动作类型、预检提示）和视口宽度变
  //  （栏 flex-wrap 换行）。前者由 renderWbActionbar 末尾显式调用覆盖，后者由
  //  window 的 resize 事件覆盖。
  //: 不用 ResizeObserver：2026-09-06 在本项目的预览浏览器里实测，它连规范保证的
  //  首次回调都不触发，挂上去等于什么都没做——而"什么都没做"在这里表现为页脚被
  //  盖住，没有任何报错。宁可用一个能当场验证的旧办法。
  function syncActionbarHeight() {
    const bar = $("wb-actionbar");
    if (!bar || bar.hidden) return;
    const h = Math.ceil(bar.getBoundingClientRect().height);
    if (h > 0) document.documentElement.style.setProperty("--wb-actionbar-h", h + "px");
    syncDrawerFit();
  }
  window.addEventListener("resize", syncActionbarHeight);

  //: 抽屉最矮到什么程度就没法用了。低于它就不该再挤，改成整屏浮层。
  const WB_DRAWER_MIN_H = 180;

  //: 抽屉要不要变成整屏浮层，取决于**竖向还剩多少地方**——此前 CSS 拿
  //  `@media (max-width: 768px)` 当这件事的替身，而宽度两个方向都判错（2026-09-07 实测）：
  //  · 844×390 的横屏手机宽度过线，走窄屏之外的分支，抽屉 top 落在 -54px，
  //    唯一那颗「关闭」整个在视口外（top -44 / bottom -17）——人只能按 Esc，
  //    而屏上没有一个字说得上还有这条路。
  //  · 768×1024 的 iPad 竖屏宽度压线，被整页盖住 86% 视高，动作栏里两个必填框
  //    （数值、理由）被抽屉压住点不到——可这块屏竖向还剩六百多像素。
  //  余量本来就是实测出来的（--wb-actionbar-h / --topbar-h），直接拿它判，
  //  不再用一个必然有一档判错的代理量。
  function syncDrawerFit() {
    const cs = getComputedStyle(document.documentElement);
    const px = (name, fallback) => {
      const v = parseFloat(cs.getPropertyValue(name));
      return Number.isFinite(v) ? v : fallback;
    };
    const room =
      document.documentElement.clientHeight - px("--wb-actionbar-h", 190) - px("--topbar-h", 116) - 96;
    document.body.classList.toggle("wb-drawer-overlay", room < WB_DRAWER_MIN_H);
  }
  window.addEventListener("resize", syncDrawerFit);

  //: 顶栏是 sticky 的，滚动目标必须给它让位，否则「滚过去让你看」等于把要看的东西
  //  推到栏底下（2026-09-06 实测：四个滚动目标的 scroll-margin-top 全是 0px）。
  //  高度实测而不是写死：栏里那行身份切换在窄屏会换行，写死的数字必然有一档是错的。
  //  与 syncActionbarHeight 同一套办法（同样不用 ResizeObserver，理由见上）。
  function syncTopbarHeight() {
    const bar = document.querySelector(".topbar");
    if (!bar) return;
    const h = Math.ceil(bar.getBoundingClientRect().height);
    if (h > 0) document.documentElement.style.setProperty("--topbar-h", h + "px");
    syncDrawerFit();
  }
  window.addEventListener("resize", syncTopbarHeight);

  function renderWbActionbar() {
    const bar = $("wb-actionbar");
    const n = wb.selected.size;
    bar.hidden = n === 0;
    document.body.classList.toggle("wb-has-selection", n > 0);
    const count = $("wb-selected-count");
    count.textContent = "已选 " + n + " / " + wb.maxSelection + " 个对象";
    const near = n >= wb.maxSelection * 0.9;
    count.classList.toggle("is-warn", near);
    if (near) count.textContent += n > wb.maxSelection ? "（已超上限）" : "（接近上限）";
    $("wb-to-mandate").disabled = n === 0;
    renderWbActionOptions();
    renderWbPrecheck();
    renderWbDrawer();
    //: 勾选清零 → 动作栏收起 → 抽屉的开关按钮跟着消失。留一个开着的空抽屉，
    //  人既回不到开它的按钮，键盘焦点也无处可去。清零的路不止一条（抽屉里的
    //  「清空全部」、动作栏的「清空勾选」、抽屉里逐个「×」或「移除本组」），
    //  所以这条规矩钉在勾选数变化的必经之路上，而不是钉在某一个按钮的处理器里。
    if (n === 0 && !$("wb-drawer").hidden) wbToggleDrawer(false);
    // 门控文案会引用勾选数（「勾了 N 个还没带入」），勾选一变就得重算，
    // 否则人勾完抬头看，签发区还在说「还没有勾选任何对象」。
    renderIssueGate();
    // 最后量：上面几个 render 刚往栏里填完内容，栏的高度到这一刻才定下来。
    syncActionbarHeight();
  }

  //: 抽屉是浮层，键盘上必须进得去、也出得来：打开后焦点若仍留在计数按钮上，
  //  Tab 会先穿过它下面整张表；没有 Escape 监听器就只能一路 Tab 到「关闭」。
  //  窄屏上这尤其要命——抽屉正压在动作栏那两个必填框上。
  function wbToggleDrawer(open) {
    const drawer = $("wb-drawer");
    //: 关掉它会不会把人扔下，取决于焦点当时在哪。三种情况可以接管：焦点在抽屉里
    //  （马上要被删掉）、焦点无处可去（<body>）、或根本没有焦点。焦点若在别的
    //  真实控件上（抽屉是被别处顺手关掉的，人正看着那儿），抢过来比丢掉更糟。
    //: 「焦点在 <body>」这一格必须算进来（2026-09-07 实测）：全局委派点击在分发前
    //  会把被点的按钮 disabled 掉（防双击重复提交），而禁用一个正被聚焦的元素，
    //  浏览器当场就把焦点丢回 <body>——于是抽屉里的「×」「移除本组」走到这里时，
    //  焦点早已不在抽屉里，只看 contains 会误判成「人在别处」，一路 return。
    const a = document.activeElement;
    const mayTakeFocus =
      !a || a === document.body || a === document.documentElement || drawer.contains(a);
    drawer.hidden = !open;
    renderWbDrawer();
    if (open) { $("wb-drawer-close").focus(); return; }
    if (!mayTakeFocus) return;
    //: 落点要逐个验过才用（2026-09-07 第二次排查）。上一版写的是「按钮不在屏上就
    //  退一步，交给一定在屏上的全选框」——而那个全选框既可能不存在（表格处在
    //  空态/加载中/失败态时 renderWbTable 根本不画表头），也可能被禁用（本页全是
    //  领星策略托管行时 headCheckbox 把它 disabled）。focus() 打在 null 或 disabled
    //  上都是彻底的空操作，焦点照样掉回 <body>，正是这条纪律要治的病。
    //  #wb-f-name 是 index.html 里的静态节点，筛选栏永远在，作为最后的落点。
    const target = [$("wb-selected-count"), $("wb-check-all"), $("wb-f-name")].find(
      (n) => n && !n.disabled && n.offsetParent !== null
    );
    if (target) target.focus();
  }

  function renderWbDrawer() {
    if ($("wb-drawer").hidden) return;
    const body = $("wb-drawer-body");
    body.textContent = "";
    $("wb-drawer-title").textContent = "已选对象 " + wb.selected.size + " / " + wb.maxSelection;
    const groups = { CAMPAIGN: [], AD_GROUP: [], AD: [], TARGET: [] };
    for (const [key, s] of wb.selected) (groups[s.level] || groups.TARGET).push([key, s]);
    for (const [level, rows] of Object.entries(groups)) {
      if (rows.length === 0) continue;
      body.append(el("div", { class: "drawer-group-head" },
        el("span", { text: (LEVEL_TEXT[level] || level) + "（" + rows.length + "）" }),
        el("button", {
          class: "btn btn-sm", type: "button", text: "移除本组",
          dataset: { action: "wb-drop-level", level },
        })
      ));
      for (const [key, s] of rows) {
        body.append(el("div", { class: "drawer-item" },
          el("span", { class: "drawer-item-name", text: friendlyTargetName(s.name) || s.name || "（无名称）", title: s.name || "" }),
          el("span", { class: "mono drawer-item-id", text: s.external_id }),
          el("button", {
            class: "chip-x", type: "button", text: "×", "aria-label": "移除 " + s.external_id,
            dataset: { action: "wb-drop-one", objectKey: key },
          })
        ));
      }
    }
    if (wb.selected.size === 0) {
      body.append(el("div", { class: "empty-state", text: "还没有勾选任何对象。" }));
    }
  }

  function wbSyncValueInput() {
    const mode = WB_ACTION_INPUT[$("wb-action").value];
    const input = $("wb-value");
    input.disabled = mode === null;
    if (mode === null) {
      input.value = "";
      input.placeholder = "无需数值";
      input.type = "text";
      $("wb-value-label").textContent = "数值（本动作不需要）";
    } else if (mode === "absolute") {
      input.type = "text";
      input.placeholder = "如 12.00";
      $("wb-value-label").textContent = "绝对值";
    } else {
      input.type = "number";
      input.min = "-50";
      input.max = "50";
      input.step = "1";
      input.placeholder = "-50 … 50";
      $("wb-value-label").textContent = "百分比 ±%";
    }
    markPreviewStale();
    renderWbPrecheck();
  }

  function wbToggleRow(objectKey, checked) {
    persistWbSelectionSoon();
    if (checked) {
      const row = wb.rows.find((r) => r.object_key === objectKey);
      wb.selected.set(objectKey, {
        level: wbLevelOfKey(objectKey),
        external_id: wbExternalId(objectKey),
        name: row && row.name ? row.name : objectKey,
      });
    } else {
      wb.selected.delete(objectKey);
    }
    markPreviewStale();
    renderWbActionbar();
    const head = $("wb-check-all");
    if (head) {
      const fresh = headCheckbox();
      head.checked = fresh.checked;
      head.indeterminate = fresh.indeterminate;
    }
  }

  function wbToggleAllOnPage(checked) {
    persistWbSelectionSoon();
    for (const r of wb.rows) {
      if (r.ads_strategy) continue;
      if (checked) {
        wb.selected.set(r.object_key, {
          level: wbLevelOfKey(r.object_key),
          external_id: wbExternalId(r.object_key),
          name: r.name || r.object_key,
        });
      } else {
        wb.selected.delete(r.object_key);
      }
    }
    markPreviewStale();
    renderWbTable();
    renderWbActionbar();
  }

  function wbClearSelection() {
    // 审计 #44：单击即毁几分钟攒出的勾选——超过 5 个时要一句确认。
    if (wb.selected.size > 5 &&
        !window.confirm("清空已勾选的 " + wb.selected.size + " 个对象？")) {
      return;
    }
    persistWbSelectionSoon();
    wb.selected.clear();
    markPreviewStale();
    //: 表格先重建、动作栏后（2026-09-07 实测）。反过来的话，动作栏那条「勾选清零就
    //  关掉抽屉」的规矩会先把焦点放到 #wb-check-all 上，紧接着 renderWbTable 把那个
    //  节点连同焦点一起删掉，焦点照样掉回 <body>——正是它要治的病。顺带：栏高在
    //  表格重建之后量才是准的。三个「清掉一部分勾选」的入口同此。
    renderWbTable();
    renderWbActionbar();
  }

  // ---------- 工作台 → 授权书作用域 ----------
  function wbToMandate() {
    const values = [...wb.selected.values()];
    if (values.length === 0) return;
    // 服务端 OBJECTIVE_SCOPE_LEVELS 只放行 campaign/ad_group：投放与广告（商品）
    // 层都无法界定否定词的包含关系，签得出去也永远选不中候选。此处预告式拒绝。
    //: 说清「有几个、去哪儿一键去掉」，而不是只说「请只勾选活动或广告组」
    //  （2026-09-07 实测）：勾了 3 个跨 2 层时，人得自己回表格里逐个找出是哪几个，
    //  而「已选清单」抽屉里每一层都摆着一颗「移除本组」——路一直在，没人指。
    const bad = values.filter((s) => s.level === "TARGET" || s.level === "AD");
    if (bad.length > 0) {
      showAlert("error",
        "已选里有 " + bad.length + " 个投放/广告（商品）层对象，它们不能作为" +
        "「清除浪费花费」授权的作用域——否定词按广告组落位。" +
        "点动作栏左端的已选计数打开清单，在那一层点「移除本组」即可。");
      return;
    }
    // 拷贝快照：带入之后工作台再改勾选不影响这份草案。
    // profile 记录对象实际所属店铺（ui-3/mandate-2）：签发时随项上传，供服务端校验。
    mandateDraft.scopeItems = values.map((s) => ({
      level: s.level.toLowerCase(),
      external_id: s.external_id,
      name: s.name,
      profile: wb.profile,
    }));
    mandateDraft.scopeProfile = wb.profile;

    $("issue-wrap").open = true;
    setFormProfile(wb.profile);
    const radio = document.querySelector("input[name=scope_kind][value=OBJECTS]");
    if (radio) radio.checked = true;
    // 带入后名称清单直接摊开（领星 IA §4：授权要落到看得见名字的对象上）——
    // 人签字前应看见自己圈了谁，而不是一个折叠的计数。
    $("scope-list-wrap").open = true;
    renderScope();

    const a = mandateDraft.scopeItems.filter((s) => s.level === "campaign").length;
    const b = mandateDraft.scopeItems.filter((s) => s.level === "ad_group").length;
    showAlert("info", "已带入 " + mandateDraft.scopeItems.length + " 个对象作为授权作用域（" +
      a + " 个活动 · " + b + " 个广告组）");
    $("issue-wrap").scrollIntoView({ behavior: "smooth", block: "start" });
    const firstPreset = $("preset-cards").querySelector("input[name=preset]");
    if (firstPreset) firstPreset.focus();
  }

  // ---------- 预览 ----------
  function markPreviewStale() {
    if (!$("wb-preview-region").firstChild) return;
    wb.previewStale = true;
    const panel = $("wb-preview-region").querySelector(".wb-result-panel");
    if (!panel || panel.querySelector(".wb-stale-banner")) return;
    panel.classList.add("is-stale-panel");
    panel.prepend(el("div", { class: "wb-stale-banner", text: "已过期：勾选或参数已变更，本预览不再对应当前设置。" }));
    //: 「已过期」必须连下载一起过期。这份 CSV 是整条链上唯一的产出，人拿着它去
    //  领星后台**手工执行**，而回执还写着「本系统不留存这批调整，请以这份文件为准」
    //  ——改完勾选或数值之后下出来的却是改动之前的旧数值。一句宣称权威的回执配一份
    //  过时的清单，比不给产出更坏。与「过期集合批准即拒」同一条 fail-closed 纪律：
    //  按钮当场停用并说清怎么恢复，而不是留着让人下出一份会被真的执行的错文件。
    const csv = panel.querySelector('[data-action="wb-preview-csv"]');
    if (csv) {
      csv.disabled = true;
      csv.title = "预览已过期，下出来的会是改动之前的旧数值——请重新点「生成预览」再下载";
    }
  }

  async function wbPreview() {
    const action = $("wb-action").value;
    const mode = WB_ACTION_INPUT[action];
    const raw = $("wb-value").value.trim();
    const intent = { action, reason: $("wb-reason").value.trim() };
    if (mode === "absolute") intent.value = raw;
    else if (mode === "percent") intent.percent = parseInt(raw, 10);
    const items = [...wb.selected.values()].map((s) => ({ level: s.level, external_id: s.external_id }));
    const btn = $("wb-preview-btn");
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "生成中…";
    try {
      const res = await api("/api/workbench/preview", {
        method: "POST",
        body: { profile_id: wb.profile, items, intent },
      });
      wb.previewStale = false;
      renderWbPreview(res);
    } catch (err) {
      alertError(err);
    } finally {
      btn.textContent = label;
      renderWbPrecheck();
    }
  }

  //: 结果面板挂上去之后把焦点送进它——与 wbToggleDrawer 打开抽屉时同一条规矩。
  //  触发它的按钮在请求在途时被 disabled（防双击），浏览器当场把焦点丢回 <body>；
  //  面板挂在页面另一处，于是只用键盘的人生成完预览，要从文档最顶上 Tab 过来才够
  //  得到面板里的「下载变更清单 CSV」——而那正是这条路唯一的产出（2026-09-07 实测：
  //  点击前焦点在 #wb-preview-btn 上，面板渲染后 activeElement 变成 BODY）。
  //  落点取面板自己的「关闭」：它在 DOM 里排第一，往后 Tab 就走进面板内容。
  //: 面板关掉之后焦点回到触发它的按钮。选择器现查而不是存节点：表格会整体重建，
  //  存下来的那个节点早已离场，focus() 打在离场节点上是空操作，焦点照样掉回 <body>。
  //: 从清单里删掉一项之后，焦点落到**同一位置**的下一项（删的是末项就落在新的末项），
  //  删空了才退到清单外那个稳定控件。不这么做的话，被点的那颗按钮随清单一起重建消失，
  //  浏览器把焦点丢回 <body>——而人多半正要接着删第二项（2026-09-07 实测：签发表单里
  //  逐条核对作用域，每删一项就被扔回文档开头一次）。
  //  index 必须在改数据之前取，重建之后原节点已经不在了。
  //  回退目标要给一串而不是一个：删掉最后一项时，清单连同它旁边的「清空」一起
  //  隐藏（2026-09-07 实测），只给一个回退等于没给。
  function focusAfterRemoval(selector, index, ...fallbacks) {
    const items = [...document.querySelectorAll(selector)];
    const at = items.length ? items[Math.min(index, items.length - 1)] : null;
    for (const n of [at, ...fallbacks.map((f) => document.querySelector(f))]) {
      if (n && !n.disabled && n.offsetParent !== null) {
        n.focus();
        return;
      }
    }
  }

  function wbFocusTrigger(selector) {
    const node = document.querySelector(selector);
    if (node && !node.disabled && node.offsetParent !== null) node.focus();
  }

  function wbFocusPanel(panel) {
    const a = document.activeElement;
    if (a && a !== document.body && a !== document.documentElement && !panel.contains(a)) return;
    const close = panel.querySelector(".wb-result-head button");
    if (close && !close.disabled && close.offsetParent !== null) close.focus();
  }

  function renderWbPreview(payload) {
    const region = $("wb-preview-region");
    region.textContent = "";
    const panel = el("div", { class: "wb-result-panel" });
    panel.append(el("div", { class: "wb-result-head" },
      el("strong", { text: "调整预览" }),
      el("span", { class: "set-meta", text: "共 " + payload.affected_total + " 个对象 · 店铺 " + profileLabel(payload.profile_id), title: payload.profile_id }),
      el("button", { class: "btn btn-sm", type: "button", text: "关闭", dataset: { action: "wb-close-preview" } })
    ));
    const it = payload.intent || {};
    const intentText = ($("wb-action").selectedOptions[0] ? $("wb-action").selectedOptions[0].dataset.baseLabel || it.action : it.action) +
      (it.value != null ? " → " + it.value : "") +
      (it.percent != null ? " → " + (it.percent > 0 ? "+" : "") + it.percent + "%" : "") +
      " · 理由：" + (it.reason || "—");
    panel.append(el("div", { class: "set-meta", text: intentText }));

    let noop = 0;
    for (const p of payload.previews || []) {
      panel.append(el("div", { class: "wb-preview-level",
        title: "level=" + p.level + " · directive_id=" + p.directive_id,
        text: (LEVEL_TEXT[p.level] || p.level) + "层 · 指令 " + shortId(p.directive_id) }));
      const table = el("table", { class: "data" },
        el("thead", null, el("tr", null,
          el("th", { text: "对象" }),
          el("th", { text: "现值" }),
          el("th", { text: "" }),
          el("th", { text: "新值" }),
        )),
        el("tbody", null, ...(p.affected || []).map((row) => {
          const same = String(row.current_value) === String(row.new_value);
          if (same) noop += 1;
          return el("tr", { class: same ? "row-muted" : null },
            el("td", null,
              el("div", { text: friendlyTargetName(row.display_name) || row.display_name, title: row.display_name || "" }),
              el("span", { class: "cell-sub mono", text: row.object_key })),
            el("td", { class: "num", text: row.current_value }),
            el("td", { class: "wb-arrow", text: "→" }),
            el("td", { class: "num wb-new-value" },
              el("span", { text: row.new_value }),
              same ? el("span", { class: "chip", text: "无变化" }) : null),
          );
        }))
      );
      panel.append(el("div", { class: "table-wrap" }, table));
    }
    if (noop > 0) {
      panel.append(el("div", { class: "wb-noop-note", text:
        "其中 " + noop + " 个对象现值与新值相同（例如已经是 paused），执行不会改变它们。" }));
    }
    if (payload.approval_required) {
      panel.append(el("div", { class: "wb-approval-note" },
        el("div", { text: "预览已生成——本系统不会执行任何修改。" }),
        el("div", { text: "要落地这批调整：下载变更清单，拿去领星后台手工执行。" }),
        el("button", {
          class: "btn btn-sm", type: "button", text: "下载变更清单 CSV",
          dataset: { action: "wb-preview-csv" },
          title: "现值→新值清单（含理由），CSV 可直接发给执行的人",
        })
      ));
      wb.lastPreview = payload;   // 供下载按钮读取（审计 #1：预览不再是零产出）
    }
    region.append(panel);
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    wbFocusPanel(panel);
  }

  //: 审计 #1：预览此前不可保存不可导出——人跨页勾选、写理由、点预览，走完全程
  //  零产出。CSV（UTF-8 BOM，Excel 直开）让「现值→新值」清单至少能带去领星执行。
  function wbDownloadPreviewCsv() {
    const payload = wb.lastPreview;
    if (!payload) return;
    // 停用之外再挡一道：按钮是委派点击派发的，将来多一条入口就会绕过那个 disabled。
    if (wb.previewStale) {
      showAlert("warn", "这份预览已过期（勾选或参数变过了），下出来的会是改动之前的" +
        "旧数值——请重新点「生成预览」再下载。");
      return;
    }
    const esc = (v) => {
      const t = String(v == null ? "" : v);
      return /[",\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
    };
    const it = payload.intent || {};
    const lines = ["﻿对象层级,对象名称,object_key,现值,新值,动作,理由"];
    for (const p of payload.previews || []) {
      for (const row of p.affected || []) {
        lines.push([
          LEVEL_TEXT[p.level] || p.level,
          esc(friendlyTargetName(row.display_name) || row.display_name),
          row.object_key,
          esc(row.current_value),
          esc(row.new_value),
          esc(ACTION_TEXT[it.action] || it.action || ""),
          esc(it.reason || ""),
        ].join(","));
      }
    }
    const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    //: 文件名要能区分两次下载（2026-09-06 排查）：原来只有店铺 ID，同一店铺再下一次
    //  就同名——而服务端不留理由、不留这批调整的任何记录，下载文件夹就是它唯一的
    //  存档，同名等于把上一次的存档挤掉。否定词 CSV 那边（exportSetCsv）早就是
    //  「店铺-日期-短ID」，这里对齐。
    const safe = (t) => String(t).replace(/[\\/:*?"<>|\s]+/g, "_");
    const stamp = new Date().toISOString().slice(0, 16).replace(/[-:]/g, "").replace("T", "-");
    const filename = ["adjustment", safe(profileLabel(payload.profile_id)), stamp]
      .filter(Boolean).join("-") + ".csv";
    const a = el("a", { href: url, download: filename });
    document.body.append(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    //: 点完必须有回执。三条下载路径里只有这条是静默的，而它下的又是唯一一份留存，
    //  人分不清「下好了」还是「按钮没响应」，只会再点一次。
    showAlert("success", "变更清单已开始下载：" + filename +
      "——每行是一个对象要在领星后台手工改的一项（含理由）。本系统不留存这批调整，请以这份文件为准。");
  }

  //: 审计 #7：跨页攒出的勾选与写好的理由只活在 JS 变量里，一次误刷新全部蒸发。
  //  sessionStorage 按店铺兜底（同标签页刷新可恢复；仅是便利，失败静默）。
  let persistTimer = null;
  function persistWbSelectionSoon() {
    // 延迟到本轮状态变更落定后再存（调用点在 toggle 入口，直接存会存到旧状态）。
    if (persistTimer) clearTimeout(persistTimer);
    persistTimer = setTimeout(persistWbSelection, 50);
  }

  function persistWbSelection() {
    try {
      const key = "wb-selected:" + wb.profile;
      if (wb.selected.size === 0) sessionStorage.removeItem(key);
      else sessionStorage.setItem(key, JSON.stringify([...wb.selected.entries()]));
      sessionStorage.setItem("wb-reason-draft", $("wb-reason").value);
    } catch { /* 隐私模式等拿不到 storage：功能照常，只是少了恢复 */ }
  }

  function restoreWbSelection() {
    try {
      const raw = sessionStorage.getItem("wb-selected:" + wb.profile);
      if (raw) for (const [k, v] of JSON.parse(raw)) wb.selected.set(k, v);
      const reason = sessionStorage.getItem("wb-reason-draft");
      if (reason && !$("wb-reason").value) $("wb-reason").value = reason;
    } catch { /* 读不出当没存过 */ }
  }

  async function wbShowHistory(objectKey) {
    let payload;
    try {
      payload = await api("/api/workbench/history?" + new URLSearchParams({ object_key: objectKey }).toString());
    } catch (err) {
      alertError(err);
      return;
    }
    wb.historyFor = objectKey;
    const region = $("wb-history-region");
    region.textContent = "";
    const panel = el("div", { class: "wb-result-panel" });
    panel.append(el("div", { class: "wb-result-head" },
      el("strong", { text: "快照历史" }),
      el("span", { class: "set-meta mono", text: objectKey }),
      el("button", { class: "btn btn-sm", type: "button", text: "关闭", dataset: { action: "wb-close-history" } })
    ));
    const entries = payload.entries || [];
    if (entries.length === 0) {
      panel.append(el("div", { class: "empty-state", text: "该对象还没有快照历史。" }));
    } else {
      const table = el("table", { class: "data" },
        el("thead", null, el("tr", null,
          el("th", { text: "记录时刻" }),
          //: 指标窗口必须与指标同排出现（#6）：两条快照并排时，「花费 35.00 → 41.20」
          //  读起来就是「涨了」，而两轮同步的窗口可以不同——变化里混着「窗口换了」
          //  这一项，此前屏幕上只有一列时刻，看不出来。
          el("th", { text: "指标窗口" }),
          el("th", { text: "状态" }),
          el("th", { text: "日预算" }),
          el("th", { text: "竞价" }),
          el("th", { text: "组默认" }),
          el("th", { text: "曝光" }),
          el("th", { text: "点击" }),
          el("th", { text: "花费" }),
          el("th", { text: "广告销售额" }),
          el("th", { text: "订单" }),
          el("th", { text: "ACOS" }),
        )),
        el("tbody", null, ...entries.map((e) => {
          const m = e.metrics || {};
          return el("tr", null,
            el("td", { class: "num", text: fmtTime(e.recorded_at) }),
            el("td", {
              class: "num", text: e.report_date || "—",
              title: e.report_date
                ? "这一行的花费/订单/ACOS 是这一段的合计"
                : "这一行不是本进程同步来的，说不出它统计的是哪一段",
            }),
            el("td", { text: stateZh(e.state) || "—", title: e.state || "" }),
            el("td", { class: "num", text: e.daily_budget != null ? e.daily_budget : "—" }),
            el("td", { class: "num", text: e.bid != null ? e.bid : "—" }),
            el("td", { class: "num", text: e.default_bid != null ? e.default_bid : "—" }),
            metricCell(m, "impressions"), metricCell(m, "clicks"), metricCell(m, "spends"),
            metricCell(m, "sales"), metricCell(m, "orders"), metricCell(m, "acos"),
          );
        }))
      );
      panel.append(el("div", { class: "table-wrap" }, table));
    }
    region.append(panel);
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    wbFocusPanel(panel);
  }

  // ---------- 同步（阻塞 60–90 秒，只报"诚实的已用时"，不画假进度） ----------
  function renderSyncStrip() {
    const strip = $("wb-sync-strip");
    if (!wb.syncTimer) { strip.hidden = true; return; }
    const secs = Math.floor((Date.now() - wb.syncStart) / 1000);
    const mmss = Math.floor(secs / 60) + ":" + pad2(secs % 60);
    $("wb-sync").textContent = "同步中 · 已用 " + mmss;
    strip.hidden = false;
    strip.textContent = "正在同步 " + profileLabel(wb.profile) + " · 首次全量约 60–90 秒 · 离开页面不会取消服务端任务";
  }

  function stopSyncTimer() {
    if (wb.syncTimer) clearInterval(wb.syncTimer);
    wb.syncTimer = null;
    $("wb-sync-strip").hidden = true;
    // 二轮审计：自动连拉的轮与轮之间不解锁——每轮 finally 解锁会在下一轮加锁前
    // 留出一个 refreshWorkbench 时长的窗口（真实店约 39 个窗口），窗口内点「同步
    // 镜像」或换店会造出同店并发双同步、游标归属错店。统一由 wbSyncAll 收尾解锁。
    if (wb.autoSyncOn) return;
    $("wb-sync").textContent = "同步镜像";
    $("wb-profile").disabled = false;
    $("wb-profile").title = "";
    $("wb-profile-select").disabled = false;
    $("wb-profile-select").title = "";
    renderWbSyncButton();   // 恢复可用性时重算白名单门控，而不是无条件解禁
  }

  //: workbench-1（2026-08-29 排查结论）：单轮同步有页数上限，报表可能没拉全。完成
  //  播报必须按 coverage 如实说「拉了多少 / 一共多少」。TARGET 由两张报表构成，
  //  coverage 会出现两条——先按层级聚合再播报；source_total 为 null 的层无法计数，
  //  如实说「总数未知」，不把它算进缺口。
  function summarizeCoverage(coverage) {
    const byLevel = new Map();
    for (const c of coverage) {
      const key = String(c.level);
      const acc = byLevel.get(key) || { covered: 0, total: 0, totalKnown: true };
      acc.covered += Number(c.rows_covered) || 0;
      if (c.source_total == null) acc.totalKnown = false;
      else acc.total += Number(c.source_total) || 0;
      byLevel.set(key, acc);
    }
    const order = ["CAMPAIGN", "AD_GROUP", "AD", "TARGET"];
    const parts = [];
    let missing = 0;
    let hasUnknown = false;
    for (const lv of [...order, ...[...byLevel.keys()].filter((k) => !order.includes(k))]) {
      const acc = byLevel.get(lv);
      if (!acc) continue;
      const label = LEVEL_TEXT_SHORT[lv.toLowerCase()] || lv;
      if (acc.totalKnown) {
        parts.push(label + " " + acc.covered + "/" + acc.total);
        missing += Math.max(0, acc.total - acc.covered);
      } else {
        parts.push(label + " " + acc.covered + "/总数未知");
        hasUnknown = true;
      }
    }
    return { text: parts.join(" · "), missing, hasUnknown };
  }

  //: 续拉条（workbench-1）：截断后常驻在同步条区域，直到拉全或换店。游标由服务端
  //  下发、原样回传，只对当前店铺与本轮报表窗口有效——换店时在 profile change 里清掉。
  function renderWbContinuation() {
    const strip = $("wb-continuation");
    strip.textContent = "";
    // 自动连拉进行中：条不藏，改为提供「停止」出口（审计 #8）。
    if (wb.autoSyncOn) {
      strip.hidden = false;
      strip.append(
        el("span", { text: "自动连续拉取中（第 " + wb.autoSyncRound + " 轮）——每轮约 60–90 秒，拉全自动停止。" }),
        el("button", {
          class: "btn btn-sm", type: "button", text: "完成本轮后停止",
          dataset: { action: "wb-sync-auto-stop" },
        })
      );
      return;
    }
    if (!wb.continuation || wb.syncTimer) {
      strip.hidden = true;   // 单轮同步进行中藏起来，防止并发第二轮
      return;
    }
    strip.hidden = false;
    const btn = el("button", {
      class: "btn btn-sm", type: "button", text: "继续拉取一轮",
      dataset: { action: "wb-sync-continue" },
      title: "从上一轮的断点接着拉一轮（约 60–90 秒），已同步的部分不重拉",
    });
    // 审计 #8：11,723 个活动 ÷ 每轮约 300 行 ≈ 39 轮，人不该守着点几十次。
    const autoBtn = el("button", {
      class: "btn btn-sm btn-primary", type: "button", text: "自动拉取直到拉全",
      dataset: { action: "wb-sync-auto" },
      title: "自动循环从断点续拉直到全部拉完，可随时停止；期间请保持页面打开",
    });
    if (!isHuman()) {
      btn.disabled = true;
      autoBtn.disabled = true;
      btn.title = autoBtn.title = "同步仅限人类身份触发——服务端强制，此处仅提示";
    }
    strip.append(
      el("span", { text: wb.continuationSummary || "上一轮同步没拉全，可从断点继续。" }),
      btn, autoBtn
    );
  }

  const AUTO_SYNC_MAX_ROUNDS = 300;

  //: 审计 #8：自动连拉——循环回传游标直到拉全 / 人叫停 / 出错，把几十次人工
  //  点击变成一次决定。每轮走的仍是同一个 wbSync（覆盖率播报、错误处理不另写）。
  //  二轮审计：出错必须真的停——wbSync 吞错误的话，失败轮游标不动、循环条件
  //  全部保持成立，服务一断就是毫秒级 300 轮请求风暴外加刷屏红条。
  async function wbSyncAll() {
    if (wb.autoSyncOn) return;
    wb.autoSyncOn = true;
    wb.autoSyncStop = false;
    wb.autoSyncRound = 0;
    let failedAt = 0;
    let hitCap = false;
    try {
      do {
        wb.autoSyncRound += 1;
        renderWbContinuation();
        const ok = await wbSync(wb.continuation);
        if (!ok) { failedAt = wb.autoSyncRound; break; }
      } while (wb.continuation && !wb.autoSyncStop && wb.autoSyncRound < AUTO_SYNC_MAX_ROUNDS);
      // 必须在 finally 把 autoSyncStop 复位**之前**判——否则人主动叫停也会被
      // 播报成「撞了上限」，而这两种的下一步不同。
      hitCap = !failedAt && Boolean(wb.continuation) && !wb.autoSyncStop &&
        wb.autoSyncRound >= AUTO_SYNC_MAX_ROUNDS;
    } finally {
      wb.autoSyncOn = false;
      wb.autoSyncStop = false;
      stopSyncTimer();   // 此刻 autoSyncOn 已复位，执行完整解锁（轮间被刻意跳过）
      renderWbContinuation();
    }
    //: 撞上 300 轮硬上限时必须开口。此前这条路径什么都不说：循环条件一假就
    //  静静退出，界面只剩一条还写着「继续拉取一轮」的续拉条，与人主动叫停后的
    //  样子逐像素相同。人看到的是「自动拉取直到拉全」跑了很久然后停了，
    //  既不知道它是拉完了、被自己点停了，还是撞了上限——而这三种的下一步不同。
    if (hitCap) {
      showAlert("warn",
        "自动拉取跑满 " + AUTO_SYNC_MAX_ROUNDS + " 轮上限后停下了，这个店还没拉全。" +
        "断点已保留——可再点一次「自动拉取直到拉全」接着跑。");
    }
    if (failedAt) {
      //: 「断点已保留」不能无条件说（2026-08-30 排查 #7）：服务重启会把内存镜像与
      //  断点簿一起清空，此时服务端已经回了 SYNC_CURSOR_STALE——照着「稍后重试」
      //  点下去必然再次失败，而人会以为是网络抖动，反复重试。
      const stale = wb.lastSyncErrorCode === "SYNC_CURSOR_STALE";
      showAlert("warn", stale
        ? "自动拉取在第 " + failedAt + " 轮停下了：服务端已经没有这个店铺的同步断点" +
          "（服务重启过，内存镜像与断点一起清空了）。重试没有用——请点「同步镜像」重新完整拉取。"
        : "自动拉取在第 " + failedAt + " 轮出错后已停止（断点已保留）——" +
          "可稍后点「继续拉取一轮」重试，或再次「自动拉取直到拉全」。");
    }
  }

  // 返回本轮是否成功（自动连拉据此决定停不停）；同步已在途时直接拒绝重入。
  async function wbSync(cont) {
    if (wb.syncTimer) return false;
    $("wb-sync").disabled = true;
    // 换店会让完成提示指向错的店，所以同步期间锁住 profile；读操作（刷新/翻页/筛选）不锁。
    // 审计 #12：手输框和下拉是同一个入口的两张脸，锁必须两个都锁——只锁隐藏的
    // 那个等于没锁，换店会让进度条与断点游标指向错的店。
    $("wb-profile").disabled = true;
    $("wb-profile").title = "同步进行中，暂不可切换店铺";
    $("wb-profile-select").disabled = true;
    $("wb-profile-select").title = "同步进行中，暂不可切换店铺";
    wb.syncStart = Date.now();
    wb.syncTimer = setInterval(renderSyncStrip, 1000);
    renderSyncStrip();
    renderWbContinuation();
    let ok = false;
    //: 失败的**原因**要能传给调用方（自动连拉据它决定说什么）。此前只回一个布尔，
    //  于是断点已经不存在的那种失败，被自动连拉播报成「断点已保留，可稍后重试」。
    wb.lastSyncErrorCode = null;
    try {
      const body = { profile_id: wb.profile };
      // 断点续拉（workbench-1）：游标原样回传，内容不在前端拼装或解读。
      if (cont) body.continuation = cont;
      const rep = await api("/api/workbench/sync", { method: "POST", body });
      // 新一轮响应到达即更新/清除游标：null = 已拉全，续拉按钮随之消失。
      wb.continuation = rep.continuation || null;
      // 末句在成功当下就告知内存镜像重启即失——2026-08-29 排查结论 workbench-4：
      // 不说这句，人到重启后只能对着空镜像猜是哪里坏了。
      const tail = "；数据截至已更新（数据保存在服务进程内存中，服务重启后需重新同步）";
      if (Array.isArray(rep.coverage) && rep.coverage.length > 0) {
        const cov = summarizeCoverage(rep.coverage);
        if (rep.truncated) {
          // 截断不给绿条：绿条 =「全部拉全」，给错颜色等于替服务端撒谎（workbench-1）。
          wb.continuationSummary = "上一轮同步只拉到一部分：" + cov.text +
            (cov.missing > 0 ? "，还有 " + cov.missing + " 个未拉取" : "") +
            (cov.hasUnknown ? "（标「总数未知」的层无法计数）" : "");
          // 自动连拉的中间轮不弹条（几十轮会刷满屏）；进度由续拉条与覆盖率行承载。
          if (!wb.autoSyncOn) {
            showAlert("warn",
              "本轮只同步了部分对象：" + cov.text +
              (cov.missing > 0 ? "，还有 " + cov.missing + " 个未拉取" : "") +
              (cov.hasUnknown ? "（标「总数未知」的层无法计数）" : "") +
              (wb.continuation ? "——可在表格上方续拉一轮，或「自动拉取直到拉全」" : "") + tail);
          }
        } else {
          wb.continuationSummary = "";
          showAlert("success", "已同步全部：" + cov.text + tail);
        }
      } else {
        // 服务端未给 coverage（旧响应形状）：按行数播报，不对覆盖率做任何断言。
        const counts = rep.per_level_rows || {};
        showAlert("success",
          "同步完成：活动 " + (counts.CAMPAIGN || 0) +
          " · 广告组 " + (counts.AD_GROUP || 0) +
          " · 投放 " + (counts.TARGET || 0) +
          "，拉取 " + rep.pages_fetched + " 页，跳过汇总行 " + rep.skipped_summary_rows +
          " 条" + tail);
      }
      ok = true;
    } catch (err) {
      if (err && err.code) {
        wb.lastSyncErrorCode = err.code;
        alertError(err); // 服务端明确拒绝：带码
        if (err.code === "SYNC_CURSOR_STALE") {
          //: 这个码是**永久**的——服务端按纪元判定（workbench_api.py 的 mirror_epoch），
          //  重启换了新纪元，这个游标再也不会被接受。就地清掉，别等下面那次
          //  refreshWorkbench：2427 行那条「服务端说没断点就清本地」的分支带着
          //  `!wb.autoSyncOn` 守卫，而自动连拉正是在 autoSyncOn 为真时撞上这个码的，
          //  于是续拉条会带着重启前的覆盖率句子留到下一次刷新，继续请人点两个
          //  注定 409 的按钮。清在这里，两条路径说同一句真话。
          wb.continuation = null;
          wb.continuationSummary = "";
        }
      } else {
        // 连接中断 ≠ 同步失败——服务端可能仍在跑，不许说"已失败"。
        showAlert("error",
          "与本地服务的连接中断，服务端可能仍在同步。请稍后点「刷新」，对照「数据截至」判断是否已完成。");
      }
    } finally {
      // 二轮审计：游标的服务端快照可能已被本轮消费（成败皆然），代次 +1 让
      // 在途 /objects 响应里的旧游标作废，防止「已同步全部」后又被恢复回来。
      wb.syncSeq += 1;
      stopSyncTimer();
    }
    await refreshWorkbench();
    renderWbContinuation();
    return ok;
  }

  function renderWbSyncButton() {
    // 服务端强制 HUMAN-only（403 HUMAN_REQUIRED）；此处仅预告式隐藏。
    const btn = $("wb-sync");
    btn.hidden = !isHuman();
    if (wb.syncTimer) return;   // 同步进行中的禁用态由 wbSync/stopSyncTimer 管理
    // 已知项②（2026-08-29）：白名单已知且当前店铺不在名单内时预告式禁用；
    // 白名单接口失败（syncProfiles=null）时不禁用，fail-open 交服务端判定。
    const known = Array.isArray(wb.syncProfiles);
    const allowed = !known || wb.syncProfiles.includes(wb.profile);
    //: 服务端 POST /sync 的判定顺序是：先 LX_MCP_KEY（LX_KEY_ABSENT）、再 LX_MCP_URL、
    //  白名单排第三。纯 Mock 部署下白名单恒空，于是按钮禁用、理由写「不在同步授权
    //  名单里」——照着它去把这个店加进 ADS_CP_SYNC_PROFILES 是白做：没有 key/URL
    //  一样拒，何况 Mock 的 profile-A 在领星那边根本不存在。通道态现成就在手里。
    const noChannel = state.channelMode === "mock";
    btn.disabled = !allowed || noChannel;
    const blockedText = noChannel
      ? "这台服务没有配领星只读通道（缺 LX_MCP_KEY / LX_MCP_URL）——当前镜像是演示种子数据，没有可同步的东西"
      : "这个店铺不在服务端的同步授权名单里，无法同步";
    btn.title = btn.disabled ? blockedText : "同步该店铺镜像（首次全量约 60–90 秒）";
    const note = $("wb-sync-note");
    note.hidden = !btn.disabled;
    note.textContent = btn.disabled ? blockedText : "";
  }

  // ---------- SoD 预告式禁用 ----------
  function renderSodUi() {
    renderIssueGate();
    renderWbSyncButton();
    renderWbContinuation();   // 续拉按钮与同步按钮同属 HUMAN-only，切身份要一起重算
  }

  // ---------- 筛选栏 ----------
  let filterTimer = null;
  function applyFilters(immediate) {
    if (filterTimer) clearTimeout(filterTimer);
    const run = async () => {
      wb.filters.name_contains = $("wb-f-name").value.trim();
      // 审计 #20：界面教人「投放中/已暂停」，人输入中文却按源侧原文精确匹配得 0 行，
      // 空态还谎称店里没有——中文词在此反查回源侧原文再上送。
      let stateRaw = $("wb-f-state").value.trim();
      const zhState = Object.entries(STATE_TEXT).find(([, zh]) => zh === stateRaw);
      if (zhState) stateRaw = zhState[0];
      wb.filters.state = stateRaw;
      wb.filters.managed_only = $("wb-f-managed").value;
      wb.sortField = $("wb-f-sort").value;
      wb.sortDir = $("wb-f-dir").value;
      wb.page = 1;
      await refreshWorkbench();
    };
    // 二轮审计：immediate 分支必须把 run 的 Promise 交出去——发射后不管的话，
    // 调用方的 await 立刻空转返回，委派链的 finally 会在请求还在途时就解禁按钮，
    // 防双击对「清除筛选/×chip」两个分支形同虚设。
    if (immediate) return run();
    filterTimer = setTimeout(run, 300);
  }

  async function clearFilters() {
    $("wb-f-name").value = "";
    $("wb-f-state").value = "";
    $("wb-f-managed").value = "";
    // 绩效分桶也是筛选（wbHasFilter 算它）：空态点「清除筛选」必须一并回「全部」，
    // 否则桶留着、行还是空，按钮等于没按。排序不是筛选，不动它。
    wb.perfBucket = "";
    await applyFilters(true);
  }

  // ---------- 事件绑定 ----------
  function bindEvents() {
    $("identity-switch").addEventListener("click", async (ev) => {
      const btn = ev.target.closest(".identity-opt");
      if (!btn || btn.dataset.token === state.token) return;
      state.token = btn.dataset.token;
      renderIdentityMeta();
      renderSodUi();   // 切身份后立刻重算按钮可用性
      await refreshAll();
    });

    //: KPI 卡片是锚点链接（href 已在 index.html 里），这里只补「顺手切到对应页签」。
    //  点「已批集合 1」落在「待批」页签上，人还得自己想到再点一下页签。
    document.querySelector(".kpi-strip").addEventListener("click", (ev) => {
      const card = ev.target.closest("[data-kpi-tab]");
      if (!card) return;
      selectSetTab(card.dataset.kpiTab);
    });

    //: 本页不轮询。开着昨天的标签页回来的人看到的是昨晚的快照，而他会把「待批 0」
    //  读成「AI 昨晚没产出」。回到前台且距上次读取超过一分钟就重读一次——比轮询轻，
    //  也比让人自己想起点刷新可靠。一分钟的下限是防止切来切去时反复打服务端。
    document.addEventListener("visibilitychange", async () => {
      if (document.visibilityState !== "visible") return;
      if (state.loadedAt && Date.now() - state.loadedAt.getTime() < 60000) return;
      await refreshAll();
    });

    $("set-tabs").addEventListener("click", (ev) => {
      const btn = ev.target.closest(".tab");
      if (!btn) return;
      selectSetTab(btn.dataset.state);
    });

    // ---------- 签发表单 ----------
    $("issue-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const btn = $("issue-submit");
      btn.disabled = true;
      try { await submitIssueForm(ev.target); } finally { renderIssueGate(); }
    });

    //: ui-2（2026-08-29 排查结论）：越界值藏在折叠的「参数细则」里时，原生校验因为
    //  控件不可聚焦而静默吞掉提交——点击看起来毫无反应。在原生校验之前拦下：先把
    //  第一个越界控件所在的折叠区展开（此时可聚焦），再让浏览器气泡出现，并补一条红条。
    $("issue-submit").addEventListener("click", (ev) => {
      const form = $("issue-form");
      if (form.checkValidity()) return;   // 全部合法：交给正常提交流程
      ev.preventDefault();
      const bad = form.querySelector("input:invalid, select:invalid, textarea:invalid");
      for (let node = bad; node && node !== form; node = node.parentElement) {
        if (node.tagName === "DETAILS") node.open = true;
      }
      form.reportValidity();
      showAlert("error", "有参数超出允许范围——看输入框下的提示（已展开对应分区）");
    });

    $("f-objective").addEventListener("change", renderObjectiveReadiness);
    //: ui-3/mandate-2：店铺输入一变就重算与带入对象的一致性警告，不等提交挨拒。
    //: 币种也必须跟着重算（2026-09-06 实测）：此前手输店铺只重算警告，币种提示
    //  停在上一个店铺的结论——把店铺改成 profile-DE-9，下面仍逐字写着
    //  「已按该店铺站点带出：USD」。这句话是这个字段存在的全部理由，而它此刻在
    //  说一个从没查过的店铺的假话；人照它签下去，每次运行都被 CURRENCY_MISMATCH 拒。
    $("f-profile").addEventListener("input", () => {
      renderScopeProfileWarn();
      syncCurrencyToProfile($("f-profile").value, { ask: false });
    });
    //: 敲完才问服务端（change 在失焦/回车时触发），免得每个按键前缀发一次请求。
    $("f-profile").addEventListener("change", () => syncCurrencyToProfile($("f-profile").value));
    //: 下拉改的是隐藏 input 的值；脚本赋值不触发 input 事件，所以警告要在这里手动重算。
    $("f-profile-select").addEventListener("change", (ev) => setFormProfile(ev.target.value));

    $("preset-cards").addEventListener("change", (ev) => {
      const input = ev.target.closest("input[name=preset]");
      if (input) applyPreset(input.value);
    });
    //: 自定义卡上的「更新/删」按钮：阻止事件冒泡，免得点按钮顺带切换了卡片选中。
    $("preset-cards").addEventListener("click", (ev) => {
      const op = ev.target.closest("button.preset-op");
      if (!op) return;
      ev.preventDefault();
      ev.stopPropagation();
      if (op.dataset.presetUpdate) updateCustomPreset(op.dataset.presetUpdate);
      else if (op.dataset.presetDelete) deleteCustomPreset(op.dataset.presetDelete);
    });
    $("preset-save-new").addEventListener("click", saveNewPresetFromForm);
    $("preset-download").addEventListener("click", downloadCurrentPreset);
    $("preset-upload-btn").addEventListener("click", () => $("preset-upload").click());
    $("preset-upload").addEventListener("change", (ev) => {
      const file = ev.target.files && ev.target.files[0];
      if (file) importPresetFile(file);
      ev.target.value = "";
    });

    $("f-interval").addEventListener("change", () => {
      // 频次反向推导日运行上限：消除「每 12 小时 + 日上限 1 次」这种必然静默失效的组合。
      //: 但人手填过就不再顶掉（2026-09-06 排查）：runbook §① 正是叫人把这个数从 1 改成 2，
      //  他再动一下频次下拉，改动无声消失、表单上一个字都不提。改成尊重明示意图，
      //  两个方向的不相容都交给下面那条警告去说——说出来比替他改掉更有用。
      if (!$("f-runs-per-day").dataset.touched) {
        $("f-runs-per-day").value = String(derivedRunsPerDay(Number($("f-interval").value)));
      }
      renderIntervalHelp();
      renderPresetSummary();
      renderAdvBadge();
      renderRunsWarn();
    });
    $("f-valid-days").addEventListener("change", renderPresetSummary);
    $("f-currency").addEventListener("input", renderPresetSummary);
    for (const [, id] of ADV_FIELDS) {
      $(id).addEventListener("input", () => {
        renderPresetSummary();
        renderAdvBadge();
        renderRunsWarn();
      });
    }
    //: 这一个字段额外记「人动过手」——频次下拉据此不再顶掉它。
    $("f-runs-per-day").addEventListener("input", markRunsPerDayTouched);
    $("adv-reset").addEventListener("click", () => applyPreset(mandateDraft.preset));

    for (const r of document.querySelectorAll("input[name=scope_kind]")) {
      r.addEventListener("change", renderScope);
    }
    $("scope-clear").addEventListener("click", () => {
      const n = mandateDraft.scopeItems.length;
      // 审计 #44：这批对象是人翻页勾出来的，单击清空且无恢复——超过 5 个要确认。
      if (n > 5 && !window.confirm("清空带入的 " + n + " 个对象？")) return;
      mandateDraft.scopeItems = [];
      renderScope();
    });

    for (const r of document.querySelectorAll("input[name=run_window_kind]")) {
      r.addEventListener("change", renderWindow);
    }
    $("f-tz").addEventListener("input", renderWindow);
    $("f-start-hour").addEventListener("change", renderWindow);
    $("f-end-hour").addEventListener("change", renderWindow);

    // ---------- 全局委派点击 ----------
    document.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("button[data-action]");
      if (!btn || btn.disabled) return;
      const d = btn.dataset;
      // 审计 #21：请求在途时按钮不禁用，双击即重复提交（第二次多半撞 409/重复动作）。
      // 统一在途禁用；分发结束恢复（按钮若已被重渲染，恢复动作落在离场节点上，无害）。
      btn.disabled = true;
      try {
      if (d.action === "approve-set") await approveSet(d.setId, d.hash, d.count);
      else if (d.action === "reject-set") await rejectSet(d.setId);
      else if (d.action === "export-set") await exportSetCsv(d.setId);
      else if (d.action === "revoke-mandate") await revokeMandate(d.mandateId);
      else if (d.action === "clone-mandate") cloneMandate(d.mandateId);
      else if (d.action === "copy-hash") await copyHash(d.hash, btn);
      else if (d.action === "wb-history") await wbShowHistory(d.objectKey);
      else if (d.action === "wb-drill") await wbDrillInto(d.objectKey);
      else if (d.action === "wb-drill-to") await wbDrillBack(d.target);
      //: 关掉面板要把焦点还给打开它的那颗按钮（2026-09-07 实测：不还的话
      //  activeElement 变成 BODY，只用键盘的人每关一次面板就被扔回文档开头）。
      //  「打开时把焦点送进去」是上面 wbFocusPanel 做的，这里是它的另一半。
      else if (d.action === "wb-close-history") {
        $("wb-history-region").textContent = "";
        wbFocusTrigger(
          'button[data-action="wb-history"][data-object-key="' + wb.historyFor + '"]');
      } else if (d.action === "wb-close-preview") {
        $("wb-preview-region").textContent = "";
        wbFocusTrigger("#wb-preview-btn");
      }
      else if (d.action === "wb-preview-csv") wbDownloadPreviewCsv();
      else if (d.action === "wb-retry") await refreshWorkbench();
      // workbench-1：断点续拉——带上上一轮响应存下的游标再走一遍同步流程。
      else if (d.action === "wb-sync-continue") await wbSync(wb.continuation);
      else if (d.action === "wb-sync-auto") await wbSyncAll();
      else if (d.action === "wb-sync-auto-stop") {
        wb.autoSyncStop = true;
        btn.textContent = "将在本轮结束后停止…";
      }
      // ui-5（2026-08-29 排查结论）：面板级重试，只重刷对应面板。
      else if (d.action === "retry-mandates") await refreshMandates();
      //: 「刷新列表」是人等 AI 跑完时唯一被指向的动作。只刷集合的话，候选出现了、
      //  同屏授权书行却还写着「还没跑过」——同一件事两个说法，人只能整页 F5
      //  （连填到一半的签发表单一起丢掉）。面板自己的失败重试仍只刷自己。
      else if (d.action === "retry-sets") await refreshAll();
      else if (d.action === "goto-approved") selectSetTab("APPROVED");
      else if (d.action === "scope-use-selected") wbToMandate();
      else if (d.action === "wb-clear-filter") await clearFilters();
      else if (d.action === "scope-remove") {
        const sel = '#scope-list button[data-action="scope-remove"]';
        const at = [...document.querySelectorAll(sel)].indexOf(btn);
        mandateDraft.scopeItems = mandateDraft.scopeItems.filter(
          (s) => !(s.external_id === d.externalId && s.level === d.level));
        renderScope();
        //: 删空之后清单和「清空」一起隐藏，而屏上恰好摆着恢复动作
        //  （「把勾选的 N 个带进来」）——那就是此刻唯一该去的地方。
        focusAfterRemoval(
          sel, at, "#scope-clear", "#scope-error button", "input[name=scope_kind]:checked");
      } else if (d.action === "wb-drop-one") {
        const sel = '#wb-drawer button[data-action="wb-drop-one"]';
        const at = [...document.querySelectorAll(sel)].indexOf(btn);
        wb.selected.delete(d.objectKey);
        persistWbSelectionSoon();
        markPreviewStale();
        renderWbTable();          // 顺序见 wbClearSelection
        renderWbActionbar();
        //: 勾选清零时 renderWbActionbar 已经把抽屉关掉并安排好落点，此处的选择器
        //  与回退目标都不可见，守卫让它变成空操作，不会把那个落点抢走。
        focusAfterRemoval(sel, at, "#wb-drawer-close");
      } else if (d.action === "wb-drop-level") {
        // 二轮审计：#44 的「>5 需确认」漏了这个兄弟入口——「移除本组」与要确认的
        // 「清空全部」并排在同一抽屉里，一键销毁跨页攒出的整组勾选却不问一句。
        const inLevel = [...wb.selected].filter(([, s]) => s.level === d.level);
        if (inLevel.length > 5 &&
            !window.confirm("移除已勾选的 " + inLevel.length + " 个" +
              (LEVEL_TEXT[d.level] || d.level) + "？")) {
          return;
        }
        for (const [key] of inLevel) wb.selected.delete(key);
        persistWbSelectionSoon();
        markPreviewStale();
        renderWbTable();          // 顺序见 wbClearSelection
        renderWbActionbar();
        //: 这一条**不**落到下一组的「移除本组」上——那是把焦点放在一个回车就能
        //  再毁掉一整组的按钮上。退到「关闭」。
        wbFocusTrigger("#wb-drawer-close");
      } else if (d.action === "wb-chip-drop") {
        if (d.chip === "name") $("wb-f-name").value = "";
        else if (d.chip === "state") $("wb-f-state").value = "";
        else if (d.chip === "managed") $("wb-f-managed").value = "";
        else if (d.chip === "sort") $("wb-f-sort").value = "";
        await applyFilters(true);
      }
      } finally {
        btn.disabled = false;
      }
    });

    // ---------- 对象工作台事件 ----------
    // 下拉与手输框共用一套换店逻辑；两个控件的值保持互相同步。
    async function onProfileChange(next) {
      if (!next || next === wb.profile) return;
      // 二轮审计：同步/自动连拉期间控件已锁，此处是竞态兜底（如轮间空隙、
      // 浏览器自动填充触发 change）——换店会让进度播报与游标指向错的店，拒绝并复位。
      if (wb.syncTimer || wb.autoSyncOn) {
        $("wb-profile").value = wb.profile;
        const sel = $("wb-profile-select");
        if (!sel.hidden) sel.value = wb.profile;
        showAlert("warn", "同步进行中暂不能切换店铺——等本轮完成或点「完成本轮后停止」。");
        return;
      }
      wb.profile = next;
      $("wb-profile").value = next;
      const select = $("wb-profile-select");
      if (!select.hidden) select.value = next;
      wb.page = 1;
      wb.selected.clear(); // 一次勾选只覆盖一个 profile（服务端同样强制）
      restoreWbSelection(); // 新店铺自己的勾选兜底（按店隔离存储）
      // 下钻的父对象 ID 是店内 ID，换店后无意义——必须清；分桶是跨店同义的浏览
      // 偏好，与名称/状态筛选一样保留。
      wb.drillCampaign = null;
      wb.drillAdGroup = null;
      // 续拉游标只对当店当窗口有效（workbench-1）：换店必须清掉，否则会把
      // 上一家店的断点回传给服务端。
      wb.continuation = null;
      wb.continuationSummary = "";
      renderWbContinuation();
      renderWbSyncButton();   // 已知项②：新店铺是否在同步白名单内要立刻重算
      $("wb-preview-region").textContent = "";
      renderWbActionbar();
      await refreshWorkbench();
    }
    $("wb-profile").addEventListener("change", (ev) => onProfileChange(ev.target.value.trim()));
    $("wb-profile-select").addEventListener("change", (ev) => onProfileChange(ev.target.value));

    $("wb-level-tabs").addEventListener("click", async (ev) => {
      const btn = ev.target.closest(".tab");
      if (!btn) return;
      wb.level = btn.dataset.level;
      wb.page = 1;
      // 页签切换层级，面包屑保持你所在的位置（领星同语义：下钻进广告组后，
      // 「广告」与「投放」是这个组下的两个并列子层，切换不该把人踢回全量）。
      // 只清与目标层不相容的父过滤：回活动层清全部，回广告组层清组过滤。
      if (wb.level === "campaign") {
        wb.drillCampaign = null;
        wb.drillAdGroup = null;
      } else if (wb.level === "ad_group") {
        wb.drillAdGroup = null;
      }
      syncLevelTabs();
      markPreviewStale();
      await refreshWorkbench();
    });

    //: 绩效分桶快捷条（领星 IA §3）：点桶即服务端筛选；再点当前桶 = 回「全部」。
    $("wb-buckets").addEventListener("click", async (ev) => {
      const btn = ev.target.closest(".bucket-btn");
      if (!btn) return;
      const next = btn.dataset.bucket || "";
      wb.perfBucket = next === wb.perfBucket ? "" : next;
      wb.page = 1;
      await refreshWorkbench();
    });

    $("wb-table-region").addEventListener("change", (ev) => {
      if (ev.target.id === "wb-check-all") { wbToggleAllOnPage(ev.target.checked); return; }
      const box = ev.target.closest("input.wb-check");
      if (!box) return;
      wbToggleRow(box.dataset.objectKey, box.checked);
    });

    async function toggleSortBy(field) {
      if (wb.sortField !== field) {
        wb.sortField = field;
        wb.sortDir = field === "name" ? "asc" : "desc";
      } else if (wb.sortDir === (field === "name" ? "asc" : "desc")) {
        wb.sortDir = wb.sortDir === "asc" ? "desc" : "asc";
      } else {
        wb.sortField = "";   // 第三次点回默认序
      }
      $("wb-f-sort").value = wb.sortField;
      $("wb-f-dir").value = wb.sortDir;
      wb.page = 1;
      await refreshWorkbench();
    }
    $("wb-table-region").addEventListener("click", (ev) => {
      const th = ev.target.closest("th.th-sort");
      if (th) toggleSortBy(th.dataset.sortField);
    });
    // 审计 #50：role=button + tabindex 的表头必须吃回车/空格，否则读屏用户被骗。
    $("wb-table-region").addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter" && ev.key !== " ") return;
      const th = ev.target.closest("th.th-sort");
      if (!th) return;
      ev.preventDefault();
      toggleSortBy(th.dataset.sortField);
    });

    $("wb-prev").addEventListener("click", async () => {
      if (wb.page > 1) { wb.page -= 1; await refreshWorkbench(); }
    });
    $("wb-next").addEventListener("click", async () => {
      wb.page += 1;
      await refreshWorkbench();
    });
    $("wb-refresh").addEventListener("click", refreshWorkbench);
    // 显式传 null：从头同步；带游标的续拉走「继续拉取」按钮（wb-sync-continue）。
    $("wb-sync").addEventListener("click", () => wbSync(null));
    $("wb-action").addEventListener("change", wbSyncValueInput);
    $("wb-value").addEventListener("input", () => { markPreviewStale(); renderWbPrecheck(); });
    $("wb-reason").addEventListener("input", () => { markPreviewStale(); renderWbPrecheck(); persistWbSelectionSoon(); });
    $("wb-preview-btn").addEventListener("click", wbPreview);
    $("wb-clear").addEventListener("click", wbClearSelection);
    $("wb-to-mandate").addEventListener("click", wbToMandate);

    $("wb-f-name").addEventListener("input", () => applyFilters(false));
    $("wb-f-name").addEventListener("keydown", (ev) => { if (ev.key === "Enter") applyFilters(true); });
    $("wb-f-state").addEventListener("change", () => applyFilters(true));
    $("wb-f-managed").addEventListener("change", () => applyFilters(true));
    $("wb-f-sort").addEventListener("change", () => applyFilters(true));
    $("wb-f-dir").addEventListener("change", () => applyFilters(true));
    $("wb-f-clear").addEventListener("click", clearFilters);

    $("wb-selected-count").addEventListener("click", () => {
      wbToggleDrawer($("wb-drawer").hidden);
    });
    $("wb-drawer-close").addEventListener("click", () => wbToggleDrawer(false));
    document.addEventListener("keydown", (ev) => {
      if (ev.key !== "Escape" || $("wb-drawer").hidden) return;
      //: 抽屉是**非模态**浮层：没有遮罩、没有 aria-modal，页面其余部分照常可用，
      //  人开着它去填动作栏的「理由」是正常操作。监听器挂在 document 上却不问
      //  焦点归属，就会在他按 Escape 想清掉输入法候选词/撤销当前输入时，
      //  把抽屉关掉并把焦点从输入框抢走（2026-09-07 排查）。
      //  只在焦点确实在抽屉里、或压根没有焦点可言时才认这次 Escape。
      const a = document.activeElement;
      const mine = !a || a === document.body || $("wb-drawer").contains(a);
      if (!mine) return;
      wbToggleDrawer(false);
    });
    //: 清空之后抽屉由 renderWbActionbar 里那条中央规则关掉（清零的路不止这一条）。
    $("wb-drawer-clear").addEventListener("click", wbClearSelection);

    // 同步进行中离开页面的守卫（服务端任务不会被取消，但人应该知道）。
    // 二轮审计：自动连拉的轮间空隙 syncTimer 短暂为 null，只看它守卫会漏——
    // autoSyncOn 期间同样要拦。
    window.addEventListener("beforeunload", (ev) => {
      if (!wb.syncTimer && !wb.autoSyncOn) return;
      ev.preventDefault();
      ev.returnValue = "";
    });
  }

  // ---------- 启动 ----------
  async function init() {
    buildIntervalOptions();
    buildHourOptions();
    buildPresetCards();
    // 时区仅预填浏览器所在时区，**不是隐式解释**：字段始终显式上传、显式回显、可改。
    let guess = "UTC";
    try { guess = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch { guess = "UTC"; }
    $("f-tz").value = guess;
    applyPreset("STEADY");
    renderWindow();
    renderScope();

    bindEvents();
    // 通道状态先于身份加载：身份清单拉不到会提前 return，而徽章/页脚的三态
    // 判定与身份无关，不能被那次失败连带留在「未知」。
    await loadRuntimeConfig();
    try {
      await loadIdentities();
    } catch (err) {
      alertError(err);
      // 审计 #22：红条 15 秒后消失，留下一个三个短横、无说明无重试的死页。
      // 首屏失败要常驻说明 + 重试按钮，不靠一条会自己消失的提示。
      const main = document.querySelector("main");
      const box = el("div", { class: "error-state" },
        el("div", { text: "连不上本地服务，页面没有加载成功。" }),
        el("div", { class: "set-meta", text: "请确认服务已启动（本页数据全部来自 127.0.0.1 的本地服务）。" }),
        el("button", { class: "btn btn-sm", type: "button", text: "重试" })
      );
      box.querySelector("button").addEventListener("click", () => window.location.reload());
      main.prepend(box);
      return;
    }
    await refreshAll();
    await loadObjectives();
    wbSyncValueInput();
    await loadWbProfiles();
    restoreWbSelection();   // 审计 #7：误刷新后找回勾选与理由草稿
    await refreshWorkbench();
  }

  init();
})();
