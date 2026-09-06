"""授权书作用域/运行时段的 HTTP 合同 + 工作台浏览端筛选排序 + 演示身份合并。

三块覆盖，都是「人怎么填」这一侧的合同，域层安全内核（参数包自包含、拒绝调用方
覆盖、配额、TTL）一个字段都没动：

- POST /mandates 的 scope / run_window：缺省行为与新增它们之前逐字一致；非法输入
  一律 422 带域层码（INVALID_TIMEZONE / SCOPE_SELECTION_REQUIRED / SELECTION_TOO_BROAD
  / MANDATE_SCOPE_CONFLICT / SCOPE_KIND_INVALID / SCOPE_LEVEL_INVALID），不静默忽略；
- GET /mandates 每条摘要的 scope_summary / run_window_summary 人话渲染（含跨午夜）；
- GET /api/workbench/objects 的 name_contains / state / managed_only / sort_field /
  sort_dir：缺省不生效，非白名单一律 400 显式拒绝。

运行时段是「系统什么时候检查广告」，不是分时竞价：这里既不产生按时段变化的写值，
也不碰对象的 ads_strategy / is_apply_time。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ads_control_plane.api.local_demo import (
    DEMO_CODEX_TOKEN,
    DEMO_OWNER_TOKEN,
    build_local_demo_app,
)
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
from ads_control_plane.mirror.sync import CATALOG_VERSION
from ads_control_plane.tasks.directive import ObjectLevel

PROFILE = "profile-A"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)

#: Owner 第 6 点的原例：吉隆坡当地凌晨 2 点到晚上 6 点。
KL_WINDOW = {"timezone": "Asia/Kuala_Lumpur", "start_hour": 2, "end_hour": 18}

BASE_BODY: dict[str, Any] = {
    "profile_external_id": PROFILE,
    "objective": "WASTED_SPEND_REMOVED",
    "statement": "清除近 30 天零转化高花费搜索词造成的广告浪费",
    "lookback_days": 30,
    "min_spend_amount": "20.00",
    "currency": "USD",
    "min_clicks": 25,
    "max_data_staleness_hours": 24,
    "max_runs_per_day": 1,
    "max_candidates_per_run": 50,
    "valid_days": 7,
    "run_interval_minutes": 1440,
}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def body(**overrides: Any) -> dict[str, Any]:
    return {**BASE_BODY, **overrides}


def demo_client() -> TestClient:
    return TestClient(build_local_demo_app())


def issue(client: TestClient, payload: dict[str, Any], token: str = DEMO_OWNER_TOKEN) -> Any:
    return client.post("/mandates", headers=bearer(token), json=payload)


# ------------------------------------------------------------------ 运行时段（签发 + 摘要）


def test_mandate_without_new_fields_behaves_exactly_as_before() -> None:
    """向后兼容锚点：不传 scope / run_window 时，既有字段与语义逐字不变。

    摘要里出现的是「整店」与 None，而不是一个 null 作用域——审计读到的永远是
    一句话，缺省不等于「他忘了填」。
    """
    res = issue(demo_client(), body())
    assert res.status_code == 200
    mandate = res.json()
    assert mandate["state"] == "ACTIVE"
    assert mandate["issued_by_person_id"] == "owner-1"
    assert mandate["bounds"]["run_interval_minutes"] == 1440
    assert mandate["scope_summary"] == "整店"
    assert mandate["run_window_summary"] is None


def test_kuala_lumpur_window_is_issued_and_rendered_in_human_words() -> None:
    """Owner 原例端到端：签发时带时区窗口，摘要渲染成人能核对的一句话。"""
    res = issue(demo_client(), body(run_window=KL_WINDOW))
    assert res.status_code == 200
    mandate = res.json()
    assert mandate["run_window_summary"] == "每天 02:00–18:00（Asia/Kuala_Lumpur）"
    # 时段是授权边界不是策略参数：不进参数包，因此不改变参数包 hash。
    assert (
        mandate["parameter_pack_hash"] == issue(demo_client(), body()).json()["parameter_pack_hash"]
    )


def test_window_crossing_midnight_renders_next_day() -> None:
    """跨午夜（当地晚 10 点到次日早 6 点）：摘要必须说「至次日」，不能显示成 22–06。"""
    window = {"timezone": "Asia/Kuala_Lumpur", "start_hour": 22, "end_hour": 6}
    res = issue(demo_client(), body(run_window=window))
    assert res.status_code == 200
    assert res.json()["run_window_summary"] == "每天 22:00 至次日 06:00（Asia/Kuala_Lumpur）"


def test_the_quota_day_boundary_is_stated_and_matches_a_cross_midnight_window() -> None:
    """「N 次/日」里那个「日」的边界必须说清，而且要跟跨午夜时段的实际行为一致。

    2026-08-30 排查：跨午夜时段（22:00→次日 06:00）里当地午夜落在窗口正中间，
    配额日因此从窗口起点算，整夜算一天。悬停若只写「按 Asia/Kuala_Lumpur 的当地日
    切换」，人会以为过了半夜配额就重置——那是这次修掉的旧行为，说法留在原处就成了
    一句描述缺陷的话，而它与同一张卡片上的「每天 22:00 至次日 06:00」并排显示。
    """
    night = issue(
        demo_client(),
        body(run_window={"timezone": "Asia/Kuala_Lumpur", "start_hour": 22, "end_hour": 6}),
    ).json()
    assert night["quota_day_summary"] == (
        "「日」按 Asia/Kuala_Lumpur 计，每天 22:00 换一次——整夜算同一天"
    )
    # 不跨午夜的时段仍是当地日历日：平移一个不需要平移的窗口，会造出人猜不到的日界。
    day = issue(demo_client(), body(run_window=KL_WINDOW)).json()
    assert (
        day["quota_day_summary"] == "「日」按 Asia/Kuala_Lumpur 的当地日切换（该时区 0 点换一次）"
    )
    # 「到当天结束为止」（end_hour=0）不是跨午夜：22→0 就是 22:00–23:59，
    # 一分钟都没跨过。说成「整夜算同一天」是对一个根本没有夜的时段说的。
    till_end = issue(
        demo_client(),
        body(run_window={"timezone": "Asia/Kuala_Lumpur", "start_hour": 22, "end_hour": 0}),
    ).json()
    assert "整夜" not in till_end["quota_day_summary"]
    assert till_end["quota_day_summary"] == day["quota_day_summary"]
    # 没设时段的授权书没有声明过任何时区，只能按 UTC。这一支绝不许说「当地 0 点」——
    # 一个 UTC+8 的卖家会把它读成自己的 0 点，而实际是他的早上 8 点。
    # 签发表单默认就是「不限时段」，这是最常见的一支。
    none_set = issue(demo_client(), body()).json()["quota_day_summary"]
    assert none_set == (
        "「日」按 UTC 切换（UTC 0 点换一次）——这份授权没设运行时段，"
        "也就没声明过任何时区；它与你所在时区的 0 点通常不是同一时刻"
    )
    assert "当地 0 点" not in none_set


def test_all_day_window_is_rendered_as_all_day() -> None:
    """起止同点 = 全天（域层语义）；摘要说全天，不假装是一个 00:00–00:00 的时段。"""
    window = {"timezone": "Asia/Tokyo", "start_hour": 0, "end_hour": 0}
    res = issue(demo_client(), body(run_window=window))
    assert res.status_code == 200
    assert res.json()["run_window_summary"] == "不限时段（Asia/Tokyo）"


def test_unknown_timezone_is_422_with_domain_code() -> None:
    """系统不会拿服务器所在地的钟点替人解释「凌晨 2 点」：认不出的时区直接拒。"""
    window = {"timezone": "Mars/Olympus", "start_hour": 2, "end_hour": 18}
    res = issue(demo_client(), body(run_window=window))
    assert res.status_code == 422
    assert res.json()["detail"] == "INVALID_TIMEZONE"


def test_hour_out_of_range_is_422_with_domain_code() -> None:
    window = {"timezone": "Asia/Kuala_Lumpur", "start_hour": 2, "end_hour": 24}
    res = issue(demo_client(), body(run_window=window))
    assert res.status_code == 422
    assert res.json()["detail"] == "RUN_WINDOW_INVALID"


def test_drifting_interval_with_window_is_422_not_403() -> None:
    """100 分钟间隔 + 固定时段会逐日漂移直到永远跑不了——这是两个请求字段的组合
    形状问题，不是授权判定，所以是 422（参数）而不是 403（不许你干）。"""
    payload = body(run_interval_minutes=100, run_window=KL_WINDOW)
    res = issue(demo_client(), payload)
    assert res.status_code == 422
    assert res.json()["detail"] == "RUN_WINDOW_INCOMPATIBLE"


def test_drifting_interval_without_window_is_accepted() -> None:
    """没有时段就没有漂移问题：不设窗口时 100 分钟间隔仍然合法，不误伤。"""
    assert issue(demo_client(), body(run_interval_minutes=100)).status_code == 200


# ------------------------------------------------------------------ 作用域（签发 + 摘要）


def test_objects_scope_is_issued_and_counted_per_level() -> None:
    """勾选对象作用域：摘要按层级分别计数，人一眼看出这份授权管到哪。"""
    scope = {
        "kind": "OBJECTS",
        "items": [
            {"level": "campaign", "external_id": "c-1"},
            {"level": "campaign", "external_id": "c-2"},
            {"level": "ad_group", "external_id": "ag-1"},
        ],
    }
    res = issue(demo_client(), body(scope=scope))
    assert res.status_code == 200
    assert res.json()["scope_summary"] == "2 个广告活动 · 1 个广告组"


def test_explicit_profile_scope_reads_as_entire_store() -> None:
    """整店是**扩大授权**的正向意思表示，显式写下来后摘要与缺省同义。"""
    res = issue(demo_client(), body(scope={"kind": "PROFILE"}))
    assert res.status_code == 200
    assert res.json()["scope_summary"] == "整店"


def test_objects_scope_without_items_is_rejected() -> None:
    """选了「只管勾选的对象」却一个都没勾——不能悄悄退化成整店授权。"""
    res = issue(demo_client(), body(scope={"kind": "OBJECTS", "items": []}))
    assert res.status_code == 422
    assert res.json()["detail"] == "SCOPE_SELECTION_REQUIRED"


def test_profile_scope_carrying_items_is_rejected() -> None:
    """整店 + 勾选清单同时出现：两种意思相互矛盾，拒绝而不是替人挑一个。"""
    scope = {"kind": "PROFILE", "items": [{"level": "campaign", "external_id": "c-1"}]}
    res = issue(demo_client(), body(scope=scope))
    assert res.status_code == 422
    assert res.json()["detail"] == "MANDATE_SCOPE_CONFLICT"


def test_scope_over_selection_limit_is_rejected_with_selection_code() -> None:
    """超 200 个对象：复用勾选集自己的上限与错误码，不新造一套。"""
    scope = {
        "kind": "OBJECTS",
        "items": [{"level": "campaign", "external_id": f"c-{i}"} for i in range(201)],
    }
    res = issue(demo_client(), body(scope=scope))
    assert res.status_code == 422
    assert res.json()["detail"] == "SELECTION_TOO_BROAD"


def test_unknown_scope_kind_is_422() -> None:
    res = issue(demo_client(), body(scope={"kind": "EVERYTHING"}))
    assert res.status_code == 422
    assert res.json()["detail"] == "SCOPE_KIND_INVALID"


def test_unknown_scope_level_is_422() -> None:
    scope = {"kind": "OBJECTS", "items": [{"level": "portfolio", "external_id": "p-1"}]}
    res = issue(demo_client(), body(scope=scope))
    assert res.status_code == 422
    assert res.json()["detail"] == "SCOPE_LEVEL_INVALID"


def test_misspelled_field_is_rejected_not_silently_ignored() -> None:
    """拼错的字段名必须报错：静默忽略会把一份「我填了时段」的授权签成全天授权。"""
    payload = body()
    payload["runwindow"] = KL_WINDOW
    res = issue(demo_client(), payload)
    assert res.status_code == 422


# ------------------------------------------------------------------ 授权判定优先级


def test_ai_still_cannot_issue_even_with_valid_scope_and_window() -> None:
    """新增字段不给 AI 开任何口子：形状再合法，签发仍是人的意思表示。"""
    scope = {"kind": "OBJECTS", "items": [{"level": "campaign", "external_id": "c-1"}]}
    res = issue(demo_client(), body(scope=scope, run_window=KL_WINDOW), token=DEMO_CODEX_TOKEN)
    assert res.status_code == 403
    assert res.json()["detail"] == "AI_CANNOT_ISSUE_MANDATE"


def test_target_level_scope_is_rejected_as_authorization_call() -> None:
    """投放层作用域：否定词按广告组落位，搜索词记录不携带触发它的 target，作用域
    在数据上无法判定包含关系。不可实现即不可授权——403，不是静默按父广告组近似。"""
    scope = {"kind": "OBJECTS", "items": [{"level": "target", "external_id": "kw-1"}]}
    res = issue(demo_client(), body(scope=scope))
    assert res.status_code == 403
    assert res.json()["detail"] == "MANDATE_SCOPE_LEVEL_UNSUPPORTED"


def test_unready_objective_still_wins_over_bad_scope_levels() -> None:
    """优先级断言：数据地基未就绪的教育路径不被新字段挤掉——即使作用域层级也不合法，
    先返回的仍是 OBJECTIVE_NOT_READY。"""
    scope = {"kind": "OBJECTS", "items": [{"level": "target", "external_id": "kw-1"}]}
    payload = body(objective="CLEARANCE_VELOCITY", scope=scope, run_window=KL_WINDOW)
    res = issue(demo_client(), payload)
    assert res.status_code == 403
    assert res.json()["detail"] == "OBJECTIVE_NOT_READY"


def test_mandate_list_carries_both_summaries() -> None:
    """GET /mandates 的每条摘要都带这两个只读字段，UI 直接显示、不再自己拼。"""
    client = demo_client()
    scope = {"kind": "OBJECTS", "items": [{"level": "ad_group", "external_id": "ag-1"}]}
    assert issue(client, body(scope=scope, run_window=KL_WINDOW)).status_code == 200
    listed = client.get("/mandates", headers=bearer(DEMO_OWNER_TOKEN)).json()["mandates"]
    assert len(listed) == 1
    assert listed[0]["scope_summary"] == "1 个广告组"
    assert listed[0]["run_window_summary"] == "每天 02:00–18:00（Asia/Kuala_Lumpur）"


# ------------------------------------------------------------------ 工作台浏览端筛选与排序

WB_TOKEN = "wb-human-token"
WB_PROFILE = "profile-w"


def _wb_snap(object_key: str, level: ObjectLevel, **overrides: Any) -> AdObjectSnapshot:
    base: dict[str, Any] = {
        "object_key": object_key,
        "level": level,
        "profile_id": WB_PROFILE,
        "source_as_of": NOW,
        "recorded_at": NOW,
        "catalog_version": CATALOG_VERSION,
        "schema_version": "ad_campaign_report-v1",
    }
    base.update(overrides)
    return AdObjectSnapshot(**base)


def workbench_client() -> TestClient:
    """4 个活动：花费可比 3 个 + 花费缺失 1 个；含托管、暂停与关键词行。"""
    repo = InMemorySnapshotRepository()
    repo.append(
        _wb_snap(
            "campaign:c1",
            ObjectLevel.CAMPAIGN,
            name="HX02-Auto-US",
            state="enabled",
            metrics={"spends": "34.10", "acos": "0.42", "orders": "3", "clicks": "120"},
        )
    )
    repo.append(
        _wb_snap(
            "campaign:c2",
            ObjectLevel.CAMPAIGN,
            name="HX02-Exact-US",
            state="paused",
            ads_strategy="分时预算",
            metrics={"spends": "61.75", "acos": "0.18", "orders": "11", "clicks": "260"},
        )
    )
    repo.append(
        _wb_snap(
            "campaign:c3",
            ObjectLevel.CAMPAIGN,
            name="Autumn-Clearance",
            state="enabled",
            metrics={"spends": "9.30", "acos": "0.63", "orders": "0", "clicks": "31"},
        )
    )
    # 花费缺失：镜像里 metrics 是源侧原样字符串，缺项/不可解析都真实存在。
    repo.append(
        _wb_snap(
            "campaign:c4",
            ObjectLevel.CAMPAIGN,
            name="No-Metrics-Yet",
            state="enabled",
            metrics={"acos": "not-a-number"},
        )
    )
    repo.append(
        _wb_snap(
            "target:kw1",
            ObjectLevel.TARGET,
            name="widget holder",
            state="enabled",
            keyword_text="widget holder",
            schema_version="ad_campaign_keyword_report-v1",
            metrics={"spends": "11.10"},
        )
    )
    verifier = InMemoryActorTokenVerifier()
    real_now = datetime.now(UTC)
    verifier.register(
        WB_TOKEN,
        ActorContext(
            principal_id=new_canonical_id(),
            principal_type=PrincipalType.HUMAN,
            organization_id=new_canonical_id(),
            roles=frozenset({Role.OPERATOR, Role.APPROVER}),
            human_person_id="owner-1",
            client_id="web-owner",
            session_id="s-owner",
            authentication_strength=AuthenticationStrength.MFA,
            issued_at=real_now,
            expires_at=real_now + timedelta(hours=8),
        ),
    )
    app = FastAPI()
    app.include_router(build_workbench_router(repo, verifier))
    return TestClient(app)


def wb_get(client: TestClient, **params: Any) -> Any:
    return client.get(
        "/api/workbench/objects",
        params={"profile_id": WB_PROFILE, **params},
        headers=bearer(WB_TOKEN),
    )


def test_objects_without_new_params_is_unchanged() -> None:
    """缺省行为锚点：不传任何新参数时返回与既往一致（含 repo.current 的原有序）。"""
    payload = wb_get(workbench_client()).json()
    assert payload["total"] == 5
    assert payload["mirror_empty"] is False
    assert [row["object_key"] for row in payload["rows"]] == [
        "campaign:c1",
        "campaign:c2",
        "campaign:c3",
        "campaign:c4",
        "target:kw1",
    ]


def test_name_contains_is_case_insensitive_and_matches_id_and_keyword() -> None:
    client = workbench_client()
    assert {r["object_key"] for r in wb_get(client, name_contains="hx02").json()["rows"]} == {
        "campaign:c1",
        "campaign:c2",
    }
    # 粘贴对象 ID 搜索同样命中——只匹配 name 会让这条最常见的用法静默落空。
    assert [r["object_key"] for r in wb_get(client, name_contains="c3").json()["rows"]] == [
        "campaign:c3"
    ]
    assert [r["object_key"] for r in wb_get(client, name_contains="WIDGET").json()["rows"]] == [
        "target:kw1"
    ]
    # 全空白视为未提供，不是「匹配空串所以全命中」也不是「什么都不返回」。
    assert wb_get(client, name_contains="   ").json()["total"] == 5


def test_state_filter_is_exact_source_side_text() -> None:
    client = workbench_client()
    assert [r["object_key"] for r in wb_get(client, state="paused").json()["rows"]] == [
        "campaign:c2"
    ]
    # 源侧原文精确相等：不做大小写折叠，也不做同义词映射。
    assert wb_get(client, state="PAUSED").json()["total"] == 0


def test_managed_only_splits_managed_from_selectable() -> None:
    client = workbench_client()
    managed = wb_get(client, managed_only=True).json()
    assert [r["object_key"] for r in managed["rows"]] == ["campaign:c2"]
    unmanaged = wb_get(client, managed_only=False).json()
    assert "campaign:c2" not in {r["object_key"] for r in unmanaged["rows"]}
    assert unmanaged["total"] == 4
    assert wb_get(client).json()["total"] == 5  # 不传即不筛


def test_sort_by_spend_puts_unparsable_rows_last_in_both_directions() -> None:
    """缺失/不可解析的指标恒排末尾：当作 0 会让它冒充「最省钱的行」。"""
    client = workbench_client()

    def keys(direction: str) -> list[str]:
        res = wb_get(client, sort_field="spend", sort_dir=direction)
        return [row["object_key"] for row in res.json()["rows"]]

    desc = keys("desc")
    assert desc == ["campaign:c2", "campaign:c1", "target:kw1", "campaign:c3", "campaign:c4"]
    asc = keys("asc")
    assert asc == ["campaign:c3", "target:kw1", "campaign:c1", "campaign:c2", "campaign:c4"]
    # c4 在两个方向上都在末尾——它没有参与翻转，因为它根本不可比较。
    assert desc[-1] == asc[-1] == "campaign:c4"


def test_sort_by_acos_skips_unparsable_string() -> None:
    """acos="not-a-number"：源侧原样字符串解析失败，不抛错也不当 0。"""
    rows = wb_get(workbench_client(), sort_field="acos", sort_dir="desc").json()["rows"]
    assert [r["object_key"] for r in rows] == [
        "campaign:c3",
        "campaign:c1",
        "campaign:c2",
        "campaign:c4",
        "target:kw1",
    ]


def test_sort_by_name_defaults_to_ascending() -> None:
    rows = wb_get(workbench_client(), sort_field="name").json()["rows"]
    assert [r["name"] for r in rows][:3] == ["Autumn-Clearance", "HX02-Auto-US", "HX02-Exact-US"]


def test_sort_applies_before_pagination() -> None:
    """排序作用于筛选后的全集：只排当前页等于骗人。"""
    page = wb_get(workbench_client(), sort_field="spend", sort_dir="desc", length=1, page=1).json()
    assert [r["object_key"] for r in page["rows"]] == ["campaign:c2"]
    assert page["total"] == 5


def test_filter_and_sort_compose() -> None:
    rows = wb_get(
        workbench_client(),
        level="campaign",
        managed_only=False,
        sort_field="clicks",
        sort_dir="desc",
    ).json()["rows"]
    assert [r["object_key"] for r in rows] == ["campaign:c1", "campaign:c3", "campaign:c4"]


def test_invalid_sort_field_is_400() -> None:
    res = wb_get(workbench_client(), sort_field="daily_budget")
    assert res.status_code == 400
    assert res.json()["detail"] == "SORT_FIELD_INVALID"


def test_invalid_sort_dir_is_400() -> None:
    res = wb_get(workbench_client(), sort_field="spend", sort_dir="descending")
    assert res.status_code == 400
    assert res.json()["detail"] == "SORT_DIR_INVALID"


def test_sort_dir_without_sort_field_is_400() -> None:
    """单给方向不给字段什么也排不了；静默返回未排序结果会让人以为排过了。"""
    res = wb_get(workbench_client(), sort_dir="desc")
    assert res.status_code == 400
    assert res.json()["detail"] == "SORT_FIELD_INVALID"


def test_a_fresh_mandate_says_how_many_runs_are_left_today() -> None:
    """发起运行的是人，而「现在跑会不会被拒」的两个数此前只给了 AI。

    人手上只有静态合同值（「1 次/日」）和上次运行时刻，要自己减出间隔、还要读懂
    跨午夜的配额日界。而被拒的尝试**故意**不进运行流水（记进去会自耗配额、把授权
    锁死），所以卡片纹丝不动——他连刚才那次有没有打到服务端都判断不出。
    """
    client = demo_client()
    issued = issue(client, body(max_runs_per_day=2)).json()
    assert issued["runs_today"] == 0
    assert issued["runs_remaining_today"] == 2
    # 没跑过就没有「最早几点能再跑」——不许编一个时刻出来。
    assert issued["next_run_allowed_at"] is None


def test_the_two_numbers_are_the_same_ones_the_contract_enforces() -> None:
    """合同里写几次，剩余就从几次开始减。两处若各算各的，卡片会拿一个假数骗人。"""
    client = demo_client()
    for limit in (1, 3):
        issued = issue(client, body(max_runs_per_day=limit)).json()
        assert issued["runs_remaining_today"] == limit


def test_the_server_says_what_currency_a_store_settles_in_before_you_sign() -> None:
    """这个事实服务端一直握着，也一直在用它 422 拒签——只是从没在签发前说出来。

    界面此前从同步白名单端点取币种，而那条路答的是「哪些店允许被同步」，
    纯 Mock 部署下恒为空。于是币种字段恒说「服务端不知道这个店铺的数据币种」，
    一句假话，而它下面紧跟着的后果是真的：填错会被拒。
    """
    client = demo_client()
    res = client.get(
        "/mandates/profile-currency",
        headers=bearer(DEMO_OWNER_TOKEN),
        params={"profile_external_id": "profile-A"},
    )
    assert res.status_code == 200
    assert res.json()["currency"] == "USD"

    # 签发闸用的是同一个事实：两处若各算各的，界面会拿一个假数骗人。
    refused = issue(client, body(currency="EUR"))
    assert refused.status_code == 422
    assert "USD" in refused.text


def test_a_store_the_server_knows_nothing_about_gets_an_honest_null() -> None:
    """不知道就答 null——「不知道」本身是真话，只是不该在服务端知道时说。"""
    res = demo_client().get(
        "/mandates/profile-currency",
        headers=bearer(DEMO_OWNER_TOKEN),
        params={"profile_external_id": "some-unbound-profile"},
    )
    assert res.status_code == 200
    assert res.json()["currency"] is None


def test_a_price_with_a_thousands_separator_gets_a_sentence_not_a_python_class_name() -> None:
    """「至少花了多少钱」填错格式时，人得知道是哪个字段、错在哪。

    这个字段是签发表单里唯一不是 type=number 的数值框（金额要保 Decimal 精度）。
    在 2026-09-06 之前，填「1,234.00」得到的红条逐字是
    「参数被服务端白名单拒绝（服务端：[<class 'decimal.ConversionSyntax'>]）」——
    一个 Python 内部异常类名，8 个可填参数一个都没点名。
    """
    res = issue(demo_client(), body(min_spend_amount="1,234.00"))
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert detail["code"] == "MIN_SPEND_NOT_A_NUMBER"
    assert "ConversionSyntax" not in detail["message"]
    assert "min_spend_amount" in detail["message"]


def test_a_mandate_summary_carries_everything_needed_to_sign_the_same_contract_again() -> None:
    """到期重签是必然动作，界面的「照这份再签一份」全靠这些字段把表单填回去。

    摘要里那两句人话（scope_summary / run_window_summary）是给人读的，拼不回表单：
    少了 scope_kind 或结构化的 run_window，克隆出来的就是另一份合同——整店变成
    勾选、限定时段变成全天，而人以为自己只是「照原样再签一次」。
    """
    client = demo_client()
    payload = body(
        scope={
            "kind": "OBJECTS",
            "items": [{"level": "campaign", "external_id": "c-1"}],
        },
        run_window={"timezone": "Asia/Kuala_Lumpur", "start_hour": 2, "end_hour": 18},
    )
    issued = issue(client, payload)
    assert issued.status_code == 200, issued.text
    got = issued.json()
    assert got["scope_kind"] == "OBJECTS"
    assert got["run_window"] == {
        "timezone": "Asia/Kuala_Lumpur",
        "start_hour": 2,
        "end_hour": 18,
    }
    assert [i["external_id"] for i in got["scope_items"]] == ["c-1"]
    # 参数与配额也必须原样回显，否则克隆出来的阈值是另一套。
    assert got["parameter_pack"]["min_clicks"] == payload["min_clicks"]
    assert got["bounds"]["run_interval_minutes"] == payload["run_interval_minutes"]


def test_a_whole_store_mandate_says_so_structurally_not_only_in_prose() -> None:
    # 缺省（整店 + 全天）也要说得出：克隆时要据此把单选按钮拨回「整店所有广告」。
    got = issue(demo_client(), body()).json()
    assert got["scope_kind"] == "PROFILE"
    assert got["run_window"] is None


def test_the_quota_a_card_shows_is_counted_over_every_run_not_just_the_five_it_lists() -> None:
    """卡片上的「今天还能跑几次」必须和 MCP 面拒不拒是同一个数。

    卡片只回显最近 5 次运行，而配额一度就在那 5 条上求和：日上限 > 5 的授权跑满后，
    卡片仍写「今天还能跑 N 次」、悬停还承诺「现在发起不会被挡回」，而服务端用未截断的
    count_on_day 必回 RUN_BUDGET_EXCEEDED。人照着卡片把指令交给 AI，当场被拒。
    """
    import uuid
    from decimal import Decimal

    from ads_control_plane.api import approval_api
    from ads_control_plane.strategies.mandate import (
        AutomationMandate,
        MandateBounds,
        MandateObjective,
    )
    from ads_control_plane.strategies.mandate_run import MandateRunOutcome, MandateRunRecord
    from ads_control_plane.strategies.negation import Money, NegationParameterPack
    from ads_control_plane.strategies.store import InMemoryMandateRunLog

    now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    mandate = AutomationMandate(
        mandate_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        profile_external_id="profile-A",
        objective=MandateObjective(objective="WASTED_SPEND_REMOVED", statement="x"),
        parameter_pack=NegationParameterPack(
            lookback_days=30,
            min_spend=Money(amount=Decimal("20.00"), currency="USD"),
            min_clicks=25,
            max_data_staleness_hours=24,
        ),
        bounds=MandateBounds(
            max_runs_per_day=6,
            max_candidates_per_run=50,
            valid_days=7,
            run_interval_minutes=60,
        ),
        issued_by_person_id="owner-1",
        issued_at=now - timedelta(hours=10),
        expires_at=now + timedelta(days=7),
    )
    log = InMemoryMandateRunLog()
    for i in range(6):
        log.record(
            MandateRunRecord(
                run_id=uuid.uuid4(),
                mandate_id=mandate.mandate_id,
                ran_at=now - timedelta(hours=6 - i),
                outcome=MandateRunOutcome.CANDIDATES,
                evaluated_ad_group_terms=10,
                distinct_search_terms=10,
                candidate_count=3,
                abstain_count=2,
                scope_filtered_out=0,
                set_id=uuid.uuid4(),
            )
        )
    # 必须走真实装配：配额是不是从截断列表算的，问题就出在 build_approval_app
    # 里那一句 runs_of 上，绕过它测等于没测。
    from ads_control_plane.identity.actor import (
        ActorContext,
        AuthenticationStrength,
        PrincipalType,
        Role,
    )
    from ads_control_plane.strategies.store import InMemoryCandidateSetStore, InMemoryMandateStore

    store = InMemoryMandateStore()
    store.save(mandate)
    verifier = InMemoryActorTokenVerifier()
    verifier.register(
        "t-owner",
        ActorContext(
            principal_id=uuid.uuid4(),
            principal_type=PrincipalType.HUMAN,
            organization_id=mandate.organization_id,
            roles=frozenset({Role.OPERATOR, Role.APPROVER}),
            human_person_id="owner-1",
            client_id="web-owner",
            session_id="s-owner",
            authentication_strength=AuthenticationStrength.MFA,
            #: 会话有效期锚在**真实墙钟**上，不是这个文件里那个固定的 now。
            #  ActorContext.is_expired() 默认拿 datetime.now(UTC) 比（identity/actor.py），
            #  而注入的 clock 只管域层判定。钉在固定日历日上，这个令牌就是一颗定时炸弹：
            #  过了那一刻，全绿的测试会在某次与代码无关的运行里突然 401。
            #  2026-09-07 真的炸过一次：test_mandate_scope_api 那条在 20:00 UTC 从绿变红，
            #  而 diff 里一个相关改动都没有——排查花掉的时间远超写对它的成本。
            issued_at=datetime.now(UTC) - timedelta(hours=1),
            expires_at=datetime.now(UTC) + timedelta(hours=8),
        ),
    )
    app = approval_api.build_approval_app(
        InMemoryCandidateSetStore(),
        verifier,
        clock=lambda: now,
        mandates=store,
        run_log=log,
    )
    listed = TestClient(app).get("/mandates", headers=bearer("t-owner")).json()
    summary = listed["mandates"][0]
    assert summary["runs_today"] == 6
    assert summary["runs_remaining_today"] == 0
    # 卡片仍然只列 5 条——截断是展示的事，不是配额的事。
    assert len(summary["recent_runs"]) == 5
    # 与服务端拒绝时用的那个数一致。
    assert log.count_on_day(mandate.mandate_id, now, mandate.quota_day) == 6


def test_an_actor_who_may_not_issue_cannot_use_the_endpoint_as_a_currency_probe() -> None:
    """不许在授权判定之前先泄露参数信息。

    币种不符的 422 文案点名该店真实结算币种（这是对的——签发的人必须知道该改成
    什么）。可它此前跑在授权判定**之前**：域层明令 AI 不能签发授权书，AI 却照样能
    拿这个端点当币种探针，一个 profile 一发地问出别家店以什么结算，全程 0 次
    成功签发、审计里只留下一串 422。

    判定顺序即信息泄露顺序：不该动手的人，连参数错在哪都不该知道。
    """
    from ads_control_plane.api.local_demo import DEMO_CODEX_TOKEN

    client = demo_client()
    # 先确认这个店确实有个真实币种、且探针用的是错的那个——否则这条测试是空的。
    known = client.get(
        f"/mandates/profile-currency?profile_external_id={PROFILE}",
        headers=bearer(DEMO_OWNER_TOKEN),
    ).json()["currency"]
    assert known == "USD", "前提变了：这条测试要的是「填错币种会被 422 点名」"

    res = issue(client, body(currency="EUR"), token=DEMO_CODEX_TOKEN)
    assert res.status_code == 403, "AI 不能签发，这一闸要先于任何参数校验"
    assert res.json()["detail"] == "AI_CANNOT_ISSUE_MANDATE"
    assert known not in res.text, "403 里仍然漏出了该店的真实结算币种"

    # 人签同一份错币种，仍要拿到点名真实币种的 422——修的是顺序，不是那句提示。
    human = issue(client, body(currency="EUR"), token=DEMO_OWNER_TOKEN)
    assert human.status_code == 422
    assert human.json()["detail"]["code"] == "CURRENCY_MISMATCH"
    assert known in human.json()["detail"]["message"]
