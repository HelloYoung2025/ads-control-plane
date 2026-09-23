"""SFW 电商 Pack 组件：一个工具、一层 Bearer 中间件、三种文件产物、时间预算、给人看的话。

全部用 tests/support 的假源与 tmp_path：不触网、不碰 8788/8790、不 import sfw.installer。
文件名里的日期是回看窗口右端日（UTC），本机时区是 +08，所以「本地日期」与「窗口日期」
在这些测试里故意不相等——相等了就测不出用错了哪个钟。
"""

from __future__ import annotations

import ast
import csv
import fcntl
import io
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from ads_control_plane.canonical.entity import (
    AdProduct,
    CanonicalEntityRef,
    EntityType,
    ParentRefs,
    Provider,
)
from ads_control_plane.canonical.money import Money
from ads_control_plane.sfw import server as server_module
from ads_control_plane.sfw.config import ConfigError, StoreConfig, parse_config
from ads_control_plane.sfw.report import (
    MAX_CANDIDATES_IN_TABLE,
    RUN_LOG_HEADER,
    file_stem,
)
from ads_control_plane.sfw.server import (
    DISCIPLINE,
    INSTRUCTIONS,
    TOOL_DESCRIPTION,
    TOOL_NAME,
    BearerMiddleware,
    build_app,
    build_server,
)
from ads_control_plane.sfw.service import (
    RunOutcome,
    StoreRun,
    run_all,
    run_once,
    summarize,
)
from ads_control_plane.strategies.negation import SearchTermRecord, render_bulk_csv, to_bulk_rows
from ads_control_plane.strategies.ports import SearchTermFetch, SearchTermSourceError
from tests.support.mock_search_terms import MockSearchTermSource

REPO = Path(__file__).resolve().parents[2]
ASSETS = Path(server_module.__file__).parent / "assets"

ORG = uuid.UUID("00000000-0000-4000-8000-000000000001")
CONN = uuid.UUID("00000000-0000-4000-8000-000000000002")
BEARER = "0123456789abcdef" * 2
#: 2026-09-19 12:00 UTC → 窗口右端日 = 09-16（退 3 天），左端 = 08-18（闭区间 30 天）。
NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
WINDOW_LAST_DAY = "2026-09-16"

#: 合成 ID（tests/unit/test_no_real_ids_in_repo.py 的 SYNTHETIC_IDS）。
US = ("1000000000000001", "2000000000000001", "US", "USD", "美国店")
JP = ("1000000000000002", "2000000000000002", "JP", "JPY", "日本店")
US_PROFILE, JP_PROFILE = US[0], JP[0]

Store = tuple[str, str, str, str, str]


# ------------------------------------------------------------------ 夹具


def config_text(
    tmp_path: Path,
    stores: list[Store] | None = None,
    *,
    time_budget: int = 2700,
    min_spend: tuple[str, ...] = ('USD = "20.00"', 'JPY = "3000"'),
) -> str:
    blocks = "".join(
        f'\n[[stores]]\nprofile_id = "{p}"\nsid = "{s}"\nmarketplace = "{m}"\n'
        f'currency = "{c}"\nnickname = "{n}"\n'
        for p, s, m, c, n in ([US, JP] if stores is None else stores)
    )
    thresholds = "\n".join(min_spend)
    return f"""\
organization_id = "{ORG}"
connection_id = "{CONN}"
sfw_bearer = "{BEARER}"
export_dir = "{tmp_path / "导出"}"
run_log_path = "{tmp_path / "运行记录.csv"}"
time_budget_seconds = {time_budget}

[lingxing]
url = "http://lx.invalid/mcp"
key = "sk-test-key"
{blocks}
[thresholds]
lookback_days = 30
min_clicks = 25
max_data_staleness_hours = 24

[thresholds.min_spend]
{thresholds}
"""


def private(tmp_path: Path, text: str, mode: int = 0o600) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def record(
    profile: str,
    term: str,
    *,
    clicks: int = 40,
    conversions: int = 0,
    spend: str = "35.00",
    currency: str = "USD",
    as_of: datetime | None = None,
    ad_group: str = "ag-1",
    is_asin: bool = False,
    campaign_name: str | None = "活动甲",
    ad_group_name: str | None = "广告组乙",
) -> SearchTermRecord:
    return SearchTermRecord(
        scope=CanonicalEntityRef(
            organization_id=ORG,
            provider=Provider.MOCK,
            provider_connection_id=CONN,
            marketplace="US",
            shop_external_id="shop-1",
            profile_external_id=profile,
            ad_product=AdProduct.SP,
            entity_type=EntityType.AD_GROUP,
            entity_external_id=ad_group,
            parent_refs=ParentRefs(campaign_external_id="c-1"),
        ),
        search_term=term,
        term_is_asin=is_asin,
        clicks=clicks,
        conversions=conversions,
        spend=Money(amount=spend, currency=currency),
        campaign_name=campaign_name,
        ad_group_name=ad_group_name,
        # 窗口由假源按端口算法现算，这里的值会被覆盖。
        window_start=NOW - timedelta(days=30),
        window_end=NOW - timedelta(days=1),
        data_as_of=NOW - timedelta(hours=2) if as_of is None else as_of,
    )


class FakeSource:
    """按 profile 决定行为的假源：正常 seed、抛带码错误、或返回「行都读不出来」的账目。"""

    def __init__(self) -> None:
        self.mock = MockSearchTermSource()
        self.failing: dict[str, str] = {}
        self.unusable: set[str] = set()

    def seed(self, profile: str, records: list[SearchTermRecord]) -> None:
        self.mock.seed(profile, records)

    def has_profile(self, profile_external_id: str) -> bool:
        return (
            profile_external_id in self.failing
            or profile_external_id in self.unusable
            or self.mock.has_profile(profile_external_id)
        )

    def fetch_search_term_performance(
        self, profile_external_id: str, lookback_days: int, as_of: datetime
    ) -> SearchTermFetch:
        if profile_external_id in self.failing:
            raise SearchTermSourceError(self.failing[profile_external_id], "upstream said no")
        if profile_external_id in self.unusable:
            return SearchTermFetch(
                records=(), source_total=7, unreadable_rows=7, unattributable_rows=7
            )
        return self.mock.fetch_search_term_performance(profile_external_id, lookback_days, as_of)


def _seeded_mock() -> MockSearchTermSource:
    source = MockSearchTermSource()
    source.seed(US_PROFILE, [record(US_PROFILE, "cheap widget")])
    source.seed(JP_PROFILE, [record(JP_PROFILE, "安い部品", spend="5000", currency="JPY")])
    return source


def _run_log_rows(path: Path) -> list[list[str]]:
    assert path.read_bytes().startswith(b"\xef\xbb\xbf"), "运行记录要带 UTF-8 BOM（给 Excel）"
    return list(csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig"))))


#: 空手而归的每一种结局各一家店（CANDIDATES 与 NOT_RUN 另测）。
EMPTY_STORES: list[Store] = [
    ("p-none", "s-none", "US", "USD", "无源店"),
    ("p-unusable", "s-unusable", "US", "USD", "坏行店"),
    ("p-empty", "s-empty", "US", "USD", "空店"),
    ("p-asin", "s-asin", "US", "USD", "ASIN店"),
    ("p-stale", "s-stale", "US", "USD", "旧店"),
    ("p-clean", "s-clean", "US", "USD", "干净店"),
    ("p-fail", "s-fail", "US", "USD", "断网店"),
    ("p-eur", "s-eur", "JP", "JPY", "错币店"),
]


def _every_empty_outcome(tmp_path: Path) -> tuple[tuple[StoreRun, ...], str, Path]:
    cfg = parse_config(config_text(tmp_path, EMPTY_STORES))
    source = FakeSource()
    source.unusable.add("p-unusable")
    source.seed("p-empty", [])
    source.seed("p-asin", [record("p-asin", "b0demo0001", is_asin=True)])
    source.seed("p-stale", [record("p-stale", "old widget", as_of=NOW - timedelta(hours=48))])
    source.seed("p-clean", [record("p-clean", "sold widget", conversions=3)])
    source.failing["p-fail"] = "LX_TRANSPORT_ERROR"
    # 店铺币种是 JPY，行却是 USD：域层带码拒绝（CURRENCY_MISMATCH）。
    source.seed("p-eur", [record("p-eur", "mismatch", currency="USD")])
    runs = run_all(cfg, source, now=NOW)
    return runs, summarize(runs, cfg), cfg.export_dir


# ------------------------------------------------------------------ 1. 工具面


async def test_only_one_tool_is_exposed(tmp_path: Path) -> None:
    server = build_server(
        private(tmp_path, config_text(tmp_path)),
        expect_uid=None,
        now_fn=lambda: NOW,
        source_factory=lambda cfg: _seeded_mock(),
    )
    tools = await server.list_tools()
    assert {t.name for t in tools} == {"find_wasted_search_terms"}
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,80}", tools[0].name)
    # 无参数：店铺、门槛、目录全来自配置，模型没有机会编一个 profile_id 进来。
    assert tools[0].input_schema.get("properties", {}) == {}
    assert tools[0].description == TOOL_DESCRIPTION
    assert server.instructions == INSTRUCTIONS
    assert server.name == "amazon-ads"


# ------------------------------------------------------------------ 2. 文件产物


def test_run_writes_csv_html_and_log_into_the_export_dir(tmp_path: Path) -> None:
    cfg = parse_config(config_text(tmp_path))
    source = _seeded_mock()
    source.seed(
        US_PROFILE,
        [record(US_PROFILE, "cheap widget"), record(US_PROFILE, "bad widget", ad_group="ag-2")],
    )
    runs = run_all(cfg, source, now=NOW)
    assert [run.outcome for run in runs] == [RunOutcome.CANDIDATES, RunOutcome.CANDIDATES]
    for run in runs:
        assert run.csv_path is not None and run.html_path is not None
        assert run.csv_path.parent == run.html_path.parent == cfg.export_dir
        frozen = run.candidate_set
        assert frozen is not None and frozen.set_hash is not None
        # CSV 逐字节 = render_bulk_csv(to_bulk_rows(set), name_of=…)：BOM、8 列、公式引导符中和。
        expected = render_bulk_csv(to_bulk_rows(frozen), name_of=lambda *_: None)
        assert run.csv_path.read_bytes() == expected.encode("utf-8")
        assert run.csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
        stem = f"{run.store.nickname}-{WINDOW_LAST_DAY}-{frozen.content_fingerprint()[:8]}"
        assert run.csv_path.name == f"否定词-{stem}.csv"
        assert run.html_path.name == f"报表-{stem}.html"
        assert run.csv_path.stat().st_mode & 0o777 == 0o644
        assert run.html_path.stat().st_mode & 0o777 == 0o644
    # 目录里只有正式文件，没有落下的临时文件。
    assert sorted(p.name for p in cfg.export_dir.iterdir()) == sorted(
        p.name for run in runs for p in (run.csv_path, run.html_path) if p is not None
    )
    rows = _run_log_rows(cfg.run_log_path)
    assert rows[0] == list(RUN_LOG_HEADER)
    assert [r[1:3] for r in rows[1:]] == [["美国店", "CANDIDATES"], ["日本店", "CANDIDATES"]]
    us = rows[1]
    # 「时间」是本地时间带偏移：解析回来是同一瞬间，偏移与本机一致，不带微秒。
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}", us[0]), us[0]
    assert datetime.fromisoformat(us[0]) == NOW
    assert datetime.fromisoformat(us[0]).utcoffset() == NOW.astimezone().utcoffset()
    assert us[3:8] == ["2", "2", "2", "0", "0"]
    assert runs[0].candidate_set is not None
    assert us[8] == runs[0].candidate_set.content_fingerprint()
    assert us[9] == runs[0].candidate_set.set_hash
    assert runs[0].csv_path is not None and us[10] == runs[0].csv_path.name


def test_run_log_time_is_the_local_wall_clock_with_its_offset(tmp_path: Path) -> None:
    """运行记录的「时间」给管理员对着墙上的钟核对「刚才那次」；写 UTC 的话 +08 的人会觉得
    每行都早了 8 小时。偏移写在值里，换了时区也还原得出同一瞬间。这里把本机时区钉成东京，
    断言才与跑测试的机器无关。"""
    cfg = parse_config(config_text(tmp_path, [US]))
    before = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Tokyo"
    time.tzset()
    try:
        run_all(cfg, MockSearchTermSource(), now=NOW)
    finally:
        if before is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = before
        time.tzset()
    stamp = _run_log_rows(cfg.run_log_path)[1][0]
    assert stamp == "2026-09-19T21:00:00+09:00"
    assert datetime.fromisoformat(stamp) == NOW


# ------------------------------------------------------------------ 3. 每店一条链接


def test_summary_has_a_link_per_store_and_no_256_run(tmp_path: Path) -> None:
    stores: list[Store] = [
        (f"p-{i:02d}", f"s-{i:02d}", "US", "USD", f"店{i:02d}") for i in range(1, 21)
    ]
    cfg = parse_config(config_text(tmp_path, stores, min_spend=('USD = "20.00"',)))
    source = MockSearchTermSource()
    for profile, *_ in stores:
        source.seed(profile, [record(profile, "cheap widget")])
    text = summarize(run_all(cfg, source, now=NOW), cfg)
    export = re.escape(str(cfg.export_dir))
    for *_, nickname in stores:
        name = rf"否定词-{nickname}-{WINDOW_LAST_DAY}-[0-9a-f]{{8}}\.csv"
        assert re.search(rf"文件：\[{name}\]\({export}/{name}\)", text), nickname
        assert text.count(f"**{nickname}**：") == 1
    blocks = text.split("\n\n")
    assert len(blocks) == 21, "20 家店各一块 + 末尾门槛一行"
    assert all(len(block.split("\n")) == 2 for block in blocks[:-1]), "每店固定两行"
    assert blocks[-1].startswith("门槛：")
    # §3-45：不含 ≥256 位连续 [A-Za-z0-9+/_-]；不含密钥形态串。
    assert not re.search(r"[A-Za-z0-9+/_-]{256,}", text)
    assert "sk-test-key" not in text and BEARER not in text


# ------------------------------------------------------------------ 4. 空手而归要说清原因


def _block(text: str, nickname: str) -> str:
    return next(b for b in text.split("\n\n") if b.startswith(f"**{nickname}**："))


def _assert_report_link_only(line: str, nickname: str, export_dir: Path) -> None:
    name = rf"报表-{nickname}-{WINDOW_LAST_DAY}-[0-9a-f]{{8}}\.html"
    assert re.fullmatch(rf"报表：\[{name}\]\({re.escape(str(export_dir))}/{name}\)", line), line


def test_overrunning_the_budget_leaves_a_line_in_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """预算不是整次调用的上界，那就得说得出这次跑了多久。

    最后开跑的那家店整个跑在预算之外；网关退化时整次调用可能越过 SFW 登记的工具超时，
    而孩子那边读到的是「工具没连上」——服务其实还在跑，doctor 也查不到（服务是活的）。
    日志里这一行是管理员唯一能拿到的证据。
    """
    stores: list[Store] = [("p-1", "s-1", "US", "USD", "店01")]
    cfg = parse_config(config_text(tmp_path, stores, time_budget=60, min_spend=('USD = "20.00"',)))
    # run_all 按顺序读表三次：起点、这家店开跑前、全部跑完之后。
    # 开跑前预算还剩得多（10 < 60），跑完已经 500 秒——正是"最后一家整个跑在预算之外"。
    ticks = iter([0.0, 10.0, 500.0])

    with caplog.at_level(logging.WARNING, logger="ads_control_plane.sfw"):
        runs = run_all(cfg, MockSearchTermSource(), now=NOW, monotonic=lambda: next(ticks))
    assert [run.outcome for run in runs] != [RunOutcome.NOT_RUN], "这家店本该真的跑了"
    assert [r.getMessage() for r in caplog.records if "超过时间预算" in r.getMessage()], (
        "跑超了预算却一行日志都没有"
    )


def test_nobody_can_be_judged_leaves_a_line_in_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """这句话以「找管理员」结尾，那么日志里就得有东西等着他。

    它此前一行日志都不写，而 doctor 也管不到这类（只看配置、权限、端口与名录），
    于是管理员被那句话指向一条不存在的排查路径。顺带：这条分支在一行都没坏时也会
    走到（同一个广告组的行对不上活动），所以那句话不能再预设「一行都读不出来」。
    """
    with caplog.at_level(logging.ERROR, logger="ads_control_plane.sfw"):
        runs, _, _ = _every_empty_outcome(tmp_path)
    assert RunOutcome.NO_USABLE_ROWS in [run.outcome for run in runs]
    lines = [r.getMessage() for r in caplog.records if "没有一组能判断" in r.getMessage()]
    assert lines, "没有一组能判断的那家店，日志里一行都没有"
    assert "坏行店" in lines[0], "日志得说是哪家店"
    assert "整组没判断" in lines[0], "两种成因得分得开：账目本身就是分法"


def test_stores_that_differ_only_by_name_share_one_line_at_the_end(tmp_path: Path) -> None:
    # 2026-09-23 首次真实运行：74 家店里 66 家是同一句「这段时间没有搜索词数据」。
    stores: list[Store] = [
        ("p-e1", "s-e1", "US", "USD", "空一"),
        ("p-clean", "s-clean", "US", "USD", "干净店"),
        ("p-e2", "s-e2", "US", "USD", "空二"),
        ("p-f1", "s-f1", "US", "USD", "断网一"),
        ("p-e3", "s-e3", "US", "USD", "空三"),
        ("p-f2", "s-f2", "US", "USD", "断网二"),
        ("p-deny", "s-deny", "US", "USD", "被拒店"),
    ]
    cfg = parse_config(config_text(tmp_path, stores))
    source = FakeSource()
    for profile in ("p-e1", "p-e2", "p-e3"):
        source.seed(profile, [])
    source.seed("p-clean", [record("p-clean", "sold widget", conversions=3)])
    source.failing |= {
        "p-f1": "LX_TRANSPORT_ERROR",
        "p-f2": "LX_TRANSPORT_ERROR",
        "p-deny": "LX_GATEWAY_ERROR",
    }
    blocks = summarize(run_all(cfg, source, now=NOW), cfg).split("\n\n")[:-1]
    # 只出现一次的话留在原位；有报表的店（两行）永不合并；合并的放在最后。
    assert [b.split("：")[0] for b in blocks] == [
        "**干净店**",
        "**被拒店**",
        "**空一、空二、空三**",
        "**断网一、断网二**",
    ]
    assert blocks[2] == "**空一、空二、空三**：这段时间没有搜索词数据。"
    assert blocks[3].startswith("**断网一、断网二**：取数失败（LX_TRANSPORT_ERROR）")


def test_an_empty_run_says_which_reason_it_was(tmp_path: Path) -> None:
    runs, text, export_dir = _every_empty_outcome(tmp_path)
    assert [run.outcome for run in runs] == [
        RunOutcome.NO_DATA_SOURCE,
        RunOutcome.NO_USABLE_ROWS,
        RunOutcome.NO_ROWS,
        RunOutcome.ALL_ASIN,
        RunOutcome.ALL_ABSTAINED,
        RunOutcome.NO_CANDIDATES,
        RunOutcome.SOURCE_ERROR,
        RunOutcome.DATA_REJECTED,
    ]
    one_liners = {
        "无源店": "这家店没接上数据源，找管理员。",
        "坏行店": "取到了 7 行，但没有一组能判断，找管理员。",
        "空店": "这段时间没有搜索词数据。",
        "断网店": "取数失败（LX_TRANSPORT_ERROR），文件没有更新；等 1 分钟，"
        "敲 /new 回车，再敲 /fd 回车回车，还不行找管理员。",
        "错币店": "数据不合规（CURRENCY_MISMATCH），文件没有更新，找管理员。",
    }
    for nickname, sentence in one_liners.items():
        assert _block(text, nickname) == f"**{nickname}**：{sentence}"
    with_report = {
        "ASIN店": "花了钱没出单的全是 ASIN（1 个），否定词挡不住，"
        "要去领星「否定投放」单独处理：b0demo0001。",
        "旧店": "数据太旧（超过 24 小时），这次没法判断；晚点敲 /new 回车，再敲 /fd 回车回车。",
        "干净店": "看了 1 组（去重 1 个词），没有要否定的词。这不等于没有浪费：门槛以下的词不算。",
    }
    for nickname, sentence in with_report.items():
        first, second = _block(text, nickname).split("\n")
        assert first == f"**{nickname}**：{sentence}"
        _assert_report_link_only(second, nickname, export_dir)
    assert text.split("\n\n")[-1] == (
        f"门槛：统计 2026-08-18 到 {WINDOW_LAST_DAY}（最后几天的订单还没结算完，不算进来）；"
        "花费 ≥ 20.00 USD、3000 JPY（按店币种）；点击 ≥ 25。"
        "有一些数据读不懂、已经跳过：上面每家店的结论只覆盖读得懂的那部分。"
        "\n本工具不改任何广告。"
    ), "一家店都没有 CSV 时，末尾不出现「把 CSV 交给管理员」那句，但「不改任何广告」要在"
    # 只有判定跑到了头的三家店有报表；谁都没有 CSV。
    assert sorted(p.name.split("-")[1] for p in export_dir.iterdir()) == [
        "ASIN店",
        "干净店",
        "旧店",
    ]
    assert not list(export_dir.glob("否定词-*"))


# ------------------------------------------------------------------ 5. ASIN 只按形状进文本


def test_asin_terms_reach_the_text_only_in_asin_shape(tmp_path: Path) -> None:
    cfg = parse_config(config_text(tmp_path))
    source = MockSearchTermSource()
    source.seed(
        US_PROFILE,
        [
            record(US_PROFILE, "cheap widget"),
            record(US_PROFILE, "b0demo0001", ad_group="ag-2", is_asin=True),
            record(US_PROFILE, "asin-like weird", ad_group="ag-3", is_asin=True),
        ],
    )
    source.seed(
        JP_PROFILE,
        [
            record(JP_PROFILE, "売れた", conversions=2, spend="5000", currency="JPY"),
            record(
                JP_PROFILE,
                "B0JPDEMO01",
                ad_group="ag-2",
                is_asin=True,
                spend="5000",
                currency="JPY",
            ),
        ],
    )
    runs = run_all(cfg, source, now=NOW)
    text = summarize(runs, cfg)
    assert [run.outcome for run in runs] == [RunOutcome.CANDIDATES, RunOutcome.NO_CANDIDATES]
    us, jp = text.split("\n\n")[:2]
    assert us.startswith(
        "**美国店**：看了 3 组（去重 3 个词），要否定 1 个。另有 2 个是 ASIN，否定词挡不住，"
        "要去领星「否定投放」单独处理：b0demo0001（其中 1 个的写法不像 ASIN，见报表）。\n文件："
    )
    # NO_CANDIDATES 那家店里的 ASIN 同样要说：不说，人会把「没有要否定的词」读成「这家店没浪费」。
    assert jp.startswith(
        "**日本店**：看了 2 组（去重 2 个词），没有要否定的词。这不等于没有浪费：门槛以下的词不算。"
        "另有 1 个是 ASIN，否定词挡不住，要去领星「否定投放」单独处理：B0JPDEMO01。\n报表："
    )
    assert "asin-like weird" not in text, "不像 ASIN 的弃权词是外部自由文本，只进报表"
    assert "cheap widget" not in text, "候选关键词永不进返回文本（AX-15）"
    assert runs[0].html_path is not None
    html = runs[0].html_path.read_text(encoding="utf-8")
    assert "asin-like weird" in html and "cheap widget" in html


# ------------------------------------------------------------------ 6. 同一批数据 = 同一个文件


def test_rerunning_the_same_data_overwrites_the_same_file(tmp_path: Path) -> None:
    cfg = parse_config(config_text(tmp_path, [US], min_spend=('USD = "20.00"',)))
    source = _seeded_mock()
    first = run_all(cfg, source, now=NOW)[0]
    second = run_all(cfg, source, now=NOW + timedelta(hours=3))[0]
    assert first.csv_path == second.csv_path and first.html_path == second.html_path
    assert first.csv_path is not None and first.html_path is not None
    assert sorted(p.name for p in cfg.export_dir.iterdir()) == sorted(
        [first.csv_path.name, first.html_path.name]
    )
    rows = _run_log_rows(cfg.run_log_path)
    assert len(rows) == 3
    assert rows[1][1:3] == rows[2][1:3] == ["美国店", "CANDIDATES"]
    assert rows[1][8] == rows[2][8], "同一批数据，同一个内容指纹"
    assert rows[1][9] != rows[2][9], "set_hash 绑定的是这一份冻结（候选编号进 hash），每次都不同"


# ------------------------------------------------------------------ 7. 单店失败不中断


def test_source_error_for_one_store_does_not_hide_the_others(tmp_path: Path) -> None:
    cfg = parse_config(config_text(tmp_path))
    source = FakeSource()
    source.seed(US_PROFILE, [record(US_PROFILE, "cheap widget")])
    source.failing[JP_PROFILE] = "LX_GATEWAY_ERROR"
    runs = run_all(cfg, source, now=NOW)
    text = summarize(runs, cfg)
    assert [run.outcome for run in runs] == [RunOutcome.CANDIDATES, RunOutcome.SOURCE_ERROR]
    assert runs[1].error_code == "LX_GATEWAY_ERROR"
    assert "**美国店**：看了 1 组（去重 1 个词），要否定 1 个。\n文件：[否定词-美国店-" in text
    assert (
        "**日本店**：取数失败（LX_GATEWAY_ERROR），文件没有更新；等 1 分钟，"
        "敲 /new 回车，再敲 /fd 回车回车，还不行找管理员。"
    ) in text
    assert not list(cfg.export_dir.glob("*日本店*"))
    assert [r[1:3] for r in _run_log_rows(cfg.run_log_path)[1:]] == [
        ["美国店", "CANDIDATES"],
        ["日本店", "SOURCE_ERROR"],
    ]


# ------------------------------------------------------------------ 8. 报表自包含


def test_html_report_is_self_contained(tmp_path: Path) -> None:
    cfg = parse_config(config_text(tmp_path, [US], min_spend=('USD = "20.00"',)))
    source = MockSearchTermSource()
    source.seed(
        US_PROFILE,
        [
            record(US_PROFILE, "=cmd|calc"),
            record(US_PROFILE, "cheap widget <b>", ad_group="ag-2", campaign_name="活动<甲>"),
            record(US_PROFILE, "b0demo0001", ad_group="ag-3", is_asin=True),
            record(US_PROFILE, "old widget", ad_group="ag-4", as_of=NOW - timedelta(hours=48)),
        ],
    )
    run = run_all(cfg, source, now=NOW)[0]
    assert run.outcome is RunOutcome.CANDIDATES
    assert run.html_path is not None and run.csv_path is not None
    frozen = run.candidate_set
    assert frozen is not None and frozen.set_hash is not None
    html = run.html_path.read_text(encoding="utf-8")
    assert "<script" not in html.lower()
    assert "http://" not in html and "https://" not in html
    assert "<link" not in html.lower() and "src=" not in html.lower()
    assert frozen.set_hash[:16] in html and frozen.content_fingerprint() in html
    assert (
        f"<title>报表-美国店-{WINDOW_LAST_DAY}-{frozen.content_fingerprint()[:8]}</title>" in html
    )
    # 点名带前置单引号的词：CSV 里多了一个引号，报表上是原词，两处一起才算说清。
    assert "有 1 个词以 = + - @ 这类字符开头（<code>=cmd|calc</code>）" in html
    assert "单引号" in html
    assert "'=cmd|calc" in run.csv_path.read_text(encoding="utf-8-sig")
    # 外部文本只作数据：逐字转义。
    assert "cheap widget &lt;b&gt;" in html and "<b>" not in html
    assert "活动&lt;甲&gt;" in html
    # ASIN、其余弃权、账目、门槛都在。
    assert "b0demo0001" in html and "否定投放" in html
    assert "old widget" in html and "STALE_DATA" in html
    assert "上游总行数" in html and "20.00 USD" in html and "点击 ≥ 25" in html
    assert f"统计 2026-08-18 到 {WINDOW_LAST_DAY}（最后几天的订单还没结算完，不算进来）" in html


def test_every_candidate_reaches_the_csv_and_only_the_table_is_cut(tmp_path: Path) -> None:
    """第 201 个候选必须拿得到。

    2026-09-22 Codex 合入前复审 P2：200 此前是**冻结**上限，于是第 201 个候选在任何一次
    运行里都不出现，而报表写着「处理完这批，再敲 /fd」——那句话暗含一个没验过的假设
    （上游 targeted_type=not_negatived 会在人加完否定词后把这批排掉）。假设不成立时那些
    词就是永远拿不到。现在只截表格：人拿去执行的是 CSV，它恒含全部候选。
    """
    cfg = parse_config(config_text(tmp_path, [US], min_spend=('USD = "20.00"',)))
    source = MockSearchTermSource()
    source.seed(
        US_PROFILE,
        [
            record(US_PROFILE, f"word {i}", ad_group=f"ag-{i:03d}", spend=f"{20 + i}.00")
            for i in range(MAX_CANDIDATES_IN_TABLE + 1)
        ],
    )
    run = run_all(cfg, source, now=NOW)[0]
    assert run.candidate_set is not None and run.csv_path is not None and run.html_path is not None
    kept = {c.search_term for c in run.candidate_set.candidates}
    assert len(kept) == MAX_CANDIDATES_IN_TABLE + 1, "冻结集合不截断：CSV 要给全"
    assert "word 0" in kept, "最便宜的那个也在里面——它此前被永久截掉，谁都拿不到"
    assert run.candidate_set.truncated_from is None, "集合没被截，就不能声称被截过"
    text = summarize([run], cfg)
    assert "要否定 201 个（报表表格只列花费最高的 200 个，CSV 里是全部）。" in text
    html = run.html_path.read_text(encoding="utf-8")
    assert "本次命中 201 个候选" in html and "CSV 里是全部 201 个" in html
    table = html.split("<h2>要否定的词")[1].split("</table>")[0]
    assert table.count("<tr>") == 1 + MAX_CANDIDATES_IN_TABLE, "表头 + 200 行"
    assert ">word 0<" not in table, "表格按花费截，最便宜的那个不上表"
    csv_lines = run.csv_path.read_text(encoding="utf-8-sig").splitlines()
    assert len(csv_lines) == 1 + 201, "CSV 里必须是全部候选——这正是复审 P2 指的那条"


# ------------------------------------------------------------------ 9. 文件名日期 = 窗口右端日


def test_filename_date_is_the_window_end_not_the_local_clock(tmp_path: Path) -> None:
    cfg = parse_config(config_text(tmp_path, [US], min_spend=('USD = "20.00"',)))
    source = _seeded_mock()
    late = datetime(2026, 9, 19, 23, 30, tzinfo=UTC)  # 本机（+08）此刻已是 09-20 07:30
    next_utc_day = datetime(2026, 9, 20, 0, 30, tzinfo=UTC)
    same_utc_day = datetime(2026, 9, 19, 1, 0, tzinfo=UTC)
    on_late = run_all(cfg, source, now=late)[0]
    on_next = run_all(cfg, source, now=next_utc_day)[0]
    on_same = run_all(cfg, source, now=same_utc_day)[0]
    assert on_late.csv_path is not None and on_next.csv_path is not None
    assert WINDOW_LAST_DAY in on_late.csv_path.name
    assert "2026-09-19" not in on_late.csv_path.name and "2026-09-20" not in on_late.csv_path.name
    assert "2026-09-17" in on_next.csv_path.name, "跨过 UTC 日界，窗口右端日 +1"
    assert on_late.csv_path == on_same.csv_path, "同一窗口、同一批数据 → 同一个文件"
    assert on_late.csv_path != on_next.csv_path
    store = StoreConfig(profile_id="p", sid="s", marketplace="US", currency="USD", nickname="店")
    end_utc = datetime(2026, 9, 17, 0, 0, tzinfo=UTC)
    end_in_plus8 = datetime(2026, 9, 17, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    assert file_stem(store, end_utc, "abcdef0123456789") == "店-2026-09-16-abcdef01"
    assert file_stem(store, end_in_plus8, "abcdef0123456789") == "店-2026-09-16-abcdef01"


# ------------------------------------------------------------------ 10. 时间预算


def test_time_budget_reports_stores_not_reached(tmp_path: Path) -> None:
    stores: list[Store] = [(f"p-{i}", f"s-{i}", "US", "USD", f"店0{i}") for i in (1, 2, 3)]
    cfg = parse_config(config_text(tmp_path, stores, time_budget=60, min_spend=('USD = "20.00"',)))
    clock = {"t": 0.0}

    class SlowSource(MockSearchTermSource):
        def fetch_search_term_performance(
            self, profile_external_id: str, lookback_days: int, as_of: datetime
        ) -> SearchTermFetch:
            clock["t"] += 50.0
            return super().fetch_search_term_performance(profile_external_id, lookback_days, as_of)

    source = SlowSource()
    for profile, *_ in stores:
        source.seed(profile, [record(profile, "cheap widget")])
    runs = run_all(cfg, source, now=NOW, monotonic=lambda: clock["t"])
    assert [run.outcome for run in runs] == [
        RunOutcome.CANDIDATES,
        RunOutcome.CANDIDATES,
        RunOutcome.NOT_RUN,
    ]
    text = summarize(runs, cfg)
    assert "**店03**：本轮没轮到（时间不够）；敲 /new 回车，再敲 /fd 回车回车。" in text
    assert text.endswith(
        "本工具不改任何广告。有文件的店：把 CSV 交给管理员，他在领星「否定词」里加上才算数。"
    ), "有 CSV 就要告诉孩子交给谁——整条链上此前唯一没写的一环"
    assert "[否定词-店01-" in text and "[否定词-店02-" in text and "店03-" not in text
    assert source.read_call_count == 2, "没轮到的店一次数都不取"
    assert [r[1:3] for r in _run_log_rows(cfg.run_log_path)[1:]] == [
        ["店01", "CANDIDATES"],
        ["店02", "CANDIDATES"],
        ["店03", "NOT_RUN"],
    ]


def test_a_store_skipped_for_time_goes_first_next_time(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """预算只够跑一家时连问三次，三家店各轮到一次（2026-09-23 Codex 复审 P2）。

    此前每次都按店铺表从头跑：排在后面的店每一次都是「没轮到」，「再问一次」永远推不到它们。
    """
    stores: list[Store] = [(f"p-{i}", f"s-{i}", "US", "USD", f"店0{i}") for i in (1, 2, 3)]
    cfg = parse_config(config_text(tmp_path, stores, time_budget=60, min_spend=('USD = "20.00"',)))
    clock = {"t": 0.0}

    class SlowSource(MockSearchTermSource):
        def fetch_search_term_performance(
            self, profile_external_id: str, lookback_days: int, as_of: datetime
        ) -> SearchTermFetch:
            clock["t"] += 100.0
            return super().fetch_search_term_performance(profile_external_id, lookback_days, as_of)

    source = SlowSource()
    for profile, *_ in stores:
        source.seed(profile, [record(profile, "cheap widget")])
    turns = []
    for minute in range(3):
        runs = run_all(
            cfg, source, now=NOW + timedelta(minutes=minute), monotonic=lambda: clock["t"]
        )
        assert [run.store.nickname for run in runs] == ["店01", "店02", "店03"], "回答按店铺表念"
        turns += [run.store.nickname for run in runs if run.outcome is not RunOutcome.NOT_RUN]
    assert turns == ["店01", "店02", "店03"]

    # 运行记录读不懂（比如被 Excel 另存成 GBK）：按店铺表顺序跑，留一行 WARNING，不失败。
    cfg.run_log_path.write_bytes("时间,店铺,结局\n".encode("gbk"))
    with caplog.at_level(logging.WARNING, logger="ads_control_plane.sfw"):
        runs = run_all(cfg, source, now=NOW + timedelta(hours=1), monotonic=lambda: clock["t"])
    assert [run.outcome for run in runs][0] is not RunOutcome.NOT_RUN
    assert any("读不懂" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------ 11/12. 配置错误


def test_config_refuses_group_readable_file_empty_store_table_and_missing_currency_threshold(
    tmp_path: Path,
) -> None:
    source = _seeded_mock()

    def refused(text: str, mode: int) -> str:
        with pytest.raises(ConfigError) as info:
            run_once(private(tmp_path, text, mode), expect_uid=None, now=NOW, source=source)
        return info.value.code

    assert refused(config_text(tmp_path), 0o644) == "CONFIG_TOO_OPEN"
    assert refused(config_text(tmp_path, []), 0o600) == "STORES_EMPTY"
    assert refused(config_text(tmp_path, min_spend=('USD = "20.00"',)), 0o600) == (
        "CURRENCY_THRESHOLD_MISSING"
    )
    assert source.read_call_count == 0, "配置没过，一次数都不取"
    assert not (tmp_path / "导出").exists()


async def test_config_error_becomes_a_tool_error_not_a_crash(tmp_path: Path) -> None:
    path = private(tmp_path, config_text(tmp_path), 0o644)
    server = build_server(
        path, expect_uid=None, now_fn=lambda: NOW, source_factory=lambda cfg: _seeded_mock()
    )
    with pytest.raises(ToolError) as info:
        await server.call_tool(TOOL_NAME, {})
    assert type(info.value) is ToolError, (
        "是预见到的失败，不是崩溃（崩溃只剩 Error executing tool，人看不到原因）"
    )
    # mcp 2.1.1 给每个 ToolError 加前缀「Error executing tool <name>: 」（2026-09-19 实测，
    # tools/base.py:207）；我们那句话原样跟在后面，模型读到的就是这一整行。
    message = str(info.value)
    assert message.startswith(f"Error executing tool {TOOL_NAME}: 配置错误：")
    assert message.endswith("找管理员")
    assert str(info.value.__cause__).startswith("配置错误：")
    assert "0644" in message and "\n" not in message
    # 进程没退出、服务器没换：管理员修好文件，下一次调用就正常。
    path.chmod(0o600)
    result = await server.call_tool(TOOL_NAME, {})
    text = getattr(result.content[0], "text", "")
    assert text.startswith(
        "**美国店**：看了 1 组（去重 1 个词），要否定 1 个。\n文件：[否定词-美国店-"
    )


async def test_the_data_source_is_reused_across_calls_until_the_config_changes(
    tmp_path: Path,
) -> None:
    built: list[MockSearchTermSource] = []

    def factory(cfg: object) -> MockSearchTermSource:
        built.append(_seeded_mock())
        return built[-1]

    path = private(tmp_path, config_text(tmp_path))
    server = build_server(path, expect_uid=None, now_fn=lambda: NOW, source_factory=factory)
    await server.call_tool(TOOL_NAME, {})
    await server.call_tool(TOOL_NAME, {})
    assert len(built) == 1 and built[0].read_call_count == 4, (
        "两次调用共用一个源（取数缓存才有意义）"
    )
    path.write_text(config_text(tmp_path, min_spend=('USD = "25.00"', 'JPY = "3000"')))
    await server.call_tool(TOOL_NAME, {})
    assert len(built) == 2, "配置变了就重建"


class _SlowSource:
    """把每次取数拖长一点并记下起止：并发的两次调用会重叠，串行的不会。"""

    def __init__(self, inner: MockSearchTermSource) -> None:
        self.inner = inner
        self.spans: list[tuple[float, float]] = []

    def has_profile(self, profile_external_id: str) -> bool:
        return self.inner.has_profile(profile_external_id)

    def fetch_search_term_performance(self, *args: Any, **kwargs: Any) -> SearchTermFetch:
        started = time.monotonic()
        time.sleep(0.05)
        result = self.inner.fetch_search_term_performance(*args, **kwargs)
        self.spans.append((started, time.monotonic()))
        return result


async def test_two_calls_at_once_run_one_after_the_other(tmp_path: Path) -> None:
    slow = _SlowSource(_seeded_mock())
    path = private(tmp_path, config_text(tmp_path))
    server = build_server(
        path, expect_uid=None, now_fn=lambda: NOW, source_factory=lambda cfg: slow
    )
    async with anyio.create_task_group() as tg:
        tg.start_soon(server.call_tool, TOOL_NAME, {})
        tg.start_soon(server.call_tool, TOOL_NAME, {})
    spans = sorted(slow.spans)
    assert len(spans) == 4, "两次调用各取两家店"
    assert all(
        a_end <= b_start for (_, a_end), (b_start, _) in zip(spans, spans[1:], strict=False)
    ), "两个对话同时敲 /fd 也不并发打领星"


async def test_a_run_in_another_process_is_waited_for_not_raced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """插件形态下每个对话一个进程，进程里的锁管不到别的对话（2026-09-23 Codex 复审 P2）。

    「另一个进程」用另开的一份文件描述符来扮：flock 认的是打开的文件，不是进程号。
    """
    monkeypatch.setattr(server_module, "LOCK_WAIT_SECONDS", 0.3)
    source = _seeded_mock()
    path = private(tmp_path, config_text(tmp_path))
    server = build_server(
        path, expect_uid=None, now_fn=lambda: NOW, source_factory=lambda cfg: source
    )
    other = os.open(tmp_path / server_module.RUN_LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(other, fcntl.LOCK_EX)
        result = await server.call_tool(TOOL_NAME, {})
        assert getattr(result.content[0], "text", "") == server_module.BUSY
        assert source.read_call_count == 0, "别的对话在取数时，这边一次都不取"
    finally:
        os.close(other)
    result = await server.call_tool(TOOL_NAME, {})
    assert getattr(result.content[0], "text", "").startswith("**美国店**")


# ------------------------------------------------------------------ 13. Bearer


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "sfw-test", "version": "0"},
    },
}
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}

_Scope = MutableMapping[str, Any]
_Receive = Callable[[], Awaitable[Any]]
_Send = Callable[[Any], Awaitable[None]]
_App = Callable[[_Scope, _Receive, _Send], Awaitable[None]]


@asynccontextmanager
async def _serving(app: _App) -> AsyncIterator[httpx2.AsyncClient]:
    """手动驱动 ASGI lifespan（httpx 的 ASGITransport 不发它，而 SDK 的会话管理器靠它启动）。"""
    startup, shutdown, done = anyio.Event(), anyio.Event(), anyio.Event()
    handed_startup = False

    async def receive() -> dict[str, str]:
        nonlocal handed_startup
        if not handed_startup:
            handed_startup = True
            return {"type": "lifespan.startup"}
        await shutdown.wait()
        return {"type": "lifespan.shutdown"}

    async def send(message: Any) -> None:
        if message["type"] == "lifespan.startup.complete":
            startup.set()
        elif message["type"] == "lifespan.shutdown.complete":
            done.set()
        else:
            raise AssertionError(message)

    async with anyio.create_task_group() as tg:
        tg.start_soon(app, {"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)
        with anyio.fail_after(10):
            await startup.wait()
        try:
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8790"
            ) as client:
                yield client
        finally:
            shutdown.set()
            with anyio.fail_after(10):
                await done.wait()


def _sse_result(text: str) -> dict[str, Any]:
    line = next(line for line in text.splitlines() if line.startswith("data: "))
    payload: dict[str, Any] = json.loads(line[len("data: ") :])
    return dict(payload["result"])


async def test_wrong_bearer_gets_401_and_right_bearer_passes(tmp_path: Path) -> None:
    path = private(tmp_path, config_text(tmp_path))
    app = build_app(
        path, expect_uid=None, now_fn=lambda: NOW, source_factory=lambda cfg: _seeded_mock()
    )
    async with _serving(app) as client:
        bare = await client.post("/mcp", json=INIT, headers=MCP_HEADERS)
        assert bare.status_code == 401 and bare.json() == {"error": "unauthorized"}
        wrong = await client.post(
            "/mcp", json=INIT, headers={**MCP_HEADERS, "authorization": f"Bearer {BEARER[:-1]}0"}
        )
        assert wrong.status_code == 401 and wrong.json() == {"error": "unauthorized"}
        with anyio.fail_after(10):
            right = await client.post(
                "/mcp", json=INIT, headers={**MCP_HEADERS, "authorization": f"Bearer {BEARER}"}
            )
        assert right.status_code == 200
        result = _sse_result(right.text)
        assert result["serverInfo"]["name"] == "amazon-ads"
        assert result["instructions"] == INSTRUCTIONS
        # 口令读不到（配置文件权限放宽了）→ 对的口令也进不来：fail closed。
        path.chmod(0o644)
        again = await client.post(
            "/mcp", json=INIT, headers={**MCP_HEADERS, "authorization": f"Bearer {BEARER}"}
        )
        assert again.status_code == 401


async def test_no_auth_mode_lets_a_bare_request_through_and_shouts_about_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = private(tmp_path, config_text(tmp_path))
    with caplog.at_level(logging.WARNING, logger="ads_control_plane.sfw"):
        app = build_app(path, expect_uid=None, no_auth=True, now_fn=lambda: NOW)
    assert any("NO AUTH" in r.getMessage() for r in caplog.records)
    async with _serving(app) as client:
        with anyio.fail_after(10):
            bare = await client.post("/mcp", json=INIT, headers=MCP_HEADERS)
        assert bare.status_code == 200


async def test_bearer_middleware_is_pure_asgi() -> None:
    tree = ast.parse(Path(server_module.__file__).read_text(encoding="utf-8"))
    imported = {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for name in (
            [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
        )
    }
    assert not {name for name in imported if name.split(".")[0] == "starlette"}, imported
    seen: list[str] = []
    sent: list[Any] = []

    async def inner(scope: _Scope, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    async def receive() -> dict[str, str]:
        return {"type": "http.request"}

    async def send(message: Any) -> None:
        sent.append(message)

    secret: dict[str, str | None] = {"value": "s3cret"}
    middleware = BearerMiddleware(inner, lambda: secret["value"])
    await middleware({"type": "lifespan"}, receive, send)
    await middleware(
        {"type": "http", "headers": [(b"authorization", b"Bearer s3cret")]}, receive, send
    )
    await middleware(
        {"type": "http", "headers": [(b"authorization", b"bearer s3cret")]}, receive, send
    )
    await middleware(
        {"type": "http", "headers": [(b"authorization", b"Basic s3cret")]}, receive, send
    )
    await middleware(
        {"type": "http", "headers": [(b"authorization", b"Bearer s3cret ")]}, receive, send
    )
    await middleware({"type": "http", "headers": []}, receive, send)
    secret["value"] = None
    await middleware(
        {"type": "http", "headers": [(b"authorization", b"Bearer s3cret")]}, receive, send
    )
    await middleware({"type": "websocket", "headers": []}, receive, send)
    assert seen == ["lifespan", "http", "http", "http"], (
        "lifespan 透传；Bearer 大小写不敏感、尾随空白容忍；WebSocket 不放进去"
    )
    statuses = [m["status"] for m in sent if m["type"] == "http.response.start"]
    assert statuses == [401, 401, 401]
    assert [m for m in sent if m["type"] == "websocket.close"] == [
        {"type": "websocket.close", "code": 1008}
    ]
    assert all(
        m["body"] == b'{"error":"unauthorized"}' for m in sent if m["type"] == "http.response.body"
    )


# ------------------------------------------------------------------ 14. 不读环境变量


def test_src_reads_no_environment_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    offenders = sorted(
        p.relative_to(REPO).as_posix()
        for p in (REPO / "src").rglob("*.py")
        if re.search(r"os\.environ|getenv|environ\[", p.read_text(encoding="utf-8"))
    )
    assert offenders == [], "凭据只存在于 0600 的配置文件里，不进环境变量"
    path = private(tmp_path, config_text(tmp_path))
    before = run_once(path, expect_uid=None, now=NOW, source=_seeded_mock())
    monkeypatch.setenv("LX_MCP_KEY", "env-must-not-matter")
    monkeypatch.setenv("LX_MCP_URL", "http://env.invalid/mcp")
    monkeypatch.setenv("ADS_PACK_CONFIG", str(tmp_path / "elsewhere.toml"))
    after = run_once(path, expect_uid=None, now=NOW, source=_seeded_mock())
    assert before == after


# ------------------------------------------------------------------ 15. 无人值守承诺


PROMISES = (
    "会定时",
    "定时跑",
    "定时运行",
    "自动运行",
    "自动跑",
    "每天自动",
    "已批准",
    "已生效",
    "已上传",
)


def _without_the_rule_that_forbids_them(text: str) -> str:
    # 纪律第 5 条本身列出了这些短语（「永远不说…」），那一行不算承诺。
    return "\n".join(line for line in text.splitlines() if "永远不说" not in line)


def test_model_facing_text_does_not_promise_unattended_runs(tmp_path: Path) -> None:
    _, summary, _ = _every_empty_outcome(tmp_path)
    cfg = parse_config(config_text(tmp_path))
    summary += "\n" + summarize(run_all(cfg, _seeded_mock(), now=NOW), cfg)
    texts = {
        "assets/AGENTS.md": (ASSETS / "AGENTS.md").read_text(encoding="utf-8"),
        "assets/fd.md": (ASSETS / "fd.md").read_text(encoding="utf-8"),
        "INSTRUCTIONS": INSTRUCTIONS,
        "TOOL_DESCRIPTION": TOOL_DESCRIPTION,
        "summarize()": summary,
        "README.md": (REPO / "README.md").read_text(encoding="utf-8"),
    }
    for name, text in texts.items():
        found = [p for p in PROMISES if p in _without_the_rule_that_forbids_them(text)]
        assert not found, f"{name} 里出现了无人值守/已生效承诺：{found}"
    # 守卫要能被证伪：不排除第 5 条时它必须命中，否则它可能只是恒真。
    assert [p for p in PROMISES if p in DISCIPLINE]


def test_agents_md_and_fd_md_carry_exactly_the_frozen_texts() -> None:
    assert (ASSETS / "AGENTS.md").read_text(encoding="utf-8") == DISCIPLINE + "\n"
    fd = (ASSETS / "fd.md").read_text(encoding="utf-8")
    assert fd == "找出所有店铺里花了钱却没出单的搜索词，做成否定词表。\n"
    assert fd.strip() in DISCIPLINE, "纪律第 1 条引用的就是 /fd 那句话，两处必须逐字相同"
    assert INSTRUCTIONS == TOOL_DESCRIPTION == DISCIPLINE
