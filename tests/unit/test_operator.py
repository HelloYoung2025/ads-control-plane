"""广告操盘手端到端（sfw/operator.py 连同 judge、diary、memory）：人说一句 → 一段固定格式的回答。

全部走假领星（只认四种只读报表）、tmp_path 里的配置与记忆库、手拨的钟。
- 每一句回答都过 check_shape：第一行一盏灯、不超过 40 字，第二行不超过 30 字，没有禁词。
- 每说一句都新建一个 Operator：插件进程随 SFW 对话起落，能记住的只有记忆库里的东西。
- 每个测试收尾再查一遍：假领星收到的调用全是只读报表。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ads_control_plane.providers.lingxing.goal_objects import windows_for
from ads_control_plane.sfw.lxlock import RUN_LOCK_NAME
from ads_control_plane.sfw.memory import SCHEMA_VERSION, Memory, Run
from ads_control_plane.sfw.operator import Operator, Setup
from ads_control_plane.sfw.parse import Kind, parse
from ads_control_plane.sfw.server import (
    OPERATOR_RULES,
    OPERATOR_TOOL,
    PLUGIN_INSTRUCTIONS,
    TOOL_NAME,
    build_server,
)
from tests.support.fake_lingxing import (
    READ_TOOLS,
    FakeLingxing,
    Thing,
    TransportDown,
    small_shop,
)
from tests.unit import test_sfw_pack as pack

#: 12:00 UTC：绝大多数时区里本地日期和 UTC 日期是同一天，报告文件名好算。
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
TODAY = NOW.date()
FIX_HINT = "改好 ~/.amazon-ads/config.toml 再说一遍"
ADOPT = "管 美国店 B0TEST0001 叫 猫抓板"
#: 回答里不许出现的词：无人值守与「已生效」一类承诺（同 test_sfw_pack.PROMISES）、Owner 否掉的
#: 「授权书」一类说法，以及会撞上 SFW 按钮或模式的词（2026-09-24 定稿计划第二节第 6 条）。
FORBIDDEN = (*pack.PROMISES, "授权", "签发", "mandate", "执行", "目标", "撤销", "停止")


class Refused(Exception):
    code = "LX_BUSINESS_ERROR"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def tomorrow(self) -> None:
        self.now += timedelta(days=1)


def check_shape(answer: str) -> None:
    first, second, *rest = answer.split("\n\n")
    assert re.fullmatch(r"\*\*【(绿灯|黄灯|红灯|关灯)】[^\n*]+\*\*", first), first
    assert len(first.strip("*")) <= 40, first
    assert "\n" not in second and 0 < len(second) <= 30, second
    for part in rest:
        if part.startswith("|"):
            assert len(part.splitlines()) <= 2 + 5, part  # 表头、分隔线、最多 5 行
        else:
            assert part.startswith(("报告：[", "给大人看：")), part
    found = [word for word in FORBIDDEN if word in answer]
    assert not found, (found, answer)


@dataclass
class Rig:
    tmp: Path
    fake: FakeLingxing
    clock: Clock
    setup: Setup

    def say(self, text: str) -> str:
        answer = Operator(self.setup).say(text)
        check_shape(answer)
        return answer

    def memory(self) -> Memory:
        return Memory.open(self.setup.memory_path)

    def last_run(self, name: str = "猫抓板") -> Run:
        memory = self.memory()
        try:
            goal = memory.goal_named(name)
            assert goal is not None
            return memory.recent_runs(goal.id, 1)[0]
        finally:
            memory.close()

    def page(self) -> Path:
        return self.setup.report_dir / f"{self.clock.now.astimezone().date().isoformat()}.html"


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Callable[..., Rig]]:
    made: list[Rig] = []

    def build(stores: list[pack.Store] | None = None) -> Rig:
        config = pack.private(tmp_path, pack.config_text(tmp_path, stores or [pack.US]))
        fake = small_shop()
        clock = Clock()
        setup = Setup(
            config_path=config,
            expect_uid=os.getuid(),
            memory_path=tmp_path / "state" / "operator.sqlite3",
            report_dir=tmp_path / "报告",
            fix_hint=FIX_HINT,
            read_port=lambda cfg: fake,
            now=clock,
            lock_wait_seconds=0.1,
        )
        made.append(Rig(tmp=tmp_path, fake=fake, clock=clock, setup=setup))
        return made[-1]

    yield build
    for rig in made:
        assert {tool for tool, _ in rig.fake.calls} <= READ_TOOLS, "只看不动：一次写调用都不许有"


@pytest.fixture
def rig(make: Callable[..., Rig]) -> Rig:
    return make()


def hot(fake: FakeLingxing, today: date = TODAY) -> None:
    """近 14 天：整个商品 ACOS 30%；kw-1 ACOS 40%；tg-1 点了 30 次、花掉一单多的钱，0 单。"""
    w = windows_for(today)
    long, short = w.long.report_date, w.short.report_date
    fake.set(long, "ad-1", impressions=5000, clicks=200, orders=10, spend="120.00", sales="400.00")
    fake.set(long, "kw-1", impressions=2000, clicks=80, orders=6, spend="60.00", sales="150.00")
    fake.set(short, "kw-1", impressions=1000, clicks=40, orders=3, spend="30.00", sales="75.00")
    fake.set(long, "tg-1", impressions=900, clicks=30, orders=0, spend="25.00", sales="0")


# ------------------------------------------------------------------ 交给它，看一遍


def test_hand_over_a_product_then_look(rig: Rig) -> None:
    hot(rig.fake)
    answer = rig.say(ADOPT)
    assert answer.startswith("**【绿灯】记住了：猫抓板**\n\n说「猫抓板现在看一遍」")
    assert "只看不动" in answer
    assert rig.fake.calls == [], "交给它时不去领星"

    answer = rig.say("猫抓板现在看一遍")
    assert answer.startswith("**【绿灯】猫抓板：本来会改 2 处**\n\n现在只看不动，没改任何广告")
    assert f"报告：[{rig.page().name}]({rig.page()})" in answer
    assert "给大人看：统计 2026-09-08 到 2026-09-21，最后 3 天订单还没结算完，不算" in answer
    # 关键词、投放表达式、商品标题只进本机网页，不进对话（AX-15）。
    for text in ("cat scratcher", "B0RIVAL001", "deluxe"):
        assert text not in answer

    memory = rig.memory()
    goal = memory.goal_named("猫抓板")
    assert goal is not None
    # 没说上限，就按近 14 天 ACOS 30% × 0.9 定 27%，而且记下是怎么定的。
    assert goal.target_acos == Decimal("0.27")
    assert memory.events(goal.id, 1)[0].detail == "按近 14 天 ACOS 定了上限 27%"
    run = memory.recent_runs(goal.id, 1)[0]
    assert {
        (d.object_id, d.by, d.mode, d.action, str(d.old_bid), str(d.new_bid), d.why)
        for d in memory.decisions(run.id)
    } == {
        ("kw-1", "operator", "shadow", "down", "1.00", "0.85", "ACOS_HIGH"),
        ("tg-1", "operator", "shadow", "down", "1.00", "0.85", "NO_ORDERS"),
    }
    assert run.facts["holds"] == {"INHERITED": 1, "SHARED": 1}
    assert set(memory.objects(goal.id)) == {
        ("keyword", "kw-1"),
        ("keyword", "kw-2"),
        ("keyword", "kw-3"),
        ("target", "tg-1"),
    }
    memory.close()


def test_the_report_page_is_private_and_escapes_what_came_from_lingxing(rig: Rig) -> None:
    hot(rig.fake)
    rig.fake.things[0].text = '<script>alert("x")</script>'
    rig.say(ADOPT)
    rig.say("猫抓板现在看一遍")
    page = rig.page()
    assert stat.S_IMODE(page.stat().st_mode) == 0o600
    assert stat.S_IMODE(page.parent.stat().st_mode) == 0o700
    text = page.read_text(encoding="utf-8")
    assert "<script" not in text
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; [exact]" in text
    assert "Cat scratcher &lt;b&gt;deluxe&lt;/b&gt;" in text
    assert "本来会改（只看不动：都没有真的改）" in text
    assert "ACOS 高过上限" in text and "花掉一单的钱还没出单" in text
    assert list(page.parent.glob(".*.tmp")) == [], "临时文件不留下"


def test_a_second_look_the_same_day_does_not_reuse_the_same_numbers(rig: Rig) -> None:
    hot(rig.fake)
    rig.say(ADOPT)
    rig.say("猫抓板现在看一遍")
    answer = rig.say("猫抓板现在看一遍")
    assert answer.startswith("**【绿灯】猫抓板：这轮不用改**")
    run = rig.last_run()
    assert run.facts["holds"] == {"COOLING": 2, "INHERITED": 1, "SHARED": 1}
    assert run.facts["proposals"] == []


def test_a_bid_someone_changed_in_lingxing_is_left_alone_for_14_days(rig: Rig) -> None:
    rig.say(ADOPT)
    rig.say("猫抓板最多25%")
    rig.say("猫抓板现在看一遍")  # 记下 kw-1 此刻的出价 1.00
    rig.fake.things[0].bid = "1.20"  # 有人在领星里改了
    rig.clock.tomorrow()
    hot(rig.fake, rig.clock.now.date())
    answer = rig.say("猫抓板现在看一遍")
    # kw-1 的数字本来够降价，但有人刚改过它：只有 tg-1 算「本来会改」。
    assert answer.startswith("**【绿灯】猫抓板：本来会改 1 处**")
    run = rig.last_run()
    assert run.facts["human"] == [
        {"kind": "keyword", "label": "cat scratcher [exact]", "old": "1.00", "new": "1.20"}
    ]
    assert run.facts["holds"]["HANDS_OFF"] == 1
    assert "有人改过出价（14 天内不碰）" in rig.page().read_text(encoding="utf-8")

    memory = rig.memory()
    goal = memory.goal_named("猫抓板")
    assert goal is not None
    kw1 = memory.objects(goal.id)[("keyword", "kw-1")]
    assert kw1.start_bid == Decimal("1.00"), "起点价是第一次见到它时的价，不跟着人改"
    assert kw1.last_bid == Decimal("1.20")
    assert kw1.hands_off_until == TODAY + timedelta(days=15)
    human = [d for d in memory.decisions(run.id) if d.by == "human"]
    assert [(d.action, d.old_bid, d.new_bid) for d in human] == [
        ("human_change", Decimal("1.00"), Decimal("1.20"))
    ]
    memory.close()

    for _ in range(13):
        rig.clock.tomorrow()
    hot(rig.fake, rig.clock.now.date())
    rig.say("猫抓板现在看一遍")
    assert rig.last_run().facts["holds"]["HANDS_OFF"] == 1, "第 14 天还不碰"
    rig.clock.tomorrow()
    hot(rig.fake, rig.clock.now.date())
    rig.say("猫抓板现在看一遍")
    proposals = rig.last_run().facts["proposals"]
    assert isinstance(proposals, list)
    # 满 14 天可以判了，但只用人改价之后的 7 天：14 天的长窗还够着改价之前。
    assert [
        (p["label"], p["old"], p["new"], p["days"]) for p in proposals if p["kind"] == "keyword"
    ] == [("cat scratcher [exact]", "1.20", "1.02", 7)]


# ------------------------------------------------------------------ 停下与继续


def test_stop_means_it_does_not_even_look(rig: Rig) -> None:
    rig.say(ADOPT)
    assert rig.say("全部停下").startswith("**【关灯】全部停下了**\n\n想接着来就说「继续干活」")
    assert rig.say("猫抓板现在看一遍").startswith("**【关灯】全部停下了，没去看**")
    assert rig.say("现在看一遍").startswith("**【关灯】全部停下了，没去看**")
    assert rig.fake.calls == []
    assert rig.say("看今天").startswith("**【关灯】全部停下了，在休息**")
    assert rig.say("继续干活").startswith("**【绿灯】继续干活**\n\n说「现在看一遍」看看情况")
    rig.say("猫抓板现在看一遍")
    assert rig.fake.calls


# ------------------------------------------------------------------ 设置


def test_a_ceiling_out_of_range_gets_a_safe_value_that_waits_for_yes(rig: Rig) -> None:
    rig.say(ADOPT)
    answer = rig.say("猫抓板最多5%")
    assert answer.startswith(
        "**【黄灯】5% 太低了，用 10% 吧？**\n\n回「好」就用 10%，10 分钟内有效"
    )
    assert rig.say("好").startswith("**【绿灯】猫抓板：ACOS 最多 10%**")
    assert rig.say("好").startswith("**【黄灯】没有等你回答的事**"), "一个「好」只管一次"
    assert rig.say("猫抓板最多90%").startswith("**【黄灯】90% 太高了，用 40% 吧？**")
    rig.clock.now += timedelta(minutes=11)
    assert rig.say("好").startswith("**【黄灯】没有等你回答的事**"), "过了 10 分钟不算数"
    assert rig.say("猫抓板最多25%").startswith("**【绿灯】猫抓板：ACOS 最多 25%**")
    assert rig.say("小狗最多25%").startswith("**【黄灯】没有叫「小狗」的商品**")
    memory = rig.memory()
    goal = memory.goal_named("猫抓板")
    assert goal is not None and goal.target_acos == Decimal("0.25")
    assert [e.detail for e in memory.events(goal.id, 3)] == [
        "ACOS 上限改成 25%",
        "ACOS 上限改成 10%",
        "交给我：美国店 B0TEST0001，叫「猫抓板」",
    ]
    memory.close()


def test_yes_for_a_product_dropped_meanwhile_changes_nothing(rig: Rig) -> None:
    rig.say(ADOPT)
    rig.say("猫抓板最多5%")
    rig.say("不管猫抓板了")
    assert rig.say("好").startswith("**【黄灯】那个商品已经不管了**")


def test_dropping_a_product_keeps_its_memory_for_next_time(rig: Rig) -> None:
    rig.say(ADOPT)
    rig.say("猫抓板现在看一遍")
    answer = rig.say("不管猫抓板了")
    assert answer.startswith("**【绿灯】不管「猫抓板」了**\n\n记录还留着，想管再交给我")
    assert rig.say("看今天").startswith("**【绿灯】还没有交给我的商品**")
    assert rig.say("猫抓板现在看一遍").startswith("**【黄灯】没有叫「猫抓板」的商品**")
    assert rig.say("管 美国店 B0TEST0001 叫 小猫").startswith("**【绿灯】记住了：小猫**")
    memory = rig.memory()
    goal = memory.goal_named("小猫")
    assert goal is not None
    assert ("keyword", "kw-1") in memory.objects(goal.id), "第一次见到的出价还记得"
    memory.close()


def test_handing_over_problems_are_explained(rig: Rig) -> None:
    answer = rig.say("管 德国店 B0TEST0001 叫 猫抓板")
    assert answer.startswith("**【黄灯】没有叫「德国店」的店**\n\n照配置里的店名再说一遍")
    assert "给大人看：配置里的店名（共 1 家）：美国店" in answer
    rig.say(ADOPT)
    assert rig.say("管 美国店 B0TEST0001 叫 小猫").startswith(
        "**【黄灯】这个商品已经交给我了，叫「猫抓板」**\n\n说「猫抓板现在看一遍」"
    )
    assert rig.say("管 美国店 B0OTHER001 叫 猫抓板").startswith(
        "**【黄灯】「猫抓板」这个名字用过了**"
    )


# ------------------------------------------------------------------ 看今天、帮助、听不懂


def test_today_before_any_look_says_what_to_do_next(rig: Rig) -> None:
    rig.say(ADOPT)
    answer = rig.say("看今天")
    assert answer.startswith("**【黄灯】1 个商品：1 黄**\n\n说「猫抓板现在看一遍」")
    assert "| 猫抓板 | 黄灯 | — | — | 还没看过 |" in answer


def test_today_shows_every_product_in_one_table(rig: Rig) -> None:
    hot(rig.fake)
    rig.say(ADOPT)
    rig.say("管 美国店 B0OTHER001 叫 小猫")
    answer = rig.say("现在看一遍")
    # 第二行是最要紧的那个商品的下一步；表里黄灯排在绿灯前面。
    assert answer.startswith("**【黄灯】看了 2 个商品，本来会改 2 处**\n\n说「小猫最多25%」定一个")
    assert "| 小猫 | 黄灯 | 还没定 ACOS 上限 |\n| 猫抓板 | 绿灯 | 本来会改 2 处 |" in answer
    answer = rig.say("看今天")
    assert answer.startswith("**【黄灯】2 个商品：1 绿、1 黄**\n\n点下面的报告看细节")
    assert "| 猫抓板 | 绿灯 | 30% | 27% | 2 处 |" in answer
    assert "| 小猫 | 黄灯 | — | — | 0 处 |" in answer
    assert f"报告：[{rig.page().name}]({rig.page()})" in answer


def test_help_uses_the_real_nickname_and_every_example_works(rig: Rig) -> None:
    assert "| 猫抓板现在看一遍 |" in rig.say("我能说什么"), "还没有商品时拿例子说"
    rig.say("管 美国店 B0TEST0001 叫 小猫")
    answer = rig.say("我能说什么")
    assert answer.startswith("**【绿灯】能说的话在下面**\n\n照着说，一次说一句")
    said = [
        line.split(" | ")[0].removeprefix("| ")
        for line in answer.splitlines()
        if line.startswith("| ")
    ][1:]
    assert said == ["看今天", "小猫现在看一遍", "小猫最多25%", "全部停下 / 继续干活", "不管小猫了"]
    for cell in said:
        for saying in cell.split(" / "):
            assert parse(saying).kind is not Kind.UNKNOWN, f"帮助里教的「{saying}」自己认不出"


def test_anything_else_is_not_guessed(rig: Rig) -> None:
    rig.say(ADOPT)
    answer = rig.say("把猫抓板的出价都降 10%")
    assert answer.startswith("**【黄灯】没听懂**\n\n说「我能说什么」看看能说的话")
    assert rig.fake.calls == []


# ------------------------------------------------------------------ 灯


def test_no_stock_is_a_yellow_light_and_nothing_is_judged(rig: Rig) -> None:
    hot(rig.fake)
    for ad in rig.fake.ads:
        ad.stock = 0
    rig.say(ADOPT)
    answer = rig.say("猫抓板现在看一遍")
    assert answer.startswith("**【黄灯】猫抓板：没库存了，这轮不判**\n\n叫大人补货，补上再让它看")
    run = rig.last_run()
    assert run.facts["proposals"] == []
    assert run.facts["holds"] == {"STOCK_OUT": 4}


def test_orders_halved_is_a_red_light(rig: Rig) -> None:
    w = windows_for(TODAY)
    rig.fake.set(w.before.report_date, "ad-1", clicks=200, orders=20, spend="50", sales="400")
    rig.fake.set(w.long.report_date, "ad-1", clicks=200, orders=5, spend="50", sales="100")
    rig.say(ADOPT)
    answer = rig.say("猫抓板现在看一遍")
    assert answer.startswith("**【红灯】猫抓板：订单比前两周少了一半多**\n\n大人点下面的报告看一眼")
    text = rig.page().read_text(encoding="utf-8")
    assert "订单比前两周少了一半多" in text and "转化率比前两周掉了三成多" in text


def test_spend_jumping_is_a_red_light(rig: Rig) -> None:
    w = windows_for(TODAY)
    rig.fake.set(w.before.report_date, "ad-1", clicks=90, orders=10, spend="30", sales="300")
    rig.fake.set(w.long.report_date, "ad-1", clicks=90, orders=10, spend="40", sales="300")
    rig.say(ADOPT)
    assert rig.say("猫抓板现在看一遍").startswith("**【红灯】猫抓板：花费比前两周多了三成多**")


def test_no_ads_in_flight_is_a_yellow_light(rig: Rig) -> None:
    for ad in rig.fake.ads[:3]:
        ad.state = "paused"
    rig.say(ADOPT)
    assert rig.say("猫抓板现在看一遍").startswith(
        "**【黄灯】猫抓板：没找到在投的广告**\n\n大人看看这个商品还在投广告吗"
    )


def test_ads_switched_off_are_the_headline_not_the_orders_they_cost(rig: Rig) -> None:
    """广告全停了，订单自然少一半：头条说原因（没在投），不说后果（订单腰斩），也不亮红灯。"""
    w = windows_for(TODAY)
    rig.fake.set(w.before.report_date, "ad-1", clicks=200, orders=20, spend="50", sales="400")
    rig.fake.set(w.long.report_date, "ad-1", clicks=20, orders=2, spend="5", sales="40")
    for ad in rig.fake.ads[:3]:
        ad.state = "paused"
    rig.say(ADOPT)
    assert rig.say("猫抓板现在看一遍").startswith("**【黄灯】猫抓板：没找到在投的广告**")
    assert rig.last_run().facts["alerts"] == ["NO_ADS"]


def test_when_nothing_can_be_tuned_it_is_not_a_green_light(rig: Rig) -> None:
    """词全在共用组、领星在管、用组默认价：它一处也调不了，不能天天报「都没事」。"""
    hot(rig.fake)
    for thing in rig.fake.things:
        thing.flags = {**thing.flags, "is_apply_time": True}
    rig.say(ADOPT)
    rig.say("猫抓板最多25%")
    answer = rig.say("猫抓板现在看一遍")
    assert answer.startswith("**【黄灯】猫抓板：能调的词是 0 个**\n\n大人看报告里「它没碰的」")


# ------------------------------------------------------------------ 出错


def test_a_config_others_can_read_stops_it_and_asks_for_an_adult(rig: Rig) -> None:
    rig.setup.config_path.chmod(0o644)
    answer = rig.say(ADOPT)
    first, second, adult = answer.split("\n\n")
    assert first == "**【红灯】配置有问题，叫大人**"
    assert second == "大人照最后一行改好再说一遍"
    assert adult.startswith("给大人看：配置错误：配置文件 ") and adult.endswith(FIX_HINT)
    assert "0644" in adult
    assert "sk-test-key" not in answer and "lx.invalid" not in answer
    rig.setup.config_path.chmod(0o600)
    assert rig.say(ADOPT).startswith("**【绿灯】记住了：猫抓板**")
    rig.setup.config_path.chmod(0o644)
    assert rig.say("猫抓板现在看一遍").startswith("**【红灯】配置有问题，叫大人**")
    assert rig.fake.calls == []


def test_three_failed_looks_in_a_row_pause_the_product(rig: Rig) -> None:
    rig.say(ADOPT)
    rig.fake.failures["ad_campaign_product_report"] = [Refused("no") for _ in range(3)]
    for _ in range(2):
        answer = rig.say("猫抓板现在看一遍")
        # 领星拒绝（权限、参数）不是等一会儿就好的事：叫大人，不叫孩子反复重试。
        assert answer.startswith("**【黄灯】猫抓板：这次没看成**\n\n叫大人看最后一行")
        assert "给大人看：没看成：领星说参数或权限不对（LX_BUSINESS_ERROR）" in answer
    answer = rig.say("猫抓板现在看一遍")
    assert answer.startswith(
        "**【红灯】猫抓板：连着 3 次没看成，歇着了**\n\n大人修好后说「猫抓板现在看一遍」"
    )
    assert "| 猫抓板 | 红灯 | — | — | 歇着了 |" in rig.say("看今天")
    # 「继续干活」连歇着的商品一起叫醒，并且说出来。
    assert rig.say("继续干活").startswith("**【绿灯】继续干活，猫抓板也接着看**")
    memory = rig.memory()
    goal = memory.goal_named("猫抓板")
    assert goal is not None and goal.status == "active" and goal.failures == 0
    memory.close()


def test_a_look_without_a_name_also_tries_products_that_stopped_after_failing(rig: Rig) -> None:
    """人说「现在看一遍」本身就是「修好了，再试试」：歇着的也看，不回「还没有要看的商品」。"""
    rig.say(ADOPT)
    rig.fake.failures["ad_campaign_product_report"] = [Refused("no") for _ in range(3)]
    for _ in range(3):
        rig.say("现在看一遍")
    answer = rig.say("现在看一遍")
    assert answer.startswith("**【黄灯】猫抓板：还没定 ACOS 上限**")
    memory = rig.memory()
    goal = memory.goal_named("猫抓板")
    assert goal is not None and goal.status == "active"
    memory.close()


def test_network_trouble_says_try_again_later_with_the_exact_words(rig: Rig) -> None:
    rig.say(ADOPT)
    rig.fake.failures["ad_campaign_product_report"] = [TransportDown("down") for _ in range(3)]
    answer = rig.say("猫抓板现在看一遍")
    assert answer.startswith("**【黄灯】猫抓板：这次没看成**\n\n过几分钟再说「猫抓板现在看一遍」")
    assert "连不上领星（网络）（LX_TRANSPORT_ERROR）" in answer


def test_several_products_that_all_failed_are_not_called_looked_at(rig: Rig) -> None:
    rig.say(ADOPT)
    rig.say("管 美国店 B0OTHER001 叫 小猫")
    rig.fake.failures["ad_campaign_product_report"] = [TransportDown("down") for _ in range(9)]
    answer = rig.say("现在看一遍")
    assert answer.startswith("**【黄灯】2 个商品这次都没看成**\n\n过几分钟再说「现在看一遍」")


def test_a_product_whose_store_left_the_config_is_not_read(rig: Rig) -> None:
    """店铺表只来自配置（AX-02）：交出来以后大人把美国店删了，就不再去读它。"""
    rig.say(ADOPT)
    rig.setup.config_path.unlink()
    pack.private(rig.tmp, pack.config_text(rig.tmp, [pack.JP]))
    answer = rig.say("猫抓板现在看一遍")
    assert answer.startswith("**【黄灯】猫抓板：这次没看成**\n\n叫大人看最后一行")
    assert "这个商品的店已经不在配置里了（GOAL_STORE_GONE）" in answer
    assert rig.fake.calls == []


def test_a_look_already_running_in_another_conversation_says_busy(rig: Rig) -> None:
    rig.say(ADOPT)
    fd = os.open(rig.tmp / RUN_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        answer = rig.say("猫抓板现在看一遍")
    finally:
        os.close(fd)
    assert answer.startswith("**【黄灯】上一轮还在看**\n\n过几分钟再说一遍")
    assert rig.fake.calls == []


def test_a_memory_it_cannot_open_asks_for_an_adult(rig: Rig) -> None:
    rig.setup.memory_path.mkdir(parents=True)  # 一个目录占着库的位置
    assert rig.say("看今天").startswith("**【红灯】记忆库打不开，叫大人**")


def test_a_memory_written_by_a_newer_version_is_left_alone(rig: Rig) -> None:
    rig.setup.memory_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(rig.setup.memory_path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    answer = rig.say("看今天")
    assert answer.startswith("**【红灯】记忆库打不开，叫大人**")
    assert "升回新版本" in answer


# ------------------------------------------------------------------ 最长的名字


def test_the_longest_names_still_fit_on_a_line(make: Callable[..., Rig]) -> None:
    store = "一二三四五六七八九十一二三四五六七八九十"  # 20 字：配置允许的最长店名
    name = "一二三四五六七八九十一二"  # 12 字：最长的小名
    rig = make([(pack.US[0], pack.US[1], "US", "USD", store)])
    hot(rig.fake)
    rig.say(f"管 {store[::-1]} B0TEST0001 叫 {name}")
    rig.say(f"管 {store} B0TEST0001 叫 {name}")
    rig.say(f"管 {store} B0TEST0001 叫 {name[::-1]}")
    rig.say(f"管 {store} B0OTHER001 叫 {name}")
    rig.say(f"{name}最多5%")
    rig.say(f"{name}最多100%")
    rig.say("好")
    rig.say(f"{name[::-1]}现在看一遍")
    rig.say(f"{name}现在看一遍")
    rig.say("我能说什么")
    rig.say("看今天")
    rig.fake.failures["ad_campaign_product_report"] = [Refused("no") for _ in range(3)]
    for _ in range(3):
        rig.say(f"{name}现在看一遍")
    assert rig.say("看今天").startswith("**【红灯】")
    rig.say(f"不管{name}了")


# ------------------------------------------------------------------ 插件形态的工具


async def test_the_plugin_form_adds_one_tool_that_takes_the_words_as_said(rig: Rig) -> None:
    server = build_server(
        rig.setup.config_path,
        expect_uid=None,
        now_fn=rig.clock,
        source_factory=lambda cfg: pack._seeded_mock(),
        operator=Operator(rig.setup),
    )
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert set(tools) == {TOOL_NAME, OPERATOR_TOOL}
    say = tools[OPERATOR_TOOL]
    assert set(say.input_schema["properties"]) == {"text"}
    assert say.input_schema["required"] == ["text"]
    assert say.description == OPERATOR_RULES
    assert server.instructions == PLUGIN_INSTRUCTIONS
    result = await server.call_tool(OPERATOR_TOOL, {"text": "看今天"})
    assert getattr(result.content[0], "text", "").startswith("**【绿灯】还没有交给我的商品**")


# ------------------------------------------------------------------ 30 天推演

#: kw-1 的行情：这两天之间便宜（ACOS 10%），其余日子贵（ACOS 40%）。上限 25%。
CHEAP_FROM = date(2026, 9, 25)
DEAR_AGAIN = date(2026, 10, 5)


def _per_day(key: str, day: date) -> tuple[int, int, int, Decimal, Decimal] | None:
    """一天的 (曝光, 点击, 订单, 花费, 销售额)。"""
    if key == "kw-1":
        spend = Decimal("1.00") if CHEAP_FROM <= day < DEAR_AGAIN else Decimal("4.00")
        return 100, 8, 1, spend, Decimal("10.00")
    if key == "tg-1":
        return 80, 5, 1, Decimal("2.50"), Decimal("10.00")  # 一直正好 25%
    if key == "ad-1":
        return 500, 20, 2, Decimal("5.00"), Decimal("20.00")
    return None


def _market(window: str, key: str) -> dict[str, object] | None:
    start, end = (date.fromisoformat(part) for part in window.split(" - "))
    days = [_per_day(key, start + timedelta(days=n)) for n in range((end - start).days + 1)]
    if not days or days[0] is None:
        return None
    rows = [d for d in days if d is not None]
    return {
        "impressions": sum(d[0] for d in rows),
        "clicks": sum(d[1] for d in rows),
        "orders": sum(d[2] for d in rows),
        "spend": str(sum(d[3] for d in rows)),
        "sales": str(sum(d[4] for d in rows)),
    }


def test_thirty_days_of_looking_once_a_day(rig: Rig) -> None:
    rig.fake.market = _market
    rig.say(ADOPT)
    rig.say("猫抓板最多25%")
    answers: list[str] = []
    for day in range(30):
        if day == 5:  # 大人新加了一个词
            rig.fake.things.append(
                Thing("keyword", "kw-7", "cmp-1", "ag-1", text="new", created="2026-09-29 10:00")
            )
        if day == 15:  # 大人删掉了一个词
            rig.fake.things = [t for t in rig.fake.things if t.object_id != "kw-2"]
        answers.append(rig.say("猫抓板现在看一遍"))
        rig.clock.tomorrow()

    assert all(a.startswith("**【绿灯】猫抓板：") for a in answers), answers
    assert len(list(rig.setup.report_dir.glob("*.html"))) == 30
    assert len(rig.fake.calls) == 30 * 8

    conn = sqlite3.connect(rig.setup.memory_path)
    assert conn.execute("SELECT status, COUNT(*) FROM runs GROUP BY status").fetchall() == [
        ("OK", 30)
    ]
    made = [
        (object_id, datetime.fromisoformat(started).date(), action, json.loads(ev)["days"], o, n)
        for object_id, started, action, ev, o, n in conn.execute(
            "SELECT d.object_id, r.started_at, d.action, d.evidence, d.old_bid, d.new_bid "
            "FROM decisions d JOIN runs r ON r.id = d.run_id WHERE d.by = 'operator' ORDER BY d.id"
        )
    ]
    conn.close()
    # kw-1：第 0 天贵 → 降（记一笔，冷却）；第 10 天起攒满 7 天改动之后的新数据，便宜 →
    # 只提示可以加，不算改动、不进账；窗口滑进又贵的日子，第 22 天（10-16）长窗 ACOS 29% → 再降。
    # tg-1 一直在上限上，从来不动。
    assert made == [
        ("kw-1", date(2026, 9, 24), "down", 14, "1.00", "0.85"),
        ("kw-1", date(2026, 10, 16), "down", 14, "1.00", "0.86"),
    ]
    # 只用新证据：后一次的窗口起点，晚于前一次「本来会改」的那天。
    for before, after in zip(made, made[1:], strict=False):
        window_start = after[1] - timedelta(days=3 + after[3] - 1)
        assert window_start > before[1]

    memory = rig.memory()
    goal = memory.goal_named("猫抓板")
    assert goal is not None
    remembered = memory.objects(goal.id)
    kw1 = remembered[("keyword", "kw-1")]
    assert kw1.start_bid == kw1.last_bid == Decimal("1.00"), "只看不动：真实出价一分没变"
    assert kw1.last_change == date(2026, 10, 16)
    assert kw1.proposed_bid == Decimal("0.86"), "大人还没照做，建议挂着"
    kw7 = remembered[("keyword", "kw-7")]
    # 交出来以后才冒出来的词：从出现那天起重新攒数，不用它出现之前的数。
    assert kw7.first_seen == kw7.last_change == date(2026, 9, 29)
    assert remembered[("keyword", "kw-2")].gone_at == date(2026, 10, 9)
    runs = memory.recent_runs(goal.id, 30)[::-1]
    # 新词先攒 17 天数据（长窗 14 + 归因 3）；满了才判，这里它没曝光。
    assert runs[5].facts["holds"]["NEW"] == 1
    # 便宜的那几天：加价只是提示，列在 hints 里。
    hints = runs[10].facts["hints"]
    assert isinstance(hints, list)
    assert [(h["label"], h["old"], h["new"], h["days"]) for h in hints] == [
        ("cat scratcher [exact]", "1.00", "1.10", 7)
    ]
    assert runs[29].facts["holds"] == {
        "COOLING": 1,
        "NO_IMPRESSIONS": 1,
        "ON_TARGET": 1,
        "SHARED": 1,
    }
    memory.close()
