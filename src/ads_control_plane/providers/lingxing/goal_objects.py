"""一个商品目标（店 + ASIN）此刻在领星里的样子。每轮从报表重新还原，不信上一轮。

只读。依赖方向同 search_terms.py：本模块自己声明 LxReadPort，不 import adapters，也不 import sfw。

2026-09-24 在本机只读实测（记录只在 ~/.amazon-ads/private-notes/，不进仓库），钉下来的事：
- 商品报表的一行是一条商品广告：带 asin、sku、活动、广告组、三层状态，也带库存
  afn_fulfillable_quantity 和标价；零曝光的广告也在表里；`state=enabled` 加页长 1000 可用。
- 关键词行带所在组的默认价 default_bid；自己的 bid 为空，就是在用组默认价。
- 投放报表按 ads_strategy 过滤，对任何取值都回 0 行，靠不住。领星在不在管，只看行内标记。
- 同一查询间隔 60 秒取两次，结果逐字相同。

「领星在管」的门槛故意很低（fail-closed）：标记读不出来也算在管。误判成「没在管」，
代价是跟领星的自动规则抢同一个出价；误判成「在管」，只是这一处这一轮不判。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol

from ads_control_plane.strategies.bidding import LONG_DAYS, SHORT_DAYS, Evidence
from ads_control_plane.strategies.ports import ATTRIBUTION_LAG_DAYS

TOOL_PRODUCTS = "ad_campaign_product_report"
TOOL_CAMPAIGNS = "ad_campaign_report"
TOOL_KEYWORDS = "ad_campaign_keyword_report"
TOOL_TARGETS = "ad_campaign_targeting_report"

#: 页长 1000 实测生效（S0：关键词报表一店 14935 行，按 1000 一页返回）。
PAGE_SIZE = 1000
#: 一种报表最多翻这么多页。到了上限是错误，不是截断：少一页，就可能漏掉一个正在出单的词。
MAX_PAGES = 10
#: 同 search_terms.py：只有「没问到」才就同一页再问。
RETRYABLE_UPSTREAM_CODE = "LX_TRANSPORT_ERROR"
UPSTREAM_ATTEMPTS = 3
#: 读不出来的对象超过这个比例，整个商品这一轮不判：剩下那部分不足以代表它。
MAX_UNREADABLE_SHARE = Decimal("0.5")

#: 行内「在管」标记。必填布尔读不出来（None、缺字段）也算在管。
_MANAGED_BOOLS = ("is_apply_rule", "is_apply_time")
_MANAGED_BOOLS_BELOW_CAMPAIGN = ("is_ad_group_apply_time",)
#: 可有可无的布尔：投放行上恒为 None（S0），只认 True。
_MANAGED_OPTIONAL_BOOLS = ("is_apply_grab",)
_MANAGED_LISTS = ("applied_templates", "ad_group_applied_templates", "rule_group_uuids")
_MANAGED_IDS = (
    "optimization_rule_id",
    "timing_base_value",
    "step_budget_object_uuid",
    "step_budget_template_uuid",
)


class LxReadPort(Protocol):
    def fetch_page(self, tool_id: str, params: Mapping[str, object]) -> Mapping[str, object]: ...


class GoalReadError(Exception):
    """这个商品这一轮读不全。`code` 给程序分辨；上游的码原样保留，人查得到。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Window:
    """报表窗口，两端都含。"""

    start: date
    end: date

    @property
    def report_date(self) -> str:
        # 分隔符逐字为 " - "，与报表 schema 的 report_date 格式一致。
        return f"{self.start.isoformat()} - {self.end.isoformat()}"


@dataclass(frozen=True)
class Windows:
    long: Window
    short: Window
    before: Window  # 长窗之前紧挨着的 14 天，只给断路器和转化率对比用


def windows_for(today: date) -> Windows:
    end = today - timedelta(days=ATTRIBUTION_LAG_DAYS)
    return Windows(
        long=Window(end - timedelta(days=LONG_DAYS - 1), end),
        short=Window(end - timedelta(days=SHORT_DAYS - 1), end),
        before=Window(end - timedelta(days=2 * LONG_DAYS - 1), end - timedelta(days=LONG_DAYS)),
    )


@dataclass(frozen=True, kw_only=True)
class AdObject:
    """商品在投的广告组里，一个有出价的东西：关键词，或投放（自动投放 / 商品投放）。"""

    kind: str  # "keyword" | "target"
    object_id: str
    campaign_id: str
    ad_group_id: str
    #: 关键词原文加匹配方式，或投放表达式。只进本机报告网页，不进对话。
    label: str
    enabled: bool
    bid: Decimal | None
    created: date | None
    managed: bool
    shared: bool
    long: Evidence
    short: Evidence


@dataclass(frozen=True, kw_only=True)
class GoalView:
    title: str | None
    stock: int | None
    #: 商品在投的 SP 广告组数，其中和别的 ASIN 共用的有几个。
    groups: int
    shared_groups: int
    objects: tuple[AdObject, ...]
    #: 整个商品的 SP 广告：长窗，以及再往前 14 天。
    now: Evidence
    before: Evidence
    #: 读不出来、这一轮没判的对象数。
    unreadable: int
    windows: Windows


# ------------------------------------------------------------------ 严格解析


class _Unreadable(Exception):
    pass


def _text(value: object) -> str | None:
    """身份与名称。只收非空字符串：str(None) 会变成非空的 "None"。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _int(field: str, value: object) -> int:
    if isinstance(value, bool):
        raise _Unreadable(f"{field}: bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            number = Decimal(value.strip())
        except InvalidOperation as exc:
            raise _Unreadable(f"{field}: {value!r}") from exc
        # 先挡 Infinity / NaN / sNaN：sNaN 一比较就抛 InvalidOperation，不是 _Unreadable。
        if number.is_finite() and number == number.to_integral_value():
            return int(number)
    raise _Unreadable(f"{field}: {value!r}")


def _money(field: str, value: object) -> Decimal:
    """金额禁 float（AX-01）。空值读不出来，绝不当 0：缺数据当成 0 单会制造降价。"""
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise _Unreadable(f"{field}: {type(value).__name__}")
    text = str(value).strip()
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise _Unreadable(f"{field}: {text!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise _Unreadable(f"{field}: {text!r}")
    return amount


def _bid(value: object) -> Decimal | None:
    """出价：空 = 继承组默认价（S0 实测）。有值就必须读得出来、而且大于 0。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    amount = _money("bid", value)
    if amount <= 0:
        raise _Unreadable(f"bid: {value!r}")
    return amount


def _evidence(row: Mapping[str, object]) -> Evidence:
    clicks = _int("clicks", row.get("clicks"))
    orders = _int("orders", row.get("orders"))
    impressions = _int("impressions", row.get("impressions"))
    if min(clicks, orders, impressions) < 0:
        raise _Unreadable("negative count")
    return Evidence(
        impressions=impressions,
        clicks=clicks,
        orders=orders,
        spend=_money("spends", row.get("spends")),
        sales=_money("sales", row.get("sales")),
    )


def _day(value: object) -> date | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _managed(row: Mapping[str, object], bools: tuple[str, ...]) -> bool:
    for field in bools:
        if row.get(field) is not False:
            return True
    for field in _MANAGED_OPTIONAL_BOOLS:
        if row.get(field) is True:
            return True
    for field in _MANAGED_LISTS:
        value = row.get(field)
        if value is not None and value != []:
            return True
    return any(row.get(field) not in (None, "") for field in _MANAGED_IDS)


def _enabled(row: Mapping[str, object]) -> bool:
    return all(
        _text(row.get(field)) == "enabled"
        for field in ("state", "campaign_state", "ad_group_state")
    )


# ------------------------------------------------------------------ 取数


def _ask(port: LxReadPort, tool: str, params: Mapping[str, object]) -> Mapping[str, object]:
    attempt = 1
    while True:
        try:
            return port.fetch_page(tool, params)
        except Exception as exc:  # 上游异常按 duck typing 取码：本模块不认识具体适配器
            code = getattr(exc, "code", None)
            if code == RETRYABLE_UPSTREAM_CODE and attempt < UPSTREAM_ATTEMPTS:
                attempt += 1
                continue
            raise GoalReadError(
                code if isinstance(code, str) and code else "GOAL_UPSTREAM_ERROR",
                f"{tool} failed after {attempt} attempt(s): {type(exc).__name__}: {str(exc)[:300]}",
            ) from exc


def _all_rows(
    port: LxReadPort, tool: str, params: Mapping[str, object], identity: str
) -> list[Mapping[str, object]]:
    """翻到底，按身份字段去重。身份为空的是汇总行。去重后的行数必须等于 total。

    一行数据的身份读不出来，会和汇总行一起被跳过，然后在「对不上 total」那里炸出来——
    读不全就整个不判，不拿半张表下结论。
    """
    rows: list[Mapping[str, object]] = []
    seen: set[str] = set()
    total: int | None = None
    page = 1
    while True:
        result = _ask(port, tool, {**params, "page": page, "length": PAGE_SIZE})
        if page == 1:
            reported = result.get("total")
            if not isinstance(reported, int) or isinstance(reported, bool):
                raise GoalReadError(
                    "GOAL_TOTAL_ABSENT",
                    f"{tool} did not report a row total; cannot tell a real zero",
                )
            total = reported
        page_rows = result.get("rows")
        fresh = 0
        for row in page_rows if isinstance(page_rows, list) else []:
            if not isinstance(row, Mapping):
                continue
            key = _text(row.get(identity))
            if key is None or key in seen:
                continue
            seen.add(key)
            rows.append(row)
            fresh += 1
        assert total is not None
        if fresh == 0 or len(seen) >= total:
            break
        if page >= MAX_PAGES:
            raise GoalReadError(
                "GOAL_TOO_MANY_PAGES", f"{tool} still had rows after {MAX_PAGES} pages"
            )
        page += 1
    assert total is not None
    if len(seen) < total:
        raise GoalReadError(
            "GOAL_PAGES_SHORT", f"{tool}: paged through {len(seen)} of {total} rows"
        )
    return rows


def _report(window: Window) -> dict[str, object]:
    return {"report_date": window.report_date, "sort_field": "spends", "sort_type": "desc"}


def _is_mine(row: Mapping[str, object], asin: str) -> bool:
    return (_text(row.get("asin")) or "").upper() == asin and _text(
        row.get("sponsored_type")
    ) == "sp"


# ------------------------------------------------------------------ 还原


def read_goal(
    port: LxReadPort,
    *,
    profile_id: str,
    asin: str,
    today: date,
    shop_ads: dict[tuple[str, str], list[Mapping[str, object]]] | None = None,
) -> GoalView:
    """一个商品此刻的样子。shop_ads 是同一批次里同一家店共用的「全店在投商品广告」。

    调用次数：全店在投商品广告 1–3 页、这个 ASIN 两个窗口各 1、活动 1、关键词与投放各两个
    窗口，大店约 10 次（S0 实测单次 2.7–5.8 秒）。
    """
    asin = asin.upper()
    windows = windows_for(today)
    cache_key = (profile_id, windows.long.report_date)
    if shop_ads is not None and cache_key in shop_ads:
        enabled_ads = shop_ads[cache_key]
    else:
        # 全店在投的商品广告：共用广告组只能从这里看出来（组里还有别的 ASIN 在投）。
        enabled_ads = _all_rows(
            port,
            TOOL_PRODUCTS,
            {**_report(windows.long), "profile_id": profile_id, "state": "enabled"},
            "ad_id",
        )
        if shop_ads is not None:
            shop_ads[cache_key] = enabled_ads
    # state=enabled 只筛广告自己的开关：活动或广告组停了的也在里面（2026-09-24 真实冒烟，
    # 一家大店的头部 ASIN：广告开着的组 118 个，1146 个关键词和投放里 1111 个其实停着）。
    # 三层都开着才算在投。
    in_flight = [row for row in enabled_ads if _enabled(row)]
    in_group: dict[str, set[str]] = {}
    for row in in_flight:
        group = _text(row.get("ad_group_id"))
        if group is not None and _text(row.get("sponsored_type")) == "sp":
            in_group.setdefault(group, set()).add((_text(row.get("asin")) or "").upper())
    mine = [row for row in in_flight if _is_mine(row, asin)]
    groups = {g for row in mine if (g := _text(row.get("ad_group_id")))}
    campaigns = sorted({c for row in mine if (c := _text(row.get("campaign_id")))})
    shared = {g for g in groups if in_group.get(g, set()) - {asin}}

    now_rows, before_rows = (
        [
            row
            for row in _all_rows(
                port,
                TOOL_PRODUCTS,
                {**_report(window), "profile_id": profile_id, "search_text": asin},
                "ad_id",
            )
            if _is_mine(row, asin)
        ]
        for window in (windows.long, windows.before)
    )
    try:
        now = _sum(now_rows)
        before = _sum(before_rows)
    except _Unreadable as exc:
        raise GoalReadError(
            "GOAL_ASIN_METRICS_UNREADABLE", f"this ASIN's own ad rows are unreadable: {exc}"
        ) from exc
    title, stock = _title_and_stock(mine or now_rows)

    objects: list[AdObject] = []
    unreadable = 0
    if campaigns:
        managed_campaigns = _managed_campaigns(port, profile_id, asin, windows, campaigns)
        for kind, tool, identity, extra in (
            ("keyword", TOOL_KEYWORDS, "keyword_id", {}),
            ("target", TOOL_TARGETS, "target_id", {"with_ring": 0}),
        ):
            params = {"profile_ids": [profile_id], "campaign_id": campaigns, **extra}
            long_rows = _all_rows(port, tool, {**_report(windows.long), **params}, identity)
            short_rows = {
                _text(row.get(identity)): row
                for row in _all_rows(port, tool, {**_report(windows.short), **params}, identity)
            }
            for row in long_rows:
                if _text(row.get("ad_group_id")) not in groups:
                    continue  # 同一活动里别的广告组：不投这个商品
                try:
                    objects.append(
                        _object(kind, identity, row, short_rows, shared, managed_campaigns)
                    )
                except _Unreadable:
                    unreadable += 1
    if unreadable and unreadable > MAX_UNREADABLE_SHARE * (unreadable + len(objects)):
        raise GoalReadError(
            "GOAL_ROWS_UNUSABLE",
            f"{unreadable} of {unreadable + len(objects)} objects were unreadable",
        )
    return GoalView(
        title=title,
        stock=stock,
        groups=len(groups),
        shared_groups=len(shared),
        objects=tuple(objects),
        now=now,
        before=before,
        unreadable=unreadable,
        windows=windows,
    )


def _sum(rows: list[Mapping[str, object]]) -> Evidence:
    total = Evidence()
    for row in rows:
        total = total.plus(_evidence(row))
    return total


def _title_and_stock(rows: list[Mapping[str, object]]) -> tuple[str | None, int | None]:
    """标题取第一个非空的；库存按 SKU 去重后相加（同一 ASIN 可以挂好几个 SKU）。"""
    title = next((t for row in rows if (t := _text(row.get("title")))), None)
    per_sku: dict[str, int] = {}
    for row in rows:
        sku = _text(row.get("sku"))
        quantity = row.get("afn_fulfillable_quantity")
        if sku is not None and isinstance(quantity, int) and not isinstance(quantity, bool):
            per_sku[sku] = quantity
    return title, (sum(per_sku.values()) if per_sku else None)


def _managed_campaigns(
    port: LxReadPort, profile_id: str, asin: str, windows: Windows, campaigns: list[str]
) -> set[str]:
    """哪些活动算在管。在投却没出现在活动报表里的，标记无从读起，也算在管。"""
    rows = _all_rows(
        port,
        TOOL_CAMPAIGNS,
        {**_report(windows.long), "profile_ids": [profile_id], "asin": asin, "state": "enabled"},
        "campaign_id",
    )
    seen = {
        c: _managed(row, _MANAGED_BOOLS) for row in rows if (c := _text(row.get("campaign_id")))
    }
    return {c for c in campaigns if seen.get(c, True)}


def _object(
    kind: str,
    identity: str,
    row: Mapping[str, object],
    short_rows: Mapping[str | None, Mapping[str, object]],
    shared: set[str],
    managed_campaigns: set[str],
) -> AdObject:
    object_id = _text(row.get(identity))
    campaign_id = _text(row.get("campaign_id"))
    ad_group_id = _text(row.get("ad_group_id"))
    if object_id is None or campaign_id is None or ad_group_id is None:
        raise _Unreadable("identity")
    short_row = short_rows.get(object_id)
    if short_row is None:
        raise _Unreadable("missing from the short window")
    if kind == "keyword":
        text = _text(row.get("keyword_text")) or "?"
        match = _text(row.get("match_type")) or "?"
        label = f"{text} [{match}]"
    else:
        label = _text(row.get("targeting_text")) or _text(row.get("exp_value")) or "?"
    return AdObject(
        kind=kind,
        object_id=object_id,
        campaign_id=campaign_id,
        ad_group_id=ad_group_id,
        label=label,
        enabled=_enabled(row),
        bid=_bid(row.get("bid")),
        created=_day(row.get("creation_date")),
        managed=campaign_id in managed_campaigns
        or _managed(row, _MANAGED_BOOLS + _MANAGED_BOOLS_BELOW_CAMPAIGN),
        shared=ad_group_id in shared,
        long=_evidence(row),
        short=_evidence(short_row),
    )
