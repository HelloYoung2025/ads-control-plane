"""镜像同步器（SyncEngine）——四报表 → AdObjectSnapshot，白名单 fail-closed。

设计依据 docs/evidence/lx-v3-feasibility-20260828.md：
- §1 四报表即唯一对象数据源；每页末尾含对象字段为 null 的汇总行，须按自身
  id 非空过滤（跳过并计数，不算错误）。
- §2 全量周期同步不可行（QPS=1）→ 白名单店铺先行；profile 不在白名单直接拒
  （SYNC_PROFILE_NOT_ALLOWED），绝不"顺手多拉"。
- §3 同名入参跨工具类型漂移：sync 只按语义传参（page/length 用 int、
  with_ring 传 int 0）；逐工具按 schemaVersion 钉扎的最终编码由 LxReadPort
  实现方负责，sync 不做工具级类型转换。

必填参数集合钉扎自 docs/evidence/lx-schema-<toolId>-20260828.json 的
data.inputSchema.required：
- ad_campaign_report:           report_date, profile_ids, page, length, sort_field, sort_type
- ad_campaign_group_report:     report_date, profile_ids, with_ring
- ad_campaign_targeting_report: report_date, profile_ids, page, length, sort_field,
                                sort_type, with_ring
- ad_campaign_keyword_report:   report_date, profile_ids, page, length, sort_field, sort_type
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol

from ads_control_plane.mirror.repository import SnapshotRepository
from ads_control_plane.mirror.snapshot import LEVEL_KEY_PREFIX, AdObjectSnapshot
from ads_control_plane.tasks.directive import ObjectLevel


class SyncError(Exception):
    """同步器显式拒绝——带 code，fail-closed。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LxReadPort(Protocol):
    """领星读端口协议：只读报表分页。实现方抛带 code 异常；汇总行过滤不在此层做。"""

    def fetch_page(self, tool_id: str, params: Mapping[str, object]) -> Mapping[str, object]:
        """返回 {"rows": list[dict], "total": int | None}。"""
        ...


TOOL_CAMPAIGN_REPORT = "ad_campaign_report"
TOOL_GROUP_REPORT = "ad_campaign_group_report"
TOOL_PRODUCT_REPORT = "ad_campaign_product_report"
TOOL_TARGETING_REPORT = "ad_campaign_targeting_report"
TOOL_KEYWORD_REPORT = "ad_campaign_keyword_report"

#: 同步顺序固定：campaign → group → product → targeting → keyword
#: （product 落 AD 层；keyword 与 targeting 同并入 TARGET 层）。
SYNC_TOOL_LEVELS: tuple[tuple[str, ObjectLevel], ...] = (
    (TOOL_CAMPAIGN_REPORT, ObjectLevel.CAMPAIGN),
    (TOOL_GROUP_REPORT, ObjectLevel.AD_GROUP),
    (TOOL_PRODUCT_REPORT, ObjectLevel.AD),
    (TOOL_TARGETING_REPORT, ObjectLevel.TARGET),
    (TOOL_KEYWORD_REPORT, ObjectLevel.TARGET),
)

#: 版本钉扎（docs/evidence/lx-schema-*-20260828.json 实测值）。
CATALOG_VERSION = "basic-open-online-20260825-v3"
SCHEMA_VERSIONS: Mapping[str, str] = {
    TOOL_CAMPAIGN_REPORT: "ad_campaign_report-v1",
    TOOL_GROUP_REPORT: "ad_campaign_group_report-v1",
    TOOL_PRODUCT_REPORT: "ad_campaign_product_report-v1",
    TOOL_TARGETING_REPORT: "ad_campaign_targeting_report-v1",
    TOOL_KEYWORD_REPORT: "ad_campaign_keyword_report-v1",
}

#: 窗口指标白名单：存在即原样字符串进 metrics，不做数值演绎。
METRIC_FIELDS: tuple[str, ...] = ("spends", "sales", "acos", "orders", "clicks", "impressions")


@dataclass(frozen=True, kw_only=True)
class ToolCoverage:
    """单个报表工具在本轮的拉取覆盖情况——用来如实回答「拉全了没有」。

    2026-08-29 排查确认：此前 run() 到达页数上限就直接跳出，上游已经给出的 total
    只用于提前退出、从不外传，于是界面把「按花费降序的前 300 行」当成整个店铺展示，
    分页条写「共 300 个对象」。任何基于这张表的「哪些广告最费钱 / 最该停」的判断，
    都建立在一个人不知道被裁掉的样本上。本结构存在的唯一目的就是让截断说得出口。
    """

    tool_id: str
    #: ObjectLevel.value；targeting 与 keyword 两个工具都落在 TARGET 层。
    level: str
    #: 上游报的总行数。上游没给（None）时无法判断是否拉全，truncated 也就无从断言。
    source_total: int | None
    #: 截至本轮结束、本窗口累计覆盖的**数据行**数（含之前轮次的实测值，不含汇总行）。
    #: 注意与 SyncRunReport.per_level_rows 的口径差异：那个是**本轮**入库行数，
    #: 跳过的表是 0；这个是窗口累计。两者不可相加。
    rows_covered: int
    #: 上游实际交给我们的行数（含汇总行与映射失败的坏行），跨轮累计。
    #: 与 rows_covered 的差就是我们自己跳过的行。两个数必须分开：拿它跟
    #: source_total 比才答得了「上游还欠不欠我们行」，而 rows_covered 答的是
    #: 「我们留下了多少」——用后者代替前者判断断供，会把「我们跳过了一行」
    #: 误判成「上游还没给完」，于是永远续拉下去（见 empty-page 分支）。
    rows_seen: int
    #: 本轮从第几页开始拉（继续拉取时 > 1）；本轮被跳过时等于上一轮的 next_page。
    start_page: int
    #: 还有剩余时的续拉入口；拉全或上游未给 total 时为 None。
    next_page: int | None
    #: True = 因页数上限停下，上游还有没拉的行。
    truncated: bool
    #: 本窗口这张表已拉全。**与 next_page is None 不等价**：上游没给 total 时
    #: next_page 也是 None，但那是"无从断言"，不是"拉完了"。续拉靠这个字段决定
    #: 跳过与否，混用会把"没拉完但不知道"当成"拉完了"而漏数据。
    complete: bool
    #: 本轮为这张表打了几页。0 = 本轮跳过（上一轮已拉全），让"跳过"成为可审计事实
    #: 而不是沉默——审计时能回答"这轮为什么只打了 3 次网关"。
    pages_fetched: int


@dataclass(frozen=True, kw_only=True)
class ToolCursor:
    """单张报表的续拉状态。续拉必须逐表携带它，而不是只记"还没拉完的那些"。"""

    next_page: int | None
    rows_covered: int
    rows_seen: int
    source_total: int | None
    complete: bool


@dataclass(frozen=True, kw_only=True)
class SyncRunReport:
    """一次同步运行的可审计汇总（不含任何业务数值，只有计数与版本）。"""

    run_id: str
    profile_id: str
    started_at: datetime
    finished_at: datetime
    #: 本轮使用的报表窗口（"YYYY-MM-DD - YYYY-MM-DD"）。续拉必须复用同一窗口，
    #: 否则同一张表里会混进两个不同时间窗的数字。人也有权知道眼前的数是哪几天的。
    report_date: str
    #: 各层成功入库快照数，键为 ObjectLevel.value（keyword 计入 TARGET）。
    per_level_rows: Mapping[str, int]
    #: 逐报表的覆盖情况，顺序同 SYNC_TOOL_LEVELS。
    coverage: tuple[ToolCoverage, ...]
    #: 被跳过的汇总行/无自身 id 行计数。
    skipped_summary_rows: int
    #: 金额字段 Decimal 解析失败计数（失败置 None，不抛）。
    decimal_parse_failures: int
    pages_fetched: int
    #: 逐工具版本记录：tool_id → catalogVersion / schemaVersion。
    catalog_versions: Mapping[str, str]
    schema_versions: Mapping[str, str]

    @property
    def truncated(self) -> bool:
        """任意一张报表没拉全 → 整轮结果是不完整的，界面必须说出来。"""
        return any(c.truncated for c in self.coverage)

    def next_pages(self) -> dict[str, int]:
        """续拉入口的**薄投影**：tool_id → 下一页，只含还要继续拉的表。

        保留它是为了兼容浏览器里还握着老游标的标签页（SyncContinuation 是 frozen +
        extra="forbid"，删字段会让那种 POST 撞 pydantic 422，前端拿不到任何可翻译的码）。
        新代码一律用 tool_cursors()：这个投影按定义丢掉了"已拉全"的表，而
        run(start_pages=...) 的 .get(tool_id, 1) 会把"丢掉"读成"从第 1 页开始"，
        于是每续拉一次就把已拉完的表整张重拉一遍。
        """
        return {c.tool_id: c.next_page for c in self.coverage if c.next_page is not None}

    def tool_cursors(self) -> dict[str, ToolCursor]:
        """续拉游标：**每张报表都出条目**，含已拉全的。

        与 next_pages() 的区别就是这个"每张都出"——已拉全的表带 complete=True，
        下一轮据此跳过，而不是因为不在字典里被当成"没开始过"。
        """
        return {
            c.tool_id: ToolCursor(
                next_page=c.next_page,
                rows_covered=c.rows_covered,
                rows_seen=c.rows_seen,
                source_total=c.source_total,
                complete=c.complete,
            )
            for c in self.coverage
        }


def _opt_str(value: object) -> str | None:
    """缺失容错的字符串提取：None/空白 → None；数值 id 转字符串原文。"""
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _parse_apply_time(value: object) -> bool | None:
    """is_apply_time 的 int/bool 兼容提取；其余类型 → None（不猜语义）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    return None


def _row_object_id(tool_id: str, row: Mapping[str, object]) -> str | None:
    """行的自身对象 id；None 即汇总行（对象字段为 null），由调用方跳过并计数。

    targeting 行的对象 id 优先 keyword_id、其次 target_id、兜底行内 key。
    """
    if tool_id == TOOL_CAMPAIGN_REPORT:
        return _opt_str(row.get("campaign_id"))
    if tool_id == TOOL_GROUP_REPORT:
        return _opt_str(row.get("ad_group_id"))
    if tool_id == TOOL_PRODUCT_REPORT:
        # 2026-08-29 实测：广告商品行的自身 id 是 ad_id；汇总行该字段为 null
        # （同行 campaign_id/asin/sku 亦全 null），照既有语义跳过并计数。
        return _opt_str(row.get("ad_id"))
    if tool_id == TOOL_TARGETING_REPORT:
        return (
            _opt_str(row.get("keyword_id"))
            or _opt_str(row.get("target_id"))
            or _opt_str(row.get("key"))
        )
    return _opt_str(row.get("keyword_id"))


def _row_name(tool_id: str, row: Mapping[str, object]) -> str | None:
    """展示名逐工具取。

    2026-08-28 真实环境实测修正：campaign/group 报表行内本对象名就叫 name
    （campaign_name/ad_group_name 是下层报表里指"所属对象"的字段），原映射
    在真实数据上得到全空名称。保留原字段作兜底以防形态差异。
    """
    if tool_id == TOOL_CAMPAIGN_REPORT:
        return _opt_str(row.get("name")) or _opt_str(row.get("campaign_name"))
    if tool_id == TOOL_GROUP_REPORT:
        return _opt_str(row.get("name")) or _opt_str(row.get("ad_group_name"))
    if tool_id == TOOL_PRODUCT_REPORT:
        # 广告商品的可读身份优先商品标题，其次 ERP 品名；都没有时退回 SKU / ASIN
        # ——运营认得 SKU 与 ASIN，认不得 ad_id，绝不用 id 冒充名称。
        return (
            _opt_str(row.get("title"))
            or _opt_str(row.get("product_name"))
            or _opt_str(row.get("sku"))
            or _opt_str(row.get("asin"))
        )
    if tool_id == TOOL_TARGETING_REPORT:
        return (
            _opt_str(row.get("expression"))
            or _opt_str(row.get("targeting_text"))
            or _opt_str(row.get("keyword_text"))
        )
    return _opt_str(row.get("keyword_text")) or _opt_str(row.get("name"))


#: 默认只同步在投放的对象。2026-08-29 实测：某店铺 1829 个活动里 1800+ 已归档，
#: 不筛状态时前 200 行几乎全是暂停/归档的老广告，工作台等于一本废弃通讯录。
#: 值按领星约定用下划线分隔；keyword 报表无 state 入参，故不传。
DEFAULT_SYNC_STATES = "enabled_paused"


def build_params(
    tool_id: str,
    profile_id: str,
    report_date: str,
    page: int,
    page_size: int,
    states: str | None = DEFAULT_SYNC_STATES,
) -> dict[str, object]:
    """按语义构造入参；类型漂移的最终编码由 LxReadPort 实现方逐工具钉扎。"""
    params: dict[str, object] = {
        "report_date": report_date,
        "page": page,
        "length": page_size,
    }
    # 2026-08-29 实测（网关 search）：product 报表的必填是 profile_id 单数且要
    # JSON number；传 profile_ids 数组会被判 code=102 参数不合法。最终类型编码在
    # 适配层逐工具钉扎，这里只决定"用哪个入参名"。
    if tool_id == TOOL_PRODUCT_REPORT:
        params["profile_id"] = profile_id
    else:
        params["profile_ids"] = [profile_id]
    if tool_id in (
        TOOL_CAMPAIGN_REPORT,
        TOOL_PRODUCT_REPORT,
        TOOL_TARGETING_REPORT,
        TOOL_KEYWORD_REPORT,
    ):
        params["sort_field"] = "spends"
        params["sort_type"] = "desc"
    if tool_id in (TOOL_GROUP_REPORT, TOOL_TARGETING_REPORT, TOOL_KEYWORD_REPORT):
        params["with_ring"] = 0
    # keyword 报表的入参集合里没有 state（实测 schema），传了会被网关判参数不合法。
    if states and tool_id in (
        TOOL_CAMPAIGN_REPORT,
        TOOL_GROUP_REPORT,
        TOOL_PRODUCT_REPORT,
        TOOL_TARGETING_REPORT,
    ):
        params["state"] = states
    return params


def _map_row(
    *,
    tool_id: str,
    level: ObjectLevel,
    row: Mapping[str, object],
    profile_id: str,
    now: datetime,
    report_date: str,
) -> tuple[AdObjectSnapshot | None, int]:
    """单行 → 快照。返回 (快照或 None=汇总行, 本行 Decimal 解析失败数)。"""
    object_id = _row_object_id(tool_id, row)
    if object_id is None:
        return None, 0

    decimal_failures = 0

    def to_decimal(value: object) -> Decimal | None:
        nonlocal decimal_failures
        if value is None:
            return None
        try:
            return Decimal(str(value).strip())
        except InvalidOperation:
            decimal_failures += 1
            return None

    daily_budget = to_decimal(row.get("budget")) if tool_id == TOOL_CAMPAIGN_REPORT else None
    default_bid = to_decimal(row.get("default_bid")) if tool_id != TOOL_CAMPAIGN_REPORT else None
    # 广告商品行携带 bid（其所在广告组/投放的竞价现值），与投放/关键词同字段名。
    bid = (
        to_decimal(row.get("bid"))
        if tool_id in (TOOL_PRODUCT_REPORT, TOOL_TARGETING_REPORT, TOOL_KEYWORD_REPORT)
        else None
    )

    metrics: dict[str, str] = {}
    for metric in METRIC_FIELDS:
        value = row.get(metric)
        if value is not None:
            metrics[metric] = str(value)

    prefix = LEVEL_KEY_PREFIX[level]
    parent_campaign_id = (
        _opt_str(row.get("campaign_id")) if tool_id != TOOL_CAMPAIGN_REPORT else None
    )
    parent_ad_group_id = (
        _opt_str(row.get("ad_group_id"))
        if tool_id in (TOOL_PRODUCT_REPORT, TOOL_TARGETING_REPORT, TOOL_KEYWORD_REPORT)
        else None
    )
    snapshot = AdObjectSnapshot(
        object_key=f"{prefix}{object_id}",
        level=level,
        profile_id=profile_id,
        sid=_opt_str(row.get("sid")),
        name=_row_name(tool_id, row),
        state=_opt_str(row.get("state")),
        daily_budget=daily_budget,
        default_bid=default_bid,
        bid=bid,
        keyword_text=_opt_str(row.get("keyword_text")),
        match_type=_opt_str(row.get("match_type")),
        targeting_type=_opt_str(row.get("targeting_type")),
        ads_strategy=_opt_str(row.get("ads_strategy")),
        is_apply_time=_parse_apply_time(row.get("is_apply_time")),
        parent_campaign_id=parent_campaign_id,
        parent_ad_group_id=parent_ad_group_id,
        metrics=metrics,
        # 指标是哪一段的，钉在行上。见 AdObjectSnapshot.report_date 的注释。
        report_date=report_date,
        source_as_of=now,
        recorded_at=now,
        catalog_version=CATALOG_VERSION,
        schema_version=SCHEMA_VERSIONS[tool_id],
    )
    return snapshot, decimal_failures


class SyncEngine:
    """把白名单店铺的四报表现值同步进快照仓库；只读、fail-closed、可审计。"""

    def __init__(
        self,
        read_port: LxReadPort,
        repo: SnapshotRepository,
        allowed_profiles: tuple[str, ...],
    ) -> None:
        if not allowed_profiles:
            raise SyncError(
                "SYNC_NO_ALLOWED_PROFILES",
                "sync engine requires an explicit non-empty profile whitelist (§2 fail-closed)",
            )
        self._read_port = read_port
        self._repo = repo
        self._allowed_profiles = allowed_profiles

    def run(
        self,
        profile_id: str,
        window_days: int = 7,
        page_size: int = 100,
        max_pages: int | None = None,
        states: str | None = DEFAULT_SYNC_STATES,
        start_pages: Mapping[str, int] | None = None,
        report_date: str | None = None,
        tool_cursors: Mapping[str, ToolCursor] | None = None,
    ) -> SyncRunReport:
        """同步单店铺一轮：五工具依次分页拉取、映射、追加入库。

        states 默认只取在投放的对象（见 DEFAULT_SYNC_STATES）；传 None 则不筛状态，
        会把归档的历史广告一并拉进镜像——仅在确需回溯历史对象时使用。

        tool_cursors / report_date 供「继续拉取」使用：前者逐表给出续拉状态，
        后者复用上一轮的窗口。窗口必须复用——否则续拉的行来自另一个时间窗，
        会和已入库的行并排显示却不可比。

        start_pages 是 tool_cursors 之前的老形态，仅在 tool_cursors 缺席时生效，
        且**保留了它原有的缺陷**：它只记"还没拉完的表"，拿不到条目的表会从第 1 页
        整张重拉。老游标解析得了但修不好——修好要靠调用方改传 tool_cursors。
        """
        if profile_id not in self._allowed_profiles:
            raise SyncError(
                "SYNC_PROFILE_NOT_ALLOWED",
                f"profile {profile_id!r} is not in the sync whitelist; refusing to fetch",
            )
        started_at = datetime.now(UTC)
        if report_date is None:
            report_date = (
                f"{(started_at - timedelta(days=window_days)).date().isoformat()}"
                f" - {started_at.date().isoformat()}"
            )
        per_level_rows: dict[str, int] = {level.value: 0 for level in ObjectLevel}
        skipped_summary_rows = 0
        decimal_parse_failures = 0
        pages_fetched = 0
        coverage: list[ToolCoverage] = []

        for tool_id, level in SYNC_TOOL_LEVELS:
            cursor = (tool_cursors or {}).get(tool_id)
            if cursor is not None and cursor.complete:
                # 上一轮已拉全：一页都不打。窗口是钉死的（续拉复用同一 report_date），
                # 同窗口同页重拉不会带来新对象，只会烧 QPS=1 的预算，并往 append-only
                # 历史里追加一条"什么都没变的变更"。要跟上 state/budget/bid 的变化，
                # 正确动作是开一轮新的完整同步（新窗口、新 run_id），而不是在旧断点链
                # 里偷偷刷新一部分表——那会造出"CAMPAIGN 层是 10 分钟前的、TARGET 层
                # 是 40 分钟前的"这种无法向人解释的混合时点。
                coverage.append(
                    ToolCoverage(
                        tool_id=tool_id,
                        level=level.value,
                        source_total=cursor.source_total,
                        rows_covered=cursor.rows_covered,
                        rows_seen=cursor.rows_seen,
                        start_page=cursor.next_page or 1,
                        next_page=None,
                        truncated=False,
                        complete=True,
                        pages_fetched=0,
                    )
                )
                continue
            if cursor is not None:
                start_page = max(1, cursor.next_page or 1)
                # 累计行数用上一轮的**实测值**，不再按 (start_page-1)*page_size 估算：
                # page_size 在两轮之间变一下，估算就直接算错。
                rows_covered = cursor.rows_covered
                rows_seen = cursor.rows_seen
            else:
                start_page = max(1, (start_pages or {}).get(tool_id, 1))
                rows_covered = (start_page - 1) * page_size
                rows_seen = rows_covered
            # 本轮开跑时的累计值。空页分支据它判「这一轮到底有没有拿到新行」——
            # 没拿到就别再给续拉入口，重试一次已经证明重试无用。
            rows_seen_at_start = rows_seen
            page = start_page
            source_total: int | None = None
            truncated = False
            next_page: int | None = None
            complete = False
            pages_this_tool = 0
            while True:
                params = build_params(tool_id, profile_id, report_date, page, page_size, states)
                result = self._read_port.fetch_page(tool_id, params)
                pages_fetched += 1
                pages_this_tool += 1
                rows_obj = result.get("rows")
                rows: list[object] = rows_obj if isinstance(rows_obj, list) else []
                total_obj = result.get("total")
                if isinstance(total_obj, int):
                    source_total = total_obj
                if not rows:
                    # 空页**不是**无条件的终止信号。上游刚说这张表有 source_total 行，
                    # 却在远没给够时返回零行，这是异常，不是「拉完了」。
                    # 无条件置 complete 的后果：truncated 留 False、next_page 留 None，
                    # continuation 被算成 None，「继续拉取」按钮当场消失，对象表的分页条
                    # 走非截断分支只剩一句「共 250 个对象」——11723 这个数字从此在界面上
                    # 不再出现。人拿到的是一条绿色成功条「已同步全部：活动 250/11723」，
                    # 绿条和「全部」说拉全了，紧挨着的分数说只有 250，没有一处告诉他信哪个。
                    # 此后所有「哪些广告最费钱、最该停」的判断，都建在一个他已被告知是
                    # 完整的、实际按花费降序截掉了绝大部分的样本上。
                    # 同一个函数下面几行把 source_total 当权威用来提前退出，这里就不能
                    # 放弃用它证伪空页——两处对同一个数字的信任度不该不一致，
                    # 更不该不一致到「宁可宣告拉全」那一边。
                    # 但判「断供」必须拿 rows_seen 比，不能拿 rows_covered 比。
                    # rows_covered 只数映射成功的行，source_total 数的是上游服务的
                    # 记录数——只要有一行映射不了（汇总行、缺字段、id 类型不对），
                    # 这个差就**永远**填不平：每轮拿到空页、每轮把 next_page 推一格，
                    # continuation 恒非 null，前端 `do{...}while(wb.continuation)`
                    # 只能靠 300 轮硬上限退出，每轮五张表各烧一次 QPS=1 的调用，
                    # 最后无声停下。而重问一次并变不出我们自己跳过的那些行。
                    # 还要求「这一轮确实拿到了新行」。上游的 total 在翻页之间会漂
                    # （2026-08-30 实测同窗口 1047→1079，因为窗口含当日）：漂上去
                    # 之后 rows_seen 永远追不上新 total，光靠上面那个比较又会把
                    # 每一轮都判成断供，空转回 300 轮。加上这一条，断供最多再探
                    # 一轮：那一轮拿不到新行就收口，拿得到就说明确实还在续上。
                    if (
                        source_total is not None
                        and rows_seen < source_total
                        and rows_seen > rows_seen_at_start
                    ):
                        truncated = True
                        next_page = page + 1
                        break
                    # 上游交付的行数已经够数（或没给 total）：这张表到底了，不再问。
                    # 此时 rows_covered 仍可能少于 source_total，那是我们自己跳过的行——
                    # 覆盖不足这句话照说（truncated），但不再把人指向一页不存在的下一页。
                    truncated = source_total is not None and rows_covered < source_total
                    complete = not truncated
                    break
                rows_seen += len(rows)
                data_rows_this_page = 0
                for row in rows:
                    if not isinstance(row, Mapping):
                        skipped_summary_rows += 1  # 无自身 id 可言，按汇总行语义计数
                        continue
                    snapshot, row_failures = _map_row(
                        tool_id=tool_id,
                        level=level,
                        row=row,
                        profile_id=profile_id,
                        now=started_at,
                        report_date=report_date,
                    )
                    decimal_parse_failures += row_failures
                    if snapshot is None:
                        skipped_summary_rows += 1
                        continue
                    self._repo.append(snapshot)
                    per_level_rows[level.value] += 1
                    data_rows_this_page += 1
                # 只累加数据行，不含汇总行。source_total 取自 recordsFiltered，若它不含
                # 汇总行，按 len(rows) 累加就每页多计 1（11,723 行按 100/页虚增约 118 行），
                # 于是 rows_covered >= source_total 提前成立、truncated 留 False——
                # 一次静默截断被报告成"已拉全"。宁可少算（多打一页拿到空页再停），绝不多算。
                rows_covered += data_rows_this_page
                if source_total is not None and rows_covered >= source_total:
                    complete = True
                    break
                # 页数上限按**本轮拉了几页**算，不按绝对页号——否则续拉时 start_page
                # 已经大于上限，一页都拉不动，「继续拉取」永远原地踏步。
                if max_pages is not None and pages_this_tool >= max_pages:
                    truncated = True
                    next_page = page + 1
                    break
                page += 1
            coverage.append(
                ToolCoverage(
                    tool_id=tool_id,
                    level=level.value,
                    source_total=source_total,
                    rows_covered=rows_covered,
                    rows_seen=rows_seen,
                    start_page=start_page,
                    next_page=next_page,
                    truncated=truncated,
                    complete=complete,
                    pages_fetched=pages_this_tool,
                )
            )

        return SyncRunReport(
            run_id=str(uuid.uuid4()),
            profile_id=profile_id,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            report_date=report_date,
            per_level_rows=per_level_rows,
            coverage=tuple(coverage),
            skipped_summary_rows=skipped_summary_rows,
            decimal_parse_failures=decimal_parse_failures,
            pages_fetched=pages_fetched,
            catalog_versions={tool_id: CATALOG_VERSION for tool_id, _ in SYNC_TOOL_LEVELS},
            schema_versions=dict(SCHEMA_VERSIONS),
        )
