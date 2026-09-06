"""执行协议：一次获批变更从 Intent 到回读的全部安全门（AX-09..AX-13 的组合）。

顺序固定：Kill 检查 → 审计预写 → 写前远端重读 → 单提交 CAS → 一次网络提交 →
立即回读 → 分级结论。任何模糊结果进 UNKNOWN 并冻结，只读对账，绝不重发。

生产形态里本模块运行在隔离执行器内；MVP 先以进程内组件证明状态与不变量。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from ads_control_plane.audit.ledger import AuditEvent, AuditUnavailable, InMemoryAuditLedger
from ads_control_plane.canonical.entity import CanonicalEntityRef
from ads_control_plane.canonical.money import Money
from ads_control_plane.providers.base import (
    ProviderCallError,
    ReadAdapter,
    WriteAdapter,
    WriteCommand,
)
from ads_control_plane.safety.execution import (
    DuplicateSubmitBlocked,
    ExecutionRecord,
    ExecutionState,
    InMemoryExecutionStore,
)
from ads_control_plane.safety.kill import InMemoryKillStore
from ads_write_executor.throttle import LocalThrottle


@dataclass(frozen=True)
class VerifiedIntent:
    """已通过授权/审批复核的执行意图（MVP 字段子集；签名验证在部署边界启用）。"""

    intent_id: str
    execution_id: str
    entity: CanonicalEntityRef
    field: str
    expected_before: Money
    absolute_target: Money
    proposal_hash: str
    approval_id: str
    kill_epoch_at_issue: int
    expires_at: datetime


class ExecutionOutcome:
    """execute() 的返回：终态 + 说明。没有布尔"成功"，只有分级结论。"""

    def __init__(self, state: ExecutionState, note: str) -> None:
        self.state = state
        self.note = note

    def __repr__(self) -> str:  # pragma: no cover
        return f"ExecutionOutcome({self.state}, {self.note!r})"


class ExecutionProtocol:
    def __init__(
        self,
        *,
        store: InMemoryExecutionStore,
        kill_store: InMemoryKillStore,
        audit: InMemoryAuditLedger,
        reader: ReadAdapter,
        writer: WriteAdapter,
        throttle: LocalThrottle | None = None,
    ) -> None:
        self._store = store
        self._kill = kill_store
        self._audit = audit
        self._reader = reader
        self._writer = writer
        self._throttle = throttle

    def _audit_event(self, intent: VerifiedIntent, event_type: str, decision: str) -> None:
        self._audit.append(
            AuditEvent(
                event_id=str(uuid.uuid4()),
                event_time=datetime.now(UTC),
                event_type=event_type,
                actor_principal_id="executor",
                actor_type="EXECUTOR",
                organization_id=str(intent.entity.organization_id),
                resource_path="/".join(intent.entity.uniqueness_key()),
                action=f"{intent.entity.entity_type}.{intent.field}.update",
                decision=decision,
                payload={"intent_id": intent.intent_id, "proposal_hash": intent.proposal_hash},
            )
        )

    def dispatch(self, intent: VerifiedIntent) -> ExecutionRecord:
        """队列投递入口：可以被重复调用（重投），但绝不会导致第二次可能生效的提交。"""
        record = self._store.get(intent.execution_id)
        self._store.record_delivery(intent.execution_id)
        if record.state is ExecutionState.NOT_STARTED:
            self._store.transition(intent.execution_id, ExecutionState.READY)
            self._store.transition(intent.execution_id, ExecutionState.DISPATCHING)
        elif record.state is ExecutionState.PROVIDER_REJECTED_NOT_EXECUTED:
            # 有界重投通道：仅当 submission_phase 已被合法复位。
            if record.submission_phase == "NOT_STARTED":
                self._store.transition(intent.execution_id, ExecutionState.DISPATCHING)
        return self._store.get(intent.execution_id)

    def execute(self, intent: VerifiedIntent, now: datetime | None = None) -> ExecutionOutcome:
        now = now or datetime.now(UTC)
        record = self.dispatch(intent)

        # 重投遇到已进入提交阶段的记录：只允许只读对账（AX-11）。
        if record.submission_phase != "NOT_STARTED":
            return self.reconcile(intent)

        # 门 0：Intent TTL。
        if now >= intent.expires_at:
            self._store.transition(intent.execution_id, ExecutionState.EXPIRED)
            return ExecutionOutcome(ExecutionState.EXPIRED, "intent expired before submit")

        # 门 1：Kill epoch —— Intent 签发后 epoch 变化即失效（AX-13）。
        current_epoch = self._kill.current_epoch()
        if current_epoch != intent.kill_epoch_at_issue or self._kill.is_killed():
            self._store.transition(intent.execution_id, ExecutionState.BLOCKED)
            return ExecutionOutcome(
                ExecutionState.BLOCKED,
                f"kill epoch moved {intent.kill_epoch_at_issue} -> {current_epoch}",
            )

        # 门 2：审计预写。不可用 → 停止生产写（AX-09）。
        try:
            self._audit_event(intent, "EXECUTION_PREWRITE", "PENDING_SUBMIT")
        except AuditUnavailable:
            self._store.transition(intent.execution_id, ExecutionState.BLOCKED)
            return ExecutionOutcome(
                ExecutionState.BLOCKED, "audit unavailable; production write stopped"
            )

        # 门 3：写前远端重读，当前值必须等于 expected_before（AX-06；RT-14/EXE-04）。
        try:
            snapshot = self._reader.read_field(intent.entity, intent.field)
        except ProviderCallError as exc:
            self._store.transition(intent.execution_id, ExecutionState.BLOCKED)
            return ExecutionOutcome(
                ExecutionState.BLOCKED, f"precondition read failed: {exc.error_class}"
            )
        if (
            snapshot.value.currency != intent.expected_before.currency
            or snapshot.value.amount != intent.expected_before.amount
        ):
            self._store.transition(intent.execution_id, ExecutionState.BLOCKED)
            self._audit_event(intent, "PRECONDITION_MISMATCH", "CONFLICT")
            return ExecutionOutcome(
                ExecutionState.BLOCKED,
                f"expected {intent.expected_before.amount}, observed {snapshot.value.amount}; "
                "stale before-value, re-proposal required",
            )

        # 门 3.5：本地桶感知节流（CAS 之前，评审 ENG-03）。拿不到令牌就保持
        # DISPATCHING 稍后重投——不消耗 Intent，也不撞 Provider 限流。
        throttle_key = f"{intent.entity.provider}:{intent.entity.provider_connection_id}:write"
        if self._throttle is not None and not self._throttle.try_acquire(throttle_key):
            return ExecutionOutcome(
                ExecutionState.DISPATCHING, "local throttle exhausted; redeliver later"
            )

        # 门 4：单提交 CAS（AX-10）。此后除"证明未执行的拒绝"外不允许重发。
        self._store.begin_submission(intent.execution_id, current_epoch)

        try:
            receipt = self._writer.submit_once(
                WriteCommand(
                    entity=intent.entity,
                    field=intent.field,
                    absolute_target=intent.absolute_target,
                )
            )
        except ProviderCallError as exc:
            if exc.may_have_applied:
                # 响应丢失/模糊 5xx：可能已生效 → UNKNOWN，冻结（AX-11）。
                self._store.transition(intent.execution_id, ExecutionState.UNKNOWN)
                self._audit_event(intent, "SUBMIT_AMBIGUOUS", "UNKNOWN")
                return ExecutionOutcome(
                    ExecutionState.UNKNOWN,
                    f"{exc.error_class}: may have applied; frozen, read-only reconcile only",
                )
            if exc.proven_not_executed_retryable:
                # 限流类拒绝：证明未执行 → 有界重投同一 Intent（不杀 Proposal/Approval）。
                try:
                    self._store.record_proven_rejection(intent.execution_id)
                except DuplicateSubmitBlocked:
                    self._store.transition(intent.execution_id, ExecutionState.MANUAL_REVIEW)
                    self._audit_event(intent, "RETRY_BUDGET_EXHAUSTED", "MANUAL_REVIEW")
                    return ExecutionOutcome(
                        ExecutionState.MANUAL_REVIEW, "bounded retry budget exhausted"
                    )
                self._audit_event(intent, "SUBMIT_THROTTLED_BY_PROVIDER", "RETRYABLE")
                return ExecutionOutcome(
                    ExecutionState.PROVIDER_REJECTED_NOT_EXECUTED,
                    f"{exc.error_class}: proven not executed; bounded redelivery allowed",
                )
            # 明确未应用（校验拒绝等）：同样载荷重放没有意义——
            # CAS 已消费，本 Intent 终结，修正需要新的 Proposal。
            self._store.transition(intent.execution_id, ExecutionState.VERIFYING)
            self._store.transition(intent.execution_id, ExecutionState.NOT_APPLIED_CONFIRMED)
            self._audit_event(intent, "SUBMIT_REJECTED", "NOT_APPLIED_CONFIRMED")
            return ExecutionOutcome(
                ExecutionState.NOT_APPLIED_CONFIRMED, f"provider rejected: {exc.error_class}"
            )

        # 提交已发出：进入验证。
        self._store.transition(intent.execution_id, ExecutionState.VERIFYING)
        self._audit_event(intent, "SUBMIT_ACCEPTED", str(receipt.accepted))
        return self._verify(intent, receipt.provider_operation_id)

    def _verify(self, intent: VerifiedIntent, operation_id: str | None) -> ExecutionOutcome:
        try:
            snapshot = self._reader.read_field(intent.entity, intent.field)
        except ProviderCallError:
            self._store.transition(intent.execution_id, ExecutionState.UNKNOWN)
            return ExecutionOutcome(ExecutionState.UNKNOWN, "readback unavailable; frozen")
        if snapshot.value.amount == intent.absolute_target.amount:
            if operation_id is not None:
                # 有不可歧义的 Provider 操作证据才能归因确认（AX-12）。
                self._store.transition(intent.execution_id, ExecutionState.DESIRED_STATE_OBSERVED)
                self._store.transition(
                    intent.execution_id, ExecutionState.EXECUTION_ATTRIBUTION_CONFIRMED
                )
                return ExecutionOutcome(
                    ExecutionState.EXECUTION_ATTRIBUTION_CONFIRMED,
                    f"target observed and attributed via {operation_id}",
                )
            self._store.transition(intent.execution_id, ExecutionState.DESIRED_STATE_OBSERVED)
            return ExecutionOutcome(
                ExecutionState.DESIRED_STATE_OBSERVED,
                "target value observed; attribution NOT proven (no provider operation id)",
            )
        if snapshot.value.amount == intent.expected_before.amount:
            # 仍是旧值：异步窗口内不能断言未应用。
            self._store.transition(intent.execution_id, ExecutionState.UNKNOWN)
            return ExecutionOutcome(
                ExecutionState.UNKNOWN, "old value still observed; ambiguous within async window"
            )
        # 第三个值：其他控制器在竞争 → 人工。
        self._store.transition(intent.execution_id, ExecutionState.CONFLICT)
        self._store.transition(intent.execution_id, ExecutionState.MANUAL_REVIEW)
        return ExecutionOutcome(
            ExecutionState.MANUAL_REVIEW,
            f"third value {snapshot.value.amount} observed; external controller suspected",
        )

    def reconcile(self, intent: VerifiedIntent) -> ExecutionOutcome:
        """UNKNOWN 的唯一出路：只读对账。永不调用 Write Adapter（AX-11）。"""
        record = self._store.get(intent.execution_id)
        if record.state not in (
            ExecutionState.UNKNOWN,
            ExecutionState.RECONCILING,
            ExecutionState.SUBMITTING,
            ExecutionState.VERIFYING,
        ):
            return ExecutionOutcome(record.state, "nothing to reconcile")
        if record.state is not ExecutionState.RECONCILING:
            if record.state is ExecutionState.SUBMITTING:
                # worker 崩溃后重投：可能已发出 → 视为 UNKNOWN。
                self._store.transition(intent.execution_id, ExecutionState.UNKNOWN)
            if self._store.get(intent.execution_id).state is ExecutionState.VERIFYING:
                self._store.transition(intent.execution_id, ExecutionState.UNKNOWN)
            self._store.transition(intent.execution_id, ExecutionState.RECONCILING)
        try:
            snapshot = self._reader.read_field(intent.entity, intent.field)
        except ProviderCallError:
            return ExecutionOutcome(ExecutionState.RECONCILING, "authoritative read unavailable")
        if snapshot.value.amount == intent.absolute_target.amount:
            self._store.transition(intent.execution_id, ExecutionState.DESIRED_STATE_OBSERVED)
            return ExecutionOutcome(
                ExecutionState.DESIRED_STATE_OBSERVED,
                "reconcile observed target value; attribution NOT proven",
            )
        self._store.transition(intent.execution_id, ExecutionState.MANUAL_REVIEW)
        return ExecutionOutcome(
            ExecutionState.MANUAL_REVIEW,
            f"reconcile observed {snapshot.value.amount}; human review required",
        )
