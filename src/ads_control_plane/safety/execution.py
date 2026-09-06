"""Execution 状态机与"平台最多一次 Provider Submit"不变量（AX-10 / AX-11 / AX-12）。

内存实现与未来 PostgreSQL 实现共享同一转换表；PG 版本额外用
CHECK (provider_submit_count <= 1) 与条件 UPDATE 强制同样语义。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum


class ExecutionState(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    READY = "READY"
    DISPATCHING = "DISPATCHING"
    SUBMITTING = "SUBMITTING"
    VERIFYING = "VERIFYING"
    DESIRED_STATE_OBSERVED = "DESIRED_STATE_OBSERVED"
    EXECUTION_ATTRIBUTION_CONFIRMED = "EXECUTION_ATTRIBUTION_CONFIRMED"
    NOT_APPLIED_CONFIRMED = "NOT_APPLIED_CONFIRMED"
    #: 被 Provider 明确拒绝且经合同测试证明业务层未触达（如限流 429/3001008）。
    #: 非终态：允许有界重投同一 Intent（辩证评审 ENG-03，防"限流杀死 Intent"死锁）。
    PROVIDER_REJECTED_NOT_EXECUTED = "PROVIDER_REJECTED_NOT_EXECUTED"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    CONFLICT = "CONFLICT"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    FAILED_BEFORE_SUBMIT = "FAILED_BEFORE_SUBMIT"
    BLOCKED = "BLOCKED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


# 注意：不存在通用 SUCCEEDED/FAILED。任何可能已到达 Provider 的异常都进 UNKNOWN。
# NOT_STARTED/READY 补 CANCELLED 出边；SUBMITTING 可进 PROVIDER_REJECTED_NOT_EXECUTED
# （评审 ENG-03：补齐 §13.1 缺失出边）。
_TRANSITIONS: dict[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.NOT_STARTED: frozenset(
        {ExecutionState.READY, ExecutionState.EXPIRED, ExecutionState.CANCELLED}
    ),
    ExecutionState.READY: frozenset(
        {
            ExecutionState.DISPATCHING,
            ExecutionState.BLOCKED,
            ExecutionState.EXPIRED,
            ExecutionState.CANCELLED,
        }
    ),
    ExecutionState.DISPATCHING: frozenset(
        {
            ExecutionState.SUBMITTING,
            ExecutionState.FAILED_BEFORE_SUBMIT,
            ExecutionState.BLOCKED,
            ExecutionState.EXPIRED,
            ExecutionState.CANCELLED,
        }
    ),
    ExecutionState.SUBMITTING: frozenset(
        {
            ExecutionState.VERIFYING,
            ExecutionState.UNKNOWN,
            ExecutionState.PROVIDER_REJECTED_NOT_EXECUTED,
        }
    ),
    ExecutionState.PROVIDER_REJECTED_NOT_EXECUTED: frozenset(
        {
            ExecutionState.DISPATCHING,
            ExecutionState.EXPIRED,
            ExecutionState.CANCELLED,
            ExecutionState.MANUAL_REVIEW,
        }
    ),
    ExecutionState.VERIFYING: frozenset(
        {
            ExecutionState.DESIRED_STATE_OBSERVED,
            ExecutionState.EXECUTION_ATTRIBUTION_CONFIRMED,
            ExecutionState.NOT_APPLIED_CONFIRMED,
            ExecutionState.UNKNOWN,
            ExecutionState.CONFLICT,
        }
    ),
    ExecutionState.DESIRED_STATE_OBSERVED: frozenset(
        {
            ExecutionState.EXECUTION_ATTRIBUTION_CONFIRMED,
            ExecutionState.RECONCILING,
            ExecutionState.MANUAL_REVIEW,
        }
    ),
    ExecutionState.UNKNOWN: frozenset({ExecutionState.RECONCILING}),
    ExecutionState.RECONCILING: frozenset(
        {
            ExecutionState.DESIRED_STATE_OBSERVED,
            ExecutionState.EXECUTION_ATTRIBUTION_CONFIRMED,
            ExecutionState.NOT_APPLIED_CONFIRMED,
            ExecutionState.CONFLICT,
            ExecutionState.MANUAL_REVIEW,
        }
    ),
    ExecutionState.EXECUTION_ATTRIBUTION_CONFIRMED: frozenset(),
    ExecutionState.NOT_APPLIED_CONFIRMED: frozenset(),
    ExecutionState.CONFLICT: frozenset({ExecutionState.MANUAL_REVIEW}),
    ExecutionState.MANUAL_REVIEW: frozenset(),  # 出口只有人工，宁可停在这里也不猜
    ExecutionState.FAILED_BEFORE_SUBMIT: frozenset(),
    ExecutionState.BLOCKED: frozenset(),
    ExecutionState.EXPIRED: frozenset(),
    ExecutionState.CANCELLED: frozenset(),
}


class IllegalExecutionTransition(Exception):
    pass


class DuplicateSubmitBlocked(Exception):
    """第二次进入网络提交被数据层拒绝——这是安全不变量在起作用，不是普通错误。"""


#: 被证明未执行的拒绝后，允许的最大重投次数（有界重投，评审 ENG-03）。
MAX_PROVEN_REJECTED_RETRIES = 3


@dataclass
class ExecutionRecord:
    execution_id: str
    intent_id: str
    state: ExecutionState = ExecutionState.NOT_STARTED
    submission_phase: str = "NOT_STARTED"  # NOT_STARTED | SUBMITTING | DONE
    provider_submit_count: int = 0
    #: 其中被 Provider 明确拒绝且证明业务层未触达的次数（限流等）。
    proven_rejected_count: int = 0
    job_delivery_count: int = 0
    kill_epoch_observed: int | None = None
    readback_note: str = ""

    @property
    def may_have_applied_submit_count(self) -> int:
        """可能已生效的提交次数——真正的安全不变量作用于它，必须 ≤ 1。"""
        return self.provider_submit_count - self.proven_rejected_count


class InMemoryExecutionStore:
    """单提交 CAS 的内存实现。与 PG 的条件 UPDATE 语义一一对应：

    UPDATE executions SET submission_phase='SUBMITTING', provider_submit_count=1, ...
    WHERE execution_id=? AND submission_phase='NOT_STARTED'
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, ExecutionRecord] = {}

    def create(self, record: ExecutionRecord) -> None:
        with self._lock:
            if record.execution_id in self._records:
                raise ValueError(f"duplicate execution_id {record.execution_id}")
            if any(r.intent_id == record.intent_id for r in self._records.values()):
                raise ValueError(f"intent {record.intent_id} already has an execution")
            self._records[record.execution_id] = record

    def get(self, execution_id: str) -> ExecutionRecord:
        with self._lock:
            return self._records[execution_id]

    def transition(self, execution_id: str, target: ExecutionState) -> ExecutionRecord:
        with self._lock:
            record = self._records[execution_id]
            allowed = _TRANSITIONS[record.state]
            if target not in allowed:
                raise IllegalExecutionTransition(
                    f"execution {execution_id}: {record.state} -> {target} is illegal"
                )
            record.state = target
            return record

    def record_delivery(self, execution_id: str) -> int:
        """队列重投计数：可以大于 1，与 provider_submit_count 是两件事。"""
        with self._lock:
            record = self._records[execution_id]
            record.job_delivery_count += 1
            return record.job_delivery_count

    def begin_submission(self, execution_id: str, current_kill_epoch: int) -> ExecutionRecord:
        """原子 CAS：只有 submission_phase == NOT_STARTED 的记录能进入网络栈。

        安全不变量的精确表述："可能已生效的提交 ≤ 1"。因此唯一的例外通道是
        record_proven_rejection() 把 submission_phase 复位（有界次数），
        其余任何状态的再次调用都被拒绝。
        """
        with self._lock:
            record = self._records[execution_id]
            if record.submission_phase != "NOT_STARTED":
                raise DuplicateSubmitBlocked(
                    f"execution {execution_id} already reached submission phase "
                    f"{record.submission_phase}; re-submit is forbidden"
                )
            if record.state is not ExecutionState.DISPATCHING:
                raise IllegalExecutionTransition(
                    f"begin_submission requires DISPATCHING, got {record.state}"
                )
            record.submission_phase = "SUBMITTING"
            record.provider_submit_count += 1
            record.kill_epoch_observed = current_kill_epoch
            record.state = ExecutionState.SUBMITTING
            return record

    def record_proven_rejection(self, execution_id: str) -> ExecutionRecord:
        """记录一次"证明未执行"的 Provider 拒绝（如限流），并允许有界重投。

        这是 submission_phase 复位的唯一合法通道；超出上限即转 MANUAL_REVIEW 语义
        由调用方处理（这里抛错）。
        """
        with self._lock:
            record = self._records[execution_id]
            if record.state is not ExecutionState.SUBMITTING:
                raise IllegalExecutionTransition(
                    f"proven rejection requires SUBMITTING, got {record.state}"
                )
            record.proven_rejected_count += 1
            record.state = ExecutionState.PROVIDER_REJECTED_NOT_EXECUTED
            if record.proven_rejected_count > MAX_PROVEN_REJECTED_RETRIES:
                # 超出预算：不复位 submission_phase（封死 CAS），由调用方转 MANUAL_REVIEW。
                raise DuplicateSubmitBlocked(
                    f"execution {execution_id} exceeded bounded retry budget "
                    f"({MAX_PROVEN_REJECTED_RETRIES}) for proven rejections"
                )
            record.submission_phase = "NOT_STARTED"
            return record

    def assert_invariants(self) -> None:
        """全局不变量：任何记录"可能已生效"的提交次数不得超过 1。"""
        with self._lock:
            for record in self._records.values():
                if record.may_have_applied_submit_count > 1:
                    raise AssertionError(
                        f"INVARIANT VIOLATED: {record.execution_id} has "
                        f"{record.may_have_applied_submit_count} possibly-applied submits"
                    )
