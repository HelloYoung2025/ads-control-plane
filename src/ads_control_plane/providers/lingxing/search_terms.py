"""领星搜索词数据源：SearchTermReadPort 的真实实现（只读）。

依赖方向沿用 adapters/lx_read.py 开篇写明的约定——本模块在自己这里声明结构化
LxReadPort，不 import adapters，也不 import mirror；具体绑定由编排层决定。

口径裁决（2026-08-30 Owner 确认，实测依据见 docs/evidence/）：
- 转化取 orders（总订单，含直接+间接归因），不取 direct_orders。取总订单更保守：
  它让更少的词成为否定候选。若误用 direct_orders，一个靠间接归因赚钱的词会被判
  「零转化」否定掉，而证据行上 conversions: 0 写得清清楚楚，人从证据里看不出口径错了。
- 「零转化」的判定留在本地 generate_negation_candidates，绝不下推给领星
  （schema 里那个 category=HasClickNoDeal 很诱人，但用它就等于把「什么叫 0 订单」
  交给上游：用哪个订单字段、按哪个窗口归因都不在我们手里）。
- min_clicks / min_spend 同样不下推：领星按 (广告组 × 词 × 匹配方式 × 来源) 出行，
  同一个 (广告组, 词) 会分裂成多行，逐行施加阈值会把聚合后本该合格的候选整组丢掉。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol

from ads_control_plane.canonical.entity import (
    AdProduct,
    CanonicalEntityRef,
    EntityType,
    ParentRefs,
    Provider,
)
from ads_control_plane.canonical.ids import CanonicalId
from ads_control_plane.canonical.money import Money
from ads_control_plane.strategies.negation import SearchTermRecord
from ads_control_plane.strategies.ports import (
    SearchTermFetch,
    SearchTermSourceError,
    UnjudgedGroup,
    attribution_window,
)

#: 只读工具 id。它已在 adapters/lx_read.py 的 READ_TOOL_ALLOWLIST 内；白名单外的
#: toolId 在任何网络调用发生之前就会被拒（结构性防写）。
TOOL_SEARCH_TERM_REPORT = "ad_campaign_search_term_report"

#: 转化口径字段名。钉成常量而不是就地取值：链式兜底
#: （row.get("orders") or row.get("direct_orders")）会在字段缺失时静默换口径。
CONVERSION_FIELD = "orders"

#: 归因滞后天数与窗口推导都搬到了端口（ports.ATTRIBUTION_LAG_DAYS /
#: attribution_window）：那是「回看窗口怎么划」的契约，不是本实现的私事——
#: 界面上「统计 X 到 Y」对每个实现都得是同一个区间。
#: 名字在本模块仍可见，原有引用不变。

#: 「未否定」——这一条下推是正确性不是性能：已否定过的词不该再被提名否定，
#: 而本地拿不到「是否已否定」这个事实，只有领星知道。
TARGETED_TYPE_NOT_NEGATIVED = "not_negatived"

#: 每页行数。2026-08-30 实测：单页耗时几乎与页大小无关（100 行 4.2s，1000 行 4.9s），
#: 因为成本在网关往返而不在传输。取 1000 把 2090 行从 21 页压到 3 页——
#: 88 秒变 15 秒。这不是调优，是可用性：MCP 客户端等不了 88 秒，
#: 一次取数就会超时，工具在真实店铺上直接不可用。
DEFAULT_PAGE_SIZE = 1000

#: 网络层失败（连接/读取超时、TLS、DNS）是「没问到」，同一页再问一次多半就问到了；
#: 网关与业务错误是「问到了、被拒」，再问还是同一句，不重试。2026-09-23 真实运行
#: 74 家店里有 1 家撞上它，整店记为取数失败，原样重跑就好了。按码认它而不 import
#: adapters（依赖方向见模块 docstring）；两次询问之间的间隔由读端口自己保证——
#: LxMcpReadClient 无论成败都停满调用间隔。
RETRYABLE_UPSTREAM_CODE = "LX_TRANSPORT_ERROR"
UPSTREAM_ATTEMPTS = 3


class LxReadPort(Protocol):
    def fetch_page(self, tool_id: str, params: Mapping[str, object]) -> Mapping[str, object]: ...


@dataclass(frozen=True, kw_only=True)
class LingxingProfileBinding:
    """一个店铺成为策略输入源所必需的平台侧身份常量。

    这些字段报表行里一个都没有，而 CanonicalEntityRef 全部必填，所以只能构造期注入。
    任一缺失即绑定不成立 → has_profile 返回 False，这是字面真话：我们确实无法为
    这个店构造 canonical 引用。

    shop_external_id 必须是领星店铺 sid，不用 profile_id 兜底。正因为
    CanonicalEntityRef.uniqueness_key() 不含它，写错不会触发任何去重冲突、不会让
    任何断言变红——它会安静地走进候选集合的冻结 hash 与审批记录，等将来写通道按
    AX-06「精确对象 + 完整父链」定位对象时才炸，那时错误的证据已经被人签过字了。
    """

    profile_external_id: str
    organization_id: CanonicalId
    provider_connection_id: CanonicalId
    marketplace: str
    shop_external_id: str
    #: 币种必须显式声明。领星行内没有币种字段，而做一张 country→currency 推断表
    #: 等于把一个未经证实的假设（「spends 用站点本币计价」）藏进代码，
    #: 而这个假设是否成立决定了 min_spend 门槛有没有意义。
    currency: str
    ad_product: AdProduct = AdProduct.SP


@dataclass(frozen=True, kw_only=True)
class _ParsedRow:
    """一条通过解析的数据行。"""

    campaign_id: str
    ad_group_id: str
    query: str
    clicks: int
    conversions: int
    spend: Decimal
    #: 这一行的 query 是不是 ASIN。见 SearchTermRecord.term_is_asin：读错的后果不是
    #: 少一个候选，是人照着 CSV 在领星加了一条挡不住任何东西的否定关键词。
    is_asin: bool
    #: 展示型指标，不参与判定（见 SearchTermRecord.impressions）。读不出来置 None，
    #: **绝不**走上面那个严格 try——那里抛错会污染整组，等于让一个纯展示的数字
    #: 有资格毙掉一条本该产出的候选。
    impressions: int | None
    #: 源侧现值名称（实测 null 率 3.2%）。读不到就是 None，不编。
    campaign_name: str | None = None
    ad_group_name: str | None = None


@dataclass(frozen=True, kw_only=True)
class _RowCounts:
    """一次解析产出的行级计数。作为返回值传出，不写进实例。"""

    skipped_summary: int = 0
    #: 读不出来的行总数。它同时是 _assert_rows_are_usable 的判据。
    rejected: int = 0
    #: 其中连身份都读不出来的那些：无法归到任何 (广告组, 词)，只能按行说。
    unattributable: int = 0
    duplicates: int = 0


def _reject(code: str, message: str) -> SearchTermSourceError:
    return SearchTermSourceError(code, message)


def _to_int(field: str, value: object) -> int:
    """严格整数解析：拒绝而不是猜。

    空串 / None 判为「该行不可判」，绝不当 0——「缺数据」变成「零转化」会直接
    制造候选。bool 也拒绝（它是 int 的子类，混进来会把 True 读成 1）。
    """
    if isinstance(value, bool):
        raise _reject("SEARCH_TERM_METRIC_SHAPE", f"{field}: bool is not a metric")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise _reject("SEARCH_TERM_METRIC_SHAPE", f"{field}: empty value is undecidable")
        try:
            parsed = Decimal(text)
        except InvalidOperation as exc:
            # 含千分位的 "1,234" 之类走这里——未知格式计入坏行，不猜它想表达什么。
            raise _reject("SEARCH_TERM_METRIC_SHAPE", f"{field}: {text!r} is not a number") from exc
        if parsed != parsed.to_integral_value():
            raise _reject("SEARCH_TERM_METRIC_SHAPE", f"{field}: {text!r} is not an integer")
        return int(parsed)
    raise _reject(
        "SEARCH_TERM_METRIC_SHAPE", f"{field}: cannot read {type(value).__name__} as an integer"
    )


def _to_decimal(field: str, value: object) -> Decimal:
    """严格金额解析。

    与 mirror/sync.py 的 to_decimal 故意不同：镜像解析失败置 None 并计数（那里的
    指标只是展示），这里失败即抛——少一个 spends 会直接改变「花掉 X 元没出单」
    这个判断本身。全链路禁 float（二进制误差会静默进金额）。
    """
    if isinstance(value, float):
        raise _reject("SEARCH_TERM_METRIC_SHAPE", f"{field}: float amounts are refused")
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise _reject(
            "SEARCH_TERM_METRIC_SHAPE", f"{field}: cannot read {type(value).__name__} as an amount"
        )
    text = str(value).strip()
    if not text:
        raise _reject("SEARCH_TERM_METRIC_SHAPE", f"{field}: empty amount is undecidable")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise _reject("SEARCH_TERM_METRIC_SHAPE", f"{field}: {text!r} is not an amount") from exc


def _opt_text(value: object) -> str | None:
    """身份字段读取。必须用 is None 判空，绝不先 str()。

    汇总行的 query 是 None，一旦 str(None) 就变成非空字符串 "None"，
    SearchTermRecord 的非空校验会放行，于是全店聚合值伪装成一个叫 "None" 的搜索词
    ——它必然过任何证据门，只要该店汇总 orders 恰为 0 就成为候选，而且花费最大，
    会排在导出 CSV 的最前面，最先被人批准。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _row_identity(row: Mapping[str, object]) -> tuple[str, ...]:
    """一行的身份。用源自己的 record_id，不用我们挑几列拼出来的键。

    2026-08-30 实测（真实店，单页 500 行）：按
    (campaign, ad_group, query, target_match_type) 这个粗键去重，**同一页内**就有
    4 组碰撞，被当成重复丢掉的 4 行合计携带 orders=4、clicks=33。它们不是重复行
    ——同一个广告组下两个不同关键词命中同一条顾客搜索词，就长这样，细看
    keyword_id/target_id 各不相同（细键 500 唯一，record_id 也 500 唯一）。

    丢掉这样一行的后果不是「少算一点花费」：orders 被抹掉，聚合出的 conversions
    变成 0，于是这个正在出单的词被提名否定——这正是整套设计里最贵的那个缺陷，
    而证据行上写着干干净净的 conversions: 0，人从证据里看不出来。

    record_id 是 32 字符非数字串，同一页取两次集合与顺序完全一致（实测），
    是源侧的行主键，跨页重复行会带着同一个 id 回来，去重仍然成立。

    拿不到 record_id 时退到含 keyword_id/target_id 的细键。两种误差不对等：
    少去重只是把某行的花费多算一遍（结论偏保守，最多多提一个候选给人看），
    多去重会抹掉订单（结论反向，把赚钱的词否定掉）。所以宁可少去重。
    """
    record_id = _opt_text(row.get("record_id"))
    if record_id is not None:
        return ("record", record_id)
    return (
        "fields",
        str(_opt_text(row.get("campaign_id")) or ""),
        str(_opt_text(row.get("ad_group_id")) or ""),
        str(_opt_text(row.get("query")) or "").casefold(),
        str(_opt_text(row.get("target_match_type")) or ""),
        str(_opt_text(row.get("match_type")) or ""),
        str(_opt_text(row.get("keyword_id")) or ""),
        str(_opt_text(row.get("target_id")) or ""),
    )


def _to_bool_flag(field: str, value: object) -> bool:
    """把源侧的 0/1 整数标记读成布尔。缺失或读不出来即抛——不许默认成 False。

    默认 False 正是本轮要修的那个病：`is_asin` 一直没人读，等价于全体默认 False，
    于是 ASIN 型搜索词被写成否定精准关键词，人照做无效而证据行上一切正常。
    在这里再默认一次，只是把同一个静默失效换了个位置。2026-08-30 实测：数据行
    恒有 `is_asin`（null 率 3.2% 恰等于汇总行占比），所以抛出来不会误伤正常数据。

    非零一律为真：抽样 30 行全是 0，未观测到 1，我们不知道源侧到底用哪个非零值
    表示 ASIN，只知道 0 表示不是。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    text = _opt_text(value)
    if text is None:
        raise _reject("SEARCH_TERM_ROW_MALFORMED", f"{field}: missing")
    try:
        return int(text, 10) != 0
    except ValueError:
        raise _reject(
            "SEARCH_TERM_ROW_MALFORMED", f"{field}: not an integer flag: {text!r}"
        ) from None


def _is_summary_row(row: Mapping[str, object]) -> bool:
    """身份字段全空 = 汇总行。

    判据只看身份字段，绝不看指标类型——实测汇总行的 orders 是字符串 '350' 而数据行
    是整数 0，那只是巧合，不是契约。分页与解析两处都要这个判定，且必须是同一套判据：
    两处一旦分叉，覆盖数与解析结果就会各算各的。
    """
    return _opt_text(row.get("query")) is None and _opt_text(row.get("ad_group_id")) is None


class LingxingSearchTermSource:
    """结构化实现 SearchTermReadPort（不继承，Protocol 按形状匹配）。"""

    def __init__(
        self,
        read_port: LxReadPort,
        *,
        bindings: Mapping[str, LingxingProfileBinding],
        page_size: int = DEFAULT_PAGE_SIZE,
        max_rows: int = 20000,
        max_pages: int = 40,
        cache_ttl_seconds: float = 900.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read_port = read_port
        self._bindings = dict(bindings)
        self._page_size = page_size
        self._max_rows = max_rows
        self._max_pages = max_pages
        self._cache_ttl = cache_ttl_seconds
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], tuple[float, SearchTermFetch]] = {}

    # ------------------------------------------------------------------ 端口方法

    def has_profile(self, profile_external_id: str) -> bool:
        """该 Profile 是否接入了本数据源。

        查绑定表即可：纯函数、不触网、永不抛异常。绝不用「取数为空」回答——
        那正是 ports.py 写死的 runtime-2 结论：「根本没接数据源」与「接了、查了、
        确实没有行」在空列表里长得一模一样，前者会被读成「查了没有浪费」。
        """
        return profile_external_id in self._bindings

    def fetch_search_term_performance(
        self,
        profile_external_id: str,
        lookback_days: int,
        as_of: datetime,
    ) -> SearchTermFetch:
        binding = self._bindings.get(profile_external_id)
        if binding is None:
            raise _reject(
                "SEARCH_TERM_PROFILE_NOT_BOUND",
                f"profile {profile_external_id!r} has no complete binding; nothing was fetched",
            )
        if as_of.tzinfo is None or as_of.tzinfo.utcoffset(as_of) is None:
            # 猜一个时区就是把别处的钟点当成这里的。fail loud，与 mandate/run_window 同规矩。
            raise _reject(
                "SEARCH_TERM_CLOCK_NAIVE", "as_of must be timezone-aware; refusing to assume UTC"
            )
        window = _Window.derive(lookback_days=lookback_days, as_of=as_of)
        cache_key = (profile_external_id, window.report_date)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        rows, source_total = self._fetch_all_rows(binding, window)
        parsed, poisoned, counts = self._parse_rows(rows)
        self._assert_rows_are_usable(parsed, source_total=source_total, rejected=counts.rejected)
        records, unjudged = self._aggregate(parsed, poisoned, binding, window)
        fetch = SearchTermFetch(
            records=records,
            source_total=source_total,
            skipped_summary_rows=counts.skipped_summary,
            unjudged_groups=unjudged,
            unattributable_rows=counts.unattributable,
            # 读不出来的行总数与真正参与聚合的行数一起报出去，账才对得上：
            # source_total = 读不出来的行 + 可用行；汇总行与重复行在 total 之外。少一格，
            # 有身份但指标读不出来的那些行在任何行级计数里都不出现。
            unreadable_rows=counts.rejected,
            usable_rows=len(parsed),
            duplicate_rows=counts.duplicates,
        )
        self._cache_put(cache_key, fetch)
        return fetch

    # ------------------------------------------------------------------ 缓存

    def _cache_get(self, key: tuple[str, str]) -> SearchTermFetch | None:
        """短 TTL 缓存，按 (profile, 窗口) 取。

        它挡的不是延迟而是权限缺口：generate_negation_candidate_set 是 AI 可调用工具，
        而即席模式（不带 mandate_id）没有任何配额——assert_run_authorized 的日运行数与
        最小间隔只在授权书模式下生效。没有缓存，AI 客户端连调 20 次就是 20 次对领星
        生产 API 的多页读取，唯一节流是每页之间那 1.1 秒 sleep。缓存把这压成一次真实拉取。
        窗口是 key 的一部分，所以换 lookback_days 会如实重新取数，不会拿旧窗口糊弄人。
        """
        if self._cache_ttl <= 0:
            return None
        now = self._monotonic()
        with self._lock:
            entry = self._cache.get(key)
            if entry is None or now - entry[0] >= self._cache_ttl:
                # 过期条目就地删掉：留着会让 _cache 随窗口滚动无限长大。
                self._cache.pop(key, None)
                return None
            # 账目跟着这条缓存自己走：命中的是哪个店的哪个窗口，返回的就是那次的账目。
            return replace(entry[1], served_from_cache=True)

    def _cache_put(self, key: tuple[str, str], fetch: SearchTermFetch) -> None:
        if self._cache_ttl <= 0:
            return
        with self._lock:
            self._cache[key] = (self._monotonic(), fetch)

    # ------------------------------------------------------------------ 取数

    def _fetch_all_rows(
        self, binding: LingxingProfileBinding, window: _Window
    ) -> tuple[list[Mapping[str, object]], int | None]:
        """分页拉全。到达上限是错误，不是截断。

        这与 mirror/sync.py 的处理故意相反：镜像截断后如实标注并给续拉入口，因为
        镜像是「浏览」，少几行只是看不全。这里不行——一个 (广告组, 词) 的行按匹配
        方式散落多页，若某页被砍掉而它恰好携带 orders=2，聚合出的 conversions 就是 0，
        于是制造一条把正在出单的词否定掉的候选。截断在这里不是「看不全」，是「结论反向」。
        端口返回的 SearchTermFetch 能如实报出「丢了多少组/多少行」，但报不出截断——
        被砍掉的那页里有什么、属于哪些组，我们根本不知道。说不出口的东西不许发生。
        """
        collected: list[Mapping[str, object]] = []
        source_total: int | None = None
        page = 1
        data_rows = 0
        counted: set[tuple[str, ...]] = set()
        while True:
            result = self._fetch_one_page(binding, window, page)
            total_obj = result.get("total")
            if isinstance(total_obj, int):
                source_total = total_obj
            if page == 1:
                if source_total is None:
                    # 空结果 + has_profile=true 会被读成「查了，没有浪费」。窗口分隔符
                    # 写错时领星就可能返回空结果而不是报错，所以 total 缺席即拒。
                    raise _reject(
                        "SEARCH_TERM_TOTAL_ABSENT",
                        "upstream did not report a row total; cannot tell an honest zero "
                        "from a malformed query",
                    )
                if source_total > self._max_rows:
                    raise _reject(
                        "SEARCH_TERM_RESULT_TOO_LARGE",
                        f"window holds {source_total} rows, above the {self._max_rows} limit; "
                        "shorten lookback_days or scope the run",
                    )
            rows_obj = result.get("rows")
            rows = (
                [r for r in rows_obj if isinstance(r, Mapping)]
                if isinstance(rows_obj, list)
                else []
            )
            collected.extend(rows)
            # 只数**去重后的数据行**。两种东西会把这个计数撑虚，都会让
            # data_rows >= source_total 提前成立、末页没拉就宣告拉全：
            #   1. 汇总行——实测 total=2090 而每页返回 101 行，汇总行不计入 total；
            #   2. 跨页重复行——按花费排序翻页时行排名会移动（实测同窗口 total
            #      从 1047 漂到 1079），同一行可能在两页各出现一次。
            # 身份判定与解析阶段共用 _row_identity，两处分叉就会各算各的。
            fresh = 0
            for row in rows:
                if _is_summary_row(row):
                    continue
                identity = _row_identity(row)
                if identity in counted:
                    continue
                counted.add(identity)
                fresh += 1
            data_rows += fresh
            # 一页没带来新数据行（空页、只有汇总行、全是上一页见过的行）就不再往后翻：
            # 再翻也只是把越界页一页页问到页数上限。
            if fresh == 0 or (source_total is not None and data_rows >= source_total):
                break
            if page >= self._max_pages:
                raise _reject(
                    "SEARCH_TERM_PAGE_BUDGET_EXCEEDED",
                    f"stopped after {self._max_pages} pages with rows remaining; "
                    "a truncated set would silently invert conclusions",
                )
            page += 1
        if source_total is not None and data_rows < source_total:
            # 总数不变时，一行在两页各出现一次，就必有另一行两页都没拿到。漏掉的那行
            # 若携带某个词的 orders=2，聚合出的 conversions 就是 0，正在出单的词被判
            # 死刑——所以对不上就整家店不给结论。2026-09-23/24 两轮真实运行的 14 份
            # 报表里，去重后的行数都正好等于 total（包括要翻 3 页的店）。
            raise _reject(
                "SEARCH_TERM_PAGES_SHORT",
                f"paged through {data_rows} of {source_total} rows; rows moved between pages, "
                "so one that carries orders may be missing",
            )
        return collected, source_total

    def _fetch_one_page(
        self, binding: LingxingProfileBinding, window: _Window, page: int
    ) -> Mapping[str, object]:
        """取一页，并把上游异常翻成带码的端口异常。

        不翻的后果是本仓库注释里已经写明「修过一次、不许再犯」的那条：
        strategy_service 只接得住 SearchTermSourceError，而 server.py 的 _coded
        只翻 ToolDenied，于是网关的 LxReadError 一路裸穿到 MCP 面，只剩一句
        Error executing tool。调用方分不清「超时可重试」和「参数签错了永远不会成功」，
        会去反复重试一个永远失败的调用，撞 QPS=1 限额。

        按 duck typing 取 .code 而不 import adapters：本模块刻意不认识具体适配器
        （见模块 docstring 的依赖方向约定），而 LX_TRANSPORT_ERROR 这些码已经在
        UI 词典里，原样保留才能让人查得到。只有「没问到」（RETRYABLE_UPSTREAM_CODE）
        会就同一页再问，最多问 UPSTREAM_ATTEMPTS 次。
        """
        params = self._build_params(binding, window, page)
        attempt = 1
        while True:
            try:
                return self._read_port.fetch_page(TOOL_SEARCH_TERM_REPORT, params)
            except SearchTermSourceError:
                raise
            except Exception as exc:
                code = getattr(exc, "code", None)
                if code == RETRYABLE_UPSTREAM_CODE and attempt < UPSTREAM_ATTEMPTS:
                    attempt += 1
                    continue
                # 带上上游原话（截断）：只有类型名时，日志里看不出是 key 错了、参数错了
                # 还是网关挂了。适配器的消息里不含 key。
                raise _reject(
                    code if isinstance(code, str) and code else "SEARCH_TERM_UPSTREAM_ERROR",
                    f"upstream read failed on page {page} after {attempt} attempt(s): "
                    f"{type(exc).__name__}: {str(exc)[:300]}",
                ) from exc

    def _build_params(
        self, binding: LingxingProfileBinding, window: _Window, page: int
    ) -> dict[str, object]:
        """入参。with_ring 不传——本工具的 schema 把它定为 boolean，而 group 报表要
        integer、targeting 报表要 number；照抄别的报表传 0 会撞 code=102。"""
        return {
            "report_date": window.report_date,
            "profile_ids": [binding.profile_external_id],
            "page": page,
            "length": self._page_size,
            "sort_field": "spends",
            "sort_type": "desc",
            "targeted_type": TARGETED_TYPE_NOT_NEGATIVED,
        }

    # ------------------------------------------------------------------ 解析

    def _parse_rows(
        self, rows: list[Mapping[str, object]]
    ) -> tuple[list[_ParsedRow], dict[tuple[str, str], set[str]], _RowCounts]:
        """逐行解析。汇总行跳过并计数，坏行跳过并计数，都不静默。

        第二个返回值是**被污染的分组**：坏行本身有身份（知道属于哪个广告组、哪个词），
        只是指标读不出来。这种行不能只是「跳过」——它所在的 (广告组, 词) 组照样会用
        剩下的行求和出一条 record，而那条 record 上的 conversions 是不完整的。
        被跳过的那行若携带 orders=2，聚合结果就是 0，正在出单的词被提名否定。
        「缺数据」在聚合层变回「零转化」，与去重键过粗是同一个病，只是入口不同。
        """
        parsed: list[_ParsedRow] = []
        seen: set[tuple[str, ...]] = set()
        #: 被污染的分组 → 这些行自报的活动 id。带上活动是为了让调用方能按作用域筛：
        #: 一份只管 1 个活动的授权书不该把全店的坏数据都算到自己头上。
        poisoned: dict[tuple[str, str], set[str]] = {}
        skipped_summary = 0
        rejected = 0
        unattributable = 0
        duplicates = 0
        for row in rows:
            query = _opt_text(row.get("query"))
            ad_group_id = _opt_text(row.get("ad_group_id"))
            campaign_id = _opt_text(row.get("campaign_id"))
            if query is None and ad_group_id is None:
                skipped_summary += 1
                continue
            if query is None or ad_group_id is None:
                # 组身份读不出来 ⇒ 这一行归不到任何 (广告组, 词)。它没法计进组数，
                # 但同样是「这一轮有东西没被判断」的一部分，必须按行说出来。
                rejected += 1
                unattributable += 1
                continue
            if campaign_id is None:
                # 活动 id 缺失**不影响**这一行归属于哪个 (广告组, 词)——组身份就在
                # 手里。此前把它一并当成「归不到任何组」丢掉，而那个组照样会用剩下
                # 的行聚合出一条候选：这一行若携带 orders，聚合出的 conversions 就是
                # 缺了它之后的和，一个正在出单的词被提名否定，而证据行上写着干干净净
                # 的 conversions: 0。这正是 poisoned 这套机制存在的理由，只是入口不同。
                rejected += 1
                poisoned.setdefault((ad_group_id, query.casefold()), set())
                continue
            try:
                clicks = _to_int("clicks", row.get("clicks"))
                conversions = _to_int(CONVERSION_FIELD, row.get(CONVERSION_FIELD))
                spend = _to_decimal("spends", row.get("spends"))
                is_asin = _to_bool_flag("is_asin", row.get("is_asin"))
            except SearchTermSourceError:
                # 一行坏数据不该让整店失去决策能力，但它所在的那个组必须失去
                # 「可判定」资格——见本方法 docstring。
                rejected += 1
                poisoned.setdefault((ad_group_id, query.casefold()), set()).add(campaign_id)
                continue
            if clicks < 0 or conversions < 0:
                rejected += 1
                poisoned.setdefault((ad_group_id, query.casefold()), set()).add(campaign_id)
                continue
            # 严格块之外：展示型指标失败不许改变判定结果。
            try:
                impressions: int | None = _to_int("impressions", row.get("impressions"))
            except SearchTermSourceError:
                impressions = None
            key = _row_identity(row)
            if key in seen:
                # 行级去重：实测同窗口 total 从 1047 漂到 1079，按花费排序翻页时行的
                # 排名会移动，同一行可能在两页出现。而下面做的是求和聚合，重复行会把
                # clicks/spend 直接翻倍。
                duplicates += 1
                continue
            seen.add(key)
            parsed.append(
                _ParsedRow(
                    campaign_id=campaign_id,
                    ad_group_id=ad_group_id,
                    query=query,
                    clicks=clicks,
                    conversions=conversions,
                    spend=spend,
                    is_asin=is_asin,
                    impressions=impressions,
                    campaign_name=_opt_text(row.get("campaign_name")),
                    ad_group_name=_opt_text(row.get("ad_group_name")),
                )
            )
        return (
            parsed,
            poisoned,
            _RowCounts(
                skipped_summary=skipped_summary,
                rejected=rejected,
                unattributable=unattributable,
                duplicates=duplicates,
            ),
        )

    def _assert_rows_are_usable(
        self, parsed: list[_ParsedRow], *, source_total: int | None, rejected: int
    ) -> None:
        """上游给了行、却一行也读不出来时，必须报错而不是返回空。

        返回空元组会和「这个店确实没有浪费」长得一模一样：调用方看到
        profile_has_data_source=true、候选为 0，读出来的是「查了，很干净」。
        这正是 2026-08-29 排查结论 runtime-2 写死的那句——「没接数据源」不得伪装成
        「查了没有」——只是这次伪装的是「读不动数据」。

        坏行占比过半同样升级为拒绝：一半的行读不出来时，剩下那一半算出来的
        「零转化」不足以支撑任何否定决定。阈值是判断，报错本身不是。
        """
        total = source_total
        if total is None or total <= 0:
            return
        if not parsed:
            raise _reject(
                "SEARCH_TERM_ROWS_UNUSABLE",
                f"upstream reported {total} rows but none could be read; "
                "an empty result here would read as 'checked, nothing wasted'",
            )
        if rejected > len(parsed):
            raise _reject(
                "SEARCH_TERM_ROWS_UNUSABLE",
                f"{rejected} of {rejected + len(parsed)} rows were unreadable; "
                "the remainder is too thin to support a negation decision",
            )

    # ------------------------------------------------------------------ 聚合

    def _aggregate(
        self,
        parsed: list[_ParsedRow],
        poisoned: dict[tuple[str, str], set[str]],
        binding: LingxingProfileBinding,
        window: _Window,
    ) -> tuple[tuple[SearchTermRecord, ...], tuple[UnjudgedGroup, ...]]:
        """聚合到 (广告组, 词)。返回 (记录, 整组没被判断的那些组)。

        分组键必须与 negation.py 的去重键逐字对齐——它用的是
        (*scope.uniqueness_key(), search_term.casefold())，所以这里也必须 casefold，
        否则 Widget 与 widget 会产出两条 record，撞上那条重复检查。

        含坏行的组整组不出 record：读不出来的那行可能正带着订单，用剩下的行求和
        等于把「缺数据」说成「零转化」。少提一个候选是可以承受的错误方向，
        提名一个正在出单的词不是。
        """
        groups: dict[tuple[str, str], list[_ParsedRow]] = {}
        for row in parsed:
            groups.setdefault((row.ad_group_id, row.query.casefold()), []).append(row)
        records: list[SearchTermRecord] = []
        # 组和行是两种单位，绝不并进同一个计数：_RowCounts.rejected 是「有几行读不出来」，
        # 它同时是 _assert_rows_are_usable 的判据；把「有几个组不可判」加进去，
        # 那道闸就会在坏行不多时被组数推着误触发。
        #
        # 先把被污染的组全部记下来，而不是在下面的循环里数：一个组若**所有**行都
        # 读不出来，它一条 parsed 行都没有，于是 groups 里根本没有这个键，循环永远
        # 碰不到它——这个组会连「被丢掉」这件事都不留痕迹。越是彻底读不出来的组
        # 越是完全消失，正好反了。
        # 活动 id 要从**同组的好行**里补齐。被「缺 campaign_id」污染的组，poisoned 里
        # 那个集合是空的——那一行本来就没有活动 id 可记。而调用方按作用域筛时，
        # 空集合等于「说不出属于哪个活动」，一份按活动圈定的授权书会把它整个漏掉：
        # 组明明就在他圈的活动下面（同组别的行写着活动 id），缺口却消失了，
        # 卡片显示这一轮干干净净。同一个组的其他行知道答案，问它们就行。
        unjudged: list[UnjudgedGroup] = [
            UnjudgedGroup(
                ad_group_external_id=key[0],
                campaign_external_ids=tuple(
                    sorted(campaigns | {r.campaign_id for r in groups.get(key, ())})
                ),
            )
            for key, campaigns in poisoned.items()
        ]
        for group_key, rows in groups.items():
            ad_group_id = group_key[0]
            if group_key in poisoned:
                continue
            campaigns = {r.campaign_id for r in rows}
            if len(campaigns) > 1:
                # 一个广告组只能属于一个活动。冲突即数据损坏，不许静默挑一个。
                # 两个活动都记下来：任一落在作用域内就该说「这里有东西没判断」。
                unjudged.append(
                    UnjudgedGroup(
                        ad_group_external_id=ad_group_id,
                        campaign_external_ids=tuple(sorted(campaigns)),
                    )
                )
                continue
            # 展示文本取点击最多的原文，并列取字典序最小。必须确定性：否则同一份
            # 数据两次运行得到不同的候选集合冻结 hash，而 hash 绑定审批（AX-07）。
            display = min(sorted({r.query for r in rows}), key=lambda q: (-_clicks_of(rows, q), q))
            # 名称同样必须确定性：它随候选一起冻结，进 set_hash，而 hash 绑定审批。
            # 组内各行的名字理应相同（同一个广告组），但实测有 3.2% 为 null，
            # 且不排除源侧不一致——取字典序最小的非空值，两次运行必得同一个。
            campaign_name = _first_name(r.campaign_name for r in rows)
            ad_group_name = _first_name(r.ad_group_name for r in rows)
            # 组内任一行说是 ASIN，整组按 ASIN 算。is_asin 是词文本自身的属性，
            # 同组各行本该一致；不一致时两种误判不对等：判成关键词会让人加一条挡不住
            # 任何东西的否定词（白做工，钱继续烧），判成 ASIN 只是少提一个候选并如实
            # 说明原因。宁可少提。
            term_is_asin = any(r.is_asin for r in rows)
            # 组内任一行读不出曝光，整组就说不出总曝光——宁可显示「—」，
            # 不给一个少算了某几行的数（它会把 CTR 算高，方向恰好是"看起来更该留着"）。
            impressions = (
                None
                if any(r.impressions is None for r in rows)
                else sum(r.impressions or 0 for r in rows)
            )
            try:
                records.append(
                    SearchTermRecord(
                        scope=CanonicalEntityRef(
                            organization_id=binding.organization_id,
                            provider=Provider.LINGXING,
                            provider_connection_id=binding.provider_connection_id,
                            marketplace=binding.marketplace,
                            shop_external_id=binding.shop_external_id,
                            profile_external_id=binding.profile_external_id,
                            ad_product=binding.ad_product,
                            entity_type=EntityType.AD_GROUP,
                            entity_external_id=ad_group_id,
                            parent_refs=ParentRefs(campaign_external_id=rows[0].campaign_id),
                        ),
                        search_term=display,
                        campaign_name=campaign_name,
                        ad_group_name=ad_group_name,
                        term_is_asin=term_is_asin,
                        impressions=impressions,
                        clicks=sum(r.clicks for r in rows),
                        conversions=sum(r.conversions for r in rows),
                        spend=Money(
                            amount=sum((r.spend for r in rows), Decimal("0")),
                            currency=binding.currency,
                        ),
                        window_start=window.window_start,
                        window_end=window.window_end,
                        data_as_of=window.data_as_of,
                    )
                )
            except ValueError:
                # 模型层拒绝（超长 id、空词、非法币种…）。整组作废并计数，不炸整店。
                unjudged.append(
                    UnjudgedGroup(
                        ad_group_external_id=ad_group_id,
                        campaign_external_ids=(rows[0].campaign_id,),
                    )
                )
        return tuple(records), tuple(unjudged)


def _first_name(values: Iterable[str | None]) -> str | None:
    """组内取一个确定的名字：字典序最小的非空值；全空则 None（不编）。

    必须确定性——名字随候选冻结进 set_hash，而 hash 绑定审批（AX-07）。
    「取第一个遇到的」会随行顺序变化，同一份数据两次运行得到不同的 hash。
    """
    names = sorted({v for v in values if v})
    return names[0] if names else None


def _clicks_of(rows: list[_ParsedRow], query: str) -> int:
    return sum(r.clicks for r in rows if r.query == query)


@dataclass(frozen=True, kw_only=True)
class _Window:
    """报表窗口。三个时刻全是 tz-aware UTC。"""

    report_date: str
    window_start: datetime
    window_end: datetime
    data_as_of: datetime

    @staticmethod
    def derive(*, lookback_days: int, as_of: datetime) -> _Window:
        """窗口推导。

        右端退 ATTRIBUTION_LAG_DAYS 天（见该常量的注释），闭区间天数仍为 lookback_days。

        data_as_of 取**取数时刻**，不是窗口右端。这两个是不同的问题：
        窗口右端回答「这批数据覆盖到哪一天」，record 上的 window_start/window_end
        已经在回答它；data_as_of 回答「这批数字有多陈」，而 negation.py 的新鲜度门
        减的正是它。Mock 源写死了这条契约（local_demo.py:169 取 now-2h，
        window_end 另取 now-1d），本模块此前把两者混为一谈。

        混淆的代价是实测出来的（2026-08-30，真实店铺）：窗口右端已被归因滞后
        刻意退掉 3 天，于是 now - window_end 恒落在 [48h, 72h)，而默认门槛是 24h
        ——真实通道下 3815 条记录 100% ABSTAIN，一条候选也产不出来。参数白名单
        上限恰好是 72h，所以 [1,47] 这段取值可证明恒失败，人怎么调都没用。
        更糟的是它对人的样子：几千条 STALE_DATA 读起来像「数据旧了，等等再来」，
        但窗口右端跟着 as_of 走，等多久 staleness 都不变。

        刻意等 3 天让订单结算，不叫数据陈旧。把一个设计参数塞进新鲜度门，量出来的
        是常数——而常数不是测量。

        如实记录能力边界：源侧没有新鲜度字段（实测确认，见 README「响应样例」一节），
        所以领星自己端上来的是不是 Amazon 的陈缓存，我们量不到，也不假装量得到。
        改成取数时刻之后，STALE_DATA 在本源上真正挡住的是一种情况：**缓存的取数
        结果过老**（见 _cache_get，默认 TTL 900s）——记录冻结时把当时的取数时刻
        一并冻住，缓存命中时它就跟着一起变旧，这是真实且唯一可测的那部分。
        """
        window_start, window_end = attribution_window(lookback_days=lookback_days, as_of=as_of)
        start_date, end_date = window_start.date(), (window_end - timedelta(days=1)).date()
        if window_end > as_of:
            # 窗口右端跑到取数时刻之后，说明 lookback/滞后算错了；放行会让 record
            # 声称覆盖了还没发生的日子。
            raise _reject(
                "SEARCH_TERM_WINDOW_IN_FUTURE",
                f"window end {window_end.isoformat()} is after as_of {as_of.isoformat()}",
            )
        return _Window(
            # 分隔符逐字为 " - "（横杠两侧带空格），与 schema 的 report_date 格式一致。
            report_date=f"{start_date.isoformat()} - {end_date.isoformat()}",
            window_start=window_start,
            window_end=window_end,
            data_as_of=as_of,
        )
