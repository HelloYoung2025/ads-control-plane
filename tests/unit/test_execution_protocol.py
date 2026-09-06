"""执行协议公理测试（AX-09..AX-13）。

覆盖 handoff 故障注入场景：EXE-02（远端成功响应丢失）、EXE-03（双 Worker）、
EXE-04（写前人工改值）、EXE-08（审计不可用）、RT-09（响应丢失后重发）、
RT-12（Kill epoch）。
"""

from datetime import UTC, datetime, timedelta

import pytest
from ads_write_executor.protocol import ExecutionProtocol, VerifiedIntent

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
    DuplicateSubmitBlocked,
    ExecutionRecord,
    ExecutionState,
    InMemoryExecutionStore,
)
from ads_control_plane.safety.kill import InMemoryKillStore, KillResumeError

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def make_entity() -> CanonicalEntityRef:
    return CanonicalEntityRef(
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


class Harness:
    def __init__(self, *, provider_has_operation_log: bool = False) -> None:
        self.entity = make_entity()
        self.provider = MockProvider(supports_operation_log=provider_has_operation_log)
        self.provider.seed(self.entity, "bid", Money(amount="0.82", currency="USD"))
        self.store = InMemoryExecutionStore()
        self.kill = InMemoryKillStore()
        self.audit = InMemoryAuditLedger()
        self.protocol = ExecutionProtocol(
            store=self.store,
            kill_store=self.kill,
            audit=self.audit,
            reader=self.provider,
            writer=self.provider,
        )

    def make_intent(self, execution_id: str = "exec-1") -> VerifiedIntent:
        self.store.create(ExecutionRecord(execution_id=execution_id, intent_id=f"i-{execution_id}"))
        return VerifiedIntent(
            intent_id=f"i-{execution_id}",
            execution_id=execution_id,
            entity=self.entity,
            field="bid",
            expected_before=Money(amount="0.82", currency="USD"),
            absolute_target=Money(amount="0.83", currency="USD"),
            proposal_hash="hash-1",
            approval_id="appr-1",
            kill_epoch_at_issue=self.kill.current_epoch(),
            expires_at=NOW + timedelta(hours=1),
        )


class TestHappyPath:
    def test_write_then_readback_without_operation_log(self) -> None:
        h = Harness()
        outcome = h.protocol.execute(h.make_intent(), now=NOW)
        # 无 Provider 操作日志 → 最高只能到 DESIRED_STATE_OBSERVED，不得宣称归因确认
        assert outcome.state is ExecutionState.DESIRED_STATE_OBSERVED
        assert "NOT proven" in outcome.note
        assert h.provider.write_call_count == 1
        h.store.assert_invariants()

    def test_attribution_confirmed_requires_operation_evidence(self) -> None:
        h = Harness(provider_has_operation_log=True)
        outcome = h.protocol.execute(h.make_intent(), now=NOW)
        assert outcome.state is ExecutionState.EXECUTION_ATTRIBUTION_CONFIRMED


class TestUnknownSemantics:
    def test_exe02_response_lost_enters_unknown_never_resubmits(self) -> None:
        h = Harness()
        h.provider.next_write_fault = FaultMode.APPLY_THEN_DROP_RESPONSE
        intent = h.make_intent()
        outcome = h.protocol.execute(intent, now=NOW)
        assert outcome.state is ExecutionState.UNKNOWN
        assert h.provider.write_call_count == 1

        # RT-09：重投同一 Intent → 只做只读对账，不产生第二次写
        outcome2 = h.protocol.execute(intent, now=NOW)
        assert h.provider.write_call_count == 1  # 没有第二次提交
        # 远端实际已应用（0.83），对账只能观察到目标值 → DESIRED_STATE_OBSERVED
        assert outcome2.state is ExecutionState.DESIRED_STATE_OBSERVED
        assert "NOT proven" in outcome2.note
        h.store.assert_invariants()

    def test_ambiguous_5xx_also_unknown(self) -> None:
        h = Harness()
        h.provider.next_write_fault = FaultMode.REJECT_WITH_AMBIGUOUS_5XX
        intent = h.make_intent()
        outcome = h.protocol.execute(intent, now=NOW)
        assert outcome.state is ExecutionState.UNKNOWN
        # 远端实际未应用（仍 0.82）→ 对账不能妄断，进人工
        outcome2 = h.protocol.reconcile(intent)
        assert outcome2.state is ExecutionState.MANUAL_REVIEW
        assert h.provider.write_call_count == 1

    def test_validation_reject_is_not_applied_and_terminal(self) -> None:
        h = Harness()
        h.provider.next_write_fault = FaultMode.VALIDATION_REJECT
        intent = h.make_intent()
        outcome = h.protocol.execute(intent, now=NOW)
        assert outcome.state is ExecutionState.NOT_APPLIED_CONFIRMED
        # CAS 已消费：同一 Intent 不可能再次提交
        outcome2 = h.protocol.execute(intent, now=NOW)
        assert h.provider.write_call_count == 1
        assert outcome2.state is ExecutionState.NOT_APPLIED_CONFIRMED


class TestSingleSubmitInvariant:
    def test_exe03_second_worker_blocked_by_cas(self) -> None:
        h = Harness()
        intent = h.make_intent()
        h.protocol.dispatch(intent)
        h.store.begin_submission(intent.execution_id, h.kill.current_epoch())
        with pytest.raises(DuplicateSubmitBlocked):
            h.store.begin_submission(intent.execution_id, h.kill.current_epoch())
        h.store.assert_invariants()

    def test_delivery_count_independent_from_submit_count(self) -> None:
        h = Harness()
        intent = h.make_intent()
        h.protocol.execute(intent, now=NOW)
        h.protocol.execute(intent, now=NOW)
        h.protocol.execute(intent, now=NOW)
        record = h.store.get(intent.execution_id)
        assert record.job_delivery_count == 3
        assert record.provider_submit_count == 1


class TestPreconditions:
    def test_exe04_manual_change_before_submit_blocks_without_provider_write(self) -> None:
        h = Harness()
        intent = h.make_intent()
        # 人工在写前把 0.82 改成 0.90（RT-14 的可检测部分）
        h.provider.external_change(h.entity, "bid", Money(amount="0.90", currency="USD"))
        outcome = h.protocol.execute(intent, now=NOW)
        assert outcome.state is ExecutionState.BLOCKED
        assert "stale before-value" in outcome.note
        assert h.provider.write_call_count == 0

    def test_exe08_audit_unavailable_stops_write(self) -> None:
        h = Harness()
        intent = h.make_intent()
        h.audit.fail_next = True
        outcome = h.protocol.execute(intent, now=NOW)
        assert outcome.state is ExecutionState.BLOCKED
        assert "audit unavailable" in outcome.note
        assert h.provider.write_call_count == 0

    def test_expired_intent_never_submits(self) -> None:
        h = Harness()
        intent = h.make_intent()
        outcome = h.protocol.execute(intent, now=NOW + timedelta(hours=2))
        assert outcome.state is ExecutionState.EXPIRED
        assert h.provider.write_call_count == 0


class TestKillSwitch:
    def test_rt12_kill_epoch_change_blocks_before_submit(self) -> None:
        h = Harness()
        intent = h.make_intent()
        h.kill.activate(scope="GLOBAL", person_id="sec-1", reason="incident")
        outcome = h.protocol.execute(intent, now=NOW)
        assert outcome.state is ExecutionState.BLOCKED
        assert h.provider.write_call_count == 0

    def test_resume_requires_two_distinct_persons(self) -> None:
        kill = InMemoryKillStore()
        kill.activate(scope="GLOBAL", person_id="sec-1", reason="incident")
        with pytest.raises(KillResumeError):
            kill.resume(
                scope="GLOBAL",
                approver_a_person_id="sec-1",
                approver_b_person_id="sec-1",
                incident_ref="INC-1",
            )
        new_epoch = kill.resume(
            scope="GLOBAL",
            approver_a_person_id="ops-1",
            approver_b_person_id="sec-2",
            incident_ref="INC-1",
        )
        assert new_epoch == 2
        assert not kill.is_killed()

    def test_intents_issued_before_kill_stay_dead_after_resume(self) -> None:
        h = Harness()
        intent = h.make_intent()  # 绑定 epoch 0
        h.kill.activate(scope="GLOBAL", person_id="sec-1", reason="incident")
        h.kill.resume(
            scope="GLOBAL",
            approver_a_person_id="ops-1",
            approver_b_person_id="sec-2",
            incident_ref="INC-1",
        )
        # 恢复后 epoch=2 ≠ 签发时 0 → 旧 Intent 不会被补执行
        outcome = h.protocol.execute(intent, now=NOW)
        assert outcome.state is ExecutionState.BLOCKED
        assert h.provider.write_call_count == 0
