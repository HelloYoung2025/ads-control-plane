"""审批 API 测试：人批得成、AI 批不成、hash 必须对得上、跨组织不可探测。"""

import threading
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from ads_control_plane.api.approval_api import build_approval_app
from ads_control_plane.api.mcp_tools.server import InMemoryActorTokenVerifier
from ads_control_plane.canonical.entity import (
    AdProduct,
    CanonicalEntityRef,
    EntityType,
    ParentRefs,
    Provider,
)
from ads_control_plane.canonical.ids import new_canonical_id
from ads_control_plane.canonical.money import Money
from ads_control_plane.identity.actor import (
    ActorContext,
    AuthenticationStrength,
    PrincipalType,
    Role,
)
from ads_control_plane.strategies.mandate import (
    AutomationMandate,
    MandateBounds,
    MandateObjective,
    MandateState,
)
from ads_control_plane.strategies.mandate_run import MandateRunOutcome, MandateRunRecord
from ads_control_plane.strategies.negation import (
    CandidateSetState,
    NegationCandidateSet,
    NegationParameterPack,
    SearchTermRecord,
    generate_negation_candidates,
)
from ads_control_plane.strategies.store import (
    InMemoryCandidateSetStore,
    InMemoryMandateRunLog,
    InMemoryMandateStore,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
ORG = new_canonical_id()
OTHER_ORG = new_canonical_id()


def make_actor(principal_type: PrincipalType, org=ORG) -> ActorContext:
    real_now = datetime.now(UTC)
    kwargs = {"human_person_id": "bob"} if principal_type is PrincipalType.HUMAN else {}
    if principal_type is PrincipalType.AI_CLIENT:
        kwargs["human_initiator_person_id"] = "alice"
    return ActorContext(
        principal_id=new_canonical_id(),
        principal_type=principal_type,
        organization_id=org,
        roles=frozenset({Role.APPROVER}),
        client_id="client-1",
        session_id="s-1",
        authentication_strength=AuthenticationStrength.MFA,
        issued_at=real_now,
        expires_at=real_now + timedelta(hours=8),
        **kwargs,
    )


def frozen_set(org=ORG) -> NegationCandidateSet:
    record = SearchTermRecord(
        scope=CanonicalEntityRef(
            organization_id=org,
            provider=Provider.MOCK,
            provider_connection_id=new_canonical_id(),
            marketplace="US",
            shop_external_id="shop-1",
            profile_external_id="profile-A",
            ad_product=AdProduct.SP,
            entity_type=EntityType.AD_GROUP,
            entity_external_id="ag-1",
            parent_refs=ParentRefs(campaign_external_id="c-1"),
        ),
        search_term="cheap widget",
        clicks=40,
        conversions=0,
        spend=Money(amount="35.00", currency="USD"),
        window_start=NOW - timedelta(days=30),
        window_end=NOW - timedelta(days=1),
        data_as_of=NOW - timedelta(hours=2),
    )
    pack = NegationParameterPack(
        lookback_days=30,
        min_spend=Money(amount="20.00", currency="USD"),
        min_clicks=25,
        max_data_staleness_hours=24,
    )
    result = generate_negation_candidates([record], pack, NOW, new_canonical_id)
    return NegationCandidateSet(
        set_id=new_canonical_id(),
        organization_id=org,
        parameter_pack=pack,
        candidates=result.candidates,
        generated_at=NOW,
        created_by_client_id="codex-1",
        created_by_person_id=None,
        source="AI",
    ).freeze()


def named_frozen_set(org=ORG) -> NegationCandidateSet:
    """候选自带名称（来自搜索词报表行本身）的冻结集合。"""
    base = frozen_set(org)
    named = tuple(
        c.model_copy(
            update={"campaign_name": "候选自带的活动名", "ad_group_name": "候选自带的广告组名"}
        )
        for c in base.candidates
    )
    return base.model_copy(
        update={"state": CandidateSetState.GENERATED, "candidates": named}
    ).freeze()


def make_client(
    store: InMemoryCandidateSetStore,
    mandates: InMemoryMandateStore | None = None,
    run_log: InMemoryMandateRunLog | None = None,
) -> tuple[TestClient, str, str]:
    verifier = InMemoryActorTokenVerifier()
    verifier.register("human-token", make_actor(PrincipalType.HUMAN))
    verifier.register("ai-token", make_actor(PrincipalType.AI_CLIENT))
    app = build_approval_app(
        store,
        verifier,
        clock=lambda: NOW + timedelta(hours=1),
        mandates=mandates,
        run_log=run_log,
    )
    return TestClient(app), "human-token", "ai-token"


class TestApprovalApi:
    def test_missing_token_is_401(self) -> None:
        client, _, _ = make_client(InMemoryCandidateSetStore())
        assert client.get("/candidate-sets").status_code == 401

    def test_human_approves_with_correct_hash(self) -> None:
        store = InMemoryCandidateSetStore()
        cs = frozen_set()
        store.save(cs)
        client, human, _ = make_client(store)
        resp = client.post(
            f"/candidate-sets/{cs.set_id}/approve",
            json={"expected_hash": cs.set_hash},
            headers={"Authorization": f"Bearer {human}"},
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == "APPROVED"
        assert resp.json()["approved_by_person_id"] == "bob"

    def test_ai_token_cannot_approve(self) -> None:
        store = InMemoryCandidateSetStore()
        cs = frozen_set()
        store.save(cs)
        client, _, ai = make_client(store)
        resp = client.post(
            f"/candidate-sets/{cs.set_id}/approve",
            json={"expected_hash": cs.set_hash},
            headers={"Authorization": f"Bearer {ai}"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "AI_CANNOT_APPROVE"

    def test_wrong_hash_is_409(self) -> None:
        store = InMemoryCandidateSetStore()
        cs = frozen_set()
        store.save(cs)
        client, human, _ = make_client(store)
        resp = client.post(
            f"/candidate-sets/{cs.set_id}/approve",
            json={"expected_hash": "deadbeef"},
            headers={"Authorization": f"Bearer {human}"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "HASH_MISMATCH"

    def test_cross_org_set_is_404(self) -> None:
        store = InMemoryCandidateSetStore()
        cs = frozen_set(org=OTHER_ORG)
        store.save(cs)
        client, human, _ = make_client(store)
        resp = client.post(
            f"/candidate-sets/{cs.set_id}/approve",
            json={"expected_hash": cs.set_hash},
            headers={"Authorization": f"Bearer {human}"},
        )
        assert resp.status_code == 404

    def test_export_requires_approval_then_returns_csv(self) -> None:
        store = InMemoryCandidateSetStore()
        cs = frozen_set()
        store.save(cs)
        client, human, _ = make_client(store)
        headers = {"Authorization": f"Bearer {human}"}
        assert (
            client.get(f"/candidate-sets/{cs.set_id}/export.csv", headers=headers).status_code
            == 409
        )
        client.post(
            f"/candidate-sets/{cs.set_id}/approve",
            json={"expected_hash": cs.set_hash},
            headers=headers,
        )
        resp = client.get(f"/candidate-sets/{cs.set_id}/export.csv", headers=headers)
        assert resp.status_code == 200
        assert "cheap widget" in resp.text
        # UTF-8 BOM 开头：这张 CSV 的既定消费方是双击 Excel 的运营，无 BOM 时
        # 非 ASCII 搜索词全是乱码（2026-08-29 排查 approval-6）。
        assert resp.text.startswith("\ufeff")
        header = resp.text.splitlines()[0].lstrip("\ufeff")
        # 执行用的 CSV 必须说清是哪家店：AX-06 的父链此前在店铺这一层就断了，
        # 两家店导出的文件除文件名里的 uuid 外无从分辨。
        assert header.startswith("profile_external_id,shop_external_id,campaign_external_id")

    def test_reject_flow(self) -> None:
        store = InMemoryCandidateSetStore()
        cs = frozen_set()
        store.save(cs)
        client, human, _ = make_client(store)
        resp = client.post(
            f"/candidate-sets/{cs.set_id}/reject",
            headers={"Authorization": f"Bearer {human}"},
        )
        assert resp.status_code == 200
        assert resp.json()["state"] == "REJECTED"


class TestMandateApi:
    def make_mandate_client(self) -> tuple[TestClient, str, str]:
        from ads_control_plane.api.mcp_tools.server import InMemoryActorTokenVerifier
        from ads_control_plane.strategies.store import InMemoryMandateStore

        verifier = InMemoryActorTokenVerifier()
        verifier.register("human-token", make_actor(PrincipalType.HUMAN))
        verifier.register("ai-token", make_actor(PrincipalType.AI_CLIENT))
        app = build_approval_app(
            InMemoryCandidateSetStore(),
            verifier,
            clock=lambda: NOW,
            mandates=InMemoryMandateStore(),
        )
        return TestClient(app), "human-token", "ai-token"

    def _body(self) -> dict:
        return {
            "profile_external_id": "profile-A",
            "objective": "WASTED_SPEND_REMOVED",
            "statement": "压降 profile-A 无效搜索词花费",
            "lookback_days": 30,
            "min_spend_amount": "20.00",
            "currency": "USD",
            "min_clicks": 25,
            "max_data_staleness_hours": 24,
            "max_runs_per_day": 2,
            "max_candidates_per_run": 50,
            "valid_days": 14,
        }

    def test_human_issues_mandate_and_contract_is_echoed(self) -> None:
        client, human, _ = self.make_mandate_client()
        resp = client.post(
            "/mandates", json=self._body(), headers={"Authorization": f"Bearer {human}"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "ACTIVE"
        assert data["issued_by_person_id"] == "bob"
        assert data["parameter_pack"]["min_clicks"] == 25
        assert data["parameter_pack_hash"]

    def test_ai_cannot_issue_mandate(self) -> None:
        client, _, ai = self.make_mandate_client()
        resp = client.post(
            "/mandates", json=self._body(), headers={"Authorization": f"Bearer {ai}"}
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "AI_CANNOT_ISSUE_MANDATE"

    def test_out_of_whitelist_mandate_rejected(self) -> None:
        client, human, _ = self.make_mandate_client()
        body = self._body() | {"valid_days": 90}
        resp = client.post("/mandates", json=body, headers={"Authorization": f"Bearer {human}"})
        assert resp.status_code == 422

    def test_ten_minute_interval_rejected(self) -> None:
        client, human, _ = self.make_mandate_client()
        body = self._body() | {"run_interval_minutes": 10}
        resp = client.post("/mandates", json=body, headers={"Authorization": f"Bearer {human}"})
        assert resp.status_code == 422

    def test_unready_objective_rejected_with_reason(self) -> None:
        client, human, _ = self.make_mandate_client()
        body = self._body() | {"objective": "SALES_GROWTH", "statement": "推高销量"}
        resp = client.post("/mandates", json=body, headers={"Authorization": f"Bearer {human}"})
        assert resp.status_code == 403
        assert resp.json()["detail"] == "OBJECTIVE_NOT_READY"

    def test_revoke_mandate(self) -> None:
        client, human, _ = self.make_mandate_client()
        headers = {"Authorization": f"Bearer {human}"}
        created = client.post("/mandates", json=self._body(), headers=headers).json()
        resp = client.post(f"/mandates/{created['mandate_id']}/revoke", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["state"] == "REVOKED"
        assert resp.json()["revoked_by_person_id"] == "bob"


class TestAuditRegressions:
    """2026-08-29 排查修复的回归锚点（approval-3/5、ui-1、ui-3/mandate-2）。"""

    def _mandate_client(self) -> tuple[TestClient, str]:
        from ads_control_plane.strategies.store import InMemoryMandateStore

        verifier = InMemoryActorTokenVerifier()
        verifier.register("human-token", make_actor(PrincipalType.HUMAN))
        app = build_approval_app(
            InMemoryCandidateSetStore(),
            verifier,
            clock=lambda: NOW,
            mandates=InMemoryMandateStore(),
        )
        return TestClient(app), "human-token"

    def test_objectives_endpoint_reports_readiness(self) -> None:
        """ui-1：端点此前不存在，UI 永远走降级分支、3 个必拒目标照样可点。"""
        client, human = self._mandate_client()
        resp = client.get("/mandates/objectives", headers={"Authorization": f"Bearer {human}"})
        assert resp.status_code == 200
        rows = {r["objective"]: r for r in resp.json()["objectives"]}
        assert rows["WASTED_SPEND_REMOVED"]["ready"] is True
        assert rows["WASTED_SPEND_REMOVED"]["missing"] == []
        for kind in ("CLEARANCE_VELOCITY", "LAUNCH_RAMP", "SALES_GROWTH"):
            assert rows[kind]["ready"] is False
            assert rows[kind]["missing"], f"{kind} 未就绪必须列出缺什么"

    def test_objectives_endpoint_requires_auth(self) -> None:
        client, _ = self._mandate_client()
        assert client.get("/mandates/objectives").status_code == 401

    def test_scope_item_from_another_profile_is_rejected(self) -> None:
        """ui-3/mandate-2：勾选项自报店铺后，跨店铺签发在签发期被拒，
        而不是被静默改挂成一份永远 0 候选却显示 ACTIVE 的授权书。"""
        client, human = self._mandate_client()
        body = TestMandateApi()._body() | {
            "scope": {
                "kind": "OBJECTS",
                "items": [
                    {
                        "level": "campaign",
                        "external_id": "c-belongs-to-B",
                        "profile_external_id": "profile-B",
                    }
                ],
            }
        }
        resp = client.post("/mandates", json=body, headers={"Authorization": f"Bearer {human}"})
        assert resp.status_code == 403
        assert resp.json()["detail"] == "SCOPE_PROFILE_MISMATCH"

    def test_scope_item_without_profile_keeps_old_behavior(self) -> None:
        client, human = self._mandate_client()
        body = TestMandateApi()._body() | {
            "scope": {
                "kind": "OBJECTS",
                "items": [{"level": "campaign", "external_id": "c-1"}],
            }
        }
        resp = client.post("/mandates", json=body, headers={"Authorization": f"Bearer {human}"})
        assert resp.status_code == 200
        assert resp.json()["scope_summary"] == "1 个广告活动"

    def test_bad_state_filter_is_422_not_500(self) -> None:
        """approval-5：写错状态名此前 500 裸文本；大小写宽容后 frozen == FROZEN。"""
        store = InMemoryCandidateSetStore()
        store.save(frozen_set())
        client, human, _ = make_client(store)
        headers = {"Authorization": f"Bearer {human}"}
        bad = client.get("/candidate-sets", params={"state": "PENDING"}, headers=headers)
        assert bad.status_code == 422
        assert bad.json()["detail"] == "CANDIDATE_STATE_INVALID"
        ok = client.get("/candidate-sets", params={"state": "frozen"}, headers=headers)
        assert ok.status_code == 200
        assert len(ok.json()["candidate_sets"]) == 1

    def test_summary_reports_expiry_for_stale_frozen_sets(self) -> None:
        """approval-3：过期集合此前与能批的一模一样；现在 summary 自带 expired。"""
        store = InMemoryCandidateSetStore()
        store.save(frozen_set())
        verifier = InMemoryActorTokenVerifier()
        verifier.register("human-token", make_actor(PrincipalType.HUMAN))
        # 时钟拨到生成 73 小时后（TTL=72h）。
        app = build_approval_app(store, verifier, clock=lambda: NOW + timedelta(hours=73))
        client = TestClient(app)
        rows = client.get(
            "/candidate-sets", headers={"Authorization": "Bearer human-token"}
        ).json()["candidate_sets"]
        assert rows[0]["expired"] is True
        assert rows[0]["expires_at"] == (NOW + timedelta(hours=72)).isoformat()

    def test_summary_fresh_set_not_expired(self) -> None:
        store = InMemoryCandidateSetStore()
        store.save(frozen_set())
        client, human, _ = make_client(store)  # clock = NOW + 1h
        rows = client.get("/candidate-sets", headers={"Authorization": f"Bearer {human}"}).json()[
            "candidate_sets"
        ]
        assert rows[0]["expired"] is False


class TestNameResolution:
    """审计 #5/#9（2026-08-29）：批准与回溯不能对着 16 位数字进行。

    候选证据、导出 CSV、授权书作用域都从镜像现值解析名称；镜像缺名留 None/空，
    不编造；不传 snapshot_repo 的旧组装完全不受影响（全部 None）。
    """

    @staticmethod
    def _mirror_repo():
        from decimal import Decimal

        from ads_control_plane.mirror.repository import InMemorySnapshotRepository
        from ads_control_plane.mirror.snapshot import AdObjectSnapshot
        from ads_control_plane.tasks.directive import ObjectLevel

        repo = InMemorySnapshotRepository()
        base = dict(
            profile_id="profile-A",
            source_as_of=NOW,
            recorded_at=NOW,
            catalog_version="lx-openapi-mcp-v1",
            schema_version="ad_campaign_report-v1",
        )
        repo.append(
            AdObjectSnapshot(
                object_key="campaign:c-1",
                level=ObjectLevel.CAMPAIGN,
                name="夏季主推-精确",
                daily_budget=Decimal("10.00"),
                **base,
            )
        )
        repo.append(
            AdObjectSnapshot(
                object_key="ad_group:ag-1",
                level=ObjectLevel.AD_GROUP,
                name="Group-泳装",
                **base,
            )
        )
        return repo

    def _named_client(self) -> tuple[TestClient, str]:
        store = InMemoryCandidateSetStore()
        store.save(frozen_set())
        verifier = InMemoryActorTokenVerifier()
        verifier.register("human-token", make_actor(PrincipalType.HUMAN))
        app = build_approval_app(
            store,
            verifier,
            clock=lambda: NOW + timedelta(hours=1),
            snapshot_repo=self._mirror_repo(),
        )
        return TestClient(app), "human-token"

    def test_candidate_rows_carry_mirror_names(self) -> None:
        client, human = self._named_client()
        rows = client.get("/candidate-sets", headers={"Authorization": f"Bearer {human}"}).json()[
            "candidate_sets"
        ][0]["candidates"]
        assert rows[0]["campaign_name"] == "夏季主推-精确"
        assert rows[0]["ad_group_name"] == "Group-泳装"

    def test_candidate_rows_without_repo_have_none_names(self) -> None:
        store = InMemoryCandidateSetStore()
        store.save(frozen_set())
        client, human, _ = make_client(store)  # 未传 snapshot_repo
        rows = client.get("/candidate-sets", headers={"Authorization": f"Bearer {human}"}).json()[
            "candidate_sets"
        ][0]["candidates"]
        assert rows[0]["campaign_name"] is None
        assert rows[0]["ad_group_name"] is None

    def test_export_csv_appends_name_columns(self) -> None:
        client, human = self._named_client()
        sets = client.get("/candidate-sets", headers={"Authorization": f"Bearer {human}"}).json()[
            "candidate_sets"
        ]
        set_id, set_hash = sets[0]["set_id"], sets[0]["set_hash"]
        client.post(
            f"/candidate-sets/{set_id}/approve",
            json={"expected_hash": set_hash},
            headers={"Authorization": f"Bearer {human}"},
        )
        csv_text = client.get(
            f"/candidate-sets/{set_id}/export.csv", headers={"Authorization": f"Bearer {human}"}
        ).text
        header = csv_text.lstrip("﻿").splitlines()[0]
        assert header == (
            "profile_external_id,shop_external_id,"
            "campaign_external_id,ad_group_external_id,search_term,match_type,"
            "campaign_name,ad_group_name"
        )
        assert "夏季主推-精确" in csv_text
        assert "Group-泳装" in csv_text

    def test_candidate_carried_names_work_without_any_mirror(self) -> None:
        """镜像覆盖不到的候选照样有名字——名字随候选一起冻结。

        2026-08-30 实测：真实店 11,723 个活动，镜像默认只拉 300 个（2.5%），
        7 条候选 0 条能从镜像解析出名字。审计 #5 加这两列是为了免除
        「拿 15 位数字去后台逐行反查」，而在真实规模上它一次都没生效过。
        """
        store = InMemoryCandidateSetStore()
        store.save(named_frozen_set())
        client, human, _ = make_client(store)  # 故意不给 snapshot_repo
        rows = client.get("/candidate-sets", headers={"Authorization": f"Bearer {human}"}).json()[
            "candidate_sets"
        ][0]["candidates"]
        assert rows[0]["campaign_name"] == "候选自带的活动名"
        assert rows[0]["ad_group_name"] == "候选自带的广告组名"

    def test_carried_names_win_over_the_mirrors_other_moment(self) -> None:
        """镜像里的名字来自另一个时点，不得盖掉审批人实际看过的那个。"""
        store = InMemoryCandidateSetStore()
        store.save(named_frozen_set())
        verifier = InMemoryActorTokenVerifier()
        verifier.register("human-token", make_actor(PrincipalType.HUMAN))
        app = build_approval_app(
            store,
            verifier,
            clock=lambda: NOW + timedelta(hours=1),
            snapshot_repo=self._mirror_repo(),
        )
        rows = (
            TestClient(app)
            .get("/candidate-sets", headers={"Authorization": "Bearer human-token"})
            .json()["candidate_sets"][0]["candidates"]
        )
        assert rows[0]["campaign_name"] == "候选自带的活动名"

    def test_export_csv_uses_carried_names_without_a_mirror(self) -> None:
        store = InMemoryCandidateSetStore()
        cs = named_frozen_set()
        store.save(cs)
        client, human, _ = make_client(store)
        client.post(
            f"/candidate-sets/{cs.set_id}/approve",
            json={"expected_hash": cs.set_hash},
            headers={"Authorization": f"Bearer {human}"},
        )
        csv_text = client.get(
            f"/candidate-sets/{cs.set_id}/export.csv",
            headers={"Authorization": f"Bearer {human}"},
        ).text
        assert "候选自带的活动名" in csv_text
        assert "候选自带的广告组名" in csv_text

    def test_mandate_summary_returns_scope_items_with_names(self) -> None:
        from ads_control_plane.strategies.store import InMemoryMandateStore

        verifier = InMemoryActorTokenVerifier()
        verifier.register("human-token", make_actor(PrincipalType.HUMAN))
        app = build_approval_app(
            InMemoryCandidateSetStore(),
            verifier,
            clock=lambda: NOW + timedelta(hours=1),
            mandates=InMemoryMandateStore(),
            snapshot_repo=self._mirror_repo(),
        )
        client = TestClient(app)
        body = TestMandateApi()._body() | {
            "scope": {
                "kind": "OBJECTS",
                "items": [{"level": "campaign", "external_id": "c-1"}],
            }
        }
        created = client.post(
            "/mandates", json=body, headers={"Authorization": "Bearer human-token"}
        ).json()
        assert created["scope_items"] == [
            {"level": "CAMPAIGN", "external_id": "c-1", "name": "夏季主推-精确"}
        ]
        listed = client.get("/mandates", headers={"Authorization": "Bearer human-token"}).json()
        assert listed["mandates"][0]["scope_items"][0]["name"] == "夏季主推-精确"

    def test_mandate_summary_scope_items_none_for_profile_scope(self) -> None:
        from ads_control_plane.strategies.store import InMemoryMandateStore

        verifier = InMemoryActorTokenVerifier()
        verifier.register("human-token", make_actor(PrincipalType.HUMAN))
        app = build_approval_app(
            InMemoryCandidateSetStore(),
            verifier,
            clock=lambda: NOW + timedelta(hours=1),
            mandates=InMemoryMandateStore(),
        )
        client = TestClient(app)
        created = client.post(
            "/mandates",
            json=TestMandateApi()._body(),
            headers={"Authorization": "Bearer human-token"},
        ).json()
        assert created["scope_items"] is None  # 整店授权：清单语义不存在，不给空数组冒充


class TestSetProvenance:
    """集合必须自报出处：哪家店、来自哪份授权书、那份授权书现在什么状态。"""

    def _mandate(self) -> AutomationMandate:
        return AutomationMandate(
            mandate_id=new_canonical_id(),
            organization_id=ORG,
            profile_external_id="profile-A",
            objective=MandateObjective(
                objective="WASTED_SPEND_REMOVED", statement="压降 profile-A 无效搜索词花费"
            ),
            parameter_pack=NegationParameterPack(
                lookback_days=30,
                min_spend=Money(amount="20.00", currency="USD"),
                min_clicks=25,
                max_data_staleness_hours=24,
            ),
            bounds=MandateBounds(max_runs_per_day=2, max_candidates_per_run=50, valid_days=14),
            issued_at=NOW,
            expires_at=NOW + timedelta(days=14),
            issued_by_person_id="bob",
        )

    def test_summary_names_the_store(self) -> None:
        """两家店各有一份待批集合时，两张卡片此前除 uuid 前 8 位外逐字同形——
        候选数、生成时间、来源、广告组名（同一条产品线在两家店常常同名）。
        人挑一份批准、下载 CSV，没有一列告诉他该打开哪家店的后台。"""
        store = InMemoryCandidateSetStore()
        cs = frozen_set()
        store.save(cs)
        client, human, _ = make_client(store)
        body = client.get("/candidate-sets", headers={"Authorization": f"Bearer {human}"}).json()
        assert body["candidate_sets"][0]["profile_external_id"] == "profile-A"

    def test_set_from_a_revoked_mandate_cannot_be_approved(self) -> None:
        """撤销确认框跟人说的是「撤销后 AI 立即停止按它运行」。而这份授权今早
        生成的集合原样留在「待批」里，与好集合逐字同形、按钮照样可点——
        人照常批准、导出、执行的正是他刚判定为「签错了、要停掉」的那套参数。"""
        mandates = InMemoryMandateStore()
        mandate = self._mandate()
        mandates.save(mandate)
        store = InMemoryCandidateSetStore()
        cs = frozen_set().model_copy(update={"mandate_id": mandate.mandate_id})
        store.save(cs)
        client, human, _ = make_client(store, mandates)

        listed = client.get("/candidate-sets", headers={"Authorization": f"Bearer {human}"}).json()
        assert listed["candidate_sets"][0]["mandate_state"] == "ACTIVE"

        mandates.save(mandate.model_copy(update={"state": MandateState.REVOKED}))
        listed = client.get("/candidate-sets", headers={"Authorization": f"Bearer {human}"}).json()
        assert listed["candidate_sets"][0]["mandate_state"] == "REVOKED"

        resp = client.post(
            f"/candidate-sets/{cs.set_id}/approve",
            json={"expected_hash": cs.set_hash},
            headers={"Authorization": f"Bearer {human}"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "MANDATE_REVOKED"

    def test_truncation_reaches_the_person_who_signs(self) -> None:
        """签发表单承诺过「运行结果会告诉你截断前有多少个」（index.html:274），
        而那个人往往就是审批屏幕前的这个人。此前 truncated_from 只进 MCP 返回值。"""
        store = InMemoryCandidateSetStore()
        cs = frozen_set().model_copy(update={"truncated_from": 137})
        store.save(cs)
        client, human, _ = make_client(store)
        body = client.get("/candidate-sets", headers={"Authorization": f"Bearer {human}"}).json()
        assert body["candidate_sets"][0]["truncated_from"] == 137


def _regenerated(base: NegationCandidateSet) -> NegationCandidateSet:
    """同一段数据再生成一次：词与证据逐字相同，只有集合与候选的编号是新的。

    这正是真实通道上实测到的形态——第二次调用全部命中缓存，输入逐行相同。
    """
    return base.model_copy(
        update={
            "set_id": new_canonical_id(),
            "state": CandidateSetState.GENERATED,
            "set_hash": None,
            "candidates": tuple(
                c.model_copy(update={"candidate_id": new_canonical_id()}) for c in base.candidates
            ),
        }
    ).freeze()


class TestDuplicatePendingSets:
    """同一段数据被生成两次，两张卡片并排躺在待批里。

    2026-08-30 在真实通道上实测：连续两次即席生成（第二次全部命中缓存、输入逐行
    相同）产出两份 7 条候选的 FROZEN 集合，内容逐字相同而 set_hash 完全不同——
    每条候选的编号都进 set_hash，它按定义就该每次不同。而界面把 set_hash 叫
    「内容指纹」，于是这两张卡片主动教人读成两批不同的发现：人会以为有 14 个词
    要处理，或者两份都批，导出两份一样的 CSV。
    """

    def test_identical_content_gets_one_fingerprint_and_two_hashes(self) -> None:
        store = InMemoryCandidateSetStore()
        first = frozen_set()
        store.save(first)
        store.save(_regenerated(first))
        client, human, _ = make_client(store)
        rows = client.get("/candidate-sets", headers={"Authorization": f"Bearer {human}"}).json()[
            "candidate_sets"
        ]
        assert len(rows) == 2
        # 冻结指纹必须不同——它绑定审批，防的是内容被改。
        assert rows[0]["set_hash"] != rows[1]["set_hash"]
        # 内容指纹必须相同——回答的是「这两份是不是同一批发现」。
        assert rows[0]["content_fingerprint"] == rows[1]["content_fingerprint"]
        assert rows[0]["same_content_as"] == [rows[1]["set_id"]]
        assert rows[1]["same_content_as"] == [rows[0]["set_id"]]

    def test_the_same_batch_fetched_an_hour_later_is_still_the_same_batch(self) -> None:
        """重复识别不许依赖「两次取数时刻恰好相同」。

        2026-08-30 排查：真实源的 data_as_of 逐字取当次调用的 now，于是两次生成
        只有落在 900 秒缓存窗口内才碰巧相同——上面那条实测正是缓存命中做的，
        它看不到这一点。缓存一过期，同一批词、同一段窗口、同样的花费点击订单，
        只因为隔了一小时再查就被判成两批不同的发现，而 same_content_as 的 []
        明确表示「查过了，没有重复」。把假阴性说成查过的结论，人会去批第二遍。

        _regenerated 逐字复制 data_as_of，所以这条必须自己改动它才测得到。
        """
        store = InMemoryCandidateSetStore()
        first = frozen_set()
        later = _regenerated(first)
        an_hour = timedelta(hours=1)
        later = later.model_copy(
            update={
                "state": CandidateSetState.GENERATED,
                "set_hash": None,
                "candidates": tuple(
                    c.model_copy(
                        update={
                            "evidence": c.evidence.model_copy(
                                update={"data_as_of": c.evidence.data_as_of + an_hour}
                            )
                        }
                    )
                    for c in later.candidates
                ),
            }
        ).freeze()
        store.save(first)
        store.save(later)
        client, human, _ = make_client(store)
        rows = client.get("/candidate-sets", headers={"Authorization": f"Bearer {human}"}).json()[
            "candidate_sets"
        ]
        assert len(rows) == 2
        assert rows[0]["content_fingerprint"] == rows[1]["content_fingerprint"]
        assert rows[0]["same_content_as"] == [rows[1]["set_id"]]

    def test_a_different_window_is_a_different_batch(self) -> None:
        """反向必须成立：窗口不同就是两批不同的发现，不许被剔 data_as_of 顺手抹平。

        窗口两端仍在指纹里——它们回答「这批数据覆盖到哪几天」，那是发现本身的一部分。
        """
        store = InMemoryCandidateSetStore()
        first = frozen_set()
        other_window = _regenerated(first)
        a_day = timedelta(days=1)
        other_window = other_window.model_copy(
            update={
                "state": CandidateSetState.GENERATED,
                "set_hash": None,
                "candidates": tuple(
                    c.model_copy(
                        update={
                            "evidence": c.evidence.model_copy(
                                update={
                                    "window_start": c.evidence.window_start - a_day,
                                    "window_end": c.evidence.window_end - a_day,
                                }
                            )
                        }
                    )
                    for c in other_window.candidates
                ),
            }
        ).freeze()
        store.save(first)
        store.save(other_window)
        client, human, _ = make_client(store)
        rows = client.get("/candidate-sets", headers={"Authorization": f"Bearer {human}"}).json()[
            "candidate_sets"
        ]
        assert rows[0]["content_fingerprint"] != rows[1]["content_fingerprint"]
        assert rows[0]["same_content_as"] == []

    def test_a_lone_set_is_not_reported_as_duplicated(self) -> None:
        store = InMemoryCandidateSetStore()
        store.save(frozen_set())
        client, human, _ = make_client(store)
        row = client.get("/candidate-sets", headers={"Authorization": f"Bearer {human}"}).json()[
            "candidate_sets"
        ][0]
        assert row["same_content_as"] == []

    def test_a_single_set_response_says_it_did_not_look(self) -> None:
        """单份响应（批准/拒绝）看不到别的集合：那就说「没查」，不说「没有重复」。

        [] 与 null 在这里是两句不同的话。把「没查」渲染成「没有重复」，就是又一次
        把沉默说成好消息——与「取数为空 ≠ 查了没有浪费」同一条纪律。
        """
        store = InMemoryCandidateSetStore()
        one = frozen_set()
        store.save(one)
        store.save(_regenerated(one))  # 内容相同的第二份确实存在
        client, human, _ = make_client(store)
        body = client.post(
            f"/candidate-sets/{one.set_id}/approve",
            headers={"Authorization": f"Bearer {human}"},
            json={"expected_hash": one.set_hash},
        ).json()
        assert body["same_content_as"] is None

    def test_different_content_is_not_lumped_together(self) -> None:
        """证据数字不同就是两批不同的发现——同一个词、同一个窗口也不例外。

        这条此前用的是「改了广告组名」，理由写着「审批人看到的东西不同，就不是同一批」。
        那个理由不成立（2026-08-30 外审）：名字是从镜像现值解析出来的**显示用**文本，
        两次生成之间同步一次镜像就会变，而两批词、两批证据一模一样。改用真正不同的
        东西——花费——来钉这条边界。名字那一面由下一条测试反向钉住。
        """
        store = InMemoryCandidateSetStore()
        first = frozen_set()
        pricier = first.model_copy(
            update={
                "set_id": new_canonical_id(),
                "state": CandidateSetState.GENERATED,
                "set_hash": None,
                "candidates": tuple(
                    c.model_copy(
                        update={
                            "evidence": c.evidence.model_copy(
                                update={"spend": Money(amount="99.99", currency="USD")}
                            )
                        }
                    )
                    for c in first.candidates
                ),
            }
        ).freeze()
        store.save(first)
        store.save(pricier)
        client, human, _ = make_client(store)
        rows = client.get("/candidate-sets", headers={"Authorization": f"Bearer {human}"}).json()[
            "candidate_sets"
        ]
        assert rows[0]["content_fingerprint"] != rows[1]["content_fingerprint"]
        assert rows[0]["same_content_as"] == []
        assert rows[1]["same_content_as"] == []

    def test_a_renamed_ad_group_does_not_hide_a_duplicate(self) -> None:
        """改个名字藏不住重复。

        campaign_name / ad_group_name 是从镜像现值解析出来的显示文本。最平常的一种
        顺序就能让它们变：生成一次 → 同步镜像 → 再生成一次；第一次镜像里还没有名字
        （解析为 null），第二次有了。两批词、两批证据逐字相同，而 same_content_as
        会回 []——那句话的意思是「查过了，没有重复」，于是人去批第二遍。

        对象身份不在名字里而在 scope，它仍在指纹中，所以剔名字不会把不同对象混为一批
        （由上一条测试守着）。名字仍进 set_hash：审批绑定的是人看见的那一份（AX-07）。
        """
        store = InMemoryCandidateSetStore()
        first = frozen_set()
        renamed = (
            _regenerated(first)
            .model_copy(
                update={
                    "state": CandidateSetState.GENERATED,
                    "set_hash": None,
                    "candidates": tuple(
                        c.model_copy(
                            update={
                                "ad_group_name": "同步之后才有的名字",
                                "campaign_name": "新活动名",
                            }
                        )
                        for c in _regenerated(first).candidates
                    ),
                }
            )
            .freeze()
        )
        store.save(first)
        store.save(renamed)
        client, human, _ = make_client(store)
        rows = client.get("/candidate-sets", headers={"Authorization": f"Bearer {human}"}).json()[
            "candidate_sets"
        ]
        assert rows[0]["content_fingerprint"] == rows[1]["content_fingerprint"]
        assert rows[0]["same_content_as"] == [rows[1]["set_id"]]
        # 冻结指纹仍必须不同：它绑定审批，人看见的那一份里名字是不一样的。
        assert rows[0]["set_hash"] != rows[1]["set_hash"]


class TestMandateRunVisibility:
    """签发之后这份授权到底跑成了什么样——此前界面上一个字都没有。

    2026-08-30 排查 #23：币种签错、店铺没接数据源、作用域把对象全挡掉、整批数据
    太旧，四种「这份授权根本跑不通」与「一切正常、这段时间确实没有该否的词」在
    界面上逐字同形：徽章「生效中」，待批空空如也。人得到的唯一信号是「没有新东西
    要批」，读出来是好消息。
    """

    def _mandate(self) -> AutomationMandate:
        return AutomationMandate(
            mandate_id=new_canonical_id(),
            organization_id=ORG,
            profile_external_id="profile-A",
            objective=MandateObjective(
                objective="WASTED_SPEND_REMOVED", statement="压降 profile-A 无效搜索词花费"
            ),
            parameter_pack=NegationParameterPack(
                lookback_days=30,
                min_spend=Money(amount="20.00", currency="USD"),
                min_clicks=25,
                max_data_staleness_hours=24,
            ),
            bounds=MandateBounds(max_runs_per_day=2, max_candidates_per_run=50, valid_days=14),
            issued_at=NOW,
            expires_at=NOW + timedelta(days=14),
            issued_by_person_id="bob",
        )

    def _run(self, mandate_id, outcome: MandateRunOutcome, at, **extra) -> MandateRunRecord:
        base = {
            "run_id": new_canonical_id(),
            "mandate_id": mandate_id,
            "ran_at": at,
            "outcome": outcome,
            "evaluated_ad_group_terms": 0,
            "distinct_search_terms": 0,
            "candidate_count": 0,
            "abstain_count": 0,
            "scope_filtered_out": 0,
        }
        base.update(extra)
        return MandateRunRecord(**base)

    def test_never_run_says_so_instead_of_pretending_all_is_well(self) -> None:
        mandates = InMemoryMandateStore()
        mandate = self._mandate()
        mandates.save(mandate)
        client, human, _ = make_client(
            InMemoryCandidateSetStore(), mandates=mandates, run_log=InMemoryMandateRunLog()
        )
        body = client.get("/mandates", headers={"Authorization": f"Bearer {human}"}).json()
        row = body["mandates"][0]
        # 「从没跑过」与「跑过但没结果」是两件事，不能都渲染成一句「一切正常」。
        assert row["run_count_known"] is False
        assert row["last_run_at"] is None
        assert row["last_outcome"] is None
        assert row["needs_attention"] is False
        assert row["recent_runs"] == []

    def test_currency_mismatch_run_surfaces_on_the_mandate(self) -> None:
        mandates = InMemoryMandateStore()
        mandate = self._mandate()
        mandates.save(mandate)
        run_log = InMemoryMandateRunLog()
        run_log.record(
            self._run(
                mandate.mandate_id,
                MandateRunOutcome.REJECTED,
                NOW + timedelta(minutes=5),
                error_code="CURRENCY_MISMATCH",
            )
        )
        client, human, _ = make_client(
            InMemoryCandidateSetStore(), mandates=mandates, run_log=run_log
        )
        row = client.get("/mandates", headers={"Authorization": f"Bearer {human}"}).json()[
            "mandates"
        ][0]
        assert row["state"] == "ACTIVE"  # 徽章仍是「生效中」——这是真话
        assert row["last_outcome"] == "REJECTED"  # 但旁边现在还有这一句
        assert row["last_error_code"] == "CURRENCY_MISMATCH"
        assert row["needs_attention"] is True

    def test_a_later_healthy_run_clears_the_flag(self) -> None:
        """昨天报错、今天跑通了的授权不该继续挂着红灯——只看最近一次。"""
        mandates = InMemoryMandateStore()
        mandate = self._mandate()
        mandates.save(mandate)
        run_log = InMemoryMandateRunLog()
        run_log.record(
            self._run(
                mandate.mandate_id,
                MandateRunOutcome.SOURCE_ERROR,
                NOW,
                error_code="LX_TIMEOUT",
            )
        )
        run_log.record(
            self._run(
                mandate.mandate_id,
                MandateRunOutcome.NO_CANDIDATES,
                NOW + timedelta(hours=2),
                evaluated_ad_group_terms=40,
                distinct_search_terms=31,
            )
        )
        client, human, _ = make_client(
            InMemoryCandidateSetStore(), mandates=mandates, run_log=run_log
        )
        row = client.get("/mandates", headers={"Authorization": f"Bearer {human}"}).json()[
            "mandates"
        ][0]
        assert row["last_outcome"] == "NO_CANDIDATES"
        assert row["needs_attention"] is False
        # 历史仍在，新的在前：人要能看出「是一直这样还是刚开始这样」。
        assert [r["outcome"] for r in row["recent_runs"]] == ["NO_CANDIDATES", "SOURCE_ERROR"]

    def test_a_run_that_judged_only_part_of_the_store_is_not_a_green_light(self) -> None:
        """「没有该否的词」是绿灯，「在我看得懂的那部分里没有」不是。

        2026-08-30 排查 #8/#16：源侧有一批行读不出来时，那些 (广告组, 词) 这一轮
        根本没被判断过，而结局仍是 NO_CANDIDATES——最近一次运行「正常」，红灯不亮，
        人看到的是「批完了，没别的了」。少提的那些候选人无从察觉：卡片上其余
        每个数字都正常，被丢掉的组连一个占位都没有。
        """
        mandates = InMemoryMandateStore()
        mandate = self._mandate()
        mandates.save(mandate)
        run_log = InMemoryMandateRunLog()
        run_log.record(
            self._run(
                mandate.mandate_id,
                MandateRunOutcome.NO_CANDIDATES,
                NOW + timedelta(hours=1),
                evaluated_ad_group_terms=118,
                distinct_search_terms=97,
                unjudged_ad_group_terms=12,
            )
        )
        client, human, _ = make_client(
            InMemoryCandidateSetStore(), mandates=mandates, run_log=run_log
        )
        row = client.get("/mandates", headers={"Authorization": f"Bearer {human}"}).json()[
            "mandates"
        ][0]
        assert row["last_outcome"] == "NO_CANDIDATES"  # 对读得懂的那部分，这是真话
        assert row["needs_attention"] is True  # 但它不是绿灯
        assert row["recent_runs"][0]["unjudged_ad_group_terms"] == 12

    def test_a_fully_judged_empty_run_stays_a_green_light(self) -> None:
        """反过来也必须成立：什么都没丢的「没有该否的词」不该挂红灯。

        否则「有东西没被判断」这句话会常驻在每一份授权上，人两天后就不再看它——
        一个永远亮着的警示灯与没有警示灯等价。
        """
        mandates = InMemoryMandateStore()
        mandate = self._mandate()
        mandates.save(mandate)
        run_log = InMemoryMandateRunLog()
        run_log.record(
            self._run(
                mandate.mandate_id,
                MandateRunOutcome.NO_CANDIDATES,
                NOW + timedelta(hours=1),
                evaluated_ad_group_terms=118,
                distinct_search_terms=97,
            )
        )
        client, human, _ = make_client(
            InMemoryCandidateSetStore(), mandates=mandates, run_log=run_log
        )
        row = client.get("/mandates", headers={"Authorization": f"Bearer {human}"}).json()[
            "mandates"
        ][0]
        assert row["needs_attention"] is False
        assert row["recent_runs"][0]["unjudged_ad_group_terms"] == 0

    def test_no_run_log_wired_reports_unknown_not_healthy(self) -> None:
        """没接流水时说「不知道」，不说「一切正常」——空是真话，编不是。"""
        mandates = InMemoryMandateStore()
        mandates.save(self._mandate())
        client, human, _ = make_client(InMemoryCandidateSetStore(), mandates=mandates)
        row = client.get("/mandates", headers={"Authorization": f"Bearer {human}"}).json()[
            "mandates"
        ][0]
        assert row["run_count_known"] is False
        assert row["last_outcome"] is None


def test_a_run_that_found_nothing_but_could_not_judge_everything_is_not_called_clean() -> None:
    """空手而归 + 有弃权，不许静悄悄地过去。

    NO_CANDIDATES 的界面文案是「不用做什么，这段窗口确实干净」。同一轮里可能正躺着
    这个店最大的一笔零转化花费，只是那行数据太旧、判不了——判不了不是干净。
    全量弃权有 ALL_ABSTAINED 兜着，部分弃权此前一路落到这里且 needs_attention 为 false。
    """
    import uuid
    from decimal import Decimal

    from ads_control_plane.api.approval_api import _mandate_summary
    from ads_control_plane.strategies.mandate_run import MandateRunOutcome, MandateRunRecord
    from ads_control_plane.strategies.negation import NegationParameterPack

    now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    mandate = AutomationMandate(
        mandate_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        profile_external_id="profile-A",
        objective=MandateObjective(objective="WASTED_SPEND_REMOVED", statement="x"),
        parameter_pack=NegationParameterPack(
            lookback_days=30,
            min_spend=Money(amount=Decimal("50.00"), currency="USD"),
            min_clicks=25,
            max_data_staleness_hours=24,
        ),
        bounds=MandateBounds(
            max_runs_per_day=1,
            max_candidates_per_run=50,
            valid_days=7,
            run_interval_minutes=1440,
        ),
        issued_by_person_id="owner-1",
        issued_at=now - timedelta(hours=2),
        expires_at=now + timedelta(days=7),
    )

    def run(abstains: int) -> MandateRunRecord:
        return MandateRunRecord(
            run_id=uuid.uuid4(),
            mandate_id=mandate.mandate_id,
            ran_at=now - timedelta(minutes=5),
            outcome=MandateRunOutcome.NO_CANDIDATES,
            evaluated_ad_group_terms=10,
            distinct_search_terms=10,
            candidate_count=0,
            abstain_count=abstains,
            scope_filtered_out=0,
        )

    with_abstains = _mandate_summary(mandate, lambda *_: None, lambda _m: (run(1),), now)
    assert with_abstains["needs_attention"] is True
    assert with_abstains["last_abstain_count"] == 1
    # 真的一条都没弃权时才是「确实干净」，那时不该催人。
    clean = _mandate_summary(mandate, lambda *_: None, lambda _m: (run(0),), now)
    assert clean["needs_attention"] is False


def test_two_people_deciding_at_once_cannot_both_win() -> None:
    """同一批候选被两个人同时批准和拒绝时，只许一个人的意思表示落库。

    批准与拒绝都是「读一份 → 域层算终态 → 写回」，store 的锁只护住单次 get 和
    单次 save。两个请求都在对方写回之前读到 FROZEN 时，两边都算得出各自的终态、
    都写成功，后写的赢——而两个 HTTP 都是 200：点「拒绝」的人收到「已拒绝」，
    集合却落在 APPROVED，出现在「已批」页签里等人导出 CSV 拿去真店执行
    （2026-09-07 实测：修复前 approve 与 reject 双 200、落库 APPROVED）。

    钉的是不变量而不是谁赢——谁先写回取决于线程调度：两个响应必须是一个 200
    一个 409，且落库状态必须等于那个 200 说的状态。
    """
    store = InMemoryCandidateSetStore()
    cs = frozen_set()
    store.save(cs)
    client, human, _ = make_client(store)
    app = client.app

    # 卡在「都读完、都还没写」这个窗口上——不卡就复现不出来，两个请求会自然错开。
    both_have_read = threading.Barrier(2, timeout=10)
    real_get = store.get

    def gated_get(set_id: uuid.UUID) -> NegationCandidateSet:
        found = real_get(set_id)
        both_have_read.wait()
        return found

    store.get = gated_get  # type: ignore[method-assign]
    outcomes: dict[str, tuple[int, str]] = {}

    def decide(name: str, path: str, payload: dict[str, object] | None) -> None:
        resp = TestClient(app).post(
            f"/candidate-sets/{cs.set_id}/{path}",
            json=payload,
            headers={"Authorization": f"Bearer {human}"},
        )
        body = resp.json()
        outcomes[name] = (resp.status_code, body.get("state") or body.get("detail"))

    hash_body = {"expected_hash": cs.set_hash}
    threads = [
        threading.Thread(target=decide, args=("approve", "approve", hash_body)),
        threading.Thread(target=decide, args=("reject", "reject", None)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    store.get = real_get  # type: ignore[method-assign]

    codes = sorted(code for code, _ in outcomes.values())
    assert codes == [200, 409], f"两个人同时判，必须只有一个成功：{outcomes}"
    winner = next(state for code, state in outcomes.values() if code == 200)
    loser = next(state for code, state in outcomes.values() if code == 409)
    assert store.get(cs.set_id).state.value == winner, (
        f"落库的不是那个收到 200 的决定：{outcomes} → {store.get(cs.set_id).state}"
    )
    # 输的那个要拿到界面词典里翻得出的码，而不是一句 200 的假话。
    assert loser == "NOT_FROZEN"
