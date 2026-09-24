"""一轮的判断：领星此刻的样子 + 记忆 → 新的记忆、要记下的决定、给报告的事实、一盏灯。纯函数。

只看不动（S1）：这里产出的「降价」只进记忆和报告，叫「本来会改」。它照样算作一次改动
（冷却从这天算），否则影子期里同一份证据会被天天重复建议，而影子期要验证的正是
「真改起来会不会重复用旧证据」。

「加价」只是提示：S2 也只执行降价，影子期要照着 S2 的样子演，所以加价不算改动、
不进决定账，每一轮按当下的数重新算一遍。也因此不会「一降一加来回摆」，不需要冻结。
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
#: 一个商品一轮最多改这么多处，按日均花费从高到低；其余下一轮再说。加价提示同样只列这么多。
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


def alerts_of(view: GoalView, spend_floor: Decimal | None) -> tuple[str, ...]:
    """从整个商品的数看出来的事。广告全停了（NO_ADS），订单少、花费变、转化掉都只是它的
    后果，不另报：头条要说原因，不说后果。"""
    now, before = view.now, view.before
    found: list[str] = []
    if view.stock == 0:
        found.append("STOCK_OUT")
    if view.groups == 0:
        return (*found, "NO_ADS")
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
    return tuple(found)


#: 红灯：钱或单出了大问题，要大人看。黄灯：看一眼。
RED_ALERTS = frozenset({"ORDERS_HALVED", "SPEND_JUMPED"})
#: 这几种「不碰」是对象本身的样子，不是证据的事：全是这几种，这个商品它一处也调不了。
UNTOUCHABLE = frozenset(
    {Why.PAUSED.value, Why.MANAGED.value, Why.SHARED.value, Why.INHERITED.value, Why.NEW.value}
)


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
    alerts = alerts_of(view, spend_floor)
    stock_out = "STOCK_OUT" in alerts

    items: dict[tuple[str, str], Remembered] = {}
    decisions: list[DecisionRow] = []
    human: list[dict[str, object]] = []
    applied: list[dict[str, object]] = []
    judged: list[tuple[AdObject, Decision]] = []
    holds: Counter[str] = Counter()
    first_look = not remembered
    for obj in view.objects:
        key = (obj.kind, obj.object_id)
        before = remembered.get(key)
        item = _seen(obj, before, today, first_look=first_look)
        # 领星在管的（分时调价），出价本来就一天几变，不算有人改；管理刚撤掉的那一轮，
        # _seen 已经把它记成一次改动。
        if (
            before is not None
            and not obj.managed
            and not before.managed
            and obj.bid != before.last_bid
        ):
            row: dict[str, object] = {
                "kind": obj.kind,
                "label": obj.label,
                "old": _money(before.last_bid),
                "new": _money(obj.bid),
            }
            if before.proposed_bid is not None and obj.bid == before.proposed_bid:
                # 大人照「本来会改」在领星里改了：不是跟它唱反调，不用躲开 14 天；
                # 只从这天起重新攒数。
                item = replace(item, last_change=today, proposed_bid=None)
                why = "APPLIED_SUGGESTION"
                applied.append(row)
            else:
                # 出价变了、又不是照它的建议改的，就是别人改的（人，或者领星的规则）。
                # 它也算一次改动：之前的数字是在旧出价下攒的。14 天不碰期满时长窗还够不着
                # 改动之后（14 + 3 > 14），不记这一笔就会拿改价前的旧数据下结论。
                item = replace(
                    item,
                    hands_off_until=today + timedelta(days=HANDS_OFF_DAYS),
                    last_change=today,
                    proposed_bid=None,
                )
                why = "BID_CHANGED_ELSEWHERE"
                human.append(row)
            decisions.append(
                DecisionRow(
                    kind=obj.kind,
                    object_id=obj.object_id,
                    by="human",
                    mode="shadow",
                    action="human_change",
                    old_bid=before.last_bid,
                    new_bid=obj.bid,
                    why=why,
                    evidence=None,
                )
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

    # 一轮最多改 MAX_CHANGES_PER_RUN 处，日均花费高的先（长窗短窗天数不同，比总数不公平）；
    # 其余这轮不记，下一轮照样会被判到。
    judged.sort(key=lambda pair: (-_daily_spend(pair[1]), pair[0].object_id))
    proposals: list[dict[str, object]] = []
    hints: list[dict[str, object]] = []
    for obj, decision in judged:
        ev = decision.evidence or Evidence()
        row = {
            "kind": obj.kind,
            "label": obj.label,
            "action": decision.verdict.value,
            "old": _money(decision.old_bid),
            "new": _money(decision.new_bid),
            "why": decision.why.value,
            "days": decision.window_days,
            **_evidence(ev),
        }
        if decision.verdict is Verdict.UP:
            if len(hints) < MAX_CHANGES_PER_RUN:
                hints.append(row)
            else:
                holds["LATER_HINTS"] += 1
            continue
        if len(proposals) >= MAX_CHANGES_PER_RUN:
            holds["LATER"] += 1
            continue
        key = (obj.kind, obj.object_id)
        items[key] = replace(items[key], last_change=today, proposed_bid=decision.new_bid)
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
        proposals.append(row)

    seen = set(items)
    for key, old in remembered.items():
        if key not in seen:
            items[key] = (
                old if old.gone_at is not None else replace(old, gone_at=today, proposed_bid=None)
            )

    touchable = len(view.objects) - sum(holds[why] for why in UNTOUCHABLE)
    if view.groups and not stock_out and touchable <= 0:
        alerts = (*alerts, "NOTHING_TO_TUNE")
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
        "hints": hints,
        "human": human,
        "applied": applied,
        "holds": dict(sorted(holds.items())),
    }
    return Outcome(
        light=light,
        alerts=alerts,
        remembered=list(items.values()),
        decisions=decisions,
        facts=facts,
    )


def _seen(obj: AdObject, old: Remembered | None, today: date, *, first_look: bool) -> Remembered:
    """把这一轮看到的写进记忆。

    起点出价（b0）在第一次看到它有独立出价、又不归领星管的时候定下，以后不动。

    下面几种算一次改动（last_change = 今天）：之前攒下的数，不是在它现在这副样子下攒的，
    要等之后的新数——
    - 商品交出来以后才冒出来的对象（比如大人把这个 ASIN 加进了别的商品用过的广告组），
      和消失一阵又回来的；
    - 所在广告组刚从「和别的 ASIN 共用」变成独占：之前的数里混着别的商品的单和花费；
    - 领星刚撤掉对它的管理：之前的数是在领星调来调去的出价下攒的。
    没法再碰的（在管、共用、停了），挂着的「本来会改」一并作废。
    """
    if old is None:
        changed = not first_look
    else:
        changed = (
            old.gone_at is not None
            or (old.shared and not obj.shared)
            or (old.managed and not obj.managed)
        )
    moot = changed or obj.managed or obj.shared or not obj.enabled
    if old is None:
        return Remembered(
            kind=obj.kind,
            object_id=obj.object_id,
            campaign_id=obj.campaign_id,
            ad_group_id=obj.ad_group_id,
            label=obj.label,
            start_bid=None if obj.managed else obj.bid,
            last_bid=obj.bid,
            first_seen=today,
            last_seen=today,
            gone_at=None,
            last_change=today if changed else None,
            hands_off_until=None,
            shared=obj.shared,
            managed=obj.managed,
            proposed_bid=None,
        )
    return replace(
        old,
        campaign_id=obj.campaign_id,
        ad_group_id=obj.ad_group_id,
        label=obj.label,
        start_bid=(old.start_bid if old.start_bid is not None or obj.managed else obj.bid),
        last_bid=obj.bid,
        last_seen=today,
        gone_at=None,
        last_change=today if changed else old.last_change,
        shared=obj.shared,
        managed=obj.managed,
        proposed_bid=None if moot else old.proposed_bid,
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
        long=obj.long,
        short=obj.short,
    )


def _daily_spend(decision: Decision) -> Decimal:
    ev = decision.evidence or Evidence()
    return ev.spend / (decision.window_days or 1)
