"""Provider Adapter 协议与错误分类（handoff §12 的 MVP 子集）。

Read Adapter 供同步/查询/对账使用；Write Adapter 只允许被隔离执行协议调用，
且 submit_once 语义上就是"至多调用一次网络提交"。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from ads_control_plane.canonical.entity import CanonicalEntityRef
from ads_control_plane.canonical.money import Money


class ProviderErrorClass(StrEnum):
    AUTH_DENIED = "AUTH_DENIED"
    NOT_FOUND = "NOT_FOUND"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    TRANSIENT_BEFORE_SUBMIT = "TRANSIENT_BEFORE_SUBMIT"
    TIMEOUT_MAY_HAVE_APPLIED = "TIMEOUT_MAY_HAVE_APPLIED"
    AMBIGUOUS_5XX = "AMBIGUOUS_5XX"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"


class ProviderCallError(Exception):
    def __init__(self, error_class: ProviderErrorClass, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class

    @property
    def may_have_applied(self) -> bool:
        """True 时请求可能已产生副作用：只允许只读对账，绝不重发。"""
        return self.error_class in (
            ProviderErrorClass.TIMEOUT_MAY_HAVE_APPLIED,
            ProviderErrorClass.AMBIGUOUS_5XX,
        )

    @property
    def proven_not_executed_retryable(self) -> bool:
        """限流类拒绝：Provider 明确拒收且业务层未触达（须经该 Provider 合同测试证明），
        同一载荷稍后重投有意义 → 走有界重投通道，不杀 Intent（评审 ENG-03）。
        """
        return self.error_class is ProviderErrorClass.RATE_LIMITED


@dataclass(frozen=True)
class FieldSnapshot:
    entity: CanonicalEntityRef
    field: str
    value: Money
    source: str  # 哪个读取通道观察到的


@dataclass(frozen=True)
class WriteCommand:
    entity: CanonicalEntityRef
    field: str
    absolute_target: Money


@dataclass(frozen=True)
class WriteReceipt:
    accepted: bool
    provider_message: str
    # Provider 若返回操作 ID，则归因确认（EXECUTION_ATTRIBUTION_CONFIRMED）才有依据。
    provider_operation_id: str | None = None


class ReadAdapter(Protocol):
    def read_field(self, entity: CanonicalEntityRef, field: str) -> FieldSnapshot: ...


class WriteAdapter(Protocol):
    def submit_once(self, command: WriteCommand) -> WriteReceipt: ...
