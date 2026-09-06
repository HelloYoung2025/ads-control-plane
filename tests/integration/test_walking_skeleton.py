"""Walking skeleton：一条端到端薄片贯穿全部安全公理。

旅程（handoff §5.1 产品使命的 MVP 纵切）：
AI 创建草案 → AI 提交被拒(AX-05) → 人类提交冻结(AX-07) → 创建者自批被拒(AX-08)
→ 第二人审批 → 篡改检测(RT-05) → 授权门(AX-03/04) → 执行协议(AX-09..13)
→ 回读分级结论(AX-12) → 审计链完整(AX-09)。
"""

from datetime import UTC, datetime, timedelta

import pytest
from ads_write_executor.protocol import ExecutionProtocol, VerifiedIntent

from ads_control_plane.approvals.model import (
    ApprovalDecision,
    ApprovalInvalid,
    ApprovalOutcome,
    assert_approval_valid,
)
from ads_control_plane.audit.ledger import InMemoryAuditLedger
from ads_control_plane.authorization.engine import evaluate
from ads_control_plane.authorization.model import (
    AccessRequest,
    Action,
    ClientType,
    Environment,
    Grant,
)
from ads_control_plane.authorization.sod import SoDViolation, check_can_approve, check_can_submit
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
from ads_control_plane.proposals.model import (
    ChangeItem,
    ObservedValue,
    Proposal,
    ProposalState,
)
from ads_control_plane.providers.mock.adapter import MockProvider
from ads_control_plane.safety.execution import (
    ExecutionRecord,
    ExecutionState,
    InMemoryExecutionStore,
)
from ads_control_plane.safety.kill import InMemoryKillStore

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
ORG = new_canonical_id()
CONN = new_canonical_id()


def actor(
    principal_type: PrincipalType,
    roles: set[Role],
    person: str | None,
    initiator: str | None = None,
) -> ActorContext:
    return ActorContext(
        principal_id=new_canonical_id(),
        principal_type=principal_type,
        organization_id=ORG,
        roles=frozenset(roles),
        human_person_id=person if principal_type is PrincipalType.HUMAN else None,
        human_initiator_person_id=initiator,
        client_id="codex-session-1" if principal_type is PrincipalType.AI_CLIENT else "web-1",
        session_id="sess-1",
        authentication_strength=AuthenticationStrength.MFA,
        #: 会话有效期锚在**真实墙钟**上，不是这个文件里那个固定的 NOW。
        #  ActorContext.is_expired() 默认拿 datetime.now(UTC) 比（identity/actor.py），
        #  而注入的 clock 只管域层判定。钉在固定日历日上，这个令牌就是一颗定时炸弹：
        #  过了那一刻，全绿的测试会在某次与代码无关的运行里突然 401。
        #  2026-09-07 真的炸过一次：test_mandate_scope_api 那条在 20:00 UTC 从绿变红，
        #  而 diff 里一个相关改动都没有——排查花掉的时间远超写对它的成本。
        issued_at=datetime.now(UTC) - timedelta(minutes=1),
        expires_at=datetime.now(UTC) + timedelta(hours=8),
    )


ENTITY = CanonicalEntityRef(
    organization_id=ORG,
    provider=Provider.MOCK,
    provider_connection_id=CONN,
    marketplace="US",
    shop_external_id="shop-1",
    profile_external_id="profile-A",
    ad_product=AdProduct.SP,
    entity_type=EntityType.TARGET,
    entity_external_id="t-42",
    parent_refs=ParentRefs(campaign_external_id="c-7", ad_group_external_id="ag-3"),
)

AI_ANALYST = actor(PrincipalType.AI_CLIENT, {Role.ANALYST}, None, initiator="alice")
ALICE_OPERATOR = actor(PrincipalType.HUMAN, {Role.OPERATOR}, "alice")
BOB_APPROVER = actor(PrincipalType.HUMAN, {Role.APPROVER}, "bob")


def operator_write_grant() -> Grant:
    return Grant(
        grant_id=new_canonical_id(),
        organization_id=ORG,
        environments=frozenset({Environment.STAGING}),
        actions=frozenset({Action.TARGET_BID_UPDATE}),
        client_types=frozenset({ClientType.WEB}),
        profile_external_ids=frozenset({"profile-A"}),
        entity_types=frozenset({EntityType.TARGET}),
        fields=frozenset({"bid"}),
        max_absolute_value=Money(amount="5.00", currency="USD"),
        max_single_delta=Money(amount="0.10", currency="USD"),
    )


def test_full_journey_draft_to_verified_execution() -> None:
    # --- 0. 环境：Mock Provider 上有一个 bid=0.82 的 Target ---
    provider = MockProvider()
    provider.seed(ENTITY, "bid", Money(amount="0.82", currency="USD"))

    # --- 1. AI 分析后创建草案 Proposal（允许）---
    draft = Proposal(
        proposal_id=new_canonical_id(),
        organization_id=ORG,
        state=ProposalState.DRAFT,
        created_by_person_id=AI_ANALYST.acting_person_id(),  # 委托人 alice
        created_by_client_id=AI_ANALYST.client_id,
        source="AI",
        change_items=(
            ChangeItem(
                change_item_id=new_canonical_id(),
                entity=ENTITY,
                field="bid",
                expected_before=ObservedValue(
                    value=Money(amount="0.82", currency="USD"),
                    snapshot_id=new_canonical_id(),
                    observed_at=NOW - timedelta(minutes=15),
                ),
                absolute_target=Money(amount="0.83", currency="USD"),
            ),
        ),
        valid_until=NOW + timedelta(hours=24),
    )

    # --- 2. AI 尝试正式提交 → SoD 拒绝（AX-05）---
    with pytest.raises(SoDViolation) as ai_submit:
        check_can_submit(AI_ANALYST, draft.created_by_client_id)
    assert ai_submit.value.code == "AI_CANNOT_SUBMIT"

    # --- 3. 人类 Operator（alice）提交并冻结（AX-07）---
    check_can_submit(ALICE_OPERATOR, draft.created_by_client_id)
    validated = draft.with_state(ProposalState.VALIDATED)
    frozen = validated.frozen_copy(ProposalState.PENDING_APPROVAL)
    assert frozen.proposal_hash is not None

    # --- 4. alice（创建者/提交者）尝试自批 → SoD 拒绝（AX-08 / AUTH-07）---
    with pytest.raises(SoDViolation):
        check_can_approve(ALICE_OPERATOR, frozen.created_by_person_id, "alice")

    # --- 5. bob 审批，绑定冻结 Hash ---
    check_can_approve(BOB_APPROVER, frozen.created_by_person_id, "alice")
    approval = ApprovalDecision(
        approval_id=new_canonical_id(),
        proposal_id=frozen.proposal_id,
        proposal_hash=frozen.proposal_hash,
        approver_principal_id=BOB_APPROVER.principal_id,
        approver_person_id="bob",
        outcome=ApprovalOutcome.APPROVED,
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=4),
    )
    approved = frozen.with_state(ProposalState.APPROVAL_SATISFIED)
    assert_approval_valid(approval, approved, NOW + timedelta(minutes=1))

    # --- 6. RT-05：审批后内容被替换 → 审批失效 ---
    tampered = approved.model_copy(
        update={
            "change_items": (
                approved.change_items[0].model_copy(
                    update={"absolute_target": Money(amount="4.99", currency="USD")}
                ),
            )
        }
    )
    with pytest.raises(ApprovalInvalid):
        assert_approval_valid(approval, tampered, NOW + timedelta(minutes=2))

    # --- 7. 执行前授权门：完整请求元组必须被单一 Grant 独立匹配（AX-03/04）---
    item = approved.change_items[0]
    decision = evaluate(
        ALICE_OPERATOR,
        AccessRequest(
            environment=Environment.STAGING,
            organization_id=ORG,
            action=Action.TARGET_BID_UPDATE,
            client_type=ClientType.WEB,
            entity=item.entity,
            field=item.field,
            target_value=item.absolute_target,
            expected_before=item.expected_before.value,
        ),
        [operator_write_grant()],
        [],
        now=NOW,
    )
    assert decision.allowed

    # --- 8. 执行协议（AX-09..13）：审计预写 → 写前重读 → 单提交 → 回读 ---
    store = InMemoryExecutionStore()
    kill = InMemoryKillStore()
    audit = InMemoryAuditLedger()
    protocol = ExecutionProtocol(
        store=store, kill_store=kill, audit=audit, reader=provider, writer=provider
    )
    store.create(ExecutionRecord(execution_id="exec-1", intent_id="intent-1"))
    intent = VerifiedIntent(
        intent_id="intent-1",
        execution_id="exec-1",
        entity=item.entity,
        field=item.field,
        expected_before=item.expected_before.value,
        absolute_target=item.absolute_target,
        proposal_hash=approved.proposal_hash or "",
        approval_id=str(approval.approval_id),
        kill_epoch_at_issue=kill.current_epoch(),
        expires_at=NOW + timedelta(hours=1),
    )
    outcome = protocol.execute(intent, now=NOW)

    # --- 9. 结论分级（AX-12）：Mock 无操作日志 → 只能是 DESIRED_STATE_OBSERVED ---
    assert outcome.state is ExecutionState.DESIRED_STATE_OBSERVED
    assert (
        provider.current_value(ENTITY, "bid").amount == Money(amount="0.83", currency="USD").amount
    )
    assert provider.write_call_count == 1
    store.assert_invariants()

    # --- 10. 审计链完整（AX-09）：预写事件先于提交事件 ---
    event_types = [e.event_type for e in audit.events()]
    assert event_types.index("EXECUTION_PREWRITE") < event_types.index("SUBMIT_ACCEPTED")


def test_shadow_mode_zero_provider_writes() -> None:
    """AX-17：Shadow（只生成 Proposal 不执行）下 Provider 写调用数必须为 0。"""
    provider = MockProvider()
    provider.seed(ENTITY, "bid", Money(amount="0.82", currency="USD"))
    # Shadow 流程 = 创建草案 + 冻结 + （不执行）。这里生成 10 个提案。
    for _ in range(10):
        Proposal(
            proposal_id=new_canonical_id(),
            organization_id=ORG,
            state=ProposalState.DRAFT,
            created_by_person_id=None,
            created_by_client_id="shadow-job",
            source="AI",
            change_items=(
                ChangeItem(
                    change_item_id=new_canonical_id(),
                    entity=ENTITY,
                    field="bid",
                    expected_before=ObservedValue(
                        value=provider.read_field(ENTITY, "bid").value,
                        snapshot_id=new_canonical_id(),
                        observed_at=NOW,
                    ),
                    absolute_target=Money(amount="0.84", currency="USD"),
                ),
            ),
            valid_until=NOW + timedelta(hours=24),
        ).with_state(ProposalState.VALIDATED).frozen_copy(ProposalState.PENDING_APPROVAL)
    assert provider.write_call_count == 0  # 读了 10 次，写 0 次
    assert provider.read_call_count == 10
