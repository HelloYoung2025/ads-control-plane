"""每天一页的报告：~/广告操盘手/<本地日期>.html。对话里只说一盏灯和一句话，细节都在这里。

- 自包含：内联 CSS、无脚本、无外链——SFW 右栏预览与浏览器直开都不出网。
- 每一个来自数据的字符串都经 html.escape：关键词、投放表达式、商品标题都是外部文本（AX-15）。
- 先写临时文件再 os.replace：点开时看到的要么是旧的完整页，要么是新的完整页。
- 0600：里面有关键词和出价，只给自己看。
- 同一天看几遍，这一页就重写几遍，永远是当天最新的样子；以前的日子各留各的一页。
"""

from __future__ import annotations

import html
import os
import tempfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ads_control_plane.sfw.memory import Goal, Memory, Run

FILE_MODE = 0o600
#: 一页里每张表最多几行：表是给人看的，全量在记忆库里。
MAX_ROWS = 50

#: 每个「没碰」的理由对应的人话。键与 strategies/bidding.py 的 Why、judge.py 的额外理由一一对应。
WHY = {
    "ACOS_HIGH": "ACOS 高过上限",
    "NO_ORDERS": "花掉一单的钱还没出单",
    "FEW_ORDERS": "只出了一两单，花的钱再多出一单也打不平",
    "ACOS_LOW": "ACOS 低于上限，可以试着加一点（只是提示）",
    "PAUSED": "没在投（它自己、广告组或活动暂停了）",
    "MANAGED": "领星的规则或分时策略在管，不碰",
    "SHARED": "广告组里还有别的商品在投，不碰",
    "INHERITED": "用的是广告组默认价，不碰",
    "NEW": "建了不到 17 天，先攒数据",
    "HANDS_OFF": "有人改过出价，14 天内不碰",
    "COOLING": "刚有变动（判过、改过或刚出现），等之后攒的新数据",
    "NO_IMPRESSIONS": "没有曝光",
    "FEW_CLICKS": "点击不到 10 次",
    "NOT_ENOUGH": "单数还不够，说不准",
    "ON_TARGET": "ACOS 就在上限附近，不用动",
    "NO_TARGET": "还没定 ACOS 上限",
    "AT_FLOOR": "已经降到起点价的 6 成（或站点最低价），不再往下降",
    "AT_CEILING": "已经到起点价的 1.4 倍，不再往上加",
    "NO_STEP": "出价太小，一步还不到一个最小单位",
    "STOCK_OUT": "没库存，这轮不判",
    "LATER": "一轮最多改 20 处，下一轮再看",
    "LATER_HINTS": "加价提示一轮最多列 20 处",
}
LIGHT_WORD = {"green": "绿灯", "yellow": "黄灯", "red": "红灯", "off": "关灯"}
ALERT_WORD = {
    "ORDERS_HALVED": "订单比前两周少了一半多",
    "SPEND_JUMPED": "花费比前两周多了三成多",
    "STOCK_OUT": "没库存了",
    "NO_ADS": "没找到在投的 SP 广告",
    "NOTHING_TO_TUNE": "在投的词它一个都不能碰（共用组、领星在管、用组默认价或刚建）",
    "CVR_DROPPED": "转化率比前两周掉了三成多：多半是价格、评价、库存或 listing 的问题",
}
KIND_WORD = {"keyword": "关键词", "target": "投放"}

_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--muted:#5b6475;--line:#e2e5eb;
--green:#1f9d55;--yellow:#c98a00;--red:#d14343;--off:#8a93a3;--chip:#eef1f5}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1a1f27;--ink:#e8ebf0;
--muted:#9aa3b2;--line:#2c333e;--chip:#232a34}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.6 -apple-system,"PingFang SC","Helvetica Neue",sans-serif}
main{max-width:880px;margin:0 auto;padding:24px 16px 48px}
header{display:flex;gap:16px;align-items:center;margin-bottom:8px}
.dot{width:44px;height:44px;border-radius:50%;flex:none;background:var(--off)}
.dot.green{background:var(--green)}.dot.yellow{background:var(--yellow)}.dot.red{background:var(--red)}
h1{font-size:22px;margin:0;text-wrap:balance}
.sub{color:var(--muted);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;
margin-top:18px}
.card h2{font-size:18px;margin:0;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.pill{font-size:12px;font-weight:600;padding:1px 8px;border-radius:999px;color:#fff;
background:var(--off)}
.pill.green{background:var(--green)}.pill.yellow{background:var(--yellow)}
.pill.red{background:var(--red)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:12px 0}
.kpi{background:var(--chip);border-radius:8px;padding:8px 10px}
.kpi b{display:block;font-size:17px;font-variant-numeric:tabular-nums}
.kpi span{font-size:12px;color:var(--muted)}
h3{font-size:14px;margin:16px 0 6px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600}
.num{white-space:nowrap;font-variant-numeric:tabular-nums}
ul{margin:4px 0;padding-left:20px}
.alert{border-left:4px solid var(--yellow);padding:6px 10px;background:var(--chip);margin:8px 0}
.alert.red{border-color:var(--red)}
details{margin-top:12px;color:var(--muted);font-size:13px}
code{font-family:Menlo,monospace;font-size:12px;word-break:break-all}
footer{margin-top:28px;color:var(--muted);font-size:13px}
"""


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _dec(value: object) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except InvalidOperation:
        return None


def _percent(value: Decimal | None) -> str:
    return "—" if value is None else f"{(value * 100).quantize(Decimal('0.1'))}%"


def _acos(ev: object) -> Decimal | None:
    if not isinstance(ev, dict):
        return None
    spend, sales = _dec(ev.get("spend")), _dec(ev.get("sales"))
    return spend / sales if spend is not None and sales else None


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    table = f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    return f"<div class='scroll'>{table}</div>"


def _kpi(value: str, label: str) -> str:
    return f"<div class='kpi'><b>{_e(value)}</b><span>{_e(label)}</span></div>"


def _money(value: object, currency: str) -> str:
    amount = _dec(value)
    return "—" if amount is None else f"{amount} {currency}"


def _card(goal: Goal, runs: list[Run], memory: Memory) -> tuple[str, str]:
    """一个商品一张卡。返回 (灯, html)。"""
    last = runs[0] if runs else None
    if goal.status == "paused":
        light = "red"
    elif last is None or last.status != "OK":
        light = "yellow"
    else:
        light = last.light or "yellow"
    headline = last.headline if last is not None and last.headline else f"{goal.name}：还没看过"
    parts = [
        f"<section class='card'><h2>{_e(goal.name)}"
        f"<span class='pill {light}'>{LIGHT_WORD[light]}</span></h2>",
        f"<p>{_e(headline)}</p>",
    ]
    facts = last.facts if last is not None and last.status == "OK" else {}
    currency = goal.currency
    if facts:
        now, before = facts.get("now", {}), facts.get("before", {})
        stock = facts.get("stock")
        parts.append(
            "<div class='kpis'>"
            + _kpi(_percent(_acos(now)), f"近 14 天 ACOS（上限 {_percent(goal.target_acos)}）")
            + _kpi(
                _money(now.get("spend") if isinstance(now, dict) else None, currency),
                "近 14 天花费",
            )
            + _kpi(str(now.get("orders", "—")) if isinstance(now, dict) else "—", "近 14 天订单")
            + _kpi(_percent(_acos(before)), "再往前 14 天 ACOS")
            + _kpi("—" if stock is None else str(stock), "FBA 可售库存")
            + "</div>"
        )
        alerts = facts.get("alerts")
        for alert in alerts if isinstance(alerts, list) else []:
            red = " red" if alert in ("ORDERS_HALVED", "SPEND_JUMPED") else ""
            parts.append(f"<div class='alert{red}'>{_e(ALERT_WORD.get(alert, alert))}</div>")
        parts.append(_proposals(facts, currency))
        parts.append(_humans(facts, currency))
        parts.append(_holds(facts))
    parts.append(_remembers(goal, memory))
    parts.append(_history(runs))
    parts.append(_details(goal, last, facts))
    parts.append("</section>")
    return light, "".join(parts)


def _proposals(facts: dict[str, object], currency: str) -> str:
    items = facts.get("proposals")
    if not isinstance(items, list) or not items:
        return "<h3>本来会改</h3><p class='sub'>这一轮没有要改的。</p>"
    rows: list[list[str]] = []
    for item in items[:MAX_ROWS]:
        if not isinstance(item, dict):
            continue
        arrow = "↓" if item.get("action") == "down" else "↑"
        rows.append(
            [
                _e(KIND_WORD.get(str(item.get("kind")), "")),
                f"<code>{_e(item.get('label', ''))}</code>",
                f"<span class='num'>{_e(item.get('old'))} → {_e(item.get('new'))} {arrow}</span>",
                _e(WHY.get(str(item.get("why")), item.get("why"))),
                f"<span class='num'>{_e(item.get('days'))} 天：点击 {_e(item.get('clicks'))}，"
                f"订单 {_e(item.get('orders'))}，花费 {_e(_money(item.get('spend'), currency))}，"
                f"ACOS {_e(_percent(_acos(item)))}</span>",
            ]
        )
    return "<h3>本来会改（只看不动：都没有真的改）</h3>" + _table(
        ["", "关键词 / 投放", "出价", "为什么", "证据"], rows
    )


def _humans(facts: dict[str, object], currency: str) -> str:
    items = facts.get("human")
    if not isinstance(items, list) or not items:
        return ""
    rows = [
        [
            _e(KIND_WORD.get(str(item.get("kind")), "")),
            f"<code>{_e(item.get('label', ''))}</code>",
            f"<span class='num'>{_e(item.get('old'))} → {_e(item.get('new'))}</span>",
        ]
        for item in items[:MAX_ROWS]
        if isinstance(item, dict)
    ]
    return "<h3>有人改过出价（14 天内不碰）</h3>" + _table(["", "关键词 / 投放", "出价"], rows)


def _holds(facts: dict[str, object]) -> str:
    holds = facts.get("holds")
    if not isinstance(holds, dict) or not holds:
        return ""
    lines = "".join(
        f"<li>{_e(WHY.get(str(why), why))}：{_e(count)} 处</li>"
        for why, count in sorted(holds.items(), key=lambda kv: -int(kv[1]))
    )
    return f"<h3>它没碰的</h3><ul>{lines}</ul>"


def _remembers(goal: Goal, memory: Memory) -> str:
    events = memory.events(goal.id, 5)
    if not events:
        return ""
    lines = "".join(
        f"<li><span class='num'>{_e(e.at.astimezone().strftime('%m-%d %H:%M'))}</span> "
        f"{_e(e.detail)}</li>"
        for e in events
    )
    return f"<h3>它记得</h3><ul>{lines}</ul>"


def _history(runs: list[Run]) -> str:
    if not runs:
        return ""
    rows = [
        [
            f"<span class='num'>{_e(r.started_at.astimezone().strftime('%m-%d %H:%M'))}</span>",
            _e(LIGHT_WORD.get(r.light or "", "—") if r.status == "OK" else "没看成"),
            _e(r.headline or ""),
        ]
        for r in runs
    ]
    return "<h3>最近几轮</h3>" + _table(["时间", "灯", "说了什么"], rows)


def _details(goal: Goal, last: Run | None, facts: dict[str, object]) -> str:
    bits = [f"店：{_e(goal.store)}", f"ASIN：<code>{_e(goal.asin)}</code>"]
    title = facts.get("title")
    if title:
        bits.append(f"标题：{_e(title)}")
    windows = facts.get("windows")
    if isinstance(windows, dict):
        long, before = windows.get("long"), windows.get("before")
        if isinstance(long, list) and isinstance(before, list):
            bits.append(
                f"统计 {_e(long[0])} 到 {_e(long[1])}（最后 3 天订单没结算完，不算）；"
                f"对比 {_e(before[0])} 到 {_e(before[1])}"
            )
    for key, word in (
        ("groups", "在投广告组"),
        ("shared_groups", "其中和别的商品共用"),
        ("objects", "关键词和投放"),
        ("unreadable", "读不出来、没判"),
    ):
        if key in facts:
            bits.append(f"{word}：{_e(facts[key])}")
    if last is not None:
        bits.append(f"第 {last.id} 轮（{_e(last.trigger)}）")
        if last.error_code:
            bits.append(f"没看成的原因：<code>{_e(last.error_code)}</code>")
    return "<details><summary>给大人看</summary><p>" + "<br>".join(bits) + "</p></details>"


def render(memory: Memory, *, now: datetime) -> str:
    goals = memory.goals()
    lights: list[str] = []
    cards: list[str] = []
    for goal in goals:
        light, card = _card(goal, memory.recent_runs(goal.id, 7), memory)
        lights.append(light)
        cards.append(card)
    if memory.paused():
        top, headline = "off", "全部停下了，在休息"
    elif not goals:
        top, headline = "green", "还没有交给它的商品"
    else:
        top = max(lights, key=lambda light: {"green": 0, "yellow": 1, "red": 2}[light])
        bad = sum(1 for light in lights if light != "green")
        headline = (
            f"{len(goals)} 个商品都没事" if not bad else f"{len(goals)} 个商品，{bad} 个要看一眼"
        )
    stamp = now.astimezone()
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>广告操盘手 {stamp.date().isoformat()}</title>"
        f"<style>{_CSS}</style></head><body><main>"
        f"<header><div class='dot {top}'></div><div><h1>【{LIGHT_WORD[top]}】{_e(headline)}</h1>"
        f"<div class='sub'>广告操盘手 · {_e(stamp.strftime('%Y-%m-%d %H:%M'))} 更新"
        " · 只看不动</div></div></header>"
        + "".join(cards)
        + "<footer>只看不动：这页里所有「本来会改」都没有真的做，领星里的出价一分没动。"
        "想停下，在 SFW 里说「全部停下」。</footer></main></body></html>"
    )


def write(report_dir: Path, memory: Memory, *, now: datetime) -> Path:
    report_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = report_dir / f"{now.astimezone().date().isoformat()}.html"
    fd, tmp = tempfile.mkstemp(dir=report_dir, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(render(memory, now=now).encode("utf-8"))
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def latest(report_dir: Path) -> Path | None:
    """最近一天的那页；一页都没有就 None。"""
    if not report_dir.is_dir():
        return None
    pages = sorted(report_dir.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].html"))
    return pages[-1] if pages else None
