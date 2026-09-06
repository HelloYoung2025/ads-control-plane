"""对象工作台 API 集成测试：镜像浏览分页/过滤、勾选预览、人触发同步、历史查看。

覆盖与 UI 合同对齐（ui_static/app.js 工作台区块实际消费的字段）：
- objects 的 rows/total/mirror_empty 与层级/父对象过滤、服务端分页；
- preview 的现值→新值、approval_required 常量、SELECTION_*/PARAMETER_REJECTED/
  PREVIEW_* 错误码透传（预览是读侧，AI 身份可调）；
- sync 的 HUMAN-only 403、LX_MCP_KEY 缺失 409 fail-closed、白名单全拒/越名单 403、
  假体端口下的全链路入库与 ADS_CP_SYNC_MAX_PAGES 生效；
- history 升序、未知 object_key 空 entries；本地演示组合根挂载与种子镜像。
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ads_control_plane.api.local_demo import DEMO_OWNER_TOKEN, build_local_demo_app
from ads_control_plane.api.mcp_tools.server import InMemoryActorTokenVerifier
from ads_control_plane.api.workbench_api import build_workbench_router
from ads_control_plane.canonical.ids import new_canonical_id
from ads_control_plane.identity.actor import (
    ActorContext,
    AuthenticationStrength,
    PrincipalType,
    Role,
)
from ads_control_plane.mirror.repository import InMemorySnapshotRepository
from ads_control_plane.mirror.snapshot import AdObjectSnapshot
from ads_control_plane.mirror.sync import CATALOG_VERSION, LxReadPort
from ads_control_plane.tasks.directive import ObjectLevel

HUMAN_TOKEN = "wb-human-token"
AI_TOKEN = "wb-ai-token"
PROFILE = "profile-w"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)

UI_STATIC_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "ads_control_plane" / "api" / "ui_static"
)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register_identities(verifier: InMemoryActorTokenVerifier) -> None:
    org = new_canonical_id()
    real_now = datetime.now(UTC)
    base: dict[str, Any] = {
        "organization_id": org,
        "authentication_strength": AuthenticationStrength.MFA,
        "issued_at": real_now,
        "expires_at": real_now + timedelta(hours=8),
    }
    verifier.register(
        HUMAN_TOKEN,
        ActorContext(
            principal_id=new_canonical_id(),
            principal_type=PrincipalType.HUMAN,
            roles=frozenset({Role.OPERATOR}),
            human_person_id="ops-1",
            client_id="web-ops",
            session_id="s-ops",
            **base,
        ),
    )
    verifier.register(
        AI_TOKEN,
        ActorContext(
            principal_id=new_canonical_id(),
            principal_type=PrincipalType.AI_CLIENT,
            roles=frozenset({Role.ANALYST}),
            human_initiator_person_id="ops-1",
            client_id="codex-1",
            session_id="s-codex",
            **base,
        ),
    )


def snap(object_key: str, level: ObjectLevel, **overrides: Any) -> AdObjectSnapshot:
    base: dict[str, Any] = {
        "object_key": object_key,
        "level": level,
        "profile_id": PROFILE,
        "source_as_of": NOW,
        "recorded_at": NOW,
        "catalog_version": CATALOG_VERSION,
        "schema_version": "ad_campaign_report-v1",
    }
    base.update(overrides)
    return AdObjectSnapshot(**base)


def seed_repo(repo: InMemorySnapshotRepository) -> None:
    """5 个现值对象；campaign:c1 另有一条更早快照（current 应取最新、history 升序）。"""
    repo.append(
        snap(
            "campaign:c1",
            ObjectLevel.CAMPAIGN,
            name="Camp-1",
            state="enabled",
            daily_budget=Decimal("12.00"),
            recorded_at=NOW - timedelta(hours=6),
            source_as_of=NOW - timedelta(hours=6),
        )
    )
    repo.append(
        snap(
            "campaign:c1",
            ObjectLevel.CAMPAIGN,
            name="Camp-1",
            state="enabled",
            daily_budget=Decimal("13.00"),
            metrics={"spends": "34.10", "acos": "0.42"},
        )
    )
    repo.append(
        snap(
            "campaign:c2",
            ObjectLevel.CAMPAIGN,
            name="Camp-2",
            state="enabled",
            daily_budget=Decimal("25.00"),
            ads_strategy="分时预算",
            is_apply_time=True,
        )
    )
    repo.append(
        snap(
            "ad_group:g1",
            ObjectLevel.AD_GROUP,
            name="Group-1",
            state="enabled",
            default_bid=Decimal("0.80"),
            parent_campaign_id="c1",
            schema_version="ad_campaign_group_report-v1",
        )
    )
    repo.append(
        snap(
            "target:t1",
            ObjectLevel.TARGET,
            name="close-match",
            state="enabled",
            bid=Decimal("0.75"),
            default_bid=Decimal("0.80"),
            parent_campaign_id="c1",
            parent_ad_group_id="g1",
            schema_version="ad_campaign_targeting_report-v1",
        )
    )
    repo.append(
        snap(
            "target:k1",
            ObjectLevel.TARGET,
            name="widget",
            state="enabled",
            bid=Decimal("0.95"),
            keyword_text="widget",
            match_type="exact",
            parent_campaign_id="c1",
            parent_ad_group_id="g1",
            schema_version="ad_campaign_keyword_report-v1",
        )
    )


class FakeReadPort:
    """脚本化读端口：campaign 报表一页一行，其余工具首页即空。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def fetch_page(self, tool_id: str, params: Mapping[str, object]) -> Mapping[str, object]:
        self.calls.append((tool_id, dict(params)))
        page = params["page"]
        if tool_id == "ad_campaign_report" and page == 1:
            rows: list[dict[str, object]] = [
                {
                    "campaign_id": "c-sync-1",
                    "campaign_name": "Synced-Camp",
                    "state": "enabled",
                    "budget": "10.50",
                    "spends": "1.23",
                }
            ]
            return {"rows": rows, "total": None}
        return {"rows": [], "total": None}


def make_app(
    read_port: LxReadPort | None = None,
) -> tuple[TestClient, InMemorySnapshotRepository]:
    repo = InMemorySnapshotRepository()
    verifier = InMemoryActorTokenVerifier()
    _register_identities(verifier)
    factory = None if read_port is None else (lambda url, key: read_port)
    app = FastAPI()
    app.include_router(build_workbench_router(repo, verifier, read_port_factory=factory))
    return TestClient(app), repo


def seeded_client() -> TestClient:
    client, repo = make_app()
    seed_repo(repo)
    return client


def _clear_sync_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LX_MCP_KEY", "LX_MCP_URL", "ADS_CP_SYNC_PROFILES", "ADS_CP_SYNC_MAX_PAGES"):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------- objects


def test_objects_requires_bearer() -> None:
    client = seeded_client()
    res = client.get("/api/workbench/objects", params={"profile_id": PROFILE})
    assert res.status_code == 401
    assert res.json()["detail"] == "AUTHENTICATION_REQUIRED"


def test_objects_lists_current_rows_with_contract_fields() -> None:
    client = seeded_client()
    res = client.get(
        "/api/workbench/objects", params={"profile_id": PROFILE}, headers=bearer(HUMAN_TOKEN)
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["total"] == 5
    assert payload["mirror_empty"] is False
    by_key = {row["object_key"]: row for row in payload["rows"]}
    # current 语义：campaign:c1 取 recorded_at 最新一条（budget 13.00 而非 12.00）
    c1 = by_key["campaign:c1"]
    assert c1["daily_budget"] == "13.00"
    assert c1["metrics"] == {"spends": "34.10", "acos": "0.42"}
    assert c1["source_as_of"] == NOW.isoformat()
    # 托管打标字段原样透出（UI 徽章与禁勾选依赖）
    c2 = by_key["campaign:c2"]
    assert c2["ads_strategy"] == "分时预算"
    assert c2["is_apply_time"] is True
    # keyword 并入 TARGET 层，keyword_text/match_type 区分
    k1 = by_key["target:k1"]
    assert k1["keyword_text"] == "widget"
    assert k1["match_type"] == "exact"
    assert k1["parent_ad_group_id"] == "g1"


def test_objects_level_filter_and_pagination() -> None:
    client = seeded_client()
    target = client.get(
        "/api/workbench/objects",
        params={"profile_id": PROFILE, "level": "target"},
        headers=bearer(HUMAN_TOKEN),
    ).json()
    assert target["total"] == 2
    assert {row["object_key"] for row in target["rows"]} == {"target:t1", "target:k1"}

    paged = client.get(
        "/api/workbench/objects",
        params={"profile_id": PROFILE, "page": 2, "length": 2},
        headers=bearer(HUMAN_TOKEN),
    ).json()
    assert paged["total"] == 5
    assert len(paged["rows"]) == 2
    last = client.get(
        "/api/workbench/objects",
        params={"profile_id": PROFILE, "page": 3, "length": 2},
        headers=bearer(HUMAN_TOKEN),
    ).json()
    assert len(last["rows"]) == 1


def test_objects_parent_filters() -> None:
    client = seeded_client()
    groups = client.get(
        "/api/workbench/objects",
        params={"profile_id": PROFILE, "level": "ad_group", "parent_campaign_id": "c1"},
        headers=bearer(HUMAN_TOKEN),
    ).json()
    assert [row["object_key"] for row in groups["rows"]] == ["ad_group:g1"]

    targets = client.get(
        "/api/workbench/objects",
        params={"profile_id": PROFILE, "level": "target", "parent_ad_group_id": "g1"},
        headers=bearer(HUMAN_TOKEN),
    ).json()
    assert targets["total"] == 2

    none = client.get(
        "/api/workbench/objects",
        params={"profile_id": PROFILE, "level": "target", "parent_ad_group_id": "g-none"},
        headers=bearer(HUMAN_TOKEN),
    ).json()
    assert none["total"] == 0
    assert none["mirror_empty"] is False  # 镜像非空，只是过滤后无行


def test_objects_resolve_parent_names_across_levels() -> None:
    """浏览子层时父对象名称照样解析——名称取自层级过滤**前**的全量现值。

    回归锚点：曾经每行都渲染「镜像中没有这个父对象的名称记录」，而镜像里
    明明有 campaign:c1（Camp-1）。界面对人说的那句话必须为真，否则人会以为
    是同步缺数据、跑去重同步一遍。
    """
    client = seeded_client()
    targets = client.get(
        "/api/workbench/objects",
        params={"profile_id": PROFILE, "level": "target"},
        headers=bearer(HUMAN_TOKEN),
    ).json()
    assert [
        (row["parent_campaign_name"], row["parent_ad_group_name"]) for row in targets["rows"]
    ] == [("Camp-1", "Group-1"), ("Camp-1", "Group-1")]

    # 活动层没有父对象：两个字段都是 None，不是漏字段（UI 据此显示「—」）。
    campaigns = client.get(
        "/api/workbench/objects",
        params={"profile_id": PROFILE, "level": "campaign"},
        headers=bearer(HUMAN_TOKEN),
    ).json()
    assert all(
        row["parent_campaign_name"] is None and row["parent_ad_group_name"] is None
        for row in campaigns["rows"]
    )


def test_objects_unresolvable_parent_name_stays_none() -> None:
    """父对象不在镜像里时留 None——此时「没有名称记录」才是真话，不编一个名字。"""
    client, repo = make_app()
    seed_repo(repo)
    repo.append(
        snap(
            "target:orphan",
            ObjectLevel.TARGET,
            name="orphan-target",
            state="enabled",
            parent_campaign_id="c-missing",
            parent_ad_group_id="g-missing",
            schema_version="ad_campaign_targeting_report-v1",
        )
    )
    rows = client.get(
        "/api/workbench/objects",
        params={"profile_id": PROFILE, "level": "target"},
        headers=bearer(HUMAN_TOKEN),
    ).json()["rows"]
    orphan = next(row for row in rows if row["object_key"] == "target:orphan")
    assert orphan["parent_campaign_name"] is None
    assert orphan["parent_ad_group_name"] is None


def test_objects_invalid_level_is_400() -> None:
    client = seeded_client()
    res = client.get(
        "/api/workbench/objects",
        params={"profile_id": PROFILE, "level": "portfolio"},
        headers=bearer(HUMAN_TOKEN),
    )
    assert res.status_code == 400
    assert res.json()["detail"] == "LEVEL_INVALID"


def test_objects_unknown_profile_reports_mirror_empty() -> None:
    client = seeded_client()
    res = client.get(
        "/api/workbench/objects",
        params={"profile_id": "profile-unknown"},
        headers=bearer(HUMAN_TOKEN),
    ).json()
    # level_coverage/report_date 为 None：本进程从没同步过这个 profile，覆盖率无从断言。
    assert res == {
        "rows": [],
        "total": 0,
        "bucket_counts": {
            "all": 0,
            "has_orders": 0,
            "clicks_no_orders": 0,
            "impressions_no_clicks": 0,
            "no_impressions": 0,
        },
        "mirror_empty": True,
        "level_coverage": None,
        "report_date": None,
        "report_windows": [],
        "report_window_unknown_rows": 0,
        "sync_continuation": None,
    }


# ---------------------------------------------------------------- perf buckets


def _bucket_client() -> TestClient:
    """绩效分桶专用种子：四桶各一行 + 两种「判定指标缺失」行。

    漏斗语义（docs/evidence/lx-ads-ia-20260829.md §3）：有成交 / 有点击无成交 /
    有曝光无点击 / 无曝光，四桶互斥；判定所需指标缺失的行不落桶（缺失≠0），
    但仍计入「全部」。
    """
    client, repo = make_app()
    seeds: list[tuple[str, dict[str, str]]] = [
        ("campaign:b-orders", {"orders": "3", "clicks": "20", "impressions": "900"}),
        ("campaign:b-clicks", {"orders": "0", "clicks": "5", "impressions": "400"}),
        ("campaign:b-impr", {"orders": "0", "clicks": "0", "impressions": "120"}),
        ("campaign:b-silent", {"orders": "0", "clicks": "0", "impressions": "0"}),
        # clicks>0 但 orders 缺失：不知道有没有成交，不许冒充「有点击无成交」。
        ("campaign:b-no-orders-metric", {"clicks": "7", "impressions": "300"}),
        # 判定指标全缺：只算「全部」，四桶都不落。
        ("campaign:b-unknown", {"spends": "1.00"}),
    ]
    for key, metrics in seeds:
        repo.append(snap(key, ObjectLevel.CAMPAIGN, name=key, state="enabled", metrics=metrics))
    return client


def test_objects_bucket_counts_follow_funnel_semantics() -> None:
    res = (
        _bucket_client()
        .get("/api/workbench/objects", params={"profile_id": PROFILE}, headers=bearer(HUMAN_TOKEN))
        .json()
    )
    # 6 行全算「全部」；缺指标的 2 行不落任何桶——四桶合计 4 < 6 是设计而非漏数。
    assert res["bucket_counts"] == {
        "all": 6,
        "has_orders": 1,
        "clicks_no_orders": 1,
        "impressions_no_clicks": 1,
        "no_impressions": 1,
    }


def test_objects_perf_bucket_filters_rows_but_counts_stay_global() -> None:
    client = _bucket_client()
    res = client.get(
        "/api/workbench/objects",
        params={"profile_id": PROFILE, "perf_bucket": "clicks_no_orders"},
        headers=bearer(HUMAN_TOKEN),
    ).json()
    assert [row["object_key"] for row in res["rows"]] == ["campaign:b-clicks"]
    assert res["total"] == 1
    # 计数对分桶前的全集算：点了某个桶之后，快捷条上的其它数字不能跟着变。
    assert res["bucket_counts"]["all"] == 6
    assert res["bucket_counts"]["no_impressions"] == 1

    # 缺指标的行不属于任何桶，任何桶的筛选都带不出它们。
    for bucket in ("has_orders", "clicks_no_orders", "impressions_no_clicks", "no_impressions"):
        rows = client.get(
            "/api/workbench/objects",
            params={"profile_id": PROFILE, "perf_bucket": bucket},
            headers=bearer(HUMAN_TOKEN),
        ).json()["rows"]
        keys = {row["object_key"] for row in rows}
        assert "campaign:b-unknown" not in keys
        assert "campaign:b-no-orders-metric" not in keys


def test_objects_bucket_counts_respect_other_filters() -> None:
    """计数对「筛选后、分桶前」的全集算：名字筛掉的行不进任何数字（含 all）。"""
    res = (
        _bucket_client()
        .get(
            "/api/workbench/objects",
            params={"profile_id": PROFILE, "name_contains": "b-orders"},
            headers=bearer(HUMAN_TOKEN),
        )
        .json()
    )
    assert res["bucket_counts"] == {
        "all": 1,
        "has_orders": 1,
        "clicks_no_orders": 0,
        "impressions_no_clicks": 0,
        "no_impressions": 0,
    }


def test_objects_invalid_perf_bucket_is_400() -> None:
    res = seeded_client().get(
        "/api/workbench/objects",
        params={"profile_id": PROFILE, "perf_bucket": "everything"},
        headers=bearer(HUMAN_TOKEN),
    )
    assert res.status_code == 400
    assert res.json()["detail"] == "PERF_BUCKET_INVALID"


def test_objects_row_projects_targeting_type() -> None:
    """targeting_type 原样透出（UI [手动]/[自动] 徽标数据源）；缺失时为 None 不是漏字段。"""
    client, repo = make_app()
    repo.append(
        snap(
            "campaign:c-manual",
            ObjectLevel.CAMPAIGN,
            name="Manual-Camp",
            state="enabled",
            targeting_type="manual",
        )
    )
    repo.append(snap("campaign:c-plain", ObjectLevel.CAMPAIGN, name="Plain-Camp", state="enabled"))
    rows = client.get(
        "/api/workbench/objects", params={"profile_id": PROFILE}, headers=bearer(HUMAN_TOKEN)
    ).json()["rows"]
    by_key = {row["object_key"]: row for row in rows}
    assert by_key["campaign:c-manual"]["targeting_type"] == "manual"
    assert by_key["campaign:c-plain"]["targeting_type"] is None


# ---------------------------------------------------------------- preview


def preview_body(items: list[dict[str, str]], intent: dict[str, Any]) -> dict[str, Any]:
    return {"profile_id": PROFILE, "items": items, "intent": intent}


def test_preview_set_daily_budget_shows_current_to_new() -> None:
    client = seeded_client()
    res = client.post(
        "/api/workbench/preview",
        headers=bearer(HUMAN_TOKEN),
        json=preview_body(
            [
                {"level": "campaign", "external_id": "c1"},
                {"level": "campaign", "external_id": "c2"},
            ],
            {"action": "SET_DAILY_BUDGET", "value": "30.00", "reason": "旺季提额"},
        ),
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["approval_required"] is True
    assert payload["affected_total"] == 2
    assert len(payload["previews"]) == 1
    preview = payload["previews"][0]
    assert preview["level"] == "CAMPAIGN"
    assert preview["directive_id"]
    rows = {row["object_key"]: row for row in preview["affected"]}
    assert rows["campaign:c1"]["current_value"] == "13.00"
    assert rows["campaign:c1"]["new_value"] == "30.00"
    assert rows["campaign:c2"]["current_value"] == "25.00"
    assert rows["campaign:c2"]["display_name"] == "Camp-2"


def test_preview_scale_bid_rounds_half_up_and_groups_by_level() -> None:
    client = seeded_client()
    res = client.post(
        "/api/workbench/preview",
        headers=bearer(HUMAN_TOKEN),
        json=preview_body(
            [
                {"level": "target", "external_id": "t1"},
                {"level": "ad_group", "external_id": "g1"},
            ],
            {"action": "SCALE_BID", "percent": 10, "reason": "词组表现好，加价一成"},
        ),
    )
    assert res.status_code == 200
    payload = res.json()
    assert {p["level"] for p in payload["previews"]} == {"TARGET", "AD_GROUP"}
    rows = {row["object_key"]: row for p in payload["previews"] for row in p["affected"]}
    # 0.75 * 1.10 = 0.825 → 半进位到分 → 0.83
    assert rows["target:t1"]["current_value"] == "0.75"
    assert rows["target:t1"]["new_value"] == "0.83"
    # ad_group 无独立 bid → 取组默认竞价 0.80 → 0.88
    assert rows["ad_group:g1"]["current_value"] == "0.80"
    assert rows["ad_group:g1"]["new_value"] == "0.88"


def test_preview_pause_uses_state_as_current_value() -> None:
    client = seeded_client()
    res = client.post(
        "/api/workbench/preview",
        headers=bearer(HUMAN_TOKEN),
        json=preview_body(
            [{"level": "campaign", "external_id": "c1"}],
            {"action": "PAUSE", "reason": "先停观察"},
        ),
    )
    assert res.status_code == 200
    row = res.json()["previews"][0]["affected"][0]
    assert row["current_value"] == "enabled"
    assert row["new_value"] == "paused"


def test_preview_is_read_side_so_ai_can_call() -> None:
    client = seeded_client()
    res = client.post(
        "/api/workbench/preview",
        headers=bearer(AI_TOKEN),
        json=preview_body(
            [{"level": "campaign", "external_id": "c1"}],
            {"action": "ENABLE", "reason": "恢复投放建议"},
        ),
    )
    assert res.status_code == 200
    assert res.json()["approval_required"] is True


def test_preview_selection_errors_are_explicit() -> None:
    client = seeded_client()
    empty = client.post(
        "/api/workbench/preview",
        headers=bearer(HUMAN_TOKEN),
        json=preview_body([], {"action": "PAUSE", "reason": "x"}),
    )
    assert empty.status_code == 422
    assert empty.json()["detail"] == "SELECTION_EMPTY"

    too_broad = client.post(
        "/api/workbench/preview",
        headers=bearer(HUMAN_TOKEN),
        json=preview_body(
            [{"level": "target", "external_id": f"t-{i}"} for i in range(201)],
            {"action": "PAUSE", "reason": "x"},
        ),
    )
    assert too_broad.status_code == 422
    assert too_broad.json()["detail"] == "SELECTION_TOO_BROAD"


def test_preview_missing_object_is_409_not_silent_partial() -> None:
    client = seeded_client()
    res = client.post(
        "/api/workbench/preview",
        headers=bearer(HUMAN_TOKEN),
        json=preview_body(
            [
                {"level": "campaign", "external_id": "c1"},
                {"level": "campaign", "external_id": "c-ghost"},
            ],
            {"action": "PAUSE", "reason": "x"},
        ),
    )
    assert res.status_code == 409
    # 结构化 detail（2026-08-29 排查 workbench-3）：code 归词典，message 点名是谁。
    detail = res.json()["detail"]
    assert detail["code"] == "PREVIEW_OBJECT_NOT_IN_MIRROR"
    assert "c-ghost" in detail["message"]


def test_preview_value_unavailable_for_level_mismatch() -> None:
    client = seeded_client()
    # target 层没有 daily_budget 现值 → 显式 409，而非猜一个数
    res = client.post(
        "/api/workbench/preview",
        headers=bearer(HUMAN_TOKEN),
        json=preview_body(
            [{"level": "target", "external_id": "t1"}],
            {"action": "SET_DAILY_BUDGET", "value": "9.99", "reason": "x"},
        ),
    )
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["code"] == "PREVIEW_VALUE_UNAVAILABLE"
    # message 点名对象并给出动作（2026-08-29 排查 workbench-3：不再让人二分查找）。
    assert "target:t1" in detail["message"]


def test_preview_whitelist_rejections_are_422() -> None:
    client = seeded_client()
    bad_action = client.post(
        "/api/workbench/preview",
        headers=bearer(HUMAN_TOKEN),
        json=preview_body(
            [{"level": "campaign", "external_id": "c1"}],
            {"action": "DELETE_EVERYTHING", "reason": "x"},
        ),
    )
    assert bad_action.status_code == 422
    # 码稳定给机器，message 给人——后者要点名是哪个值不合法。
    assert bad_action.json()["detail"]["code"] == "PARAMETER_REJECTED"
    assert "DELETE_EVERYTHING" in bad_action.json()["detail"]["message"]

    over_step = client.post(
        "/api/workbench/preview",
        headers=bearer(HUMAN_TOKEN),
        json=preview_body(
            [{"level": "campaign", "external_id": "c1"}],
            {"action": "SCALE_DAILY_BUDGET", "percent": 80, "reason": "x"},
        ),
    )
    assert over_step.status_code == 422
    assert over_step.json()["detail"]["code"] == "PARAMETER_REJECTED"

    bad_level = client.post(
        "/api/workbench/preview",
        headers=bearer(HUMAN_TOKEN),
        json=preview_body(
            [{"level": "portfolio", "external_id": "p1"}],
            {"action": "PAUSE", "reason": "x"},
        ),
    )
    assert bad_level.status_code == 400
    assert bad_level.json()["detail"] == "LEVEL_INVALID"


# ---------------------------------------------------------------- sync


def test_sync_rejects_ai_with_human_required(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_sync_env(monkeypatch)
    client, _ = make_app()
    res = client.post("/api/workbench/sync", headers=bearer(AI_TOKEN), json={"profile_id": PROFILE})
    assert res.status_code == 403
    assert res.json()["detail"] == "HUMAN_REQUIRED"


def test_sync_without_key_is_409_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_sync_env(monkeypatch)
    client, _ = make_app()
    res = client.post(
        "/api/workbench/sync", headers=bearer(HUMAN_TOKEN), json={"profile_id": PROFILE}
    )
    assert res.status_code == 409
    assert res.json()["detail"] == "LX_KEY_ABSENT"


def test_sync_empty_whitelist_rejects_all(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_sync_env(monkeypatch)
    monkeypatch.setenv("LX_MCP_KEY", "k-test")
    monkeypatch.setenv("LX_MCP_URL", "https://lx.example/mcp")
    client, _ = make_app(read_port=FakeReadPort())
    res = client.post(
        "/api/workbench/sync", headers=bearer(HUMAN_TOKEN), json={"profile_id": PROFILE}
    )
    assert res.status_code == 403
    assert res.json()["detail"] == "SYNC_NO_ALLOWED_PROFILES"


def test_sync_profile_outside_whitelist_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_sync_env(monkeypatch)
    monkeypatch.setenv("LX_MCP_KEY", "k-test")
    monkeypatch.setenv("LX_MCP_URL", "https://lx.example/mcp")
    monkeypatch.setenv("ADS_CP_SYNC_PROFILES", "profile-other")
    client, _ = make_app(read_port=FakeReadPort())
    res = client.post(
        "/api/workbench/sync", headers=bearer(HUMAN_TOKEN), json={"profile_id": PROFILE}
    )
    assert res.status_code == 403
    assert res.json()["detail"] == "SYNC_PROFILE_NOT_ALLOWED"


def test_sync_happy_path_writes_into_shared_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_sync_env(monkeypatch)
    monkeypatch.setenv("LX_MCP_KEY", "k-test")
    monkeypatch.setenv("LX_MCP_URL", "https://lx.example/mcp")
    monkeypatch.setenv("ADS_CP_SYNC_PROFILES", f"profile-other, {PROFILE}")
    monkeypatch.setenv("ADS_CP_SYNC_MAX_PAGES", "1")
    port = FakeReadPort()
    client, _ = make_app(read_port=port)
    res = client.post(
        "/api/workbench/sync", headers=bearer(HUMAN_TOKEN), json={"profile_id": PROFILE}
    )
    assert res.status_code == 200
    report = res.json()
    assert report["profile_id"] == PROFILE
    assert report["per_level_rows"]["CAMPAIGN"] == 1
    # max_pages=1 生效：campaign 拉 1 页即停，其余四工具各 1 页空 → 共 5 页
    assert report["pages_fetched"] == 5
    assert report["catalog_versions"]["ad_campaign_report"] == CATALOG_VERSION

    listed = client.get(
        "/api/workbench/objects", params={"profile_id": PROFILE}, headers=bearer(HUMAN_TOKEN)
    ).json()
    assert listed["mirror_empty"] is False
    assert [row["object_key"] for row in listed["rows"]] == ["campaign:c-sync-1"]
    assert listed["rows"][0]["daily_budget"] == "10.50"


def test_sync_invalid_max_pages_env_is_409(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_sync_env(monkeypatch)
    monkeypatch.setenv("LX_MCP_KEY", "k-test")
    monkeypatch.setenv("LX_MCP_URL", "https://lx.example/mcp")
    monkeypatch.setenv("ADS_CP_SYNC_PROFILES", PROFILE)
    monkeypatch.setenv("ADS_CP_SYNC_MAX_PAGES", "zero")
    client, _ = make_app(read_port=FakeReadPort())
    res = client.post(
        "/api/workbench/sync", headers=bearer(HUMAN_TOKEN), json={"profile_id": PROFILE}
    )
    assert res.status_code == 409
    assert res.json()["detail"] == "SYNC_CONFIG_INVALID"


def test_sync_profiles_endpoint_lists_env_whitelist(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配通道：白名单照常回显，alias/country 为 None（名称只是增强，不是前提）。"""
    _clear_sync_env(monkeypatch)
    monkeypatch.setenv("ADS_CP_SYNC_PROFILES", "p-1, p-2, ")
    client, _ = make_app()
    res = client.get("/api/workbench/sync-profiles", headers=bearer(AI_TOKEN))
    assert res.status_code == 200
    assert res.json() == {
        "profiles": [
            {"profile_id": "p-1", "alias": None, "country": None, "currency": None},
            {"profile_id": "p-2", "alias": None, "country": None, "currency": None},
        ]
    }


class ShopsReadPort:
    """ad_auth_shops 返回名录；可注入失败/空响应验证 fail-open 与缓存语义。"""

    def __init__(self, *, fail: bool = False, empty: bool = False) -> None:
        self.fail = fail
        self.empty = empty
        self.calls = 0

    def fetch_page(self, tool_id: str, params: Mapping[str, object]) -> Mapping[str, object]:
        assert tool_id == "ad_auth_shops"
        self.calls += 1
        if self.fail:
            from ads_control_plane.adapters.lx_read import LxReadError

            raise LxReadError("LX_TRANSPORT_ERROR", "boom")
        if self.empty:
            return {"rows": [], "total": 0}
        return {
            "rows": [
                {"profile_id": "p-1", "alias": "HX 主号", "country": "US", "currency": None},
                {"profile_id": "p-other", "alias": "别家店", "country": "CA", "currency": None},
            ],
            "total": 2,
        }


def test_sync_profiles_carries_shop_alias_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-08-29 Owner 反馈：下拉一串 16 位数字认不出店。alias/country 随白名单下发；
    名录不含的 profile 留 None；成功后进程级缓存（第二次请求不再出网）。"""
    _clear_sync_env(monkeypatch)
    monkeypatch.setenv("ADS_CP_SYNC_PROFILES", "p-1,p-2")
    monkeypatch.setenv("LX_MCP_KEY", "k")
    monkeypatch.setenv("LX_MCP_URL", "https://example.invalid/mcp")
    port = ShopsReadPort()
    client, _ = make_app(read_port=port)  # type: ignore[arg-type]
    res = client.get("/api/workbench/sync-profiles", headers=bearer(HUMAN_TOKEN)).json()
    assert res == {
        "profiles": [
            {"profile_id": "p-1", "alias": "HX 主号", "country": "US", "currency": None},
            {"profile_id": "p-2", "alias": None, "country": None, "currency": None},
        ]
    }
    client.get("/api/workbench/sync-profiles", headers=bearer(HUMAN_TOKEN))
    assert port.calls == 1  # 缓存生效


def test_sync_profiles_survives_directory_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """名录上游失败：白名单照常回显（fail-open），且失败不缓存——下次请求再试。"""
    _clear_sync_env(monkeypatch)
    monkeypatch.setenv("ADS_CP_SYNC_PROFILES", "p-1")
    monkeypatch.setenv("LX_MCP_KEY", "k")
    monkeypatch.setenv("LX_MCP_URL", "https://example.invalid/mcp")
    port = ShopsReadPort(fail=True)
    client, _ = make_app(read_port=port)  # type: ignore[arg-type]
    for _ in range(2):
        res = client.get("/api/workbench/sync-profiles", headers=bearer(HUMAN_TOKEN)).json()
        assert res == {
            "profiles": [{"profile_id": "p-1", "alias": None, "country": None, "currency": None}]
        }
    assert port.calls == 2  # 失败不缓存


def test_sync_profiles_empty_directory_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """二轮审计：成功信封但零有效行不算成功——若照常置 loaded，会把空名录缓存到
    进程死亡，下拉永远退化成裸 ID 且刷新无效。空结果按可重试处理：第二次必须再出网。"""
    _clear_sync_env(monkeypatch)
    monkeypatch.setenv("ADS_CP_SYNC_PROFILES", "p-1")
    monkeypatch.setenv("LX_MCP_KEY", "k")
    monkeypatch.setenv("LX_MCP_URL", "https://example.invalid/mcp")
    port = ShopsReadPort(empty=True)
    client, _ = make_app(read_port=port)  # type: ignore[arg-type]
    for _ in range(2):
        res = client.get("/api/workbench/sync-profiles", headers=bearer(HUMAN_TOKEN)).json()
        assert res == {
            "profiles": [{"profile_id": "p-1", "alias": None, "country": None, "currency": None}]
        }
    assert port.calls == 2  # 空名录不进终态缓存


def test_sort_survives_nan_metric() -> None:
    """二轮审计：NaN 能过 Decimal 构造但参与比较即抛 InvalidOperation——源侧一行
    脏数据不得把整页排序打成 500。NaN 与解析失败同罪：恒排末尾，正常行照常排。"""
    client, repo = make_app()
    repo.append(
        snap(
            "campaign:good",
            ObjectLevel.CAMPAIGN,
            name="Good",
            state="enabled",
            metrics={"spends": "5.00"},
        )
    )
    repo.append(
        snap(
            "campaign:nan",
            ObjectLevel.CAMPAIGN,
            name="Dirty",
            state="enabled",
            metrics={"spends": "NaN"},
        )
    )
    res = client.get(
        "/api/workbench/objects",
        params={"profile_id": PROFILE, "sort_field": "spend"},
        headers=bearer(HUMAN_TOKEN),
    )
    assert res.status_code == 200
    keys = [r["object_key"] for r in res.json()["rows"]]
    assert keys == ["campaign:good", "campaign:nan"]


# ---------------------------------------------------------------- history


def test_history_is_ascending_and_unknown_key_is_empty() -> None:
    client = seeded_client()
    res = client.get(
        "/api/workbench/history",
        params={"object_key": "campaign:c1"},
        headers=bearer(HUMAN_TOKEN),
    )
    assert res.status_code == 200
    entries = res.json()["entries"]
    assert [e["daily_budget"] for e in entries] == ["12.00", "13.00"]
    assert entries[0]["recorded_at"] < entries[1]["recorded_at"]

    unknown = client.get(
        "/api/workbench/history",
        params={"object_key": "campaign:ghost"},
        headers=bearer(HUMAN_TOKEN),
    ).json()
    assert unknown == {"object_key": "campaign:ghost", "entries": []}


def test_history_rows_say_which_window_their_metrics_cover() -> None:
    """历史面板是逐行比大小的地方：两条快照并排，「花费 34.10 → 41.20」读起来就是
    「涨了」。而两轮同步的窗口可以不同（截断的同步隔天再开一轮，窗口右端跟着 as_of
    走），变化里于是混着「窗口换了」这一项——此前这一列根本不存在，看不出来。

    说不出窗口的行如实为 None（不是同步来的，比如演示种子），不编一个。
    """
    client, repo = make_app()
    repo.append(
        snap(
            "campaign:win",
            ObjectLevel.CAMPAIGN,
            name="Camp-Win",
            recorded_at=NOW - timedelta(days=1),
            metrics={"spends": "34.10"},
            report_date="2026-08-21 - 2026-08-28",
        )
    )
    repo.append(
        snap(
            "campaign:win",
            ObjectLevel.CAMPAIGN,
            name="Camp-Win",
            metrics={"spends": "41.20"},
            report_date="2026-08-22 - 2026-08-29",
        )
    )
    repo.append(snap("campaign:seed", ObjectLevel.CAMPAIGN, name="Seed"))
    entries = client.get(
        "/api/workbench/history",
        params={"object_key": "campaign:win"},
        headers=bearer(HUMAN_TOKEN),
    ).json()["entries"]
    assert [e["report_date"] for e in entries] == [
        "2026-08-21 - 2026-08-28",
        "2026-08-22 - 2026-08-29",
    ]
    seed = client.get(
        "/api/workbench/history",
        params={"object_key": "campaign:seed"},
        headers=bearer(HUMAN_TOKEN),
    ).json()["entries"]
    assert seed[0]["report_date"] is None


# ---------------------------------------------------------------- 组合根与 UI 烟囱


def test_local_demo_mounts_workbench_with_seeded_mirror() -> None:
    client = TestClient(build_local_demo_app())
    unauth = client.get("/api/workbench/objects", params={"profile_id": "profile-A"})
    assert unauth.status_code == 401

    res = client.get(
        "/api/workbench/objects",
        params={"profile_id": "profile-A"},
        headers=bearer(DEMO_OWNER_TOKEN),
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["mirror_empty"] is False
    assert payload["total"] == 7  # 2 活动 + 2 广告组 + 3 投放（含 keyword 并层）
    managed = [row for row in payload["rows"] if row["ads_strategy"]]
    assert managed, "种子镜像应包含托管打标对象（UI 锁形徽章演示）"


def test_ui_app_js_passes_node_syntax_check() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available on this machine; UI syntax smoke skipped")
    result = subprocess.run(
        [node, "--check", str(UI_STATIC_DIR / "app.js")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------- 覆盖告知与续拉（HTTP 面）
# 2026-08-29 排查 P0：同步截断从不外传，界面把截断样本当店铺全貌。以下钉死 HTTP 合同。


class PagedReadPort:
    """campaign 报表 5 行、页长 100 下单页拉全；配合 max_pages 可制造截断。"""

    def __init__(self, campaign_total: int = 5) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._total = campaign_total

    def fetch_page(self, tool_id: str, params: Mapping[str, object]) -> Mapping[str, object]:
        self.calls.append((tool_id, dict(params)))
        page = params["page"]
        assert isinstance(page, int)
        if tool_id == "ad_campaign_report" and page <= self._total:
            # 页长 100 但每页只回 1 行 + total=5 → 引擎必须依赖 total/上限判断截断。
            rows: list[dict[str, object]] = [
                {"campaign_id": f"c-p{page}", "name": f"Camp-{page}", "state": "enabled"}
            ]
            return {"rows": rows, "total": self._total}
        return {"rows": [], "total": 0 if tool_id != "ad_campaign_report" else self._total}


def _sync_env(monkeypatch: pytest.MonkeyPatch, max_pages: str) -> None:
    _clear_sync_env(monkeypatch)
    monkeypatch.setenv("LX_MCP_KEY", "k-test")
    monkeypatch.setenv("LX_MCP_URL", "https://lx.example/mcp")
    monkeypatch.setenv("ADS_CP_SYNC_PROFILES", PROFILE)
    monkeypatch.setenv("ADS_CP_SYNC_MAX_PAGES", max_pages)


def test_sync_response_reports_truncation_and_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sync_env(monkeypatch, max_pages="2")
    client, _ = make_app(read_port=PagedReadPort())
    res = client.post(
        "/api/workbench/sync", headers=bearer(HUMAN_TOKEN), json={"profile_id": PROFILE}
    ).json()
    assert res["truncated"] is True
    campaign_cov = next(c for c in res["coverage"] if c["tool_id"] == "ad_campaign_report")
    assert campaign_cov == {
        "tool_id": "ad_campaign_report",
        "level": "CAMPAIGN",
        "source_total": 5,
        "rows_covered": 2,
        "truncated": True,
    }
    cont = res["continuation"]
    assert cont["report_date"] == res["report_date"]
    assert cont["next_pages"] == {"ad_campaign_report": 3}

    # /objects 如实回答「镜像里的 CAMPAIGN 层不是全部」
    listed = client.get(
        "/api/workbench/objects",
        params={"profile_id": PROFILE, "level": "campaign"},
        headers=bearer(HUMAN_TOKEN),
    ).json()
    assert listed["level_coverage"] == {
        "source_total": 5,
        "rows_covered": 2,
        "truncated": True,
    }
    assert listed["report_date"] == res["report_date"]
    # 审计 #4/#6：游标随 /objects 回传——页面刷新后续拉入口据此恢复，
    # 而不是让人对着「还有 N 个未同步」永远找不到当初的按钮。
    assert listed["sync_continuation"] == cont


def test_sync_continuation_round_trip_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    """把上一轮的 continuation 原样传回 → 从断点续拉 → 拉全后 continuation 消失。"""
    _sync_env(monkeypatch, max_pages="2")
    port = PagedReadPort()
    client, _ = make_app(read_port=port)
    first = client.post(
        "/api/workbench/sync", headers=bearer(HUMAN_TOKEN), json={"profile_id": PROFILE}
    ).json()
    port.calls.clear()
    monkeypatch.setenv("ADS_CP_SYNC_MAX_PAGES", "5")
    second = client.post(
        "/api/workbench/sync",
        headers=bearer(HUMAN_TOKEN),
        json={"profile_id": PROFILE, "continuation": first["continuation"]},
    ).json()
    campaign_calls = [p for t, p in port.calls if t == "ad_campaign_report"]
    assert campaign_calls[0]["page"] == 3
    assert all(p["report_date"] == first["report_date"] for p in campaign_calls)
    assert second["truncated"] is False
    assert second["continuation"] is None
    # 覆盖状态随最后一轮更新：CAMPAIGN 层已拉全。
    listed = client.get(
        "/api/workbench/objects",
        params={"profile_id": PROFILE, "level": "campaign"},
        headers=bearer(HUMAN_TOKEN),
    ).json()
    assert listed["level_coverage"]["truncated"] is False
    assert listed["level_coverage"]["rows_covered"] >= 5
    assert listed["sync_continuation"] is None  # 拉全后不再提供续拉入口


def test_sync_complete_run_has_null_continuation(monkeypatch: pytest.MonkeyPatch) -> None:
    _sync_env(monkeypatch, max_pages="10")
    client, _ = make_app(read_port=PagedReadPort())
    res = client.post(
        "/api/workbench/sync", headers=bearer(HUMAN_TOKEN), json={"profile_id": PROFILE}
    ).json()
    assert res["truncated"] is False
    assert res["continuation"] is None


def test_cursor_from_a_previous_process_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """镜像是纯内存的，重启即清空；而 demo token 是固定串，浏览器里的旧断点
    重启后照样 POST 得进去。

    原样采信的后果：标着 complete 的表一页都不拉，游标里的 rows_covered 被抄进
    本轮 coverage 当作事实上报——黄条写「广告组 250/250」看上去这层是齐的，
    人切过去表却是空的，空态还平铺直叙地说「该店铺镜像里没有广告组对象」。
    这句话是假的：不是店里没有，是这轮压根没去拉。而按花费降序拉取意味着
    缺的正是花钱最多的那一段。
    """
    _sync_env(monkeypatch, max_pages="2")
    client, _ = make_app(read_port=PagedReadPort())
    first = client.post(
        "/api/workbench/sync", headers=bearer(HUMAN_TOKEN), json={"profile_id": PROFILE}
    ).json()
    assert first["continuation"]["epoch"]

    # 新进程 = 新纪元 = 新镜像。旧游标描述的那份数据已经不在了。
    restarted, _ = make_app(read_port=PagedReadPort())
    res = restarted.post(
        "/api/workbench/sync",
        headers=bearer(HUMAN_TOKEN),
        json={"profile_id": PROFILE, "continuation": first["continuation"]},
    )
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "SYNC_CURSOR_STALE"


def test_an_empty_page_short_of_total_is_not_reported_as_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上游说这张表有 N 行、却在远没给够时返回零行，这是异常，不是「拉完了」。

    无条件置 complete 的后果：truncated 留 False、continuation 变 None，
    「继续拉取」按钮当场消失，对象表分页条走非截断分支只剩「共 250 个对象」——
    11723 这个数字从此在界面上不再出现。人拿到的是一条绿色成功条
    「已同步全部：活动 250/11723」：绿条和「全部」说拉全了，紧挨着的分数说只有
    250，没有一处告诉他信哪个。
    """

    class StallingPort(PagedReadPort):
        def fetch_page(self, tool_id: str, params: dict) -> dict:
            page = params["page"]
            if tool_id == "ad_campaign_report" and page >= 2:
                return {"rows": [], "total": 500}  # 上游说 500 行，第 2 页就断供
            return super().fetch_page(tool_id, params)

    _sync_env(monkeypatch, max_pages="5")
    client, _ = make_app(read_port=StallingPort())
    res = client.post(
        "/api/workbench/sync", headers=bearer(HUMAN_TOKEN), json={"profile_id": PROFILE}
    ).json()
    campaign = next(c for c in res["coverage"] if c["tool_id"] == "ad_campaign_report")
    assert campaign["truncated"] is True, "没给够就断供不能报告成已拉全"
    assert res["continuation"] is not None, "续拉入口必须留着"


# ---------------------------------------------------------------- 指标窗口一致性（#13）


def test_rows_from_one_window_report_that_window() -> None:
    client, repo = make_app()
    for key in ("campaign:w1", "campaign:w2"):
        repo.append(
            snap(
                key,
                ObjectLevel.CAMPAIGN,
                name=key,
                state="enabled",
                metrics={"spends": "10.00"},
                report_date="2026-08-13 - 2026-08-26",
            )
        )
    payload = client.get(
        "/api/workbench/objects",
        params={"profile_id": PROFILE, "level": "campaign"},
        headers=bearer(HUMAN_TOKEN),
    ).json()
    assert payload["report_date"] == "2026-08-13 - 2026-08-26"
    assert payload["report_windows"] == ["2026-08-13 - 2026-08-26"]
    assert payload["report_window_unknown_rows"] == 0


def test_rows_from_two_windows_refuse_to_name_one() -> None:
    """截断的同步隔天再开一轮新的：没被重拉到的行仍带着上一个窗口的花费。

    此前窗口是 profile 级的一个全局值、每轮无条件覆写，于是表头会挑最新那个窗口
    印上去，当成全体的标签。人照着它把整张表按花费排序，比的却是两段不同时间的
    合计——屏幕上没有一个字提示过这件事。现在窗口记在每一行上：口径不齐时
    report_date 为 None，由界面去说「不可横向比较」，而不是挑一个印出来。
    """
    client, repo = make_app()
    repo.append(
        snap(
            "campaign:old",
            ObjectLevel.CAMPAIGN,
            name="上一轮拉到的",
            state="enabled",
            metrics={"spends": "10.00"},
            report_date="2026-08-12 - 2026-08-25",
        )
    )
    repo.append(
        snap(
            "campaign:new",
            ObjectLevel.CAMPAIGN,
            name="这一轮拉到的",
            state="enabled",
            metrics={"spends": "10.00"},
            report_date="2026-08-13 - 2026-08-26",
        )
    )
    payload = client.get(
        "/api/workbench/objects",
        params={"profile_id": PROFILE, "level": "campaign"},
        headers=bearer(HUMAN_TOKEN),
    ).json()
    assert payload["report_date"] is None
    assert payload["report_windows"] == ["2026-08-12 - 2026-08-25", "2026-08-13 - 2026-08-26"]
    assert payload["report_window_unknown_rows"] == 0


def test_rows_without_a_window_are_counted_not_absorbed() -> None:
    """「说不出自己是哪一段的行」与「窗口不止一个」是两种不同的不齐，不能合成一句。"""
    client, repo = make_app()
    repo.append(
        snap(
            "campaign:synced",
            ObjectLevel.CAMPAIGN,
            name="同步来的",
            state="enabled",
            report_date="2026-08-13 - 2026-08-26",
        )
    )
    repo.append(snap("campaign:seeded", ObjectLevel.CAMPAIGN, name="种子数据", state="enabled"))
    payload = client.get(
        "/api/workbench/objects",
        params={"profile_id": PROFILE, "level": "campaign"},
        headers=bearer(HUMAN_TOKEN),
    ).json()
    assert payload["report_date"] is None
    assert payload["report_windows"] == ["2026-08-13 - 2026-08-26"]
    assert payload["report_window_unknown_rows"] == 1


def test_window_is_computed_over_filtered_rows_not_the_current_page() -> None:
    """表头标的是这张表，而排序与「共 N 行」都按过滤后的全部行算——窗口也必须。

    只看当前页会让第 1 页说「口径一致」、第 2 页说「不一致」，而人正是在跨页排序。
    """
    client, repo = make_app()
    repo.append(
        snap(
            "campaign:a",
            ObjectLevel.CAMPAIGN,
            name="A",
            state="enabled",
            report_date="2026-08-12 - 2026-08-25",
        )
    )
    for i in range(3):
        repo.append(
            snap(
                f"campaign:b{i}",
                ObjectLevel.CAMPAIGN,
                name=f"B{i}",
                state="enabled",
                report_date="2026-08-13 - 2026-08-26",
            )
        )
    payload = client.get(
        "/api/workbench/objects",
        params={"profile_id": PROFILE, "level": "campaign", "length": 1, "page": 1},
        headers=bearer(HUMAN_TOKEN),
    ).json()
    assert len(payload["rows"]) == 1  # 当前页只有一行，若按页算就会说「口径一致」
    assert payload["report_date"] is None
    assert len(payload["report_windows"]) == 2


def test_continuation_is_gated_on_having_a_next_page_not_on_truncation() -> None:
    """续拉入口只在「确实还有下一页」时出现，不因「覆盖不足」出现。

    truncated 说的是「拿到的行比上游报的少」；有没有下一页是另一回事。上游提前
    给出空页时两者分道扬镳：覆盖确实不足，但下一页并不存在。此时若仍发
    continuation，前端 `do{...}while(wb.continuation)` 会一直回传游标、一直拿到
    空页，直到 300 轮硬上限**无声**退出——每轮五张表各烧一次 QPS=1 的调用。

    覆盖不足这句话仍要说（truncated 留 True，分页条照写「拉到 1/2」），
    只是不再把人和自动循环指向一条不存在的下一页。
    """
    from ads_control_plane.api.workbench_api import _report_summary
    from ads_control_plane.mirror.sync import SyncRunReport, ToolCoverage

    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    short_but_exhausted = ToolCoverage(
        tool_id="ad_campaign_report",
        level="CAMPAIGN",
        source_total=2,
        rows_covered=1,
        rows_seen=2,  # 上游把 2 行都给了，其中 1 行我们映射不了
        start_page=1,
        next_page=None,  # 上游不欠行了，没有下一页可问
        truncated=True,  # 但覆盖确实不足
        complete=False,
        pages_fetched=2,
    )
    report = SyncRunReport(
        run_id="r1",
        profile_id="profile-1",
        started_at=now,
        finished_at=now,
        report_date="2026-08-23 - 2026-08-30",
        per_level_rows={"CAMPAIGN": 1},
        coverage=(short_but_exhausted,),
        skipped_summary_rows=1,
        decimal_parse_failures=0,
        pages_fetched=2,
        catalog_versions={},
        schema_versions={},
    )
    summary = _report_summary(report, "epoch-1")
    assert summary["truncated"] is True, "覆盖不足这句话不能不说"
    assert summary["continuation"] is None, "没有下一页却发了续拉游标，自动拉取会空转到 300 轮"
