"""出价规则 v1（strategies/bidding.py）：每一个理由都走一遍，夹紧与取整，窗口选择的边界。

最重的一条（对抗评审）：只用上次改动之后的新证据。所以 pick_window 的边界单独钉死——
差一天，同一份旧证据就会被第二次拿来降价。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from ads_control_plane.strategies.bidding import (
    LONG_DAYS,
    NEW_OBJECT_DAYS,
    SHORT_DAYS,
    Aim,
    Evidence,
    Subject,
    Verdict,
    Why,
    decide,
    pick_window,
)
from ads_control_plane.strategies.ports import ATTRIBUTION_LAG_DAYS

TODAY = date(2026, 9, 24)
#: 长窗 [09-08, 09-21]，短窗 [09-15, 09-21]（右端退 3 天）。
LONG_START = date(2026, 9, 8)
SHORT_START = date(2026, 9, 15)


def ev(
    clicks: int = 0,
    orders: int = 0,
    spend: str = "0",
    sales: str = "0",
    impressions: int | None = None,
) -> Evidence:
    return Evidence(
        impressions=impressions if impressions is not None else max(clicks * 20, 0),
        clicks=clicks,
        orders=orders,
        spend=Decimal(spend),
        sales=Decimal(sales),
    )


def subject(**changes: object) -> Subject:
    base = Subject(
        bid=Decimal("1.00"),
        start_bid=Decimal("1.00"),
        created=date(2026, 1, 1),
        enabled=True,
        managed=False,
        shared=False,
        last_change=None,
        hands_off_until=None,
        frozen_until=None,
        long=ev(),
        short=ev(),
    )
    return replace(base, **changes)


AIM = Aim(
    target_acos=Decimal("0.25"),
    cpa_cap=Decimal("5.00"),
    spend_floor=Decimal("20.00"),
    min_bid=Decimal("0.02"),
    tick=Decimal("0.01"),
)


# ------------------------------------------------------------------ 窗口


def test_windows_start_the_day_after_the_last_change() -> None:
    assert (TODAY - LONG_START).days == ATTRIBUTION_LAG_DAYS + LONG_DAYS - 1
    assert pick_window(None, TODAY) == LONG_DAYS
    # 改动那天比长窗起点早一天：长窗全是改动之后的数据。
    assert pick_window(LONG_START - timedelta(days=1), TODAY) == LONG_DAYS
    # 改动落在长窗起点当天：长窗含改动当天，不能用；短窗可以。
    assert pick_window(LONG_START, TODAY) == SHORT_DAYS
    assert pick_window(SHORT_START - timedelta(days=1), TODAY) == SHORT_DAYS
    # 改动落在短窗里：两个窗口都沾到了改动之前的数据 → 冷却。
    assert pick_window(SHORT_START, TODAY) is None
    assert pick_window(TODAY, TODAY) is None


def test_a_change_waits_for_seven_full_days_of_fresh_data() -> None:
    """改了之后，第 10 天（7 天短窗 + 3 天归因）起才有干净的短窗。"""
    changed = TODAY
    waited = [
        pick_window(changed, changed + timedelta(days=d)) for d in range(0, 2 * LONG_DAYS + 5)
    ]
    first_short = waited.index(SHORT_DAYS)
    first_long = waited.index(LONG_DAYS)
    assert first_short == SHORT_DAYS + ATTRIBUTION_LAG_DAYS
    assert first_long == LONG_DAYS + ATTRIBUTION_LAG_DAYS
    assert all(w is None for w in waited[:first_short])


# ------------------------------------------------------------------ 能不能碰


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"enabled": False}, Why.PAUSED),
        ({"managed": True}, Why.MANAGED),
        ({"shared": True}, Why.SHARED),
        ({"bid": None}, Why.INHERITED),
        ({"created": None}, Why.NEW),
        ({"created": TODAY - timedelta(days=NEW_OBJECT_DAYS - 1)}, Why.NEW),
        ({"hands_off_until": TODAY + timedelta(days=1)}, Why.HANDS_OFF),
        ({"frozen_until": TODAY + timedelta(days=1)}, Why.FROZEN),
        ({"last_change": SHORT_START}, Why.COOLING),
    ],
)
def test_things_it_must_not_touch(changes: dict[str, object], why: Why) -> None:
    # 证据本来足以降价：确认是「不能碰」挡住的，不是证据不够。
    hot = ev(clicks=100, orders=5, spend="100", sales="200")
    decision = decide(subject(long=hot, short=hot, **changes), AIM, TODAY)
    assert decision.verdict is Verdict.HOLD
    assert decision.why is why
    assert decision.new_bid is None


def test_the_first_reason_wins() -> None:
    """同时满足好几条时，报的是最先挡住它的那条：暂停比在管先说。"""
    decision = decide(subject(enabled=False, managed=True, shared=True), AIM, TODAY)
    assert decision.why is Why.PAUSED


def test_an_object_old_enough_is_judged() -> None:
    hot = ev(clicks=100, orders=5, spend="100", sales="200")
    old_enough = TODAY - timedelta(days=NEW_OBJECT_DAYS)
    decision = decide(subject(created=old_enough, long=hot), AIM, TODAY)
    assert decision.verdict is Verdict.DOWN


def test_hands_off_and_frozen_end_on_their_day() -> None:
    hot = ev(clicks=100, orders=5, spend="100", sales="200")
    assert decide(subject(hands_off_until=TODAY, long=hot), AIM, TODAY).verdict is Verdict.DOWN
    assert decide(subject(frozen_until=TODAY, long=hot), AIM, TODAY).verdict is Verdict.DOWN


# ------------------------------------------------------------------ 证据够不够


def test_no_impressions_and_few_clicks() -> None:
    assert decide(subject(long=ev(impressions=0)), AIM, TODAY).why is Why.NO_IMPRESSIONS
    assert decide(subject(long=ev(clicks=9, orders=3, sales="10")), AIM, TODAY).why is (
        Why.FEW_CLICKS
    )


def test_one_or_two_orders_are_noise() -> None:
    decision = decide(subject(long=ev(clicks=60, orders=2, spend="50", sales="40")), AIM, TODAY)
    assert decision.verdict is Verdict.HOLD
    assert decision.why is Why.NOT_ENOUGH


def test_without_a_target_it_only_says_so() -> None:
    aim = replace(AIM, target_acos=None, cpa_cap=None)
    decision = decide(subject(long=ev(clicks=60, orders=5, spend="80", sales="100")), aim, TODAY)
    assert decision.why is Why.NO_TARGET


# ------------------------------------------------------------------ 算数


def test_acos_high_steps_down_toward_the_target() -> None:
    # ACOS 30% 对上限 25%：× 25/30 = 0.8333 → 0.83。比 0.85 的最大步长更狠，受步长限制。
    decision = decide(subject(long=ev(clicks=80, orders=6, spend="60", sales="200")), AIM, TODAY)
    assert decision.verdict is Verdict.DOWN
    assert decision.why is Why.ACOS_HIGH
    assert decision.old_bid == Decimal("1.00")
    assert decision.new_bid == Decimal("0.85")
    assert decision.window_days == LONG_DAYS


def test_a_small_overshoot_moves_less_than_the_max_step() -> None:
    # ACOS 28% 对 25%：× 25/28 = 0.8928… → 取整到 0.89（向下，保守）。
    decision = decide(subject(long=ev(clicks=80, orders=6, spend="56", sales="200")), AIM, TODAY)
    assert decision.new_bid == Decimal("0.89")


def test_the_dead_band_holds() -> None:
    # ACOS 27% 在 25% ± 10% 里：不动。
    decision = decide(subject(long=ev(clicks=80, orders=6, spend="54", sales="200")), AIM, TODAY)
    assert decision.why is Why.ON_TARGET


def test_acos_low_suggests_up_by_at_most_ten_percent() -> None:
    decision = decide(subject(long=ev(clicks=80, orders=6, spend="20", sales="200")), AIM, TODAY)
    assert decision.verdict is Verdict.UP
    assert decision.why is Why.ACOS_LOW
    assert decision.new_bid == Decimal("1.10")


def test_no_orders_steps_down_only_after_spending_an_orders_worth() -> None:
    poor = ev(clicks=30, orders=0, spend="4.99")
    assert decide(subject(long=poor), AIM, TODAY).why is Why.NOT_ENOUGH
    decision = decide(subject(long=replace(poor, spend=Decimal("5.00"))), AIM, TODAY)
    assert decision.verdict is Verdict.DOWN
    assert decision.why is Why.NO_ORDERS
    assert decision.new_bid == Decimal("0.85")


def test_no_orders_falls_back_to_the_config_floor_without_a_cpa() -> None:
    aim = replace(AIM, cpa_cap=None)
    assert decide(subject(long=ev(clicks=30, spend="19.99")), aim, TODAY).why is Why.NOT_ENOUGH
    assert decide(subject(long=ev(clicks=30, spend="20.00")), aim, TODAY).why is Why.NO_ORDERS
    no_floor = replace(aim, spend_floor=None)
    assert decide(subject(long=ev(clicks=30, spend="999")), no_floor, TODAY).why is Why.NOT_ENOUGH


def test_twenty_four_clicks_is_not_enough_to_cut_a_zero_order_word() -> None:
    assert decide(subject(long=ev(clicks=24, spend="99")), AIM, TODAY).why is Why.NOT_ENOUGH


def test_the_short_window_is_used_after_a_recent_change() -> None:
    long = ev(clicks=80, orders=6, spend="20", sales="200")  # 这份会让它加价
    short = ev(clicks=40, orders=3, spend="30", sales="100")  # 这份会让它降价
    decision = decide(subject(long=long, short=short, last_change=LONG_START), AIM, TODAY)
    assert decision.window_days == SHORT_DAYS
    assert decision.verdict is Verdict.DOWN
    assert decision.evidence == short


# ------------------------------------------------------------------ 夹紧


def test_never_below_six_tenths_of_the_start_bid() -> None:
    hot = ev(clicks=80, orders=6, spend="100", sales="200")
    decision = decide(subject(bid=Decimal("0.65"), long=hot), AIM, TODAY)
    assert decision.new_bid == Decimal("0.60")
    at_floor = decide(subject(bid=Decimal("0.60"), long=hot), AIM, TODAY)
    assert at_floor.verdict is Verdict.HOLD
    assert at_floor.why is Why.AT_FLOOR


def test_never_above_one_point_four_of_the_start_bid() -> None:
    cheap = ev(clicks=80, orders=6, spend="20", sales="200")
    decision = decide(subject(bid=Decimal("1.35"), long=cheap), AIM, TODAY)
    assert decision.new_bid == Decimal("1.40")
    at_ceiling = decide(subject(bid=Decimal("1.40"), long=cheap), AIM, TODAY)
    assert at_ceiling.why is Why.AT_CEILING


def test_the_site_minimum_wins_over_the_start_bid_floor() -> None:
    hot = ev(clicks=80, orders=6, spend="100", sales="200")
    fine = replace(AIM, tick=Decimal("0.001"))
    decision = decide(
        subject(bid=Decimal("0.022"), start_bid=Decimal("0.022"), long=hot), fine, TODAY
    )
    # 0.022 × 0.85 = 0.0187：高过起点价的 6 成（0.0132），低于站点最低 0.02 → 夹到 0.02。
    assert decision.new_bid == Decimal("0.020")


def test_yen_rounds_down_to_whole_yen() -> None:
    aim = replace(AIM, min_bid=Decimal("2"), tick=Decimal("1"), cpa_cap=Decimal("500"))
    hot = ev(clicks=80, orders=6, spend="6000", sales="20000")
    decision = decide(subject(bid=Decimal("47"), start_bid=Decimal("47"), long=hot), aim, TODAY)
    # 47 × max(25/30, 0.85) = 39.95 → 39。
    assert decision.new_bid == Decimal("39")


def test_rounding_that_leaves_the_bid_unchanged_is_a_hold() -> None:
    """0.02 × 0.85 取整后还是 0.02：不假装改了一分钱。"""
    hot = ev(clicks=80, orders=6, spend="100", sales="200")
    decision = decide(subject(bid=Decimal("0.02"), start_bid=Decimal("0.02"), long=hot), AIM, TODAY)
    assert decision.why is Why.AT_FLOOR


def test_an_old_start_bid_keeps_its_bounds_after_many_steps() -> None:
    """起点价 b0 定下后不再变：一路降下去，最低就是 0.6·b0，不会以当前价为基准复利。"""
    hot = ev(clicks=80, orders=6, spend="100", sales="200")
    bid = Decimal("1.00")
    for _ in range(10):
        decision = decide(subject(bid=bid, start_bid=Decimal("1.00"), long=hot), AIM, TODAY)
        if decision.verdict is Verdict.HOLD:
            break
        assert decision.new_bid is not None
        bid = decision.new_bid
    assert bid == Decimal("0.60")
