"""V2 系统工程审查锚点（2026-08-28）——安全公理与合同一致性的回归钉子。

对应审查项：
1. 防写结构：不可编码参数与白名单外工具一样，在任何网络调用之前带码拒绝；
   SyncEngine 的 toolId 全部来自常量，且落在适配器只读白名单之内。
2. 安全公理：/sync 对一切非人身份 403 HUMAN_REQUIRED（SERVICE_ACCOUNT 含，
   且是第一道检查——env 齐备也不进入同步逻辑）；ExitGuard/StrategyBundle
   源码中不存在 mandate.revoke 调用路径（tripwire）。
3. 合同一致性：to_selectors 在出口处二次上限检查（pydantic model_copy 绕过
   构造校验也拦得住）；快照 metrics 与调用方 dict 隔离（append-only 历史
   不被别名改写）；空白 mandate id 是空引用，显式拒绝。
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ads_control_plane.strategies.bundle as bundle_module
import ads_control_plane.strategies.exit_guard as exit_guard_module
from ads_control_plane.adapters.lx_read import (
    READ_TOOL_ALLOWLIST,
    LxMcpReadClient,
    LxReadError,
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
from ads_control_plane.mirror.sync import SYNC_TOOL_LEVELS
from ads_control_plane.strategies.bundle import BundleError, StrategyBundle
from ads_control_plane.strategies.exit_guard import ExitPolicy
from ads_control_plane.tasks.directive import MAX_AFFECTED_OBJECTS, ObjectLevel
from ads_control_plane.tasks.selection import SelectedObject, SelectionError, SelectionSet

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def _sel(level: ObjectLevel, external_id: str, profile: str = "profile-A") -> SelectedObject:
    return SelectedObject(level=level, external_id=external_id, profile_external_id=profile)


# ---------------------------------------------------- 合同一致性：出口处二次上限


class TestSelectionExitRecheck:
    def test_model_copy_bypass_is_caught_at_to_selectors(self) -> None:
        """pydantic model_copy(update=) 不重跑校验器——出口处的第二道锁必须拦住。"""
        base = SelectionSet(items=(_sel(ObjectLevel.TARGET, "t-0"),))
        oversized = tuple(
            _sel(ObjectLevel.TARGET, f"t-{i}") for i in range(MAX_AFFECTED_OBJECTS + 1)
        )
        smuggled = base.model_copy(update={"items": oversized})
        with pytest.raises(SelectionError) as e:
            smuggled.to_selectors()
        assert e.value.code == "SELECTION_TOO_BROAD"

    def test_cap_holds_across_level_grouping_with_duplicates(self) -> None:
        """三层分组后各 selector 显式 ID 之和 == 去重总量 ≤ 200，无第二种去重口径。"""
        items = [_sel(ObjectLevel.CAMPAIGN, f"c-{i}") for i in range(100)]
        items += [_sel(ObjectLevel.AD_GROUP, f"g-{i}") for i in range(60)]
        items += [_sel(ObjectLevel.TARGET, f"t-{i}") for i in range(40)]
        items += items[:25]  # 重复条目不计入去重总量
        selectors = SelectionSet(items=tuple(items)).to_selectors()
        assert sum(len(s.external_ids) for s in selectors) == MAX_AFFECTED_OBJECTS
        assert all(s.external_ids and not s.name_contains for s in selectors)


# ---------------------------------------------------- 合同一致性：append-only 数据层


class TestSnapshotMetricsIsolation:
    def test_caller_dict_mutation_cannot_rewrite_history(self) -> None:
        source = {"spends": "1.00"}
        snap = AdObjectSnapshot(
            object_key="campaign:c-1",
            level=ObjectLevel.CAMPAIGN,
            profile_id="profile-A",
            metrics=source,
            source_as_of=NOW,
            recorded_at=NOW,
            catalog_version="cat-v",
            schema_version="sch-v",
        )
        source["spends"] = "999.99"
        source["injected"] = "x"
        assert dict(snap.metrics) == {"spends": "1.00"}
        with pytest.raises(TypeError):
            snap.metrics["spends"] = "999.99"  # type: ignore[index]


# ---------------------------------------------------- 防写结构：网络前带码拒绝


class _RecordingClient(LxMcpReadClient):
    """截获网络层唯一入口；calls 非空即意味着发生了「网络」调用。"""

    def __init__(self) -> None:
        super().__init__("http://lx.invalid/mcp", "sk-test", min_interval_seconds=0)
        self.calls: list[dict[str, str]] = []

    def _perform_call(self, envelope: dict[str, str]) -> dict[str, object]:  # type: ignore[override]
        self.calls.append(dict(envelope))
        return {"code": 0, "message": "ok", "data": {"code": 0, "recordsFiltered": 0, "data": []}}


class TestAdapterPreNetworkRejection:
    def test_non_encodable_param_is_coded_and_pre_network(self) -> None:
        client = _RecordingClient()
        with pytest.raises(LxReadError) as e:
            client.fetch_page("ad_campaign_report", {"budget": Decimal("1.0")})
        assert e.value.code == "LX_PARAM_NOT_ENCODABLE"
        assert client.calls == []  # 编码失败发生在任何网络调用之前

    def test_sync_tool_constants_all_inside_read_allowlist(self) -> None:
        """SyncEngine 的 toolId 全集来自常量表，且必须是适配器只读白名单的子集。"""
        sync_tools = {tool_id for tool_id, _ in SYNC_TOOL_LEVELS}
        assert sync_tools <= READ_TOOL_ALLOWLIST
        assert not any(t.startswith(("put_", "post_")) for t in sync_tools)


# ---------------------------------------------------- 安全公理：无 revoke 路径 / 空引用


class TestNoRevokePath:
    def test_exit_guard_and_bundle_never_touch_mandate_revoke(self) -> None:
        for module in (exit_guard_module, bundle_module):
            source = inspect.getsource(module)
            assert ".revoke(" not in source
            assert "from ads_control_plane.strategies.mandate" not in source


class TestBundleReferences:
    def test_blank_mandate_id_rejected(self) -> None:
        with pytest.raises(BundleError) as e:
            StrategyBundle(
                bundle_id=new_canonical_id(),
                name="cleanup",
                profile_external_id="profile-A",
                mandate_ids=("  ",),
                exit_policy=ExitPolicy(),
                created_by="boss",
                created_at=NOW,
            )
        assert e.value.code == "BUNDLE_NO_MANDATES"


# ---------------------------------------------------- 安全公理：/sync 人别闸最先生效


def _workbench_client() -> TestClient:
    """注册 SERVICE_ACCOUNT 与 HUMAN 两个 token 的最小工作台应用。"""
    verifier = InMemoryActorTokenVerifier()
    real_now = datetime.now(UTC)
    org = new_canonical_id()
    base: dict[str, object] = {
        "organization_id": org,
        "issued_at": real_now,
        "expires_at": real_now + timedelta(hours=1),
    }
    verifier.register(
        "svc-token",
        ActorContext(
            principal_id=new_canonical_id(),
            principal_type=PrincipalType.SERVICE_ACCOUNT,
            roles=frozenset({Role.OPERATOR}),
            client_id="svc-1",
            session_id="s-svc",
            authentication_strength=AuthenticationStrength.SERVICE_CREDENTIAL,
            **base,  # type: ignore[arg-type]
        ),
    )
    verifier.register(
        "human-token",
        ActorContext(
            principal_id=new_canonical_id(),
            principal_type=PrincipalType.HUMAN,
            roles=frozenset({Role.OPERATOR}),
            human_person_id="ops-1",
            client_id="web-ops",
            session_id="s-ops",
            authentication_strength=AuthenticationStrength.MFA,
            **base,  # type: ignore[arg-type]
        ),
    )
    app = FastAPI()
    app.include_router(build_workbench_router(InMemorySnapshotRepository(), verifier))
    return TestClient(app)


class TestSyncHumanGate:
    def test_service_account_gets_403_human_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """人别闸是第一道检查：env 齐备（key/url/白名单都在）也不进入同步逻辑。"""
        monkeypatch.setenv("LX_MCP_KEY", "k-test")
        monkeypatch.setenv("LX_MCP_URL", "https://lx.example/mcp")
        monkeypatch.setenv("ADS_CP_SYNC_PROFILES", "profile-A")
        client = _workbench_client()
        res = client.post(
            "/api/workbench/sync",
            headers={"Authorization": "Bearer svc-token"},
            json={"profile_id": "profile-A"},
        )
        assert res.status_code == 403
        assert res.json()["detail"] == "HUMAN_REQUIRED"

    def test_url_absent_is_409_fail_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """key 在而 url 缺：显式 409 LX_URL_ABSENT，不得静默或落到别的分支。"""
        monkeypatch.setenv("LX_MCP_KEY", "k-test")
        monkeypatch.delenv("LX_MCP_URL", raising=False)
        monkeypatch.setenv("ADS_CP_SYNC_PROFILES", "profile-A")
        client = _workbench_client()
        res = client.post(
            "/api/workbench/sync",
            headers={"Authorization": "Bearer human-token"},
            json={"profile_id": "profile-A"},
        )
        assert res.status_code == 409
        assert res.json()["detail"] == "LX_URL_ABSENT"
