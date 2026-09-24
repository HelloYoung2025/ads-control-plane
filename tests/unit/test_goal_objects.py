"""一个商品目标每轮从报表还原（providers/lingxing/goal_objects.py）。

全部走 tests/support/fake_lingxing.py：形状照 2026-09-24 的只读实测，ID 全是编的。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal

import pytest

from ads_control_plane.providers.lingxing import goal_objects
from ads_control_plane.providers.lingxing.goal_objects import (
    AdObject,
    GoalReadError,
    GoalView,
    read_goal,
    windows_for,
)
from ads_control_plane.strategies.bidding import Evidence
from tests.support.fake_lingxing import (
    ASIN,
    MISSING,
    PROFILE,
    Ad,
    FakeLingxing,
    Thing,
    TransportDown,
    small_shop,
)

TODAY = date(2026, 9, 24)
W = windows_for(TODAY)
LONG, SHORT, BEFORE = W.long.report_date, W.short.report_date, W.before.report_date


ShopAds = dict[tuple[str, str], list[Mapping[str, object]]]


def read(fake: FakeLingxing, shop_ads: ShopAds | None = None) -> GoalView:
    return read_goal(fake, profile_id=PROFILE, asin=ASIN, today=TODAY, shop_ads=shop_ads)


def by_id(view: GoalView) -> dict[str, AdObject]:
    return {obj.object_id: obj for obj in view.objects}


class Refused(Exception):
    code = "LX_BUSINESS_ERROR"


# ------------------------------------------------------------------ 窗口


def test_windows_leave_three_days_for_orders_to_settle() -> None:
    assert (W.long.start, W.long.end) == (date(2026, 9, 8), date(2026, 9, 21))
    assert (W.short.start, W.short.end) == (date(2026, 9, 15), date(2026, 9, 21))
    assert (W.before.start, W.before.end) == (date(2026, 8, 25), date(2026, 9, 7))
    assert W.before.end + timedelta(days=1) == W.long.start
    assert LONG == "2026-09-08 - 2026-09-21"


# ------------------------------------------------------------------ 找到它的广告


def test_it_finds_every_keyword_and_target_of_the_product() -> None:
    view = read(small_shop())
    objects = by_id(view)
    # kw-4 在别的商品独占的活动里；它不属于这个商品。
    assert set(objects) == {"kw-1", "kw-2", "tg-1", "kw-3"}
    assert view.groups == 3
    assert view.shared_groups == 1
    assert objects["kw-1"].label == "cat scratcher [exact]"
    assert objects["tg-1"].kind == "target"
    assert objects["tg-1"].label == 'asinSameAs="B0RIVAL001"'
    assert objects["kw-1"].bid == Decimal("1.00")
    assert objects["kw-1"].created == date(2026, 1, 1)
    assert objects["kw-1"].enabled
    assert not objects["kw-1"].managed
    assert not objects["kw-1"].shared


def test_a_group_another_product_also_advertises_in_is_shared() -> None:
    objects = by_id(read(small_shop()))
    assert objects["kw-3"].shared
    assert not objects["kw-1"].shared


def test_an_empty_bid_means_it_uses_the_group_default() -> None:
    assert by_id(read(small_shop()))["kw-2"].bid is None


def test_other_groups_in_the_same_campaign_are_not_its_business() -> None:
    fake = small_shop()
    fake.things.append(Thing("keyword", "kw-9", "cmp-1", "ag-9", text="someone else"))
    assert "kw-9" not in by_id(read(fake))


def test_a_paused_ad_takes_its_group_out() -> None:
    fake = small_shop()
    fake.ads[1].state = "paused"  # ad-2 → ag-2 / cmp-2
    view = read(fake)
    assert view.groups == 2
    assert "tg-1" not in by_id(view)


@pytest.mark.parametrize("where", ["campaign", "ad_group"])
def test_an_enabled_ad_in_a_paused_campaign_or_group_is_not_in_flight(where: str) -> None:
    """state=enabled 只筛广告自己的开关（2026-09-24 真实冒烟）：活动或组停了，它就不在投。"""
    fake = small_shop()
    if where == "campaign":
        fake.campaign_state["cmp-2"] = "paused"
    else:
        fake.ads[1].ad_group_state = "paused"
    view = read(fake)
    assert view.groups == 2
    assert "tg-1" not in by_id(view)
    asked = [p["campaign_id"] for t, p in fake.calls if t == "ad_campaign_targeting_report"]
    assert all("cmp-2" not in campaigns for campaigns in asked), "停着的活动不去问"


def test_sponsored_brands_ads_are_not_counted() -> None:
    fake = small_shop()
    for ad in fake.ads[:3]:
        ad.sponsored_type = "sb"
    view = read(fake)
    assert view.groups == 0
    assert view.objects == ()
    tools = [tool for tool, _ in fake.calls]
    assert "ad_campaign_keyword_report" not in tools, "没有在投的组，就不去问关键词"


def test_an_asin_is_matched_whatever_the_case() -> None:
    view = read_goal(small_shop(), profile_id=PROFILE, asin=ASIN.lower(), today=TODAY)
    assert view.groups == 3


def test_paused_or_half_paused_objects_are_not_enabled() -> None:
    fake = small_shop()
    fake.things[0].ad_group_state = "paused"
    assert not by_id(read(fake))["kw-1"].enabled


def test_a_bad_creation_date_is_unknown_not_old() -> None:
    fake = small_shop()
    fake.things[0].created = "yesterday"
    assert by_id(read(fake))["kw-1"].created is None


# ------------------------------------------------------------------ 领星在管


@pytest.mark.parametrize(
    "flags",
    [
        {"is_apply_rule": True},
        {"is_apply_time": True},
        {"is_ad_group_apply_time": True},
        {"is_apply_rule": None},  # 读不出来也算在管
        {"applied_templates": ["tpl-1"]},
        {"ad_group_applied_templates": ["tpl-1"]},
        {"rule_group_uuids": ["rg-1"]},
        {"optimization_rule_id": "rule-1"},
        {"timing_base_value": "1.20"},
        {"is_apply_grab": True},
    ],
)
def test_anything_lingxing_manages_is_marked_managed(flags: dict[str, object]) -> None:
    fake = small_shop()
    fake.things[0].flags = flags
    objects = by_id(read(fake))
    assert objects["kw-1"].managed
    assert not objects["kw-2"].managed, "只标记那一行"


@pytest.mark.parametrize("value", ["maybe", MISSING])
def test_an_unreadable_or_missing_row_flag_counts_as_managed(value: object) -> None:
    fake = small_shop()
    fake.things[0].flags = {"is_apply_time": value}
    assert by_id(read(fake))["kw-1"].managed


def test_an_empty_grab_flag_on_a_target_is_not_managed() -> None:
    """投放行上 is_apply_grab 恒为空（S0）：它可有可无，只认 True。"""
    assert not by_id(read(small_shop()))["tg-1"].managed


def test_a_managed_campaign_marks_everything_under_it() -> None:
    fake = small_shop()
    fake.campaign_flags["cmp-1"] = {"step_budget_template_uuid": "sb-1"}
    objects = by_id(read(fake))
    assert objects["kw-1"].managed and objects["kw-2"].managed
    assert not objects["tg-1"].managed


def test_a_campaign_missing_from_the_campaign_report_counts_as_managed() -> None:
    fake = small_shop()
    fake.campaign_report_omits.add("cmp-2")  # 在投，但活动报表没给它的行：标记无从读起
    assert by_id(read(fake))["tg-1"].managed


# ------------------------------------------------------------------ 数字


def test_evidence_comes_from_each_window_and_the_summary_row_is_ignored() -> None:
    fake = small_shop()
    fake.set(LONG, "kw-1", impressions=900, clicks=40, orders=4, spend="30.50", sales="120.00")
    fake.set(SHORT, "kw-1", impressions=300, clicks=15, orders=1, spend="11.00", sales="30.00")
    fake.set(LONG, "ad-1", clicks=50, orders=5, spend="40.00", sales="150.00")
    fake.set(LONG, "ad-2", clicks=10, orders=1, spend="5.00", sales="30.00")
    fake.set(LONG, "ad-4", clicks=99, orders=9, spend="99.00", sales="999.00")  # 别的商品
    fake.set(BEFORE, "ad-1", clicks=60, orders=8, spend="45.00", sales="240.00")
    view = read(fake)
    kw1 = by_id(view)["kw-1"]
    assert kw1.long == Evidence(
        impressions=900, clicks=40, orders=4, spend=Decimal("30.50"), sales=Decimal("120.00")
    )
    assert kw1.short.clicks == 15
    # 汇总行（身份为空）的 orders=999 没被加进来；别的商品的广告也没有。
    assert view.now.orders == 6
    assert view.now.spend == Decimal("45.00")
    assert view.before.orders == 8


def test_title_and_stock_come_from_the_product_report() -> None:
    fake = small_shop()
    fake.ads.append(Ad("ad-6", ASIN, "cmp-1", "ag-1", sku="SKU-1B", stock=7))
    view = read(fake)
    assert view.title == "Cat scratcher <b>deluxe</b>"
    # 同一个 SKU 挂在三个广告上只算一次；两个 SKU 相加。
    assert view.stock == 107


def test_unknown_stock_is_not_zero() -> None:
    fake = small_shop()
    for ad in fake.ads:
        ad.stock = None
    assert read(fake).stock is None


def test_this_asins_own_numbers_must_be_readable() -> None:
    fake = small_shop()
    fake.set(LONG, "ad-1", clicks=5, spend=None)
    with pytest.raises(GoalReadError) as caught:
        read(fake)
    assert caught.value.code == "GOAL_ASIN_METRICS_UNREADABLE"


def test_a_few_unreadable_objects_are_skipped_and_counted() -> None:
    fake = small_shop()
    fake.set(LONG, "kw-1", clicks="lots")
    view = read(fake)
    assert view.unreadable == 1
    assert "kw-1" not in by_id(view)


def test_mostly_unreadable_objects_stop_the_whole_product() -> None:
    fake = small_shop()
    for object_id in ("kw-1", "kw-2", "tg-1"):
        fake.set(LONG, object_id, spend="-1")
    with pytest.raises(GoalReadError) as caught:
        read(fake)
    assert caught.value.code == "GOAL_ROWS_UNUSABLE"


def test_a_zero_bid_is_unreadable_not_free() -> None:
    fake = small_shop()
    fake.things[0].bid = "0"
    assert read(fake).unreadable == 1


# ------------------------------------------------------------------ 翻页与完整性


def test_it_pages_to_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(goal_objects, "PAGE_SIZE", 2)
    view = read(small_shop())
    assert set(by_id(view)) == {"kw-1", "kw-2", "tg-1", "kw-3"}


def test_too_many_pages_is_an_error_not_a_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(goal_objects, "PAGE_SIZE", 1)
    monkeypatch.setattr(goal_objects, "MAX_PAGES", 2)
    with pytest.raises(GoalReadError) as caught:
        read(small_shop())
    assert caught.value.code == "GOAL_TOO_MANY_PAGES"


def test_rows_short_of_the_total_is_an_error() -> None:
    fake = small_shop()
    fake.total_bonus = 1
    with pytest.raises(GoalReadError) as caught:
        read(fake)
    assert caught.value.code == "GOAL_PAGES_SHORT"


def test_a_missing_total_is_an_error() -> None:
    fake = small_shop()
    fake.hide_total = True
    with pytest.raises(GoalReadError) as caught:
        read(fake)
    assert caught.value.code == "GOAL_TOTAL_ABSENT"


def test_a_dropped_connection_is_asked_again() -> None:
    fake = small_shop()
    fake.failures["ad_campaign_keyword_report"] = [TransportDown(), TransportDown()]
    assert "kw-1" in by_id(read(fake))


def test_three_dropped_connections_give_up_with_the_upstream_code() -> None:
    fake = small_shop()
    fake.failures["ad_campaign_keyword_report"] = [TransportDown() for _ in range(3)]
    with pytest.raises(GoalReadError) as caught:
        read(fake)
    assert caught.value.code == "LX_TRANSPORT_ERROR"


def test_a_refusal_is_not_asked_again() -> None:
    fake = small_shop()
    fake.failures["ad_campaign_report"] = [Refused("no")]
    with pytest.raises(GoalReadError) as caught:
        read(fake)
    assert caught.value.code == "LX_BUSINESS_ERROR"
    assert [tool for tool, _ in fake.calls].count("ad_campaign_report") == 1


# ------------------------------------------------------------------ 调用


def test_one_product_costs_eight_read_calls_and_no_writes() -> None:
    fake = small_shop()
    read(fake)
    assert len(fake.calls) == 8
    assert {params["report_date"] for _, params in fake.calls} == {LONG, SHORT, BEFORE}


def test_products_in_the_same_shop_share_the_shop_wide_ad_list() -> None:
    fake = small_shop()
    shop_ads: ShopAds = {}
    read(fake, shop_ads)
    read_goal(fake, profile_id=PROFILE, asin="B0OTHER001", today=TODAY, shop_ads=shop_ads)
    enabled_lists = [p for t, p in fake.calls if t == "ad_campaign_product_report" and "state" in p]
    assert len(enabled_lists) == 1
