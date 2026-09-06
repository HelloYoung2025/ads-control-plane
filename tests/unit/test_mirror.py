"""镜像层（DEC-117）测试：快照校验、append-only 仓库、四报表同步器。"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ads_control_plane.mirror.repository import InMemorySnapshotRepository
from ads_control_plane.mirror.snapshot import AdObjectSnapshot, SnapshotError
from ads_control_plane.mirror.sync import (
    CATALOG_VERSION,
    TOOL_CAMPAIGN_REPORT,
    TOOL_GROUP_REPORT,
    TOOL_KEYWORD_REPORT,
    TOOL_PRODUCT_REPORT,
    TOOL_TARGETING_REPORT,
    SyncEngine,
    SyncError,
)
from ads_control_plane.tasks.directive import ObjectLevel

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
PROFILE = "profile-1"


def make_snapshot(**overrides: object) -> AdObjectSnapshot:
    base: dict[str, object] = {
        "object_key": "campaign:c1",
        "level": ObjectLevel.CAMPAIGN,
        "profile_id": PROFILE,
        "source_as_of": NOW,
        "recorded_at": NOW,
        "catalog_version": CATALOG_VERSION,
        "schema_version": "ad_campaign_report-v1",
    }
    base.update(overrides)
    return AdObjectSnapshot(**base)  # type: ignore[arg-type]


class FakeReadPort:
    """脚本化读端口：按 tool_id 提供分页行数据，并记录每次调用参数。"""

    def __init__(
        self,
        pages: dict[str, list[list[dict[str, object]]]],
        totals: dict[str, int | None] | None = None,
    ) -> None:
        self._pages = pages
        self._totals = totals or {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    def fetch_page(self, tool_id: str, params: Mapping[str, object]) -> Mapping[str, object]:
        self.calls.append((tool_id, dict(params)))
        page = params["page"]
        assert isinstance(page, int)
        tool_pages = self._pages.get(tool_id, [])
        rows: list[dict[str, object]] = tool_pages[page - 1] if page <= len(tool_pages) else []
        return {"rows": rows, "total": self._totals.get(tool_id)}


def campaign_row(**overrides: object) -> dict[str, object]:
    # 2026-08-28 真实环境实测：campaign 报表行内本对象名就叫 name。
    row: dict[str, object] = {
        "campaign_id": "c1",
        "name": "HX02-auto",
        "state": "enabled",
        "budget": "10.50",
        "targeting_type": "auto",
        "is_apply_time": 0,
        "spends": "1.23",
        "sales": "4.56",
        "acos": "0.27",
        "orders": 3,
        "clicks": 10,
        "impressions": 100,
    }
    row.update(overrides)
    return row


def group_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "ad_group_id": "g1",
        "name": "Group-A",
        "campaign_id": "c1",
        "state": "enabled",
        "default_bid": "0.75",
        "is_apply_time": False,
    }
    row.update(overrides)
    return row


def product_row(**overrides: object) -> dict[str, object]:
    """「广告」层（商品）行。字段名取自 2026-08-29 真实网关实测的
    ad_campaign_product_report 返回：自身 id 是 ad_id，可读身份优先 title。"""
    row: dict[str, object] = {
        "ad_id": "a1",
        "asin": "B0TEST0001",
        "sku": "HX02-BLK-M",
        "title": "Wireless Charger Stand 15W",
        "campaign_id": "c1",
        "ad_group_id": "g1",
        "state": "enabled",
        "bid": "0.88",
        "default_bid": "0.70",
        "is_apply_time": False,
        "spends": "12.34",
    }
    row.update(overrides)
    return row


def targeting_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "target_id": "t1",
        "campaign_id": "c1",
        "ad_group_id": "g1",
        "state": "enabled",
        "bid": "0.66",
        "default_bid": "0.70",
        "expression": "close-match",
        "is_apply_time": 1,
    }
    row.update(overrides)
    return row


def keyword_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "keyword_id": "k1",
        "keyword_text": "wireless charger",
        "match_type": "exact",
        "targeting_type": "manual",
        "campaign_id": "c1",
        "ad_group_id": "g1",
        "state": "enabled",
        "bid": "0.55",
        "default_bid": "0.70",
        "is_apply_time": True,
        "spends": "9.99",
    }
    row.update(overrides)
    return row


def full_pages() -> dict[str, list[list[dict[str, object]]]]:
    return {
        TOOL_CAMPAIGN_REPORT: [[campaign_row()]],
        TOOL_GROUP_REPORT: [[group_row()]],
        TOOL_PRODUCT_REPORT: [[product_row()]],
        TOOL_TARGETING_REPORT: [[targeting_row()]],
        TOOL_KEYWORD_REPORT: [[keyword_row()]],
    }


def make_engine(
    pages: dict[str, list[list[dict[str, object]]]],
    totals: dict[str, int | None] | None = None,
    allowed: tuple[str, ...] = (PROFILE,),
) -> tuple[SyncEngine, FakeReadPort, InMemorySnapshotRepository]:
    port = FakeReadPort(pages, totals)
    repo = InMemorySnapshotRepository()
    return SyncEngine(port, repo, allowed), port, repo


# ---------------------------------------------------------------- snapshot


def test_snapshot_rejects_object_key_level_mismatch() -> None:
    with pytest.raises(SnapshotError) as exc:
        make_snapshot(object_key="ad_group:g1", level=ObjectLevel.CAMPAIGN)
    assert exc.value.code == "OBJECT_KEY_LEVEL_MISMATCH"


def test_snapshot_rejects_empty_external_id_in_object_key() -> None:
    with pytest.raises(SnapshotError) as exc:
        make_snapshot(object_key="campaign:")
    assert exc.value.code == "OBJECT_KEY_LEVEL_MISMATCH"


def test_snapshot_rejects_naive_recorded_at() -> None:
    with pytest.raises(SnapshotError) as exc:
        make_snapshot(recorded_at=datetime(2026, 8, 28, 12, 0))
    assert exc.value.code == "NAIVE_DATETIME_REJECTED"


def test_snapshot_rejects_naive_source_as_of() -> None:
    with pytest.raises(SnapshotError) as exc:
        make_snapshot(source_as_of=datetime(2026, 8, 28, 12, 0))
    assert exc.value.code == "NAIVE_DATETIME_REJECTED"


def test_snapshot_rejects_empty_profile_id() -> None:
    with pytest.raises(SnapshotError) as exc:
        make_snapshot(profile_id="  ")
    assert exc.value.code == "PROFILE_ID_REQUIRED"


def test_snapshot_keyword_lives_in_target_level() -> None:
    snap = make_snapshot(
        object_key="target:k1",
        level=ObjectLevel.TARGET,
        keyword_text="wireless charger",
        match_type="exact",
        bid=Decimal("0.55"),
    )
    assert snap.level is ObjectLevel.TARGET
    assert snap.keyword_text == "wireless charger"


# ---------------------------------------------------------------- repository


def test_repository_current_takes_latest_per_object_key() -> None:
    repo = InMemorySnapshotRepository()
    repo.append(make_snapshot(state="enabled"))
    repo.append(make_snapshot(state="paused", recorded_at=NOW + timedelta(minutes=5)))
    repo.append(make_snapshot(object_key="campaign:c2", state="enabled"))
    current = repo.current(PROFILE)
    assert [s.object_key for s in current] == ["campaign:c1", "campaign:c2"]
    assert current[0].state == "paused"


def test_repository_current_filters_profile_and_level() -> None:
    repo = InMemorySnapshotRepository()
    repo.append(make_snapshot())
    repo.append(make_snapshot(object_key="ad_group:g1", level=ObjectLevel.AD_GROUP))
    repo.append(make_snapshot(object_key="campaign:c9", profile_id="other-profile"))
    assert len(repo.current(PROFILE)) == 2
    only_groups = repo.current(PROFILE, level=ObjectLevel.AD_GROUP)
    assert [s.object_key for s in only_groups] == ["ad_group:g1"]
    assert repo.current("unknown-profile") == []


def test_repository_history_is_ascending() -> None:
    repo = InMemorySnapshotRepository()
    repo.append(make_snapshot(state="paused", recorded_at=NOW + timedelta(hours=1)))
    repo.append(make_snapshot(state="enabled", recorded_at=NOW))
    history = repo.history("campaign:c1")
    assert [s.state for s in history] == ["enabled", "paused"]
    assert repo.history("campaign:none") == []


def test_repository_is_append_only() -> None:
    repo = InMemorySnapshotRepository()
    mutating = [
        name
        for name in dir(repo)
        if not name.startswith("_")
        and any(word in name.lower() for word in ("delete", "remove", "update", "clear", "pop"))
    ]
    assert mutating == []  # DEC-117：仓库不提供任何删改入口


# ---------------------------------------------------------------- sync engine


def test_sync_engine_rejects_empty_whitelist() -> None:
    with pytest.raises(SyncError) as exc:
        SyncEngine(FakeReadPort({}), InMemorySnapshotRepository(), ())
    assert exc.value.code == "SYNC_NO_ALLOWED_PROFILES"


def test_sync_rejects_profile_outside_whitelist_before_any_fetch() -> None:
    engine, port, _ = make_engine(full_pages())
    with pytest.raises(SyncError) as exc:
        engine.run("profile-outside")
    assert exc.value.code == "SYNC_PROFILE_NOT_ALLOWED"
    assert port.calls == []  # fail-closed：一页都不许拉


def test_sync_maps_four_reports_and_merges_keyword_into_target() -> None:
    engine, _, repo = make_engine(full_pages())
    report = engine.run(PROFILE)
    assert report.per_level_rows == {"CAMPAIGN": 1, "AD_GROUP": 1, "AD": 1, "TARGET": 2}
    targets = repo.current(PROFILE, level=ObjectLevel.TARGET)
    assert {s.object_key for s in targets} == {"target:t1", "target:k1"}
    keyword = next(s for s in targets if s.object_key == "target:k1")
    assert keyword.keyword_text == "wireless charger"
    assert keyword.match_type == "exact"
    assert keyword.bid == Decimal("0.55")
    assert keyword.parent_campaign_id == "c1"
    assert keyword.parent_ad_group_id == "g1"
    assert keyword.targeting_type == "manual"  # [手动]/[自动] 徽标数据源


def test_sync_maps_product_report_into_ad_level() -> None:
    """「广告」层（商品）：自身 id 取 ad_id、名称优先 title、父链两级都在。
    2026-08-29 Owner 反馈工作台缺这一层，数据源 ad_campaign_product_report。"""
    engine, _, repo = make_engine(full_pages())
    engine.run(PROFILE)
    ads = repo.current(PROFILE, level=ObjectLevel.AD)
    assert [s.object_key for s in ads] == ["ad:a1"]
    ad = ads[0]
    assert ad.name == "Wireless Charger Stand 15W"
    assert ad.parent_campaign_id == "c1"
    assert ad.parent_ad_group_id == "g1"
    assert ad.bid == Decimal("0.88")
    assert ad.metrics["spends"] == "12.34"
    assert ad.schema_version == "ad_campaign_product_report-v1"


def test_sync_product_name_falls_back_to_sku_then_asin() -> None:
    """标题缺失时退回 SKU、再退 ASIN——运营认得这两个，认不得 ad_id；绝不用 id 冒充名称。"""
    pages = full_pages()
    pages[TOOL_PRODUCT_REPORT] = [[product_row(title=None, product_name=None)]]
    engine, _, repo = make_engine(pages)
    engine.run(PROFILE)
    assert repo.current(PROFILE, level=ObjectLevel.AD)[0].name == "HX02-BLK-M"

    pages[TOOL_PRODUCT_REPORT] = [[product_row(title=None, product_name=None, sku=None)]]
    engine2, _, repo2 = make_engine(pages)
    engine2.run(PROFILE)
    assert repo2.current(PROFILE, level=ObjectLevel.AD)[0].name == "B0TEST0001"


def test_sync_maps_current_values_per_level() -> None:
    engine, _, repo = make_engine(full_pages())
    engine.run(PROFILE)
    campaign = repo.current(PROFILE, level=ObjectLevel.CAMPAIGN)[0]
    assert campaign.daily_budget == Decimal("10.50")
    assert campaign.name == "HX02-auto"
    assert campaign.targeting_type == "auto"
    assert campaign.is_apply_time is False  # int 0 兼容
    assert campaign.metrics["spends"] == "1.23"
    assert campaign.metrics["orders"] == "3"  # 原样字符串，不做数值演绎
    group = repo.current(PROFILE, level=ObjectLevel.AD_GROUP)[0]
    assert group.default_bid == Decimal("0.75")
    assert group.parent_campaign_id == "c1"
    target = next(
        s for s in repo.current(PROFILE, level=ObjectLevel.TARGET) if s.object_key == "target:t1"
    )
    assert target.bid == Decimal("0.66")
    assert target.name == "close-match"
    assert target.is_apply_time is True  # int 1 兼容
    assert target.targeting_type is None  # 报表没给就留 None，不猜


def test_sync_skips_and_counts_summary_rows() -> None:
    pages = full_pages()
    pages[TOOL_CAMPAIGN_REPORT] = [[campaign_row(), campaign_row(campaign_id=None)]]
    pages[TOOL_GROUP_REPORT] = [[group_row(ad_group_id=None), group_row()]]
    engine, _, repo = make_engine(pages)
    report = engine.run(PROFILE)
    assert report.skipped_summary_rows == 2
    assert report.per_level_rows == {"CAMPAIGN": 1, "AD_GROUP": 1, "AD": 1, "TARGET": 2}
    assert len(repo.current(PROFILE)) == 5


def test_sync_counts_decimal_parse_failures_without_raising() -> None:
    pages = full_pages()
    pages[TOOL_CAMPAIGN_REPORT] = [[campaign_row(budget="not-a-number")]]
    pages[TOOL_KEYWORD_REPORT] = [[keyword_row(bid="", default_bid="oops")]]
    engine, _, repo = make_engine(pages)
    report = engine.run(PROFILE)
    assert report.decimal_parse_failures == 3
    campaign = repo.current(PROFILE, level=ObjectLevel.CAMPAIGN)[0]
    assert campaign.daily_budget is None
    keyword = next(
        s for s in repo.current(PROFILE, level=ObjectLevel.TARGET) if s.object_key == "target:k1"
    )
    assert keyword.bid is None
    assert keyword.default_bid is None


def test_sync_missing_money_fields_are_none_not_failures() -> None:
    pages = full_pages()
    pages[TOOL_CAMPAIGN_REPORT] = [[campaign_row(budget=None)]]
    engine, _, repo = make_engine(pages)
    report = engine.run(PROFILE)
    assert report.decimal_parse_failures == 0
    assert repo.current(PROFILE, level=ObjectLevel.CAMPAIGN)[0].daily_budget is None


def test_sync_pagination_stops_when_total_reached() -> None:
    pages = full_pages()
    pages[TOOL_CAMPAIGN_REPORT] = [
        [campaign_row(campaign_id="c1")],
        [campaign_row(campaign_id="c2")],
        [campaign_row(campaign_id="c3")],  # total=2：不得拉到第三页
    ]
    engine, port, _ = make_engine(pages, totals={TOOL_CAMPAIGN_REPORT: 2})
    report = engine.run(PROFILE, page_size=1)
    campaign_calls = [params for tool, params in port.calls if tool == TOOL_CAMPAIGN_REPORT]
    assert [p["page"] for p in campaign_calls] == [1, 2]
    assert report.per_level_rows["CAMPAIGN"] == 2


def test_sync_pagination_stops_on_empty_page() -> None:
    pages = full_pages()
    pages[TOOL_GROUP_REPORT] = [[group_row()], []]  # total 未知：拉到空页为止
    engine, port, _ = make_engine(pages)
    report = engine.run(PROFILE)
    group_calls = [params for tool, params in port.calls if tool == TOOL_GROUP_REPORT]
    assert [p["page"] for p in group_calls] == [1, 2]
    assert report.pages_fetched == 10  # total 未知时五工具均拉到空页为止：5 × 2 页


def test_sync_pagination_respects_max_pages() -> None:
    pages = {
        TOOL_CAMPAIGN_REPORT: [[campaign_row(campaign_id=f"c{i}")] for i in range(1, 6)],
        TOOL_GROUP_REPORT: [[group_row()]],
        TOOL_PRODUCT_REPORT: [[product_row()]],
        TOOL_TARGETING_REPORT: [[targeting_row()]],
        TOOL_KEYWORD_REPORT: [[keyword_row()]],
    }
    engine, port, _ = make_engine(pages)
    engine.run(PROFILE, page_size=1, max_pages=2)
    campaign_calls = [params for tool, params in port.calls if tool == TOOL_CAMPAIGN_REPORT]
    assert [p["page"] for p in campaign_calls] == [1, 2]


def test_sync_params_follow_pinned_required_sets() -> None:
    engine, port, _ = make_engine(full_pages())
    engine.run(PROFILE, window_days=7, page_size=50)
    by_tool = dict(port.calls)
    for tool_id, params in by_tool.items():
        # 2026-08-29 实测：product 报表必填 profile_id 单数（网关要 JSON number，
        # 最终编码在适配层钉扎）；其余报表族用 profile_ids 数组。
        if tool_id == TOOL_PRODUCT_REPORT:
            assert params["profile_id"] == PROFILE
            assert "profile_ids" not in params
        else:
            assert params["profile_ids"] == [PROFILE]
        assert isinstance(params["page"], int) and isinstance(params["length"], int)
        report_date = params["report_date"]
        assert isinstance(report_date, str) and " - " in report_date
        if tool_id == TOOL_GROUP_REPORT:
            assert "sort_field" not in params  # group 报表 required 无排序
        else:
            assert (params["sort_field"], params["sort_type"]) == ("spends", "desc")
    assert by_tool[TOOL_GROUP_REPORT]["with_ring"] == 0
    assert by_tool[TOOL_TARGETING_REPORT]["with_ring"] == 0
    assert by_tool[TOOL_KEYWORD_REPORT]["with_ring"] == 0
    assert "with_ring" not in by_tool[TOOL_CAMPAIGN_REPORT]
    assert "with_ring" not in by_tool[TOOL_PRODUCT_REPORT]


def test_sync_filters_to_serving_states_by_default() -> None:
    """默认只同步在投放的对象——2026-08-29 实测某店铺 1829 个活动里 1800+ 已归档，
    不筛状态时镜像前 200 行几乎全是暂停/归档的老广告。keyword 报表无 state 入参。"""
    engine, port, _ = make_engine(full_pages())
    engine.run(PROFILE, window_days=7, page_size=50)
    by_tool = dict(port.calls)
    for tool_id in (
        TOOL_CAMPAIGN_REPORT,
        TOOL_GROUP_REPORT,
        TOOL_PRODUCT_REPORT,
        TOOL_TARGETING_REPORT,
    ):
        assert by_tool[tool_id]["state"] == "enabled_paused"
    assert "state" not in by_tool[TOOL_KEYWORD_REPORT]


def test_sync_states_none_disables_the_filter() -> None:
    """显式传 None 才拉归档对象——回溯历史时的逃生舱，不是默认。"""
    engine, port, _ = make_engine(full_pages())
    engine.run(PROFILE, window_days=7, page_size=50, states=None)
    for _tool_id, params in port.calls:
        assert "state" not in params


def test_sync_targeting_object_id_prefers_keyword_then_target_then_key() -> None:
    pages = full_pages()
    pages[TOOL_TARGETING_REPORT] = [
        [
            targeting_row(keyword_id="kw9", target_id="t9"),
            targeting_row(target_id="t8"),
            targeting_row(target_id=None, key="raw-key-7"),
            targeting_row(target_id=None, key=None),  # 三者皆空 → 汇总行
        ]
    ]
    pages[TOOL_KEYWORD_REPORT] = [[]]
    engine, _, repo = make_engine(pages)
    report = engine.run(PROFILE)
    targets = {s.object_key for s in repo.current(PROFILE, level=ObjectLevel.TARGET)}
    assert targets == {"target:kw9", "target:t8", "target:raw-key-7"}
    assert report.skipped_summary_rows == 1


def test_sync_is_apply_time_tolerates_unknown_types() -> None:
    pages = full_pages()
    pages[TOOL_CAMPAIGN_REPORT] = [[campaign_row(is_apply_time="yes")]]
    pages[TOOL_GROUP_REPORT] = [[group_row(is_apply_time=None)]]
    engine, _, repo = make_engine(pages)
    engine.run(PROFILE)
    assert repo.current(PROFILE, level=ObjectLevel.CAMPAIGN)[0].is_apply_time is None
    assert repo.current(PROFILE, level=ObjectLevel.AD_GROUP)[0].is_apply_time is None


def test_sync_records_versions_per_tool() -> None:
    engine, _, repo = make_engine(full_pages())
    report = engine.run(PROFILE)
    assert set(report.catalog_versions) == {
        TOOL_CAMPAIGN_REPORT,
        TOOL_GROUP_REPORT,
        TOOL_PRODUCT_REPORT,
        TOOL_TARGETING_REPORT,
        TOOL_KEYWORD_REPORT,
    }
    assert set(report.catalog_versions.values()) == {CATALOG_VERSION}
    assert report.schema_versions[TOOL_KEYWORD_REPORT] == "ad_campaign_keyword_report-v1"
    keyword = next(
        s for s in repo.current(PROFILE, level=ObjectLevel.TARGET) if s.object_key == "target:k1"
    )
    assert keyword.schema_version == "ad_campaign_keyword_report-v1"
    assert keyword.catalog_version == CATALOG_VERSION
    assert report.started_at.tzinfo is not None and report.finished_at.tzinfo is not None


def test_sync_second_run_updates_current_and_keeps_history() -> None:
    pages = full_pages()
    engine, _, repo = make_engine(pages)
    engine.run(PROFILE)
    pages[TOOL_CAMPAIGN_REPORT] = [[campaign_row(state="paused")]]
    engine.run(PROFILE)
    current = repo.current(PROFILE, level=ObjectLevel.CAMPAIGN)
    assert len(current) == 1
    assert current[0].state == "paused"  # 现值来自最新一轮
    history = repo.history("campaign:c1")
    assert [s.state for s in history] == ["enabled", "paused"]  # 历史 append-only 保留


# ---------------------------------------------------------------- 覆盖如实告知与续拉
# 2026-08-29 排查 P0（workbench-1/lxchannel-3）：此前 run() 到达页数上限直接跳出，
# 上游给出的 total 只用于提前退出、从不外传，界面把截断样本当店铺全貌展示。
# 以下锚点钉死三件事：截断必须说得出口、续拉必须真的从断点继续、拉全时不得谎报截断。


def test_sync_truncation_is_reported_not_silent() -> None:
    """截断时报告必须写明：哪张表断的、上游共多少行、已覆盖多少、从哪页续。"""
    pages = {
        TOOL_CAMPAIGN_REPORT: [[campaign_row(campaign_id=f"c{i}")] for i in range(1, 6)],
        TOOL_GROUP_REPORT: [[group_row()]],
        TOOL_PRODUCT_REPORT: [[product_row()]],
        TOOL_TARGETING_REPORT: [[targeting_row()]],
        TOOL_KEYWORD_REPORT: [[keyword_row()]],
    }
    engine, _, _ = make_engine(pages, totals={TOOL_CAMPAIGN_REPORT: 5})
    report = engine.run(PROFILE, page_size=1, max_pages=2)
    assert report.truncated is True
    campaign_cov = next(c for c in report.coverage if c.tool_id == TOOL_CAMPAIGN_REPORT)
    assert campaign_cov.truncated is True
    assert campaign_cov.source_total == 5
    assert campaign_cov.rows_covered == 2
    assert campaign_cov.next_page == 3
    assert report.next_pages() == {TOOL_CAMPAIGN_REPORT: 3}


def test_sync_complete_run_reports_no_truncation() -> None:
    """拉全时 truncated 必须为 False、无续拉入口——不得把完整同步谎报成截断。"""
    engine, _, _ = make_engine(full_pages(), totals={TOOL_CAMPAIGN_REPORT: 1})
    report = engine.run(PROFILE)
    assert report.truncated is False
    assert report.next_pages() == {}
    assert all(c.next_page is None for c in report.coverage)


def test_sync_continuation_resumes_from_next_page_same_window() -> None:
    """续拉从断点页开始、复用同一窗口；max_pages 按本轮页数计，续拉不被上限卡死。"""
    pages = {
        TOOL_CAMPAIGN_REPORT: [[campaign_row(campaign_id=f"c{i}")] for i in range(1, 6)],
        TOOL_GROUP_REPORT: [[group_row()]],
        TOOL_PRODUCT_REPORT: [[product_row()]],
        TOOL_TARGETING_REPORT: [[targeting_row()]],
        TOOL_KEYWORD_REPORT: [[keyword_row()]],
    }
    engine, port, _ = make_engine(pages, totals={TOOL_CAMPAIGN_REPORT: 5})
    first = engine.run(PROFILE, page_size=1, max_pages=2)
    port.calls.clear()
    second = engine.run(
        PROFILE,
        page_size=1,
        max_pages=2,
        tool_cursors=first.tool_cursors(),
        report_date=first.report_date,
    )
    campaign_calls = [params for tool, params in port.calls if tool == TOOL_CAMPAIGN_REPORT]
    # 从第 3 页接着拉，且窗口与第一轮逐字一致（否则两轮的行不可比）。
    assert [p["page"] for p in campaign_calls] == [3, 4]
    assert all(p["report_date"] == first.report_date for p in campaign_calls)
    assert second.report_date == first.report_date
    campaign_cov = next(c for c in second.coverage if c.tool_id == TOOL_CAMPAIGN_REPORT)
    assert campaign_cov.start_page == 3
    assert campaign_cov.rows_covered == 4  # 上一轮实测 2 行 + 本轮 2 行
    assert campaign_cov.next_page == 5
    # 其余四张表第一轮就拉全了 → 本轮一页都不该打。此前它们从 next_pages() 里
    # 消失，被 .get(tool_id, 1) 读成"从第 1 页开始"，于是每续拉一次就整张重拉一遍。
    for tool in (TOOL_GROUP_REPORT, TOOL_PRODUCT_REPORT, TOOL_TARGETING_REPORT):
        assert not [params for t, params in port.calls if t == tool], f"{tool} 被重拉了"
        cov = next(c for c in second.coverage if c.tool_id == tool)
        assert cov.complete is True
        assert cov.pages_fetched == 0


def test_sync_continuation_final_round_reports_complete() -> None:
    """最后一轮拉到 total 后，truncated 归 False——「继续拉取」按钮就此消失。"""
    pages = {
        TOOL_CAMPAIGN_REPORT: [[campaign_row(campaign_id=f"c{i}")] for i in range(1, 6)],
        TOOL_GROUP_REPORT: [[group_row()]],
        TOOL_PRODUCT_REPORT: [[product_row()]],
        TOOL_TARGETING_REPORT: [[targeting_row()]],
        TOOL_KEYWORD_REPORT: [[keyword_row()]],
    }
    engine, _, _ = make_engine(pages, totals={TOOL_CAMPAIGN_REPORT: 5})
    first = engine.run(PROFILE, page_size=1, max_pages=2)
    second = engine.run(
        PROFILE,
        page_size=1,
        max_pages=3,
        start_pages=first.next_pages(),
        report_date=first.report_date,
    )
    assert second.truncated is False
    assert second.next_pages() == {}


def test_sync_auto_pull_converges_without_relooping_completed_tools() -> None:
    """自动拉取必须在有限轮内收敛。

    这条对应的用户可见症状是「自动拉取一直不停」：已拉全的表每轮被重拉，重拉又
    撞满 max_pages 于是 truncated 恒为 True，continuation 永远非 null，前端那个
    `do{...}while(wb.continuation)` 只能靠 300 轮硬上限退出。
    """
    pages = {
        TOOL_CAMPAIGN_REPORT: [[campaign_row(campaign_id=f"c{i}")] for i in range(1, 7)],
        TOOL_GROUP_REPORT: [[group_row()]],
        TOOL_PRODUCT_REPORT: [[product_row()]],
        TOOL_TARGETING_REPORT: [[targeting_row()]],
        TOOL_KEYWORD_REPORT: [[keyword_row()]],
    }
    engine, port, _ = make_engine(pages, totals={TOOL_CAMPAIGN_REPORT: 6})
    report = engine.run(PROFILE, page_size=1, max_pages=2)
    # 小表第一轮就拉全（第 1 页有行、第 2 页空页确认到底）。清掉第一轮的记录，
    # 后续轮次里它们再出现一次都是重拉。
    port.calls.clear()
    rounds = 1
    while report.truncated:
        assert rounds < 10, "续拉没有收敛"
        report = engine.run(
            PROFILE,
            page_size=1,
            max_pages=2,
            tool_cursors=report.tool_cursors(),
            report_date=report.report_date,
        )
        rounds += 1
    assert rounds == 3  # 6 页 ÷ 每轮 2 页
    for tool in (TOOL_GROUP_REPORT, TOOL_PRODUCT_REPORT, TOOL_TARGETING_REPORT):
        assert not [1 for t, _ in port.calls if t == tool], f"{tool} 在续拉轮被重拉"
    # 续拉的两轮只该打 campaign：4 页（第 3-6 页）。
    assert len(port.calls) == 4


def test_sync_resume_keeps_coverage_of_completed_tools() -> None:
    """续拉轮不得让已拉全的表覆盖率倒退。

    coverage 的语义是「截至本轮末、本窗口累计覆盖到哪」。跳过的表若贡献 0，
    界面上的「已覆盖」就会在轮次间忽大忽小——TARGET 层尤其明显，它由
    targeting + keyword 两张表相加，两表进度不同步时数字会跳。
    """
    pages = {
        TOOL_CAMPAIGN_REPORT: [[campaign_row(campaign_id=f"c{i}")] for i in range(1, 6)],
        TOOL_GROUP_REPORT: [[group_row()]],
        TOOL_PRODUCT_REPORT: [[product_row()]],
        TOOL_TARGETING_REPORT: [[targeting_row()]],
        TOOL_KEYWORD_REPORT: [[keyword_row()]],
    }
    engine, _, _ = make_engine(pages, totals={TOOL_CAMPAIGN_REPORT: 5})
    first = engine.run(PROFILE, page_size=1, max_pages=2)
    second = engine.run(
        PROFILE,
        page_size=1,
        max_pages=2,
        tool_cursors=first.tool_cursors(),
        report_date=first.report_date,
    )
    before = {c.tool_id: c.rows_covered for c in first.coverage}
    after = {c.tool_id: c.rows_covered for c in second.coverage}
    for tool_id, covered in before.items():
        assert after[tool_id] >= covered, f"{tool_id} 覆盖率倒退：{covered} → {after[tool_id]}"


def test_sync_rows_covered_excludes_summary_rows() -> None:
    """汇总行不得计入覆盖数，否则会把截断静默报成「已拉全」。

    source_total 取自 recordsFiltered。若它不含汇总行，而覆盖数按整页行数累加，
    每页就多计 1，rows_covered >= source_total 提前成立 → truncated 留 False、
    next_page 留 None，于是一次截断被报告成拉全，界面把部分样本当店铺全貌。
    """
    # 每页 1 行汇总行（无 campaign_id）+ 1 行数据行；上游 total 只数数据行。
    summary = {k: v for k, v in campaign_row().items() if k != "campaign_id"}
    pages = {
        TOOL_CAMPAIGN_REPORT: [[summary, campaign_row(campaign_id=f"c{i}")] for i in range(1, 5)],
        TOOL_GROUP_REPORT: [[group_row()]],
        TOOL_PRODUCT_REPORT: [[product_row()]],
        TOOL_TARGETING_REPORT: [[targeting_row()]],
        TOOL_KEYWORD_REPORT: [[keyword_row()]],
    }
    engine, _, _ = make_engine(pages, totals={TOOL_CAMPAIGN_REPORT: 4})
    report = engine.run(PROFILE, page_size=2, max_pages=10)
    cov = next(c for c in report.coverage if c.tool_id == TOOL_CAMPAIGN_REPORT)
    assert cov.rows_covered == 4  # 4 个数据行，4 个汇总行不算
    assert report.skipped_summary_rows >= 4
    assert cov.complete is True
    assert cov.truncated is False


def test_sync_legacy_start_pages_cursor_still_reloops_completed_tools() -> None:
    """老 start_pages 游标的行为原样钉住——它没有被这次修复捎带修好。

    老游标只记「还没拉完的表」，已拉全的表不在里面，会被 .get(tool_id, 1) 读成
    「从第 1 页开始」。要修好必须改传 tool_cursors；这条测试的存在是为了防止
    后来的人以为老路径也已经安全了。
    """
    pages = {
        TOOL_CAMPAIGN_REPORT: [[campaign_row(campaign_id=f"c{i}")] for i in range(1, 6)],
        TOOL_GROUP_REPORT: [[group_row()]],
        TOOL_PRODUCT_REPORT: [[product_row()]],
        TOOL_TARGETING_REPORT: [[targeting_row()]],
        TOOL_KEYWORD_REPORT: [[keyword_row()]],
    }
    engine, port, _ = make_engine(pages, totals={TOOL_CAMPAIGN_REPORT: 5})
    first = engine.run(PROFILE, page_size=1, max_pages=2)
    port.calls.clear()
    engine.run(
        PROFILE,
        page_size=1,
        max_pages=2,
        start_pages=first.next_pages(),
        report_date=first.report_date,
    )
    group_pages = [params["page"] for t, params in port.calls if t == TOOL_GROUP_REPORT]
    assert 1 in group_pages, "老游标下已拉全的表仍会被从第 1 页整张重拉"


def test_sync_auto_pull_terminates_when_a_row_cannot_be_mapped() -> None:
    """上游服务的行里只要有一行映射不了，续拉仍必须收敛。

    rows_covered 只累加映射成功的数据行（汇总行、缺字段、id 类型不对的都跳过），
    source_total 却是上游服务的记录数——两者差一行，就永远差着。于是「拉到空页
    且覆盖不足」那条分支每轮把 next_page 往后推一格，continuation 恒非 null，
    前端 `do{...}while(wb.continuation)` 只能靠 300 轮硬上限退出：每轮五张表各打
    一个空页、烧掉 QPS=1 的预算，最后**无声**停下——人看到的是「自动拉取直到
    拉全」跑了很久，然后什么都没说，续拉条还在原地。

    空页证明的是「上游没有下一页了」，不是「回头再问一次就会有」。所以这里
    next_page 必须收成 None（不再问），truncated 保持 True（覆盖确实不足，
    这句话仍要说给人听）——两个事实分开表达，不合并成一个布尔。
    """
    summary = {k: v for k, v in campaign_row().items() if k != "campaign_id"}
    pages = {
        TOOL_CAMPAIGN_REPORT: [[summary, campaign_row(campaign_id="c1")]],
        TOOL_GROUP_REPORT: [[group_row()]],
        TOOL_PRODUCT_REPORT: [[product_row()]],
        TOOL_TARGETING_REPORT: [[targeting_row()]],
        TOOL_KEYWORD_REPORT: [[keyword_row()]],
    }
    # 上游数了 2 条记录（含那条我们映射不了的），我们只映射得出 1 条。
    engine, port, _ = make_engine(pages, totals={TOOL_CAMPAIGN_REPORT: 2})
    report = engine.run(PROFILE, page_size=2, max_pages=10)
    rounds = 1
    while report.next_pages():
        assert rounds < 10, "续拉没有收敛：一行映射不了就把自动拉取拖进 300 轮空转"
        report = engine.run(
            PROFILE,
            page_size=2,
            max_pages=10,
            tool_cursors=report.tool_cursors(),
            report_date=report.report_date,
        )
        rounds += 1
    assert rounds == 1, "空页已经证明上游没有下一页了，不该再要一轮"
    cov = next(c for c in report.coverage if c.tool_id == TOOL_CAMPAIGN_REPORT)
    assert cov.next_page is None, "空页之后不该再问下一页"
    assert cov.truncated is True, "覆盖确实不足（1/2），这句话不能因为不再续拉就不说"
    assert cov.complete is False, "没拿全就不许叫「已拉全」"
    campaign_pages = [
        p for t, p in ((t, q["page"]) for t, q in port.calls) if t == TOOL_CAMPAIGN_REPORT
    ]
    assert campaign_pages == [1, 2], f"多打了白页：{campaign_pages}"


def test_sync_auto_pull_terminates_when_upstream_total_drifts_upward() -> None:
    """上游 total 在翻页之间往上漂，续拉仍必须收敛。

    2026-08-30 实测：同一窗口两次调用 total 从 1047 变成 1079——窗口含当日，行会
    一直加。漂上去之后 rows_seen 永远追不上最新的 total，若只看「拿到的比它说的
    少」，每一轮都会被判成上游断供，next_page 一路往后推，自动拉取又回到 300 轮
    空转。断供只该允许再探一轮：那一轮拿不到新行，就说明重试无用。

    收敛的同时，覆盖不足这句话仍要说——truncated 保持 True，人看得见「拉到 4/6」。
    """

    class DriftingPort:
        """campaign 表：2 页各 2 行，total 从 4 漂到 6；第 3 页起为空。"""

        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        def fetch_page(self, tool_id: str, params: Mapping[str, object]) -> Mapping[str, object]:
            page = params["page"]
            assert isinstance(page, int)
            self.calls.append((tool_id, page))
            if tool_id != TOOL_CAMPAIGN_REPORT:
                return {"rows": [], "total": 0}
            if page == 1:
                return {
                    "rows": [campaign_row(campaign_id="c1"), campaign_row(campaign_id="c2")],
                    "total": 4,
                }
            if page == 2:
                return {
                    "rows": [campaign_row(campaign_id="c3"), campaign_row(campaign_id="c4")],
                    "total": 6,
                }  # 窗口含当日，行还在加
            return {"rows": [], "total": 6}

    port = DriftingPort()
    engine = SyncEngine(port, InMemorySnapshotRepository(), (PROFILE,))
    report = engine.run(PROFILE, page_size=2, max_pages=10)
    rounds = 1
    while report.next_pages():
        assert rounds < 10, "total 往上漂就让自动拉取空转到 300 轮上限"
        report = engine.run(
            PROFILE,
            page_size=2,
            max_pages=10,
            tool_cursors=report.tool_cursors(),
            report_date=report.report_date,
        )
        rounds += 1
    assert rounds == 2, "断供只该再探一轮；那一轮没拿到新行就该收口"
    cov = next(c for c in report.coverage if c.tool_id == TOOL_CAMPAIGN_REPORT)
    assert cov.rows_covered == 4
    assert cov.source_total == 6
    assert cov.truncated is True, "拉到 4/6，这句话不能因为不再续拉就不说"
    assert cov.next_page is None
