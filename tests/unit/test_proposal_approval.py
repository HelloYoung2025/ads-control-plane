"""AX-07 公理测试。覆盖红队场景 RT-05（审批后替换对象/数值）、RT-06（批量集合扩大）。"""

from datetime import UTC, datetime, timedelta

import pytest

from ads_control_plane.approvals.model import (
    ApprovalDecision,
    ApprovalInvalid,
    ApprovalOutcome,
    assert_approval_valid,
)
from ads_control_plane.canonical.entity import (
    AdProduct,
    CanonicalEntityRef,
    EntityType,
    ParentRefs,
    Provider,
)
from ads_control_plane.canonical.ids import new_canonical_id
from ads_control_plane.canonical.money import Money
from ads_control_plane.proposals.model import (
    ChangeItem,
    IllegalTransition,
    ObservedValue,
    Proposal,
    ProposalState,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
ORG = new_canonical_id()


def make_item(target: str = "0.83", entity_id: str = "t-1") -> ChangeItem:
    return ChangeItem(
        change_item_id=new_canonical_id(),
        entity=CanonicalEntityRef(
            organization_id=ORG,
            provider=Provider.MOCK,
            provider_connection_id=new_canonical_id(),
            marketplace="US",
            shop_external_id="shop-1",
            profile_external_id="profile-A",
            ad_product=AdProduct.SP,
            entity_type=EntityType.TARGET,
            entity_external_id=entity_id,
            parent_refs=ParentRefs(campaign_external_id="c-1", ad_group_external_id="ag-1"),
        ),
        field="bid",
        expected_before=ObservedValue(
            value=Money(amount="0.82", currency="USD"),
            snapshot_id=new_canonical_id(),
            observed_at=NOW - timedelta(minutes=10),
        ),
        absolute_target=Money(amount=target, currency="USD"),
    )


def make_proposal(items: tuple[ChangeItem, ...] | None = None) -> Proposal:
    return Proposal(
        proposal_id=new_canonical_id(),
        organization_id=ORG,
        state=ProposalState.VALIDATED,
        created_by_person_id="person-a",
        created_by_client_id="client-1",
        source="AI",
        change_items=(make_item(),) if items is None else items,
        valid_until=NOW + timedelta(hours=24),
    )


def approve(proposal: Proposal, person: str = "person-b") -> ApprovalDecision:
    return ApprovalDecision(
        approval_id=new_canonical_id(),
        proposal_id=proposal.proposal_id,
        proposal_hash=proposal.proposal_hash or "",
        approver_principal_id=new_canonical_id(),
        approver_person_id=person,
        outcome=ApprovalOutcome.APPROVED,
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=4),
    )


class TestProposalFreeze:
    def test_freeze_computes_stable_hash(self) -> None:
        p = make_proposal().frozen_copy(ProposalState.PENDING_APPROVAL)
        assert p.proposal_hash and p.proposal_hash == p.compute_hash()

    def test_cross_currency_change_item_rejected(self) -> None:
        with pytest.raises(ValueError, match="one currency"):
            ChangeItem(
                change_item_id=new_canonical_id(),
                entity=make_item().entity,
                field="bid",
                expected_before=ObservedValue(
                    value=Money(amount="0.82", currency="EUR"),
                    snapshot_id=new_canonical_id(),
                    observed_at=NOW,
                ),
                absolute_target=Money(amount="0.83", currency="USD"),
            )

    def test_empty_proposal_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one change item"):
            make_proposal(items=())

    def test_illegal_transition_blocked(self) -> None:
        p = make_proposal()  # VALIDATED
        with pytest.raises(IllegalTransition):
            p.with_state(ProposalState.APPROVAL_SATISFIED)  # 必须先 PENDING_APPROVAL

    def test_terminal_states_have_no_exit(self) -> None:
        p = make_proposal().with_state(ProposalState.CANCELLED)
        with pytest.raises(IllegalTransition):
            p.with_state(ProposalState.VALIDATED)


class TestApprovalBinding:
    def test_valid_approval_passes(self) -> None:
        p = make_proposal().frozen_copy(ProposalState.PENDING_APPROVAL)
        assert_approval_valid(approve(p), p, NOW + timedelta(minutes=5))

    def test_rt05_value_swap_after_approval_invalidates(self) -> None:
        p = make_proposal().frozen_copy(ProposalState.PENDING_APPROVAL)
        approval = approve(p)
        # 攻击：审批后把 0.83 替换为 8.30（保持同一 proposal_id 与旧 hash 字段）
        tampered = p.model_copy(update={"change_items": (make_item(target="8.30"),)})
        with pytest.raises(ApprovalInvalid) as exc:
            assert_approval_valid(approval, tampered, NOW + timedelta(minutes=5))
        assert exc.value.code == "CONTENT_DRIFT"

    def test_rt06_batch_set_expansion_invalidates(self) -> None:
        p = make_proposal().frozen_copy(ProposalState.PENDING_APPROVAL)
        approval = approve(p)
        expanded = p.model_copy(
            update={"change_items": p.change_items + (make_item(entity_id="t-2"),)}
        )
        with pytest.raises(ApprovalInvalid):
            assert_approval_valid(approval, expanded, NOW + timedelta(minutes=5))

    def test_expired_approval_rejected(self) -> None:
        p = make_proposal().frozen_copy(ProposalState.PENDING_APPROVAL)
        with pytest.raises(ApprovalInvalid) as exc:
            assert_approval_valid(approve(p), p, NOW + timedelta(hours=5))
        assert exc.value.code == "APPROVAL_EXPIRED"

    def test_revoked_approval_rejected(self) -> None:
        p = make_proposal().frozen_copy(ProposalState.PENDING_APPROVAL)
        approval = approve(p).model_copy(update={"revoked_at": NOW + timedelta(minutes=1)})
        with pytest.raises(ApprovalInvalid) as exc:
            assert_approval_valid(approval, p, NOW + timedelta(minutes=5))
        assert exc.value.code == "REVOKED"

    def test_unfrozen_proposal_cannot_be_approved(self) -> None:
        p = make_proposal()  # 未冻结，无 hash
        approval = approve(p)
        with pytest.raises(ApprovalInvalid) as exc:
            assert_approval_valid(approval, p, NOW)
        assert exc.value.code == "PROPOSAL_NOT_FROZEN"


def test_all_three_surfaces_agree_on_the_exact_expiry_instant() -> None:
    """恰好 72 小时那一刻，域层闸与两个 API 面必须给同一个答案。

    此前三处各写各的：域层 approve() 是 `now - generated_at > 72h`（开区间），
    approval_api 与 strategy_service 的 expired 字段是 `now >= generated_at + 72h`
    （闭区间）。那一瞬界面说「已过期」，服务端其实还收。

    差一瞬看着无所谓，代价却不小：界面为过期集合给的下一步是「拒绝后重新生成」，
    而默认打法 1 次/日——照做等于赔掉一整天的配额，去换一份服务端本来还肯批的集合。
    判据只准有一个（candidate_set_expired），第三个面再来也不会各写一套。
    """
    import inspect
    from datetime import UTC, datetime, timedelta

    from ads_control_plane.api import approval_api
    from ads_control_plane.api.mcp_tools import strategy_service
    from ads_control_plane.strategies.negation import (
        CANDIDATE_SET_TTL_HOURS,
        candidate_set_expired,
    )

    made = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    exactly = made + timedelta(hours=CANDIDATE_SET_TTL_HOURS)
    assert candidate_set_expired(made, exactly) is False, "恰好到点仍可批——与 approve() 同一判据"
    assert candidate_set_expired(made, exactly + timedelta(microseconds=1)) is True
    assert candidate_set_expired(made, exactly - timedelta(microseconds=1)) is False

    # 两个 API 面必须调它，不许自己再写一个比较。
    for mod in (approval_api, strategy_service):
        src = inspect.getsource(mod)
        assert "candidate_set_expired(" in src, f"{mod.__name__} 没用共用判据"
        assert "now >= expires_at" not in src, f"{mod.__name__} 还留着自己那套闭区间比较"
        assert "now >= s.generated_at" not in src, f"{mod.__name__} 还留着自己那套闭区间比较"
