"""一轮的判断：领星此刻的样子 + 记忆 → 新的记忆、要记下的决定、给报告的事实、一盏灯。纯函数。

只看不动（S1）：这里产出的「降价 / 加价」只进记忆和报告，叫「本来会改」。它照样算作
一次改动（冷却、来回摆动都从这天算），否则影子期里同一份证据会被天天重复建议，
而影子期要验证的正是「真改起来会不会重复用旧证据」。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal

from ads_control_plane.providers.lingxing.goal_objects import AdObject, GoalView, Window
from ads_control_plane.sfw.memory import DecisionRow, Goal, Remembered
from ads_control_plane.strategies.bidding import (
    Aim,
    Decision,
    Evidence,
    Subject,
    Verdict,
    Why,
    decide,
)

#: 有人改过出价，这么多天不碰（DEC-121 的默认冷却）。
HANDS_OFF_DAYS = 14
#: 30 天内方向翻转 2 次，冻结 30 天。
FLIP_WINDOW_DAYS = 30
FLIPS_TO_FREEZE = 2
FREEZE_DAYS = 30
#: 一个商品一轮最多改这么多处，按花费从高到低；其余下一轮再说。
MAX_CHANGES_PER_RUN = 20
#: 默认 ACOS 上限 = 近 14 天 ACOS × 0.9，取整后夹在 10%–40%。
DEFAULT_TARGET_FACTOR = Decimal("0.9")
TARGET_RANGE = (10, 40)
#: 断路器：订单掉一半（前 14 天至少 10 单才算）；花费涨三成（前 14 天花费过门槛才算）。
ORDERS_DROP_MIN_BEFORE = 10
SPEND_JUMP = Decimal("1.3")
#: 转化率掉三成：两段都至少 100 次点击。
CVR_MIN_CLICKS = 100
CVR_DROP = Decimal("0.7")

#: 站点最低出价与币种最小单位。表外的币种按 0.02 / 0.01 算——只看不动时无害；
#: 真改（S2）只对表内币种开放。
_BID_UNITS: dict[str, tuple[Decimal, Decimal]] = {
    "USD": (Decimal("0.02"), Decimal("0.01")),
    "CAD": (Decimal("0.02"), Decimal("0.01")),
    "GBP": (Decimal("0.02"), Decimal("0.01")),
    "EUR": (Decimal("0.02"), Decimal("0.01")),
    "JPY": (Decimal("2"), Decimal("1")),
}
_DEFAULT_UNITS = (Decimal("0.02"), Decimal("0.01"))


@dataclass(frozen=True, kw_only=True)
class Outcome:
    light: str  # green | yellow | red
    alerts: tuple[str, ...]
    remembered: list[Remembered]
    decisions: list[DecisionRow]
    facts: dict[str, object]


def default_target(view: GoalView) -> int | None:
    """近 14 天 ACOS × 0.9，取整，夹在 10–40。这段时间没有销售额就定不出来。"""
    acos = view.now.acos
    if acos is None:
        return None
    percent = int((acos * 100 * DEFAULT_TARGET_FACTOR).to_integral_value())
    return min(max(percent, TARGET_RANGE[0]), TARGET_RANGE[1])


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _evidence(ev: Evidence) -> dict[str, object]:
    return {
        "impressions": ev.impressions,
        "clicks": ev.clicks,
        "orders": ev.orders,
        "spend": str(ev.spend),
        "sales": str(ev.sales),
    }


def _window(window: Window) -> list[str]:
    return [window.start.isoformat(), window.end.isoformat()]


def _alerts(view: GoalView, spend_floor: Decimal | None) -> tuple[str, ...]:
    now, before = view.now, view.before
    found: list[str] = []
    if view.stock == 0:
        found.append("STOCK_OUT")
    if before.orders >= ORDERS_DROP_MIN_BEFORE and now.orders * 2 < before.orders:
        found.append("ORDERS_HALVED")
    if (
        spend_floor is not None
        and before.spend >= spend_floor
        and now.spend > before.spend * SPEND_JUMP
    ):
        found.append("SPEND_JUMPED")
    if (
        now.clicks >= CVR_MIN_CLICKS
        and before.clicks >= CVR_MIN_CLICKS
        and before.orders > 0
        # 整数交叉相乘比较，不经过小数：now.orders/now.clicks < 0.7 × before.orders/before.clicks
        and Decimal(now.orders * before.clicks) < CVR_DROP * before.orders * now.clicks
    ):
        found.append("CVR_DROPPED")
    if view.groups == 0:
        found.append("NO_ADS")
    return tuple(found)


#: 红灯：钱或单出了大问题，要大人看。黄灯：看一眼。
RED_ALERTS = frozenset({"ORDERS_HALVED", "SPEND_JUMPED"})


def judge(
    goal: Goal,
    view: GoalView,
    remembered: dict[tuple[str, str], Remembered],
    *,
    today: date,
    spend_floor: Decimal | None,
) -> Outcome:
    target = goal.target_acos
    min_bid, tick = _BID_UNITS.get(goal.currency, _DEFAULT_UNITS)
    now = view.now
    cpa_cap = (now.sales / now.orders) * target if target is not None and now.orders >= 3 else None
    aim = Aim(
        target_acos=target, cpa_cap=cpa_cap, spend_floor=spend_floor, min_bid=min_bid, tick=tick
    )
    alerts = _alerts(view, spend_floor)
    stock_out = "STOCK_OUT" in alerts

    items: dict[tuple[str, str], Remembered] = {}
    decisions: list[DecisionRow] = []
    human: list[dict[str, object]] = []
    judged: list[tuple[AdObject, Decision]] = []
    holds: Counter[str] = Counter()
    for obj in view.objects:
        key = (obj.kind, obj.object_id)
        item = _seen(obj, remembered.get(key), today)
        before = remembered.get(key)
        if before is not None and obj.bid != before.last_bid:
            # 只看不动期我们一分钱没改，所以出价变了就是别人改的（人，或者领星的规则）。
            # 它也算一次改动：之前的数字是在旧出价下攒的。14 天不碰期满时长窗还够不着改动
            # 之后（14 + 3 > 14），不记这一笔就会拿改价前的旧数据下结论。
            item = replace(
                item,
                hands_off_until=today + timedelta(days=HANDS_OFF_DAYS),
                last_change=today,
            )
            decisions.append(
                DecisionRow(
                    kind=obj.kind,
                    object_id=obj.object_id,
                    by="human",
                    mode="shadow",
                    action="human_change",
                    old_bid=before.last_bid,
                    new_bid=obj.bid,
                    why="BID_CHANGED_ELSEWHERE",
                    evidence=None,
                )
            )
            human.append(
                {
                    "kind": obj.kind,
                    "label": obj.label,
                    "old": _money(before.last_bid),
                    "new": _money(obj.bid),
                }
            )
        items[key] = item
        if stock_out:
            holds["STOCK_OUT"] += 1
            continue
        decision = decide(_subject(obj, item), aim, today)
        if decision.verdict is Verdict.HOLD:
            holds[decision.why.value] += 1
        else:
            judged.append((obj, decision))

    # 一轮最多改 MAX_CHANGES_PER_RUN 处，花费高的先；其余这轮不记，下一轮照样会被判到。
    judged.sort(key=lambda pair: (-(pair[1].evidence or Evidence()).spend, pair[0].object_id))
    later = max(0, len(judged) - MAX_CHANGES_PER_RUN)
    proposals: list[dict[str, object]] = []
    for obj, decision in judged[:MAX_CHANGES_PER_RUN]:
        key = (obj.kind, obj.object_id)
        item, frozen = _after_change(items[key], decision, today)
        items[key] = item
        if frozen:
            holds[Why.FROZEN.value] += 1
            continue
        ev = decision.evidence or Evidence()
        decisions.append(
            DecisionRow(
                kind=obj.kind,
                object_id=obj.object_id,
                by="operator",
                mode="shadow",
                action=decision.verdict.value,
                old_bid=decision.old_bid,
                new_bid=decision.new_bid,
                why=decision.why.value,
                evidence={"days": decision.window_days, **_evidence(ev)},
            )
        )
        proposals.append(
            {
                "kind": obj.kind,
                "label": obj.label,
                "action": decision.verdict.value,
                "old": _money(decision.old_bid),
                "new": _money(decision.new_bid),
                "why": decision.why.value,
                "days": decision.window_days,
                **_evidence(ev),
            }
        )
    if later:
        holds["LATER"] += later

    seen = set(items)
    for key, old in remembered.items():
        if key not in seen:
            items[key] = old if old.gone_at is not None else replace(old, gone_at=today)

    light = (
        "red"
        if RED_ALERTS.intersection(alerts)
        else ("yellow" if alerts or target is None else "green")
    )
    facts: dict[str, object] = {
        "title": view.title,
        "stock": view.stock,
        "groups": view.groups,
        "shared_groups": view.shared_groups,
        "objects": len(view.objects),
        "unreadable": view.unreadable,
        "currency": goal.currency,
        "target": _money(target),
        "windows": {
            "long": _window(view.windows.long),
            "short": _window(view.windows.short),
            "before": _window(view.windows.before),
        },
        "now": _evidence(view.now),
        "before": _evidence(view.before),
        "alerts": list(alerts),
        "proposals": proposals,
        "human": human,
        "holds": dict(sorted(holds.items())),
    }
    return Outcome(
        light=light,
        alerts=alerts,
        remembered=list(items.values()),
        decisions=decisions,
        facts=facts,
    )


def _seen(obj: AdObject, old: Remembered | None, today: date) -> Remembered:
    """把这一轮看到的写进记忆。起点出价（b0）只在第一次看到它有独立出价时定下，以后不动。"""
    if old is None:
        return Remembered(
            kind=obj.kind,
            object_id=obj.object_id,
            campaign_id=obj.campaign_id,
            ad_group_id=obj.ad_group_id,
            label=obj.label,
            start_bid=obj.bid,
            last_bid=obj.bid,
            first_seen=today,
            last_seen=today,
            gone_at=None,
            last_change=None,
            last_direction=None,
            flips=(),
            frozen_until=None,
            hands_off_until=None,
        )
    return replace(
        old,
        campaign_id=obj.campaign_id,
        ad_group_id=obj.ad_group_id,
        label=obj.label,
        start_bid=old.start_bid if old.start_bid is not None else obj.bid,
        last_bid=obj.bid,
        last_seen=today,
        gone_at=None,
    )


def _subject(obj: AdObject, item: Remembered) -> Subject:
    return Subject(
        bid=obj.bid,
        start_bid=item.start_bid,
        created=obj.created,
        enabled=obj.enabled,
        managed=obj.managed,
        shared=obj.shared,
        last_change=item.last_change,
        hands_off_until=item.hands_off_until,
        frozen_until=item.frozen_until,
        long=obj.long,
        short=obj.short,
    )


def _after_change(item: Remembered, decision: Decision, today: date) -> tuple[Remembered, bool]:
    """记下一次（本来会做的）改动。返回 (新记忆, 是否因来回摆而冻结、这次不改)。"""
    direction = -1 if decision.verdict is Verdict.DOWN else 1
    flips = [d for d in item.flips if d > today - timedelta(days=FLIP_WINDOW_DAYS)]
    if item.last_direction is not None and direction != item.last_direction:
        flips.append(today)
    if len(flips) >= FLIPS_TO_FREEZE:
        return (
            replace(item, flips=tuple(flips), frozen_until=today + timedelta(days=FREEZE_DAYS)),
            True,
        )
    return replace(item, flips=tuple(flips), last_change=today, last_direction=direction), False
