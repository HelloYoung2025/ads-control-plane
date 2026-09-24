"""出价规则 v1（strategies/bidding.py）：每一个理由都走一遍，夹紧与取整，窗口选择的边界。

最重的一条（对抗评审）：只用上次改动之后的新证据。所以 pick_window 的边界单独钉死——
差一天，同一份旧证据就会被第二次拿来降价。

第二重的一条（2026-09-24 多智能体评审）：一步最多 −15% / +10%，在任何情况下都成立——
出价已经在 0.6–1.4 倍的边外（人改的）、出价小到取整会吃掉一大截，都不例外。
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


def test_hands_off_ends_on_its_day() -> None:
    hot = ev(clicks=100, orders=5, spend="100", sales="200")
    assert decide(subject(hands_off_until=TODAY, long=hot), AIM, TODAY).verdict is Verdict.DOWN


# ------------------------------------------------------------------ 证据够不够


def test_no_impressions_and_few_clicks() -> None:
    assert decide(subject(long=ev(impressions=0)), AIM, TODAY).why is Why.NO_IMPRESSIONS
    assert decide(subject(long=ev(clicks=9, orders=3, sales="10")), AIM, TODAY).why is (
        Why.FEW_CLICKS
    )


def test_one_or_two_orders_are_noise_until_even_one_more_would_not_pay() -> None:
    """1、2 单时 ACOS 说明不了什么；但花费够「再多出一单也还超一单该花的钱」（CPA 上限 5），
    就是真贵：2 单要花到 15、1 单要花到 10。"""
    two = ev(clicks=60, orders=2, spend="14.99", sales="40")
    assert decide(subject(long=two), AIM, TODAY).why is Why.NOT_ENOUGH
    decision = decide(subject(long=replace(two, spend=Decimal("15.00"))), AIM, TODAY)
    assert decision.verdict is Verdict.DOWN
    assert decision.why is Why.FEW_ORDERS
    assert decision.new_bid == Decimal("0.85")
    one = ev(clicks=300, orders=1, spend="9.99", sales="20")
    assert decide(subject(long=one), AIM, TODAY).why is Why.NOT_ENOUGH
    assert decide(subject(long=replace(one, spend=Decimal("10"))), AIM, TODAY).why is (
        Why.FEW_ORDERS
    )
    # 点击不够 25 次，花再多也不下结论。
    few = ev(clicks=24, orders=1, spend="999", sales="20")
    assert decide(subject(long=few), AIM, TODAY).why is Why.NOT_ENOUGH


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
    # ACOS 28% 对 25%：× 25/28 = 0.8928… → 取整朝旧价那一侧，到 0.90。
    decision = decide(subject(long=ev(clicks=80, orders=6, spend="56", sales="200")), AIM, TODAY)
    assert decision.new_bid == Decimal("0.90")


def test_the_dead_band_holds() -> None:
    # ACOS 27% 在 25% ± 10% 里：不动。
    decision = decide(subject(long=ev(clicks=80, orders=6, spend="54", sales="200")), AIM, TODAY)
    assert decision.why is Why.ON_TARGET


def test_acos_low_suggests_up_by_at_most_ten_percent() -> None:
    decision = decide(subject(long=ev(clicks=80, orders=6, spend="20", sales="200")), AIM, TODAY)
    assert decision.verdict is Verdict.UP
    assert decision.why is Why.ACOS_LOW
    assert decision.new_bid == Decimal("1.10")


def test_orders_and_sales_with_zero_spend_do_not_divide_by_zero() -> None:
    """领星偶尔给「有单、有销售额、花费 0」的行：ACOS 为 0，按最大步长提示，不抛异常。"""
    odd = ev(clicks=20, orders=5, spend="0", sales="100")
    decision = decide(subject(long=odd), AIM, TODAY)
    assert decision.verdict is Verdict.UP
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


YEN = replace(AIM, min_bid=Decimal("2"), tick=Decimal("1"), cpa_cap=Decimal("500"))


def test_yen_rounds_to_whole_yen_toward_the_old_bid() -> None:
    hot = ev(clicks=80, orders=6, spend="6000", sales="20000")
    decision = decide(subject(bid=Decimal("47"), start_bid=Decimal("47"), long=hot), YEN, TODAY)
    # 47 × max(25/30, 0.85) = 39.95 → 40：向上取整，一步不超过 15%。
    assert decision.new_bid == Decimal("40")


def test_rounding_never_makes_a_step_bigger_than_the_limit() -> None:
    """小额出价：向下取整会让一步变成 −20% 以上（$0.07 → $0.05、¥9 → ¥7）。"""
    hot = ev(clicks=80, orders=6, spend="100", sales="200")
    for bid, aim, new in ((Decimal("0.07"), AIM, "0.06"), (Decimal("9"), YEN, "8")):
        decision = decide(subject(bid=bid, start_bid=bid, long=hot), aim, TODAY)
        assert decision.verdict is Verdict.DOWN
        assert decision.new_bid == Decimal(new)
        assert decision.new_bid >= bid * Decimal("0.85")


def test_a_step_smaller_than_one_tick_is_a_hold_not_an_edge() -> None:
    """一步还不到一个最小单位：不假装改了，也不谎称到了 0.6 / 1.4 倍的边。"""
    hot = ev(clicks=80, orders=6, spend="100", sales="200")
    down = decide(subject(bid=Decimal("0.05"), start_bid=Decimal("0.05"), long=hot), AIM, TODAY)
    assert down.verdict is Verdict.HOLD
    assert down.why is Why.NO_STEP
    cheap = ev(clicks=80, orders=6, spend="2000", sales="20000")
    up = decide(subject(bid=Decimal("9"), start_bid=Decimal("9"), long=cheap), YEN, TODAY)
    assert up.why is Why.NO_STEP


def test_at_the_site_minimum_it_is_at_the_floor() -> None:
    hot = ev(clicks=80, orders=6, spend="100", sales="200")
    decision = decide(subject(bid=Decimal("0.02"), start_bid=Decimal("0.02"), long=hot), AIM, TODAY)
    assert decision.why is Why.AT_FLOOR


@pytest.mark.parametrize(
    ("bid", "acos_spend", "verdict", "why", "new"),
    [
        # 人把价改到 3.00（b0 = 1.00）：ACOS 28% 对 25% → × 0.8928… → 2.68，不一步跳回 1.40。
        ("3.00", "56", Verdict.DOWN, Why.ACOS_HIGH, "2.68"),
        # 已经在 1.4 倍上面：加价提示不给，也不会「加价」加成降价。
        ("3.00", "20", Verdict.HOLD, Why.AT_CEILING, None),
        # 人把价改到 0.30：ACOS 10% → 只加一成到 0.33，不一步跳到 0.60。
        ("0.30", "20", Verdict.UP, Why.ACOS_LOW, "0.33"),
        # 已经在 0.6 倍下面：不再往下降。
        ("0.30", "100", Verdict.HOLD, Why.AT_FLOOR, None),
    ],
)
def test_a_bid_outside_the_band_moves_back_one_step_at_a_time(
    bid: str, acos_spend: str, verdict: Verdict, why: Why, new: str | None
) -> None:
    evidence = ev(clicks=80, orders=6, spend=acos_spend, sales="200")
    decision = decide(subject(bid=Decimal(bid), long=evidence), AIM, TODAY)
    assert decision.verdict is verdict
    assert decision.why is why
    assert decision.new_bid == (Decimal(new) if new is not None else None)


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
