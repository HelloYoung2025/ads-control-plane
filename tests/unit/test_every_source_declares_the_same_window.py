"""「统计区间 X 至 Y；最近 N 天不计入」这句话，对每个数据源实现都必须是同一句。

2026-09-07 排查：真实源按 UTC 日历天对齐、右端退 3 天（归因滞后），Mock 却手搓了
另一套——`now - 30d` 到 `now - 1d`，既不对齐日界也不认那 3 天，而且**整个丢掉**
调用方传进来的 lookback_days。后果全在演示上，而演示是这套系统教人「真东西长什么样」
的唯一途径：

- 授权书卡片写「回看 30 天」，证据表写「2026-08-07 至 2026-09-04」——人一数是 29 天；
- 证据表写「最近 2 天不计入」，生产上会是 3 天；
- 人在演示里签一份「回看 7 天」的授权书，证据表照样写 30 天的区间——
  而那个 7 是他刚刚亲手选的。

三条都不会报错、不会变红。下面钉的是算术本身与「两个实现说同一句话」。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ads_control_plane.canonical.entity import (
    AdProduct,
    CanonicalEntityRef,
    EntityType,
    ParentRefs,
    Provider,
)
from ads_control_plane.canonical.ids import new_canonical_id
from ads_control_plane.canonical.money import Money
from ads_control_plane.strategies.negation import SearchTermRecord
from ads_control_plane.strategies.ports import ATTRIBUTION_LAG_DAYS, attribution_window
from tests.support.mock_search_terms import MockSearchTermSource

AS_OF = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def test_the_window_spans_exactly_the_lookback_days() -> None:
    """闭区间天数必须恰好等于 lookback_days——人会去数。"""
    for lookback in (7, 14, 30, 60, 90):
        start, end = attribution_window(lookback_days=lookback, as_of=AS_OF)
        assert (end - start).days == lookback, (
            f"回看 {lookback} 天，算出来的区间却是 {(end - start).days} 天"
        )


def test_the_window_stops_short_by_the_attribution_lag() -> None:
    """右端必须退掉归因滞后那几天，且是排他右端（end 是次日零点）。"""
    _, end = attribution_window(lookback_days=30, as_of=AS_OF)
    last_included = (end - timedelta(days=1)).date()
    assert last_included == AS_OF.date() - timedelta(days=ATTRIBUTION_LAG_DAYS), (
        "窗口右端没有退掉归因滞后的天数——会拿最新、最未结算的几天把正在出单的词判死刑"
    )


def test_the_window_is_pinned_to_utc_midnight() -> None:
    """两端都对齐 UTC 日界：不对齐时「X 至 Y」印出来会少一天。"""
    for lookback in (7, 30):
        for hour in (0, 12, 23):
            moment = AS_OF.replace(hour=hour)
            start, end = attribution_window(lookback_days=lookback, as_of=moment)
            for label, m in (("start", start), ("end", end)):
                assert m.tzinfo is not None, f"{label} 不带时区"
                assert (m.hour, m.minute, m.second, m.microsecond) == (0, 0, 0, 0), (
                    f"{label} 没有对齐日界：{m.isoformat()}"
                )


def _record(org: object, term: str) -> SearchTermRecord:
    return SearchTermRecord(
        scope=CanonicalEntityRef(
            organization_id=org,  # type: ignore[arg-type]
            provider=Provider.MOCK,
            provider_connection_id=new_canonical_id(),
            marketplace="US",
            shop_external_id="shop-1",
            profile_external_id="p-1",
            ad_product=AdProduct.SP,
            entity_type=EntityType.AD_GROUP,
            entity_external_id="ag-1",
            parent_refs=ParentRefs(campaign_external_id="c-1"),
        ),
        search_term=term,
        clicks=30,
        conversions=0,
        spend=Money(amount=Decimal("25.00"), currency="USD"),
        # 故意种一个**错的**窗口：源必须按调用方传进来的 lookback 重新声明它。
        window_start=datetime(2000, 1, 1, tzinfo=UTC),
        window_end=datetime(2000, 1, 2, tzinfo=UTC),
        data_as_of=AS_OF - timedelta(hours=2),
    )


def test_the_mock_declares_the_window_the_caller_asked_for() -> None:
    """Mock 必须按传进来的 lookback_days 现算窗口，不能拿 seed 里那个凑数。

    这条直接对应演示上人看得见的那句话：签「回看 7 天」，证据表就得写 7 天的区间。
    """
    org = new_canonical_id()
    source = MockSearchTermSource()
    source.seed("p-1", [_record(org, "widget holder")])
    for lookback in (7, 30):
        want = attribution_window(lookback_days=lookback, as_of=AS_OF)
        got = source.fetch_search_term_performance("p-1", lookback, AS_OF)
        assert got.records, "Mock 一条都没返回"
        for r in got.records:
            assert (r.window_start, r.window_end) == want, (
                f"回看 {lookback} 天，Mock 声称的区间却是 "
                f"{r.window_start.date()} 至 {r.window_end.date()}"
            )


def test_the_mock_does_not_quietly_reuse_one_window_for_every_lookback() -> None:
    """换一个 lookback 就必须换一个区间——否则上面那条也可能是巧合。"""
    org = new_canonical_id()
    source = MockSearchTermSource()
    source.seed("p-1", [_record(org, "widget holder")])
    seven = source.fetch_search_term_performance("p-1", 7, AS_OF).records[0]
    thirty = source.fetch_search_term_performance("p-1", 30, AS_OF).records[0]
    assert seven.window_start != thirty.window_start, "两个不同的回看天数给出了同一个区间"
    assert seven.window_end == thirty.window_end, "右端由归因滞后决定，不该随回看天数变"
