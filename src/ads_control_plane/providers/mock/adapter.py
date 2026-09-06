"""Mock Provider：内存实体存储 + 故障注入。

Development/CI 的唯一 Provider（SECURITY.md 环境约束）。
故障注入覆盖 handoff §19.5 执行器故障场景；write_call_count 支撑 AX-17 的
"Shadow 写调用数必须为 0" 断言。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import StrEnum

from ads_control_plane.canonical.entity import CanonicalEntityRef
from ads_control_plane.canonical.money import Money
from ads_control_plane.providers.base import (
    FieldSnapshot,
    ProviderCallError,
    ProviderErrorClass,
    WriteCommand,
    WriteReceipt,
)


class FaultMode(StrEnum):
    NONE = "NONE"
    #: 请求从未发出（连接建立失败）——唯一可安全重调度的失败类型。
    FAIL_BEFORE_SUBMIT = "FAIL_BEFORE_SUBMIT"
    #: 远端已应用但响应丢失——必须进入 UNKNOWN。
    APPLY_THEN_DROP_RESPONSE = "APPLY_THEN_DROP_RESPONSE"
    #: 远端未应用且返回模糊 5xx——同样必须进入 UNKNOWN（无法与上一种区分）。
    REJECT_WITH_AMBIGUOUS_5XX = "REJECT_WITH_AMBIGUOUS_5XX"
    #: 明确校验拒绝（未应用）。
    VALIDATION_REJECT = "VALIDATION_REJECT"
    #: 限流拒绝（未应用，可稍后重投）——模拟领星令牌桶 3001008。
    RATE_LIMIT_REJECT = "RATE_LIMIT_REJECT"


@dataclass
class MockProvider:
    """同时实现 ReadAdapter 与 WriteAdapter（生产中两者是隔离部署的不同凭据）。"""

    _lock: threading.Lock = field(default_factory=threading.Lock)
    _values: dict[tuple[str, ...], Money] = field(default_factory=dict)
    next_write_fault: FaultMode = FaultMode.NONE
    write_call_count: int = 0
    read_call_count: int = 0
    #: 模拟 Provider 是否提供操作日志（领星"操作日志"API 的抽象）。
    supports_operation_log: bool = False

    @staticmethod
    def _key(entity: CanonicalEntityRef, field_name: str) -> tuple[str, ...]:
        return (*entity.uniqueness_key(), field_name)

    def seed(self, entity: CanonicalEntityRef, field_name: str, value: Money) -> None:
        with self._lock:
            self._values[self._key(entity, field_name)] = value

    def external_change(self, entity: CanonicalEntityRef, field_name: str, value: Money) -> None:
        """模拟平台之外的控制器（人工/规则）直接改值——不计入写调用。"""
        with self._lock:
            self._values[self._key(entity, field_name)] = value

    def current_value(self, entity: CanonicalEntityRef, field_name: str) -> Money:
        with self._lock:
            return self._values[self._key(entity, field_name)]

    # -- ReadAdapter --
    def read_field(self, entity: CanonicalEntityRef, field_name: str) -> FieldSnapshot:
        with self._lock:
            self.read_call_count += 1
            key = self._key(entity, field_name)
            if key not in self._values:
                raise ProviderCallError(ProviderErrorClass.NOT_FOUND, "entity/field not found")
            return FieldSnapshot(
                entity=entity, field=field_name, value=self._values[key], source="mock"
            )

    # -- WriteAdapter --
    def submit_once(self, command: WriteCommand) -> WriteReceipt:
        with self._lock:
            fault, self.next_write_fault = self.next_write_fault, FaultMode.NONE
            if fault is FaultMode.FAIL_BEFORE_SUBMIT:
                # 网络栈未获得请求：不计写调用，不改状态。
                raise ProviderCallError(
                    ProviderErrorClass.TRANSIENT_BEFORE_SUBMIT, "connect failed"
                )
            self.write_call_count += 1
            key = self._key(command.entity, command.field)
            if fault is FaultMode.VALIDATION_REJECT:
                raise ProviderCallError(ProviderErrorClass.VALIDATION_ERROR, "rejected")
            if fault is FaultMode.RATE_LIMIT_REJECT:
                raise ProviderCallError(ProviderErrorClass.RATE_LIMITED, "token bucket empty")
            if fault is FaultMode.REJECT_WITH_AMBIGUOUS_5XX:
                raise ProviderCallError(ProviderErrorClass.AMBIGUOUS_5XX, "internal error")
            self._values[key] = command.absolute_target
            if fault is FaultMode.APPLY_THEN_DROP_RESPONSE:
                raise ProviderCallError(
                    ProviderErrorClass.TIMEOUT_MAY_HAVE_APPLIED, "response lost"
                )
            operation_id = f"op-{self.write_call_count}" if self.supports_operation_log else None
            return WriteReceipt(
                accepted=True, provider_message="ok", provider_operation_id=operation_id
            )
