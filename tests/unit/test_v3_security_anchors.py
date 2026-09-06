"""V3 系统工程审查锚点（2026-08-29）——"UX 简化有没有偷偷弱化安全内核"的回归钉子。

审查前提：本轮为落 Owner 反馈做了三件会碰到安全内核的事——演示身份合二为一、
授权书新增作用域、授权书新增运行时段。这三件事各自都有一种"看起来做了、其实
没约束"的失败形态，本文件逐条把它钉死：

1. 身份合并 ≠ 取消职责分离。合并后 AI 仍不能批准 / 否决 / 签发 / 撤销 / 触发同步；
   且这些断言不是恒真的——同一批闸对人身份放行（否则"AI 被拒"只是因为整条路
   都是死的）。
2. 作用域必须真的约束行为。授权书写着"只管这个活动"，运行期就不能在别的活动
   里生成候选——这条曾经**只存不用**，是静默扩权。
3. 运行时段闸必须长在真正的运行入口上（不是只在构造期校验过），且用授权书
   自己的时区判定，不用服务器本地时区。
4. 新参数面不得成为注入面：排序字段是真白名单，时区串只经 zoneinfo 校验后使用。
5. fail-closed 完备：每条新增拒绝都带码，没有一条靠裸异常或静默放行。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ads_control_plane.api.approval_api import build_approval_app
from ads_control_plane.api.mcp_tools.server import InMemoryActorTokenVerifier
from ads_control_plane.api.mcp_tools.service import ToolDenied
from ads_control_plane.api.mcp_tools.strategy_service import StrategyToolService
from ads_control_plane.api.workbench_api import _SORT_FIELDS, build_workbench_router
from ads_control_plane.authorization.model import Action, ClientType, Environment, Grant
from ads_control_plane.authorization.sod import SoDViolation
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
from ads_control_plane.mirror.repository import InMemorySnapshotRepository
from ads_control_plane.providers.mock.search_terms import MockSearchTermSource
from ads_control_plane.strategies.mandate import (
    MandateBounds,
    MandateObjective,
    MandateScope,
    MandateScopeKind,
    MandateViolation,
    RunWindow,
    assert_run_authorized,
    issue_mandate,
)
from ads_control_plane.strategies.negation import (
    NegationCandidateSet,
    NegationParameterPack,
    SearchTermRecord,
    generate_negation_candidates,
)
from ads_control_plane.strategies.store import InMemoryCandidateSetStore, InMemoryMandateStore
from ads_control_plane.tasks.selection import SelectedObject, SelectionSet

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
ORG = new_canonical_id()
PROFILE = "profile-A"
KL = "Asia/Kuala_Lumpur"


def actor(principal_type: PrincipalType, *, org=ORG, person: str = "owner-1") -> ActorContext:
    real_now = datetime.now(UTC)
    extra = (
        {"human_person_id": person}
        if principal_type is PrincipalType.HUMAN
        else {"human_initiator_person_id": person}
    )
    return ActorContext(
        principal_id=new_canonical_id(),
        principal_type=principal_type,
        organization_id=org,
        # 合二为一后的人身份：OPERATOR ∪ APPROVER（与 local_demo 的 owner 同构）。
        roles=frozenset({Role.OPERATOR, Role.APPROVER})
        if principal_type is PrincipalType.HUMAN
        else frozenset({Role.ANALYST}),
        client_id="c-1",
        session_id="s-1",
        authentication_strength=AuthenticationStrength.MFA,
        issued_at=real_now,
        expires_at=real_now + timedelta(hours=8),
        **extra,
    )


def record(term: str, *, ad_group: str, campaign: str, spend: str = "35.00") -> SearchTermRecord:
    """一条必然入选的搜索词绩效（零转化 + 高点击 + 高花费 + 新鲜）。"""
    return SearchTermRecord(
        scope=CanonicalEntityRef(
            organization_id=ORG,
            provider=Provider.MOCK,
            provider_connection_id=new_canonical_id(),
            marketplace="US",
            shop_external_id="shop-1",
            profile_external_id=PROFILE,
            ad_product=AdProduct.SP,
            entity_type=EntityType.AD_GROUP,
            entity_external_id=ad_group,
            parent_refs=ParentRefs(campaign_external_id=campaign),
        ),
        search_term=term,
        clicks=40,
        conversions=0,
        spend=Money(amount=spend, currency="USD"),
        window_start=NOW - timedelta(days=30),
        window_end=NOW - timedelta(days=1),
        data_as_of=NOW - timedelta(hours=2),
    )


def pack() -> NegationParameterPack:
    return NegationParameterPack(
        lookback_days=30,
        min_spend=Money(amount="20.00", currency="USD"),
        min_clicks=25,
        max_data_staleness_hours=24,
    )


def make_mandate(**overrides):
    kwargs = {
        "mandate_id": new_canonical_id(),
        "profile_external_id": PROFILE,
        "objective": MandateObjective(
            objective="WASTED_SPEND_REMOVED", statement="压降无效搜索词花费"
        ),
        "parameter_pack": pack(),
        "bounds": MandateBounds(max_runs_per_day=5, max_candidates_per_run=50, valid_days=14),
        "now": NOW,
    }
    kwargs.update(overrides)
    return issue_mandate(actor(PrincipalType.HUMAN), **kwargs)


def objects_scope(*campaign_ids: str) -> MandateScope:
    return MandateScope(
        kind=MandateScopeKind.OBJECTS,
        selection=SelectionSet(
            items=tuple(
                SelectedObject(level="CAMPAIGN", external_id=cid, profile_external_id=PROFILE)
                for cid in campaign_ids
            )
        ),
    )


def make_service(
    records: list[SearchTermRecord],
) -> tuple[StrategyToolService, InMemoryCandidateSetStore, InMemoryMandateStore]:
    source = MockSearchTermSource()
    source.seed(PROFILE, records)
    store = InMemoryCandidateSetStore()
    mandates = InMemoryMandateStore()
    service = StrategyToolService(
        environment=Environment.STAGING,
        grants=[
            Grant(
                grant_id=new_canonical_id(),
                organization_id=ORG,
                environments=frozenset({Environment.STAGING}),
                actions=frozenset({Action.PROPOSAL_CREATE_DRAFT, Action.RESOURCE_READ}),
                client_types=frozenset({ClientType.MCP_AI}),
            )
        ],
        denies=[],
        search_terms=source,
        store=store,
        mandates=mandates,
        clock=lambda: NOW,
    )
    return service, store, mandates


# ------------------------------------------------- 审查项 2：作用域是否真的约束了行为


class TestMandateScopeIsEnforcedAtRun:
    """作用域曾经只存不用：授权书写着"只管 c-1"，运行期照样全店生成候选。

    那是最坏的一种失效——授权书状态 ACTIVE、摘要写着「1 个广告活动」、界面上
    看不出任何异常，而系统实际按整店在跑。下面几条是那个缺口的回归钉子。
    """

    def test_out_of_scope_campaign_produces_no_candidate(self) -> None:
        service, _, mandates = self.setup_two_campaigns()
        mandate = make_mandate(scope=objects_scope("c-1"))
        mandates.save(mandate)
        result = service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id=PROFILE,
            mandate_id=str(mandate.mandate_id),
        )
        assert result["candidate_count"] == 1
        assert [c["ad_group_external_id"] for c in result["candidates"]] == ["ag-1"]
        # 被挡掉的那条要能被数出来：静默过滤与静默放行一样让人看不见真相。
        assert result["scope_filtered_out"] == 1

    def test_scope_filter_precedes_evaluation_not_just_display(self) -> None:
        """过滤发生在评估之前：evaluated_ad_group_terms 必须只数作用域内的输入。

        若只在最后裁候选，报告会说"评估了 2 条词"却只对 1 条负责——人核对时
        无从判断这份授权到底看过什么。
        """
        service, _, mandates = self.setup_two_campaigns()
        mandate = make_mandate(scope=objects_scope("c-1"))
        mandates.save(mandate)
        result = service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id=PROFILE,
            mandate_id=str(mandate.mandate_id),
        )
        assert result["evaluated_ad_group_terms"] == 1

    def test_scope_matching_nothing_yields_empty_set_not_whole_store(self) -> None:
        """作用域一个对象都没命中 → 空手而归，而不是回退成整店。

        fail-closed 的方向在这里是"什么也不做"；回退到整店才是灾难。
        """
        service, store, mandates = self.setup_two_campaigns()
        mandate = make_mandate(scope=objects_scope("c-does-not-exist"))
        mandates.save(mandate)
        result = service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id=PROFILE,
            mandate_id=str(mandate.mandate_id),
        )
        assert result["candidate_count"] == 0
        assert result["set_id"] is None
        assert result["scope_filtered_out"] == 2
        assert store.list_by_state(ORG) == ()

    def test_profile_scope_still_covers_whole_store(self) -> None:
        """整店作用域（显式 PROFILE）不因新增过滤而收窄。"""
        service, _, mandates = self.setup_two_campaigns()
        mandate = make_mandate(scope=MandateScope(kind=MandateScopeKind.PROFILE))
        mandates.save(mandate)
        result = service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id=PROFILE,
            mandate_id=str(mandate.mandate_id),
        )
        assert result["candidate_count"] == 2
        assert result["scope_filtered_out"] == 0

    def test_legacy_mandate_without_scope_is_unchanged(self) -> None:
        """既有授权书（scope=None）行为逐字不变——向后兼容不是靠人记得住。"""
        service, _, mandates = self.setup_two_campaigns()
        mandate = make_mandate()
        assert mandate.scope is None
        mandates.save(mandate)
        result = service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id=PROFILE,
            mandate_id=str(mandate.mandate_id),
        )
        assert result["candidate_count"] == 2
        assert result["scope_filtered_out"] == 0

    def test_ad_hoc_run_reports_scope_filtered_out_as_none(self) -> None:
        """即席模式没有作用域可言 → None，不是 0。0 会假装"有作用域且没挡住"。"""
        service, _, _ = self.setup_two_campaigns()
        result = service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT), profile_external_id=PROFILE
        )
        assert result["scope_filtered_out"] is None

    def test_truncation_cannot_hand_the_slot_to_an_out_of_scope_candidate(self) -> None:
        """配额裁剪必须发生在作用域过滤之后，否则名额会被域外候选按花费抢走。

        这里把**域外**那条的花费做成全场最高（90 > 35）、配额只给 1 个。若先裁剪
        再过滤，留下的会是域外的 ag-2，最终产出 0 条——一份"什么都没做"的授权书；
        若裁剪时根本没过滤，产出的就是一条人从未授权过的候选。两种错法都被这条钉死。
        """
        service, _, mandates = make_service(
            [
                record("cheap widget holder", ad_group="ag-1", campaign="c-1", spend="35.00"),
                record("wobbly widget hack", ad_group="ag-2", campaign="c-2", spend="90.00"),
            ]
        )
        mandate = make_mandate(
            scope=objects_scope("c-1"),
            bounds=MandateBounds(max_runs_per_day=5, max_candidates_per_run=1, valid_days=14),
        )
        mandates.save(mandate)
        result = service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id=PROFILE,
            mandate_id=str(mandate.mandate_id),
        )
        assert result["candidate_count"] == 1
        assert result["candidates"][0]["ad_group_external_id"] == "ag-1"

    @staticmethod
    def setup_two_campaigns():
        return make_service(
            [
                record("cheap widget holder", ad_group="ag-1", campaign="c-1"),
                record("wobbly widget hack", ad_group="ag-2", campaign="c-2"),
            ]
        )


# ------------------------------------------------- 审查项 3：运行时段闸长在真正的运行入口上


class TestRunWindowGateAtRealEntry:
    def test_outside_window_denies_the_actual_generation_call(self) -> None:
        """窗口闸不是构造期装饰：它必须挡住真正会产出候选的那个调用。

        UTC 12:00 = 吉隆坡 20:00，落在 02:00–18:00 之外 → 整个生成被拒，
        一条候选都不产生（不是"产生了但标个警告"）。
        """
        service, store, mandates = make_service(
            [record("cheap widget holder", ad_group="ag-1", campaign="c-1")]
        )
        mandate = make_mandate(run_window=RunWindow(timezone=KL, start_hour=2, end_hour=18))
        mandates.save(mandate)
        with pytest.raises(ToolDenied) as exc:
            service.generate_negation_candidate_set(
                actor(PrincipalType.AI_CLIENT),
                profile_external_id=PROFILE,
                mandate_id=str(mandate.mandate_id),
            )
        assert exc.value.code == "OUTSIDE_RUN_WINDOW"
        assert store.list_by_state(ORG) == ()

    def test_inside_window_allows_the_same_call(self) -> None:
        """反向锚点：同一份授权在窗口内放行——否则上一条只是证明整条路是死的。"""
        source = MockSearchTermSource()
        source.seed(PROFILE, [record("cheap widget holder", ad_group="ag-1", campaign="c-1")])
        store = InMemoryCandidateSetStore()
        mandates = InMemoryMandateStore()
        # UTC 02:00 = 吉隆坡 10:00，落在 02:00–18:00 之内。
        inside = datetime(2026, 8, 28, 2, 0, tzinfo=UTC)
        service = StrategyToolService(
            environment=Environment.STAGING,
            grants=[
                Grant(
                    grant_id=new_canonical_id(),
                    organization_id=ORG,
                    environments=frozenset({Environment.STAGING}),
                    actions=frozenset({Action.PROPOSAL_CREATE_DRAFT}),
                    client_types=frozenset({ClientType.MCP_AI}),
                )
            ],
            denies=[],
            search_terms=source,
            store=store,
            mandates=mandates,
            clock=lambda: inside,
        )
        mandates.save(make_mandate_at(inside, RunWindow(timezone=KL, start_hour=2, end_hour=18)))
        mandate = mandates.list_for_org(ORG)[0]
        result = service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id=PROFILE,
            mandate_id=str(mandate.mandate_id),
        )
        assert result["candidate_count"] == 1

    def test_membership_uses_mandate_timezone_not_server_local(self) -> None:
        """P4：同一个 UTC 时刻，按服务器 UTC 钟点看在窗外、按吉隆坡钟点看在窗内。

        这条只有在判定确实把 now 转到授权书自己的时区之后才能通过；用
        datetime.now() 的本地钟点或裸 UTC 钟点判定都会挂。
        """
        window = RunWindow(timezone=KL, start_hour=2, end_hour=18)
        # UTC 19:00 → 吉隆坡次日 03:00（UTC+8），在窗内。
        moment = datetime(2026, 8, 28, 19, 0, tzinfo=UTC)
        assert not 2 <= moment.hour < 18  # 按 UTC 钟点看：窗外
        assert moment.astimezone(ZoneInfo(KL)).hour == 3  # 按授权书时区看：窗内
        assert window.is_open_at(moment) is True

    def test_cross_midnight_boundaries_are_left_closed_right_open(self) -> None:
        """跨午夜窗口的边界钟点：start 那刻算开、end 那刻算关，两端不含糊。"""
        window = RunWindow(timezone="UTC", start_hour=22, end_hour=6)

        def at(hour: int) -> datetime:
            return datetime(2026, 8, 28, hour, 0, tzinfo=UTC)

        assert window.is_open_at(at(22)) is True  # start：含
        assert window.is_open_at(at(23)) is True
        assert window.is_open_at(at(0)) is True  # 跨过午夜仍开着
        assert window.is_open_at(at(5)) is True
        assert window.is_open_at(at(6)) is False  # end：不含
        assert window.is_open_at(at(21)) is False  # start 前一小时：关

    def test_naive_now_is_a_coded_rejection_not_a_bare_typeerror(self) -> None:
        """闸链最前端拒 naive now：结果同样是"不放行"，但拒绝理由不再丢失。

        修复前这里抛的是 TypeError（HTTP 面 500），人只看到"服务器错误"。
        """
        mandate = make_mandate(run_window=RunWindow(timezone=KL, start_hour=2, end_hour=18))
        with pytest.raises(MandateViolation) as exc:
            assert_run_authorized(
                mandate,
                organization_id=ORG,
                profile_external_id=PROFILE,
                runs_today=0,
                now=datetime(2026, 8, 28, 12, 0),  # naive
            )
        assert exc.value.code == "NAIVE_DATETIME_REJECTED"


def make_mandate_at(moment: datetime, window: RunWindow):
    return issue_mandate(
        actor(PrincipalType.HUMAN),
        mandate_id=new_canonical_id(),
        profile_external_id=PROFILE,
        objective=MandateObjective(
            objective="WASTED_SPEND_REMOVED", statement="压降无效搜索词花费"
        ),
        parameter_pack=pack(),
        bounds=MandateBounds(max_runs_per_day=5, max_candidates_per_run=50, valid_days=14),
        now=moment,
        run_window=window,
    )


# ------------------------------------------------- 审查项 1：合并身份后仍被拒绝的动作


class TestMergedIdentityKeepsAiFenced:
    """演示身份合二为一动的只是"人有几个"，AI 那条红线一寸没动。

    每条 AI 拒绝都配一条人身份放行的反向断言——否则"AI 被拒"可能只是因为
    那条路对谁都是死的（恒真断言正是本次审查要找的东西）。
    """

    def build(self):
        source = MockSearchTermSource()
        source.seed(PROFILE, [record("cheap widget holder", ad_group="ag-1", campaign="c-1")])
        store = InMemoryCandidateSetStore()
        mandates = InMemoryMandateStore()
        verifier = InMemoryActorTokenVerifier()
        human = actor(PrincipalType.HUMAN)
        ai = actor(PrincipalType.AI_CLIENT)
        verifier.register("h", human)
        verifier.register("a", ai)
        app = build_approval_app(store, verifier, clock=lambda: NOW, mandates=mandates)
        app.include_router(
            build_workbench_router(InMemorySnapshotRepository(), verifier, clock=lambda: NOW)
        )
        return TestClient(app), store, mandates

    ISSUE = {
        "profile_external_id": PROFILE,
        "objective": "WASTED_SPEND_REMOVED",
        "statement": "压降无效搜索词花费",
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

    def test_ai_cannot_issue_but_the_single_human_can(self) -> None:
        client, _, _ = self.build()
        denied = client.post("/mandates", headers=_bearer("a"), json=self.ISSUE)
        assert denied.status_code == 403
        assert denied.json()["detail"] == "AI_CANNOT_ISSUE_MANDATE"
        allowed = client.post("/mandates", headers=_bearer("h"), json=self.ISSUE)
        assert allowed.status_code == 200

    def test_ai_cannot_revoke_but_the_single_human_can(self) -> None:
        client, _, _ = self.build()
        mid = client.post("/mandates", headers=_bearer("h"), json=self.ISSUE).json()["mandate_id"]
        denied = client.post(f"/mandates/{mid}/revoke", headers=_bearer("a"))
        assert denied.status_code == 403
        assert denied.json()["detail"] == "HUMAN_REQUIRED"
        allowed = client.post(f"/mandates/{mid}/revoke", headers=_bearer("h"))
        assert allowed.status_code == 200
        assert allowed.json()["state"] == "REVOKED"

    def test_ai_cannot_trigger_sync(self) -> None:
        """同步是拉真实外部数据的动作；AI 身份在任何 env 配置下都进不去。"""
        client, _, _ = self.build()
        denied = client.post(
            "/api/workbench/sync", headers=_bearer("a"), json={"profile_id": PROFILE}
        )
        assert denied.status_code == 403
        assert denied.json()["detail"] == "HUMAN_REQUIRED"

    def test_ai_cannot_approve_but_the_single_human_can(self) -> None:
        client, store, _ = self.build()
        frozen = _frozen_set(created_by_person_id=None)
        store.save(frozen)
        path = f"/candidate-sets/{frozen.set_id}/approve"
        body = {"expected_hash": frozen.set_hash}
        denied = client.post(path, headers=_bearer("a"), json=body)
        assert denied.status_code == 403
        assert denied.json()["detail"] == "AI_CANNOT_APPROVE"
        allowed = client.post(path, headers=_bearer("h"), json=body)
        assert allowed.status_code == 200
        assert allowed.json()["state"] == "APPROVED"

    def test_ai_cannot_reject_but_the_single_human_can(self) -> None:
        """否决同样是审批意思表示。修复前这条端点一道主体闸都没有：AI 可以

        把人的整个待批队列一键清空（REJECTED 是终态），且没有任何提示。
        """
        client, store, _ = self.build()
        frozen = _frozen_set(created_by_person_id=None)
        store.save(frozen)
        path = f"/candidate-sets/{frozen.set_id}/reject"
        denied = client.post(path, headers=_bearer("a"))
        assert denied.status_code == 403
        assert denied.json()["detail"] == "AI_CANNOT_REJECT"
        allowed = client.post(path, headers=_bearer("h"))
        assert allowed.status_code == 200
        assert allowed.json()["state"] == "REJECTED"

    def test_reject_gate_lives_in_the_domain_not_only_in_http(self) -> None:
        """闸装在域层：未来若长出 MCP reject 工具，它会自动继承这条约束。"""
        with pytest.raises(SoDViolation) as exc:
            _frozen_set(created_by_person_id=None).reject(actor(PrincipalType.AI_CLIENT))
        assert exc.value.code == "AI_CANNOT_REJECT"

    def test_sod_is_not_vacuous_after_merge(self) -> None:
        """单人世界里 SoD 仍有牙：人不能批准自己生成的集合。

        合并前"另一个人"是靠 demo-ops-token 提供的；那个 token 没了之后，
        这条分支若无人守着就会退化成永不触发的死代码。
        """
        client, store, _ = self.build()
        own = _frozen_set(created_by_person_id="owner-1")
        store.save(own)
        denied = client.post(
            f"/candidate-sets/{own.set_id}/approve",
            headers=_bearer("h"),
            json={"expected_hash": own.set_hash},
        )
        assert denied.status_code == 403
        assert denied.json()["detail"] == "CREATOR_CANNOT_APPROVE"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _frozen_set(*, created_by_person_id: str | None) -> NegationCandidateSet:
    """一份已冻结、待批的候选集合。created_by_person_id=None 模拟 AI 生成。"""
    result = generate_negation_candidates(
        [record("cheap widget holder", ad_group="ag-1", campaign="c-1")],
        pack(),
        NOW,
        new_canonical_id,
    )
    return NegationCandidateSet(
        set_id=new_canonical_id(),
        organization_id=ORG,
        parameter_pack=pack(),
        candidates=result.candidates,
        generated_at=NOW,
        created_by_client_id="c-1",
        created_by_person_id=created_by_person_id,
        source="HUMAN" if created_by_person_id else "AI",
    ).freeze()


# ------------------------------------------------- 审查项 5：拒绝码必须真的到得了调用方


async def test_mcp_surface_carries_the_rejection_code_not_a_generic_crash() -> None:
    """MCP 面的拒绝必须带码送到调用方，且不被记成崩溃。

    修复前：ToolDenied 不是 SDK 认识的 ToolError，于是 SDK 按"工具崩了"处理——
    调用方只看到 "Error executing tool generate_negation_candidate_set"，
    OUTSIDE_RUN_WINDOW 被吞掉。后果是 AI 分不清"策略拒绝"和"服务器坏了"，
    会把一条有意的拒绝当瞬时故障重试；同时正常的边界拒绝污染崩溃日志。

    本条对着**真实 MCP 会话**断言，而不是对着 _coded 辅助函数——只有真跑一趟
    才能证明 SDK 的 ToolError / UnexpectedToolError 分流确实落在我们这一侧。
    """
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from ads_control_plane.api.local_demo import DEMO_CODEX_TOKEN, build_local_demo_app

    # UTC 12:00 = 吉隆坡 20:00，落在 02:00–18:00 之外。
    moment = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    app = build_local_demo_app(clock=lambda: moment)
    client = TestClient(app)
    issued = client.post(
        "/mandates",
        headers=_bearer("demo-owner-token"),
        json={
            "profile_external_id": PROFILE,
            "objective": "WASTED_SPEND_REMOVED",
            "statement": "压降无效搜索词花费",
            "lookback_days": 30,
            "min_spend_amount": "20.00",
            "currency": "USD",
            "min_clicks": 25,
            "max_data_staleness_hours": 24,
            "max_runs_per_day": 2,
            "max_candidates_per_run": 50,
            "valid_days": 7,
            "run_interval_minutes": 1440,
            "run_window": {"timezone": KL, "start_hour": 2, "end_hour": 18},
        },
    )
    assert issued.status_code == 200

    base = "http://127.0.0.1:8788"
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with (
            httpx2.AsyncClient(
                transport=transport, base_url=base, headers=_bearer(DEMO_CODEX_TOKEN)
            ) as http,
            streamable_http_client(f"{base}/mcp", http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(
                "generate_negation_candidate_set",
                {
                    "profile_external_id": PROFILE,
                    "mandate_id": issued.json()["mandate_id"],
                },
            )
    assert result.is_error
    text = result.content[0].text  # type: ignore[union-attr]
    assert "OUTSIDE_RUN_WINDOW" in text


# ------------------------------------------------- 审查项 4：新参数面不是注入面


class TestNewParameterSurfacesAreNotInjectable:
    @pytest.mark.parametrize(
        "hostile",
        [
            "../../../etc/passwd",
            "/etc/passwd",
            "Asia/Kuala_Lumpur/../../etc/passwd",
            "UTC\n",
            "\x00",
            "localtime",
            "",
            "   ",
        ],
    )
    def test_timezone_string_only_reaches_zoneinfo_after_validation(self, hostile: str) -> None:
        """时区串唯一的用法是喂给 zoneinfo，且构造期就必须过；路径穿越一律带码拒。"""
        with pytest.raises(MandateViolation) as exc:
            RunWindow(timezone=hostile, start_hour=2, end_hour=18)
        assert exc.value.code == "INVALID_TIMEZONE"

    def test_sort_field_whitelist_is_a_real_whitelist(self) -> None:
        """白名单是闭集常量，不是"排除几个坏词"——任意字段名不可能落进来。"""
        assert set(_SORT_FIELDS) == {
            "spend",
            "acos",
            "orders",
            "clicks",
            "impressions",
            "sales",
            "name",
        }

    @pytest.mark.parametrize(
        "hostile",
        ["__class__", "metrics", "object_key", "state", "spend; DROP TABLE", "SPEND", "spends"],
    )
    def test_arbitrary_sort_field_is_400_not_silently_ignored(self, hostile: str) -> None:
        """看不懂就当没传 = 返回一份顺序不符预期的结果，而人以为排过了。"""
        verifier = InMemoryActorTokenVerifier()
        verifier.register("h", actor(PrincipalType.HUMAN))
        app = FastAPI()
        app.include_router(
            build_workbench_router(InMemorySnapshotRepository(), verifier, clock=lambda: NOW)
        )
        res = TestClient(app).get(
            "/api/workbench/objects",
            params={"profile_id": PROFILE, "sort_field": hostile},
            headers=_bearer("h"),
        )
        assert res.status_code == 400
        assert res.json()["detail"] == "SORT_FIELD_INVALID"


# ------------------------------------------------- 审查项 5：fail-closed 完备（带码，不静默）


class TestNewRejectionsAllCarryCodes:
    """每条新增拒绝路径都必须带 SCREAMING_SNAKE 码——裸 ValueError/TypeError 不算。"""

    def test_scope_and_window_construction_rejections_carry_codes(self) -> None:
        cases = [
            (lambda: MandateScope(kind=MandateScopeKind.OBJECTS), "SCOPE_SELECTION_REQUIRED"),
            (
                lambda: MandateScope(
                    kind=MandateScopeKind.PROFILE, selection=objects_scope("c-1").selection
                ),
                "MANDATE_SCOPE_CONFLICT",
            ),
            (
                lambda: RunWindow(timezone="Nowhere/Nowhere", start_hour=2, end_hour=8),
                "INVALID_TIMEZONE",
            ),
            (lambda: RunWindow(timezone=KL, start_hour=24, end_hour=8), "RUN_WINDOW_INVALID"),
        ]
        for build, expected in cases:
            with pytest.raises(MandateViolation) as exc:
                build()
            assert exc.value.code == expected

    def test_scope_profile_mismatch_is_refused_at_issuance(self) -> None:
        """跨店铺勾选在签发期就拒——放行的话授权书会是一份永远跑不出东西的 ACTIVE。"""
        other = MandateScope(
            kind=MandateScopeKind.OBJECTS,
            selection=SelectionSet(
                items=(
                    SelectedObject(
                        level="CAMPAIGN", external_id="c-1", profile_external_id="profile-B"
                    ),
                )
            ),
        )
        with pytest.raises(MandateViolation) as exc:
            make_mandate(scope=other)
        assert exc.value.code == "SCOPE_PROFILE_MISMATCH"
