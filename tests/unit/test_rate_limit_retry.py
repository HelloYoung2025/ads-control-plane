"""评审 ENG-03 修正的测试：限流不杀 Intent（有界重投）、本地节流先于 CAS。

不变量的精确表述："可能已生效的提交 ≤ 1"——被证明未执行的限流拒绝不计入。
"""

from datetime import UTC, datetime, timedelta

from ads_write_executor.protocol import ExecutionProtocol, VerifiedIntent
from ads_write_executor.throttle import LocalThrottle

from ads_control_plane.audit.ledger import InMemoryAuditLedger
from ads_control_plane.canonical.entity import (
    AdProduct,
    CanonicalEntityRef,
    EntityType,
    ParentRefs,
    Provider,
)
from ads_control_plane.canonical.ids import new_canonical_id
from ads_control_plane.canonical.money import Money
from ads_control_plane.providers.mock.adapter import FaultMode, MockProvider
from ads_control_plane.safety.execution import (
    MAX_PROVEN_REJECTED_RETRIES,
    ExecutionRecord,
    ExecutionState,
    InMemoryExecutionStore,
)
from ads_control_plane.safety.kill import InMemoryKillStore

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)

ENTITY = CanonicalEntityRef(
    organization_id=new_canonical_id(),
    provider=Provider.MOCK,
    provider_connection_id=new_canonical_id(),
    marketplace="US",
    shop_external_id="shop-1",
    profile_external_id="profile-A",
    ad_product=AdProduct.SP,
    entity_type=EntityType.TARGET,
    entity_external_id="t-1",
    parent_refs=ParentRefs(campaign_external_id="c-1", ad_group_external_id="ag-1"),
)


def make_harness(throttle: LocalThrottle | None = None):
    provider = MockProvider()
    provider.seed(ENTITY, "bid", Money(amount="0.82", currency="USD"))
    store = InMemoryExecutionStore()
    kill = InMemoryKillStore()
    protocol = ExecutionProtocol(
        store=store,
        kill_store=kill,
        audit=InMemoryAuditLedger(),
        reader=provider,
        writer=provider,
        throttle=throttle,
    )
    store.create(ExecutionRecord(execution_id="exec-1", intent_id="intent-1"))
    intent = VerifiedIntent(
        intent_id="intent-1",
        execution_id="exec-1",
        entity=ENTITY,
        field="bid",
        expected_before=Money(amount="0.82", currency="USD"),
        absolute_target=Money(amount="0.83", currency="USD"),
        proposal_hash="h",
        approval_id="a",
        kill_epoch_at_issue=kill.current_epoch(),
        expires_at=NOW + timedelta(hours=1),
    )
    return provider, store, protocol, intent


class TestProviderRateLimit:
    def test_rate_limit_does_not_kill_intent(self) -> None:
        provider, store, protocol, intent = make_harness()
        provider.next_write_fault = FaultMode.RATE_LIMIT_REJECT
        outcome = protocol.execute(intent, now=NOW)
        assert outcome.state is ExecutionState.PROVIDER_REJECTED_NOT_EXECUTED
        # 同一 Intent 重投成功，无需新 Proposal/Approval
        outcome2 = protocol.execute(intent, now=NOW)
        assert outcome2.state is ExecutionState.DESIRED_STATE_OBSERVED
        record = store.get("exec-1")
        assert record.provider_submit_count == 2  # 两次网络请求，如实记录
        assert record.proven_rejected_count == 1
        assert record.may_have_applied_submit_count == 1  # 真正的安全不变量
        store.assert_invariants()

    def test_retry_budget_is_bounded(self) -> None:
        provider, store, protocol, intent = make_harness()
        for _ in range(MAX_PROVEN_REJECTED_RETRIES):
            provider.next_write_fault = FaultMode.RATE_LIMIT_REJECT
            outcome = protocol.execute(intent, now=NOW)
            assert outcome.state is ExecutionState.PROVIDER_REJECTED_NOT_EXECUTED
        # 第 MAX+1 次限流：预算耗尽 → 人工
        provider.next_write_fault = FaultMode.RATE_LIMIT_REJECT
        outcome = protocol.execute(intent, now=NOW)
        assert outcome.state is ExecutionState.MANUAL_REVIEW
        # 之后重投不再产生任何网络提交
        writes_before = provider.write_call_count
        outcome_final = protocol.execute(intent, now=NOW)
        assert provider.write_call_count == writes_before
        assert outcome_final.state is ExecutionState.MANUAL_REVIEW
        store.assert_invariants()

    def test_ambiguous_failure_still_freezes_no_retry(self) -> None:
        # 限流重投通道不得弱化 UNKNOWN 语义：模糊失败仍然冻结
        provider, store, protocol, intent = make_harness()
        provider.next_write_fault = FaultMode.APPLY_THEN_DROP_RESPONSE
        outcome = protocol.execute(intent, now=NOW)
        assert outcome.state is ExecutionState.UNKNOWN
        record = store.get("exec-1")
        assert record.proven_rejected_count == 0
        assert record.submission_phase == "SUBMITTING"  # 未复位：CAS 封死


class TestLocalThrottle:
    def test_throttle_blocks_before_cas_without_consuming_intent(self) -> None:
        throttle = LocalThrottle(capacity_per_key=1)
        provider, store, protocol, intent = make_harness(throttle)
        key = f"{ENTITY.provider}:{ENTITY.provider_connection_id}:write"
        assert throttle.try_acquire(key)  # 先耗尽唯一令牌
        outcome = protocol.execute(intent, now=NOW)
        assert outcome.state is ExecutionState.DISPATCHING  # 本地节流，未进 CAS
        assert provider.write_call_count == 0
        record = store.get("exec-1")
        assert record.provider_submit_count == 0  # Intent 未被消耗
        # 令牌补充后重投成功
        throttle.refill(key)
        outcome2 = protocol.execute(intent, now=NOW)
        assert outcome2.state is ExecutionState.DESIRED_STATE_OBSERVED
        assert provider.write_call_count == 1
