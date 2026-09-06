"""L1 纵切端到端：Codex(AI) 生成候选 → 人批准 → CSV 导出 → 日志核验 → 目标函数。

链路里的每个身份都被验证：AI 只能生成（AX-05），批准者是 HUMAN，
目标函数只统计核验落地的候选（DEC-113）。全程零 Provider 写调用。
"""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from ads_control_plane.api.approval_api import build_approval_app
from ads_control_plane.api.mcp_tools.server import InMemoryActorTokenVerifier
from ads_control_plane.api.mcp_tools.strategy_service import StrategyToolService
from ads_control_plane.authorization.model import Action, ClientType, Environment, Grant
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
from ads_control_plane.providers.mock.search_terms import MockSearchTermSource
from ads_control_plane.strategies.negation import (
    NEGATIVE_CREATE_ACTION,
    OperationLogEntry,
    OperationLogSource,
    SearchTermRecord,
    VerificationStatus,
    estimate_waste_removed,
    verify_applied,
)
from ads_control_plane.strategies.store import InMemoryCandidateSetStore

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
ORG = new_canonical_id()
CONNECTION = new_canonical_id()


def record(term: str, clicks: int, conversions: int, spend: str, stale: bool = False):
    return SearchTermRecord(
        scope=CanonicalEntityRef(
            organization_id=ORG,
            provider=Provider.MOCK,
            provider_connection_id=CONNECTION,
            marketplace="US",
            shop_external_id="shop-1",
            profile_external_id="profile-A",
            ad_product=AdProduct.SP,
            entity_type=EntityType.AD_GROUP,
            entity_external_id="ag-1",
            parent_refs=ParentRefs(campaign_external_id="c-1"),
        ),
        search_term=term,
        clicks=clicks,
        conversions=conversions,
        spend=Money(amount=spend, currency="USD"),
        window_start=NOW - timedelta(days=30),
        window_end=NOW - timedelta(days=1),
        data_as_of=NOW - timedelta(hours=48 if stale else 2),
    )


def test_full_negation_journey() -> None:
    real_now = datetime.now(UTC)
    ai = ActorContext(
        principal_id=new_canonical_id(),
        principal_type=PrincipalType.AI_CLIENT,
        organization_id=ORG,
        roles=frozenset({Role.ANALYST}),
        human_initiator_person_id="alice",
        client_id="codex-1",
        session_id="s-ai",
        authentication_strength=AuthenticationStrength.MFA,
        issued_at=real_now,
        expires_at=real_now + timedelta(hours=8),
    )
    human = ActorContext(
        principal_id=new_canonical_id(),
        principal_type=PrincipalType.HUMAN,
        organization_id=ORG,
        roles=frozenset({Role.APPROVER}),
        human_person_id="bob",
        client_id="web-1",
        session_id="s-h",
        authentication_strength=AuthenticationStrength.MFA,
        issued_at=real_now,
        expires_at=real_now + timedelta(hours=8),
    )

    # 数据面：1 个达标词、1 个有转化词（永不候选）、1 个数据过旧词（ABSTAIN）
    source = MockSearchTermSource()
    source.seed(
        "profile-A",
        [
            record("cheap widget", clicks=40, conversions=0, spend="35.00"),
            record("best widget", clicks=200, conversions=9, spend="150.00"),
            record("old widget", clicks=50, conversions=0, spend="60.00", stale=True),
        ],
    )
    store = InMemoryCandidateSetStore()
    strategy = StrategyToolService(
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
        clock=lambda: NOW,
    )

    # 1. AI（Codex）生成候选集合：只有达标词入选；stale 词显式 ABSTAIN
    generated = strategy.generate_negation_candidate_set(ai, profile_external_id="profile-A")
    assert generated["candidate_count"] == 1
    assert generated["abstains"][0]["search_term"] == "old widget"
    set_id = generated["set_id"]
    set_hash = generated["set_hash"]

    # 2. 人经审批 API 批准（绑定 hash）
    verifier = InMemoryActorTokenVerifier()
    verifier.register("t-human", human)
    verifier.register("t-ai", ai)
    client = TestClient(build_approval_app(store, verifier, clock=lambda: NOW + timedelta(hours=1)))
    denied = client.post(
        f"/candidate-sets/{set_id}/approve",
        json={"expected_hash": set_hash},
        headers={"Authorization": "Bearer t-ai"},
    )
    assert denied.status_code == 403  # AI 到不了批准这一步
    approved = client.post(
        f"/candidate-sets/{set_id}/approve",
        json={"expected_hash": set_hash},
        headers={"Authorization": "Bearer t-human"},
    )
    assert approved.status_code == 200

    # 3. 导出执行清单（L1.5：人拿着它去领星后台应用）
    csv_resp = client.get(
        f"/candidate-sets/{set_id}/export.csv", headers={"Authorization": "Bearer t-human"}
    )
    assert csv_resp.status_code == 200
    assert "cheap widget" in csv_resp.text

    # 4. 操作日志出现应用记录 → 核验闭环
    final_set = store.list_by_state(ORG)[0]
    report = verify_applied(
        final_set,
        [
            OperationLogEntry(
                occurred_at=NOW + timedelta(hours=2),
                operator_name="op-bob",
                source=OperationLogSource.ERP,
                action=NEGATIVE_CREATE_ACTION,
                ad_group_external_id="ag-1",
                search_term="cheap widget",
            )
        ],
    )
    assert report[0].status is VerificationStatus.APPLIED_VERIFIED

    # 5. 目标函数（DEC-113）：只统计核验落地的候选
    estimate = estimate_waste_removed(final_set, report)
    assert str(estimate.estimated_amount.amount) == "35.00"
    assert estimate.causality == "INCONCLUSIVE"

    # 身份链完整性：AI 生成、人批准，全程零 Provider 写
    assert final_set.source == "AI"
    assert final_set.approved_by_person_id == "bob"
    assert source.read_call_count == 1
