"""广告操盘手：人说一句话 → 认成命令 → 做 → 一段固定格式的回答。

回答的格式是照 7–10 岁的孩子定的（2026-09-24 定稿计划第二节第 6 条），测试钉着：
- 第一行 `**【灯】…**`，不超过 40 字。灯只有四种：绿灯没事、黄灯看一眼、红灯叫大人、关灯在休息。
- 第二行说下一步做什么，不超过 30 字。
- 可选：一张不超过 5 行的表、一个报告链接、一行「给大人看」。
关键词原文、买家搜索词永远不进回答（AX-15），只进本机的报告网页。

现在是只看不动（S1）：到领星只读，判断只记成「本来会改」，一分钱不动。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ads_control_plane.providers.lingxing.goal_objects import GoalReadError, LxReadPort, read_goal
from ads_control_plane.sfw import diary
from ads_control_plane.sfw.config import ConfigError, PackConfig, load_config
from ads_control_plane.sfw.judge import TARGET_RANGE, alerts_of, default_target, judge
from ads_control_plane.sfw.lxlock import RUN_LOCK_NAME, one_process_at_a_time
from ads_control_plane.sfw.memory import Goal, Memory, MemoryStoreError, Pending
from ads_control_plane.sfw.parse import Command, Kind, parse

logger = logging.getLogger("ads_control_plane.sfw.operator")

LIGHTS = {"green": "绿灯", "yellow": "黄灯", "red": "红灯", "off": "关灯"}
_RANK = {"green": 0, "yellow": 1, "red": 2}
#: 回「好」的有效期。
PENDING_FOR = timedelta(minutes=10)
#: 连着这么多轮没看成，这个商品先停下，等大人看。
FAILURES_TO_PAUSE = 3
#: 回答里的表最多几行：再多就淹掉第一行那盏灯了，其余看报告。
MAX_TABLE_ROWS = 5
EXAMPLE_NAME = "猫抓板"

#: 按严重程度排：一轮里同时有几件事，第一行只说最要紧的那件。
_ALERT_ORDER = (
    "ORDERS_HALVED",
    "SPEND_JUMPED",
    "STOCK_OUT",
    "NO_ADS",
    "CVR_DROPPED",
    "NOTHING_TO_TUNE",
)
_ALERT_HEADLINE = {
    "ORDERS_HALVED": "订单比前两周少了一半多",
    "SPEND_JUMPED": "花费比前两周多了三成多",
    "STOCK_OUT": "没库存了，这轮不判",
    "NO_ADS": "没找到在投的广告",
    "CVR_DROPPED": "买的人比前两周少了三成多",
    "NOTHING_TO_TUNE": "能调的词是 0 个",
}
_ALERT_NEXT = {
    "ORDERS_HALVED": "大人点下面的报告看一眼",
    "SPEND_JUMPED": "大人点下面的报告看一眼",
    "STOCK_OUT": "叫大人补货，补上再让它看",
    "NO_ADS": "大人看看这个商品还在投广告吗",
    "CVR_DROPPED": "叫大人看看价格和评价",
    "NOTHING_TO_TUNE": "大人看报告里「它没碰的」",
}
SHADOW_NEXT = "现在只看不动，没改任何广告"

#: 过几分钟再看多半就好的码：网络没通、翻页时领星的数据在动。其余的要大人修，
#: 叫孩子反复重试只会把商品推到「连着 3 次没看成」（2026-09-24 评审）。
RETRY_LATER_CODES = frozenset({"LX_TRANSPORT_ERROR", "GOAL_PAGES_SHORT"})
#: 给大人看的一句中文。表外的码照原样给出。
CODE_WORDS = {
    "LX_TRANSPORT_ERROR": "连不上领星（网络）",
    "LX_GATEWAY_ERROR": "领星网关不让进（多半是 key 失效或没权限）",
    "LX_BUSINESS_ERROR": "领星说参数或权限不对",
    "LX_ENVELOPE_SHAPE": "领星回的东西形状不对",
    "LX_TOOL_NOT_ALLOWED": "要用的领星工具不在只读名单里",
    "GOAL_PAGES_SHORT": "翻页时领星的数据在变",
    "GOAL_TOTAL_ABSENT": "领星没给总行数，没法确认读全了",
    "GOAL_TOO_MANY_PAGES": "这个商品的词太多，一轮读不完",
    "GOAL_ROWS_UNUSABLE": "一半以上的词读不出来",
    "GOAL_ASIN_METRICS_UNREADABLE": "这个商品自己的广告数读不出来",
    "GOAL_STORE_GONE": "这个商品的店已经不在配置里了",
    "GOAL_UPSTREAM_ERROR": "领星那边出错了",
    "OPERATOR_INTERNAL_ERROR": "插件自己出错了，看 ~/.amazon-ads/ 下的运行日志",
}


def code_words(code: str) -> str:
    return f"{CODE_WORDS.get(code, '没料到的错')}（{code}）"


@dataclass(frozen=True, kw_only=True)
class Setup:
    config_path: Path
    expect_uid: int | None
    memory_path: Path
    report_dir: Path
    #: 配置出错时那句话的结尾：告诉人自己去改哪儿（同 server.build_server 的 fix_hint）。
    fix_hint: str
    read_port: Callable[[PackConfig], LxReadPort]
    now: Callable[[], datetime]
    lock_wait_seconds: float = 30.0


@dataclass(frozen=True, kw_only=True)
class GoalResult:
    goal: Goal
    light: str
    headline: str
    next_step: str
    error_code: str | None = None
    facts: dict[str, object] = field(default_factory=dict)


# ------------------------------------------------------------------ 回答的样子


def reply(
    light: str,
    first: str,
    second: str,
    *,
    table: Sequence[Sequence[str]] | None = None,
    link: Path | None = None,
    adult: str | None = None,
) -> str:
    parts = [f"**【{LIGHTS[light]}】{first}**", second]
    if table:
        header, *rows = table
        lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
        lines += ["| " + " | ".join(row) + " |" for row in rows]
        parts.append("\n".join(lines))
    if link is not None:
        # SFW 只把 [文字](地址) 渲染成能点的链接，裸路径点不开（2026-09-24 评审）。
        parts.append(f"报告：[{link.name}]({link})")
    if adult:
        parts.append(f"给大人看：{adult}")
    return "\n\n".join(parts)


def _worst(lights: Sequence[str]) -> str:
    return max(lights, key=lambda light: _RANK.get(light, 0), default="green")


def _percent(value: Decimal | None) -> str:
    return "—" if value is None else f"{(value * 100).quantize(Decimal('1'))}%"


def _acos_of(facts: dict[str, object]) -> Decimal | None:
    now = facts.get("now")
    if not isinstance(now, dict):
        return None
    sales = Decimal(str(now.get("sales", "0")))
    return Decimal(str(now.get("spend", "0"))) / sales if sales > 0 else None


def _changes_of(facts: dict[str, object]) -> int:
    proposals = facts.get("proposals")
    return len(proposals) if isinstance(proposals, list) else 0


# ------------------------------------------------------------------ 入口


class Operator:
    def __init__(self, setup: Setup) -> None:
        self._setup = setup
        # 同一个进程里两个对话同时说「看一遍」：第二个等第一个看完。跨进程的在 lxlock。
        self._run_lock = threading.Lock()

    def say(self, text: str) -> str:
        command = parse(text)
        now = self._setup.now()
        try:
            memory = Memory.open(self._setup.memory_path)
        except (MemoryStoreError, sqlite3.Error, OSError) as exc:
            logger.error("记忆库打不开：%s", exc)
            return reply("red", "记忆库打不开，叫大人", "大人看最后一行", adult=str(exc))
        try:
            return self._handle(command, memory, now)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc) or "busy" in str(exc):
                # 另一个进程占着写锁超过 30 秒。
                logger.error("记忆库忙：%s", exc)
                return reply("yellow", "记忆库正忙", "过一会儿再说一遍")
            # 磁盘满、只读、坏了：等多久都没用。
            logger.error("记忆库出错：%s", exc)
            return reply(
                "red",
                "记忆库出错了，叫大人",
                "大人看最后一行",
                adult=f"{self._setup.memory_path}：{exc}",
            )
        finally:
            memory.close()

    def _handle(self, command: Command, memory: Memory, now: datetime) -> str:
        kind = command.kind
        if kind is Kind.TODAY:
            return self._today(memory)
        if kind is Kind.STOP:
            memory.set_paused(True)
            return reply("off", "全部停下了", "想接着来就说「继续干活」")
        if kind is Kind.RESUME:
            woke = memory.resume(now)
            return reply(
                "green",
                "继续干活" + (f"，{'、'.join(woke[:3])}也接着看" if woke else ""),
                "说「现在看一遍」看看情况",
            )
        if kind is Kind.HELP:
            return _help(memory)
        if kind is Kind.YES:
            return self._yes(memory, now)
        if kind is Kind.TARGET:
            assert command.name is not None and command.percent is not None
            return self._target(memory, command.name, command.percent, now)
        if kind is Kind.DROP:
            assert command.name is not None
            return self._drop(memory, command.name, now)
        if kind is Kind.ADOPT:
            return self._adopt(memory, command, now)
        if kind is Kind.LOOK:
            return self._look(memory, command.name, now)
        return reply("yellow", "没听懂", "说「我能说什么」看看能说的话")

    def _config(self) -> PackConfig | str:
        try:
            return load_config(self._setup.config_path, expect_uid=self._setup.expect_uid)
        except ConfigError as exc:
            return reply(
                "red",
                "配置有问题，叫大人",
                "大人照最后一行改好再说一遍",
                adult=f"配置错误：{exc}，{self._setup.fix_hint}",
            )

    # ------------------------------------------------------------------ 设置类

    def _yes(self, memory: Memory, now: datetime) -> str:
        pending = memory.pending(now)
        if pending is None:
            return reply("yellow", "没有等你回答的事", "说「我能说什么」看看能说的话")
        goal = next((g for g in memory.goals() if g.id == pending.goal_id), None)
        memory.set_pending(None)
        if goal is None:
            return reply("yellow", "那个商品已经不管了", "说「看今天」看看都有哪些")
        percent = pending.target_percent
        memory.set_target(goal.id, percent, f"ACOS 上限改成 {percent}%", now)
        return reply("green", f"{goal.name}：ACOS 最多 {percent}%", "下次看的时候按这个算")

    def _target(self, memory: Memory, name: str, percent: int, now: datetime) -> str:
        goal = memory.goal_named(name)
        if goal is None:
            return _not_found(name)
        low, high = TARGET_RANGE
        if low <= percent <= high:
            memory.set_pending(None)
            memory.set_target(goal.id, percent, f"ACOS 上限改成 {percent}%", now)
            return reply("green", f"{name}：ACOS 最多 {percent}%", "下次看的时候按这个算")
        safe = low if percent < low else high
        memory.set_pending(
            Pending(goal_id=goal.id, target_percent=safe, expires_at=now + PENDING_FOR)
        )
        word = "太低了" if percent < low else "太高了"
        return reply(
            "yellow",
            f"{percent}% {word}，用 {safe}% 吧？",
            f"回「好」就用 {safe}%，10 分钟内有效",
            adult=f"ACOS 上限只收 {low}% 到 {high}%",
        )

    def _drop(self, memory: Memory, name: str, now: datetime) -> str:
        goal = memory.goal_named(name)
        if goal is None:
            return _not_found(name)
        memory.set_status(goal.id, "retired", "不管了", now)
        return reply("green", f"不管「{name}」了", "记录还留着，想管再交给我")

    def _adopt(self, memory: Memory, command: Command, now: datetime) -> str:
        assert command.store is not None and command.asin is not None and command.name is not None
        cfg = self._config()
        if isinstance(cfg, str):
            return cfg
        # 人说的话在 parse 里做过 NFKC（全角数字变半角），配置里的店名也按同样的规矩比。
        store = next(
            (s for s in cfg.stores if unicodedata.normalize("NFKC", s.nickname) == command.store),
            None,
        )
        if store is None:
            names = [s.nickname for s in cfg.stores]
            shown = "、".join(names[:MAX_TABLE_ROWS]) + ("…" if len(names) > MAX_TABLE_ROWS else "")
            return reply(
                "yellow",
                f"没有叫「{command.store}」的店",
                "照配置里的店名再说一遍",
                adult=f"配置里的店名（共 {len(names)} 家）：{shown}",
            )
        existing = memory.goal_for(store.profile_id, command.asin)
        if existing is not None and existing.status != "retired":
            return reply(
                "yellow",
                f"这个商品已经交给我了，叫「{existing.name}」",
                f"说「{existing.name}现在看一遍」",
            )
        taken = memory.goal_named(command.name)
        if taken is not None:
            return reply("yellow", f"「{command.name}」这个名字用过了", "换个小名再说一遍")
        try:
            goal = memory.adopt(
                store=store.nickname,
                profile_id=store.profile_id,
                asin=command.asin,
                name=command.name,
                currency=store.currency,
                at=now,
            )
        except sqlite3.IntegrityError:
            return reply("yellow", f"「{command.name}」这个名字用过了", "换个小名再说一遍")
        if goal.name != command.name:
            # 另一个对话刚刚把同一个商品交过来了。
            return reply(
                "yellow",
                f"这个商品已经交给我了，叫「{goal.name}」",
                f"说「{goal.name}现在看一遍」",
            )
        return reply(
            "green",
            f"记住了：{goal.name}",
            f"说「{goal.name}现在看一遍」",
            adult=f"{store.nickname} {goal.asin}；只看不动，不改广告",
        )

    # ------------------------------------------------------------------ 看

    def _today(self, memory: Memory) -> str:
        goals = memory.goals()
        if not goals:
            return reply("green", "还没有交给我的商品", "请大人说：管 店名 ASIN 叫 小名")
        rows: list[list[str]] = [["商品", "灯", "近14天 ACOS", "上限", "本来会改"]]
        lights: list[str] = []
        for goal in goals:
            runs = memory.recent_runs(goal.id, 1)
            run = runs[0] if runs else None
            if goal.status == "paused":
                # 不说「停下了」：那是「全部停下」的词，解法也不一样。
                light, acos, changes = "red", "—", "歇着了"
            elif run is None:
                light, acos, changes = "yellow", "—", "还没看过"
            elif run.status != "OK":
                light, acos, changes = "yellow", "—", "没看成"
            else:
                light = run.light or "yellow"
                acos, changes = _percent(_acos_of(run.facts)), f"{_changes_of(run.facts)} 处"
            lights.append(light)
            rows.append([goal.name, LIGHTS[light], acos, _percent(goal.target_acos), changes])
        link = diary.latest(self._setup.report_dir)
        if memory.paused():
            return reply(
                "off",
                "全部停下了，在休息",
                "想接着来就说「继续干活」",
                table=rows[: MAX_TABLE_ROWS + 1],
                link=link,
            )
        worst = _worst(lights)
        counts = "、".join(
            f"{lights.count(light)} {LIGHTS[light][0]}"
            for light in ("green", "yellow", "red")
            if light in lights
        )
        first = (
            f"{len(goals)} 个商品都没事" if worst == "green" else f"{len(goals)} 个商品：{counts}"
        )
        more = len(goals) - MAX_TABLE_ROWS
        return reply(
            worst,
            first,
            "点下面的报告看细节" if link is not None else f"说「{goals[0].name}现在看一遍」",
            table=rows[: MAX_TABLE_ROWS + 1],
            link=link,
            adult=f"还有 {more} 个商品没列，在报告里" if more > 0 else None,
        )

    def _look(self, memory: Memory, name: str | None, now: datetime) -> str:
        """人叫它看，就把点到的（没点名就是全部）都看一遍，连着失败被自动停下的也看：
        人主动说的这一句就是「修好了，再试试」。自动停下只挡以后无人值守的定时看。"""
        if memory.paused():
            return reply("off", "全部停下了，没去看", "先说「继续干活」")
        if name is not None:
            goal = memory.goal_named(name)
            if goal is None:
                return _not_found(name)
            goals = [goal]
        else:
            goals = memory.goals()
            if not goals:
                return reply("yellow", "还没有要看的商品", "请大人说：管 店名 ASIN 叫 小名")
        cfg = self._config()
        if isinstance(cfg, str):
            return cfg
        busy = reply("yellow", "上一轮还在看", "过几分钟再说一遍")
        if not self._run_lock.acquire(timeout=self._setup.lock_wait_seconds):
            return busy
        try:
            lock_path = cfg.run_log_path.parent / RUN_LOCK_NAME
            with one_process_at_a_time(lock_path, self._setup.lock_wait_seconds) as ours:
                if not ours:
                    return busy
                port = self._setup.read_port(cfg)
                shop_ads: dict[tuple[str, str], list[Mapping[str, object]]] = {}
                results: list[GoalResult] = []
                for goal in goals:
                    # 等锁、看前一个商品都要时间：这期间人可能说了「全部停下」或「不管它了」。
                    if memory.paused():
                        break
                    fresh = memory.goal(goal.id)
                    if fresh is None or fresh.status == "retired":
                        continue
                    results.append(self.run_goal(memory, cfg, fresh, port, now, "manual", shop_ads))
        finally:
            self._run_lock.release()
        if not results:
            if memory.paused():
                return reply("off", "全部停下了，没去看", "先说「继续干活」")
            return reply("yellow", "还没有要看的商品", "请大人说：管 店名 ASIN 叫 小名")
        try:
            link: Path | None = diary.write(self._setup.report_dir, memory, now=now)
            trouble = None
        except OSError as exc:
            logger.error("报告没写成：%s", exc)
            link, trouble = None, f"报告没写成（{self._setup.report_dir}）：{exc.strerror or exc}"
        stopped = len(results) < len(goals) and memory.paused()
        return _look_reply(results, link, trouble=trouble, stopped=stopped)

    def run_goal(
        self,
        memory: Memory,
        cfg: PackConfig,
        goal: Goal,
        port: LxReadPort,
        now: datetime,
        trigger: str,
        shop_ads: dict[tuple[str, str], list[Mapping[str, object]]],
    ) -> GoalResult:
        """看一个商品一遍：读 → 判 → 记。任何失败都收成一个带码的结局，不让一个商品拖垮一批。"""
        run_id = memory.start_run(goal.id, trigger, now)
        today = now.astimezone(UTC).date()
        try:
            # 店只认配置里此刻的店铺表（AX-02）：交出来之后大人把店删了，就不再去读它；
            # 币种也用配置里现在的（大人可能改正过）。
            store = next((s for s in cfg.stores if s.profile_id == goal.profile_id), None)
            if store is None:
                raise GoalReadError(
                    "GOAL_STORE_GONE", "this product's store is no longer in the config"
                )
            goal = replace(goal, currency=store.currency)
            spend_floor = cfg.thresholds.min_spend.get(goal.currency)
            view = read_goal(
                port,
                profile_id=goal.profile_id,
                asin=goal.asin,
                today=today,
                shop_ads=shop_ads,
            )
            # 还没定上限就按近 14 天定一个。这一轮本身不正常（断货、订单腰斩……）就先不定：
            # 从不正常的数里定出来的上限，之后会一直用下去。
            if goal.target_acos is None and not alerts_of(view, spend_floor):
                percent = default_target(view)
                if percent is not None:
                    memory.set_target(
                        goal.id,
                        percent,
                        f"按近 14 天 ACOS 定了上限 {percent}%",
                        now,
                        only_if_unset=True,
                    )
                    # 人可能在这几十秒里刚说过「最多N%」：以库里的为准。
                    saved = memory.goal(goal.id)
                    if saved is not None:
                        goal = replace(goal, target_acos=saved.target_acos)
            outcome = judge(
                goal,
                view,
                memory.objects(goal.id),
                today=today,
                spend_floor=spend_floor,
            )
        except Exception as exc:
            code = exc.code if isinstance(exc, GoalReadError) else "OPERATOR_INTERNAL_ERROR"
            if not isinstance(exc, GoalReadError):
                logger.exception("看 %s 时出了没料到的错", goal.name)
            else:
                logger.error("看 %s 没看成 %s：%s", goal.name, code, exc)
            failures = memory.finish_run(
                run_id,
                goal_id=goal.id,
                ok=False,
                light="yellow",
                headline=f"{goal.name}：这次没看成",
                facts={},
                error_code=code,
                at=now,
            )
            if failures >= FAILURES_TO_PAUSE and memory.set_status(
                goal.id,
                "paused",
                f"连着 {failures} 轮没看成，先歇着",
                now,
                only_from="active",
            ):
                return GoalResult(
                    goal=goal,
                    light="red",
                    headline=f"{goal.name}：连着 {failures} 次没看成，歇着了",
                    next_step=f"大人修好后说「{goal.name}现在看一遍」",
                    error_code=code,
                )
            return GoalResult(
                goal=goal,
                light="yellow",
                headline=f"{goal.name}：这次没看成",
                next_step=(
                    f"过几分钟再说「{goal.name}现在看一遍」"
                    if code in RETRY_LATER_CODES
                    else "叫大人看最后一行"
                ),
                error_code=code,
            )
        headline, next_step = _headline(goal, outcome.alerts, outcome.facts)
        memory.finish_run(
            run_id,
            goal_id=goal.id,
            ok=True,
            light=outcome.light,
            headline=headline,
            facts=outcome.facts,
            error_code=None,
            at=now,
            remembered=outcome.remembered,
            decisions=outcome.decisions,
        )
        if goal.status == "paused":
            memory.set_status(goal.id, "active", "看成了，接着看", now, only_from="paused")
        return GoalResult(
            goal=goal,
            light=outcome.light,
            headline=headline,
            next_step=next_step,
            facts=outcome.facts,
        )


def _headline(goal: Goal, alerts: Sequence[str], facts: dict[str, object]) -> tuple[str, str]:
    first_alert = next((a for a in _ALERT_ORDER if a in alerts), None)
    if first_alert is not None:
        return f"{goal.name}：{_ALERT_HEADLINE[first_alert]}", _ALERT_NEXT[first_alert]
    if goal.target_acos is None:
        return f"{goal.name}：还没定 ACOS 上限", f"说「{goal.name}最多25%」定一个"
    changes = _changes_of(facts)
    if changes:
        return f"{goal.name}：本来会改 {changes} 处", SHADOW_NEXT
    return f"{goal.name}：这轮不用改", SHADOW_NEXT


def _window_line(facts: dict[str, object]) -> str | None:
    windows = facts.get("windows")
    if not isinstance(windows, dict) or not isinstance(windows.get("long"), list):
        return None
    start, end = windows["long"]
    return f"统计 {start} 到 {end}，最后 3 天订单还没结算完，不算"


def _look_reply(
    results: list[GoalResult],
    link: Path | None,
    *,
    trouble: str | None = None,
    stopped: bool = False,
) -> str:
    """看完的回答。trouble：报告没写成的原因；stopped：看到一半有人说了「全部停下」。"""
    notes: list[str] = []
    if len(results) == 1:
        result = results[0]
        notes.append(
            f"没看成：{code_words(result.error_code)}"
            if result.error_code is not None
            else (_window_line(result.facts) or "")
        )
        if trouble:
            notes.append(trouble)
        return reply(
            result.light,
            result.headline,
            "全部停下了，剩下的没看" if stopped else result.next_step,
            link=link,
            adult="；".join(n for n in notes if n) or None,
        )
    # 红灯排最前：表只列 MAX_TABLE_ROWS 行，要紧的那个不能被挤出去。
    ranked = sorted(results, key=lambda r: -_RANK.get(r.light, 0))
    worst = ranked[0]
    failed = [r for r in results if r.error_code is not None]
    changes = sum(_changes_of(r.facts) for r in results)
    if len(failed) == len(results):
        first = f"{len(results)} 个商品这次都没看成"
        second = (
            "过几分钟再说「现在看一遍」"
            if all(r.error_code in RETRY_LATER_CODES for r in failed)
            else "叫大人看最后一行"
        )
    else:
        first = f"看了 {len(results)} 个商品，本来会改 {changes} 处" + (
            f"，{len(failed)} 个没看成" if failed else ""
        )
        second = worst.next_step if worst.light != "green" else SHADOW_NEXT
    if stopped:
        second = "全部停下了，剩下的没看"
    rows: list[list[str]] = [["商品", "灯", "这一轮"]]
    for result in ranked[:MAX_TABLE_ROWS]:
        rows.append(
            [
                result.goal.name,
                LIGHTS[result.light],
                result.headline.removeprefix(f"{result.goal.name}："),
            ]
        )
    more = len(results) - MAX_TABLE_ROWS
    if more > 0:
        notes.append(f"还有 {more} 个没列" + ("，在报告里" if link is not None else ""))
    if failed:
        notes.append(
            "没看成：" + "、".join(f"{r.goal.name}{code_words(r.error_code or '')}" for r in failed)
        )
    if trouble:
        notes.append(trouble)
    return reply(
        worst.light,
        first,
        second,
        table=rows,
        link=link,
        adult="；".join(notes) or None,
    )


def _not_found(name: str) -> str:
    return reply("yellow", f"没有叫「{name}」的商品", "说「看今天」看看都有哪些")


def _help(memory: Memory) -> str:
    goals = memory.goals()
    name = goals[0].name if goals else EXAMPLE_NAME
    return reply(
        "green",
        "能说的话在下面",
        "照着说，一次说一句",
        table=[
            ["你说", "它做"],
            ["看今天", "看每个商品怎么样"],
            [f"{name}现在看一遍", "马上去领星看一遍"],
            [f"{name}最多25%", "ACOS 最多 25%"],
            ["全部停下 / 继续干活", "停下 / 接着来"],
            [f"不管{name}了", "不再看它"],
        ],
        adult="交商品给它说「管 店名 ASIN 叫 小名」，店名用配置里的 nickname",
    )
