"""出价规则 v1：一个关键词或投放，这一轮该不该动出价、动多少。纯函数，不碰网络、不碰记忆。

阶段裁决（2026-09-24 多智能体设计定稿，计划第四节）：S1 只把结论记成「本来会改」，
一分钱不动；S2 才执行，而且只执行降价。所以本模块给出的「加价」永远只是建议。

最重的一条来自对抗评审：**只用上次改动之后的新证据**。「冷却 7 天 + 固定回看 14 天」
会让同一份旧证据被连用几次，出价一路复利往下压。这里用两个固定窗口实现它：
上次改动（含只看不动期的「本来会改」）落在窗口起点之前，才用那个窗口；
两个窗口都覆盖到改动那天，就是冷却中，不判。窗口右端统一退 ATTRIBUTION_LAG_DAYS 天。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

from ads_control_plane.strategies.ports import ATTRIBUTION_LAG_DAYS

#: 长窗 14 天、短窗 7 天。对象上次改动后攒满 7 天新数据才用短窗，攒满 14 天用长窗。
LONG_DAYS = 14
SHORT_DAYS = 7

#: 窗内点击少于这个数，什么都不判（NegationParameterPack 的点击下限同为 10）。
MIN_CLICKS = 10
#: 按 ACOS 调价至少要这么多单：1、2 单时 ACOS 是噪声。
MIN_ORDERS = 3
#: 0 单也降价：点击要够多，花费还要超过「一单该花的钱」。
NO_ORDER_CLICKS = 25
#: 死区 ±10%：ACOS 离上限这么近就不动，免得在上限附近来回摆。
DEAD_BAND = Decimal("0.10")
#: 一次最多降 15%、最多加 10%（旧 docs/ad-control-plan.md §3.2 与业内常见做法）。
MAX_STEP_DOWN = Decimal("0.85")
MAX_STEP_UP = Decimal("1.10")
#: 出价永远夹在第一次看到它时的 0.6 到 1.4 倍之间：到边就停，报给大人。
FLOOR_OF_START = Decimal("0.6")
CEILING_OF_START = Decimal("1.4")
#: 首次出现不满这么多天的对象不判：长窗 + 归因滞后都要覆盖到它出生之后。
NEW_OBJECT_DAYS = LONG_DAYS + ATTRIBUTION_LAG_DAYS


@dataclass(frozen=True, kw_only=True)
class Evidence:
    """一个窗口里的五个数。金额是 Decimal（AX-01），币种由调用方负责一致。"""

    impressions: int = 0
    clicks: int = 0
    orders: int = 0
    spend: Decimal = Decimal("0")
    sales: Decimal = Decimal("0")

    def plus(self, other: Evidence) -> Evidence:
        return Evidence(
            impressions=self.impressions + other.impressions,
            clicks=self.clicks + other.clicks,
            orders=self.orders + other.orders,
            spend=self.spend + other.spend,
            sales=self.sales + other.sales,
        )

    @property
    def acos(self) -> Decimal | None:
        return self.spend / self.sales if self.sales > 0 else None


@dataclass(frozen=True, kw_only=True)
class Subject:
    """一个对象此刻的样子（来自报表）加上记忆里关于它的几件事。"""

    bid: Decimal | None  # None = 继承广告组默认价
    start_bid: Decimal | None  # 第一次看到它有独立出价时的那个价（b0）
    created: date | None
    enabled: bool  # 自己、广告组、活动三层都在投
    managed: bool  # 领星规则 / 分时 / 模板在管（读不出标记也算在管）
    shared: bool  # 所在广告组里还有别的 ASIN 在投
    last_change: date | None  # 上次改动（含只看不动期的「本来会改」）
    hands_off_until: date | None  # 有人改过，这天之前不碰
    frozen_until: date | None  # 来回摆，这天之前不碰
    long: Evidence  # 长窗
    short: Evidence  # 短窗


@dataclass(frozen=True, kw_only=True)
class Aim:
    """商品目标给的尺子。"""

    target_acos: Decimal | None  # 0.25 = 25%；None = 还没定
    cpa_cap: Decimal | None  # 一单最多花多少（该 ASIN 客单价 × 上限）；单数不够时 None
    spend_floor: Decimal | None  # cpa_cap 没有时，0 单降价用的花费门槛
    min_bid: Decimal  # 站点最低出价
    tick: Decimal  # 币种最小单位


class Verdict(StrEnum):
    DOWN = "down"
    UP = "up"
    HOLD = "hold"


class Why(StrEnum):
    """结论的理由。每一项在报告里对应一句人话（sfw/diary.py）。"""

    PAUSED = "PAUSED"
    MANAGED = "MANAGED"
    SHARED = "SHARED"
    INHERITED = "INHERITED"
    NEW = "NEW"
    HANDS_OFF = "HANDS_OFF"
    FROZEN = "FROZEN"
    COOLING = "COOLING"
    NO_IMPRESSIONS = "NO_IMPRESSIONS"
    FEW_CLICKS = "FEW_CLICKS"
    NOT_ENOUGH = "NOT_ENOUGH"
    ON_TARGET = "ON_TARGET"
    NO_TARGET = "NO_TARGET"
    ACOS_HIGH = "ACOS_HIGH"
    NO_ORDERS = "NO_ORDERS"
    ACOS_LOW = "ACOS_LOW"
    AT_FLOOR = "AT_FLOOR"
    AT_CEILING = "AT_CEILING"


@dataclass(frozen=True, kw_only=True)
class Decision:
    verdict: Verdict
    why: Why
    old_bid: Decimal | None = None
    new_bid: Decimal | None = None
    window_days: int | None = None
    evidence: Evidence | None = None


def _hold(
    why: Why, subject: Subject, *, days: int | None = None, ev: Evidence | None = None
) -> Decision:
    return Decision(
        verdict=Verdict.HOLD, why=why, old_bid=subject.bid, window_days=days, evidence=ev
    )


def pick_window(last_change: date | None, today: date) -> int | None:
    """用哪个窗口：14、7，或 None（冷却中）。

    窗口 [today-lag-(n-1), today-lag] 的起点必须晚于上次改动那天，证据才全是改动之后的。
    """
    end = today - timedelta(days=ATTRIBUTION_LAG_DAYS)
    if last_change is None or last_change < end - timedelta(days=LONG_DAYS - 1):
        return LONG_DAYS
    if last_change < end - timedelta(days=SHORT_DAYS - 1):
        return SHORT_DAYS
    return None


def _clamp(new: Decimal, subject: Subject, aim: Aim) -> Decimal:
    start = subject.start_bid if subject.start_bid is not None else subject.bid
    assert start is not None and subject.bid is not None
    low = max(start * FLOOR_OF_START, aim.min_bid)
    high = start * CEILING_OF_START
    bounded = min(max(new, low), high)
    # 向下取到币种最小单位：降价时多降一分，加价时少加一分，都是保守的那一侧。
    return (bounded / aim.tick).to_integral_value(rounding=ROUND_DOWN) * aim.tick


def decide(subject: Subject, aim: Aim, today: date) -> Decision:
    """一个对象这一轮的结论。判定顺序即优先级：先问能不能碰，再问证据够不够，最后才算数。"""
    if not subject.enabled:
        return _hold(Why.PAUSED, subject)
    if subject.managed:
        return _hold(Why.MANAGED, subject)
    if subject.shared:
        return _hold(Why.SHARED, subject)
    if subject.bid is None:
        # 继承组默认价的词：写一次独立出价，它就永远脱离了组默认价。
        return _hold(Why.INHERITED, subject)
    if subject.created is None or subject.created > today - timedelta(days=NEW_OBJECT_DAYS):
        return _hold(Why.NEW, subject)
    if subject.hands_off_until is not None and today < subject.hands_off_until:
        return _hold(Why.HANDS_OFF, subject)
    if subject.frozen_until is not None and today < subject.frozen_until:
        return _hold(Why.FROZEN, subject)
    days = pick_window(subject.last_change, today)
    if days is None:
        return _hold(Why.COOLING, subject)
    ev = subject.long if days == LONG_DAYS else subject.short
    if ev.impressions == 0:
        return _hold(Why.NO_IMPRESSIONS, subject, days=days, ev=ev)
    if ev.clicks < MIN_CLICKS:
        return _hold(Why.FEW_CLICKS, subject, days=days, ev=ev)
    target = aim.target_acos
    acos = ev.acos
    bid = subject.bid
    if target is not None and ev.orders >= MIN_ORDERS and acos is not None:
        if acos > target * (1 + DEAD_BAND):
            return _move(
                Verdict.DOWN,
                Why.ACOS_HIGH,
                bid * max(target / acos, MAX_STEP_DOWN),
                subject,
                aim,
                days,
                ev,
            )
        if acos < target * (1 - DEAD_BAND):
            return _move(
                Verdict.UP,
                Why.ACOS_LOW,
                bid * min(target / acos, MAX_STEP_UP),
                subject,
                aim,
                days,
                ev,
            )
        return _hold(Why.ON_TARGET, subject, days=days, ev=ev)
    if ev.orders == 0 and ev.clicks >= NO_ORDER_CLICKS:
        cap = aim.cpa_cap if aim.cpa_cap is not None else aim.spend_floor
        if cap is not None and ev.spend >= cap:
            return _move(Verdict.DOWN, Why.NO_ORDERS, bid * MAX_STEP_DOWN, subject, aim, days, ev)
    if target is None and ev.orders >= MIN_ORDERS:
        return _hold(Why.NO_TARGET, subject, days=days, ev=ev)
    return _hold(Why.NOT_ENOUGH, subject, days=days, ev=ev)


def _move(
    verdict: Verdict,
    why: Why,
    raw: Decimal,
    subject: Subject,
    aim: Aim,
    days: int,
    ev: Evidence,
) -> Decision:
    new = _clamp(raw, subject, aim)
    old = subject.bid
    assert old is not None
    if (verdict is Verdict.DOWN and new >= old) or (verdict is Verdict.UP and new <= old):
        # 夹紧或取整之后动不了：到边了。不假装改了一分钱。
        edge = Why.AT_FLOOR if verdict is Verdict.DOWN else Why.AT_CEILING
        return _hold(edge, subject, days=days, ev=ev)
    return Decision(
        verdict=verdict, why=why, old_bid=old, new_bid=new, window_days=days, evidence=ev
    )
