"""Capability Registry（handoff §10.7）：Provider 工具能力的证据状态机。

现实动机（2026-08-28 已实证）：handoff 会话观察到领星 MCP 有 ~15 个广告写工具，
而当前官方公开文档显示广告工具全部只读——Provider 工具面确实会漂移。
因此：新工具默认 DISCOVERED（禁用）；Schema Hash 变化立即冻结对应能力（RT-17/RT-29）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum


class CapabilityState(StrEnum):
    DISCOVERED = "DISCOVERED"
    CONTRACT_VERIFIED = "CONTRACT_VERIFIED"
    READ_TESTED = "READ_TESTED"
    WRITE_TESTED = "WRITE_TESTED"
    CANARY_APPROVED = "CANARY_APPROVED"
    ACTIVE = "ACTIVE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    DISABLED = "DISABLED"


#: 晋级必须逐级、显式；任何状态都可以被打回 REVIEW_REQUIRED/DISABLED。
_PROMOTION_ORDER = [
    CapabilityState.DISCOVERED,
    CapabilityState.CONTRACT_VERIFIED,
    CapabilityState.READ_TESTED,
    CapabilityState.WRITE_TESTED,
    CapabilityState.CANARY_APPROVED,
    CapabilityState.ACTIVE,
]

#: 允许用于生产写路径的最低状态：只有 ACTIVE。
_WRITE_USABLE = frozenset({CapabilityState.ACTIVE})


class CapabilityError(Exception):
    pass


@dataclass(frozen=True)
class CapabilityRecord:
    provider: str
    provider_connection_id: str
    tool_name: str
    tool_schema_hash: str
    state: CapabilityState
    observed_at: datetime
    verified_by: str | None = None
    disabled_reason: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.provider, self.provider_connection_id, self.tool_name)


class CapabilityRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str, str], CapabilityRecord] = {}

    def observe_discovery(
        self,
        *,
        provider: str,
        connection_id: str,
        tool_name: str,
        schema_hash: str,
        observed_at: datetime,
    ) -> CapabilityRecord:
        """工具发现快照进入登记。

        - 新工具 → DISCOVERED（默认禁用，出现在 Tool List 不是功能发布）；
        - 已知工具且 hash 不变 → 保持现状；
        - 已知工具但 hash 变化 → 立即降级 REVIEW_REQUIRED（写能力冻结）。
        """
        key = (provider, connection_id, tool_name)
        with self._lock:
            existing = self._records.get(key)
            if existing is None:
                record = CapabilityRecord(
                    provider=provider,
                    provider_connection_id=connection_id,
                    tool_name=tool_name,
                    tool_schema_hash=schema_hash,
                    state=CapabilityState.DISCOVERED,
                    observed_at=observed_at,
                )
            elif existing.tool_schema_hash != schema_hash:
                record = replace(
                    existing,
                    tool_schema_hash=schema_hash,
                    state=CapabilityState.REVIEW_REQUIRED,
                    observed_at=observed_at,
                    disabled_reason=(
                        f"schema drift: {existing.tool_schema_hash[:12]} -> {schema_hash[:12]}"
                    ),
                )
            else:
                record = replace(existing, observed_at=observed_at)
            self._records[key] = record
            return record

    def observe_disappearance(
        self, *, provider: str, connection_id: str, tool_name: str, observed_at: datetime
    ) -> CapabilityRecord:
        """工具从目录消失：同样是供应链/权限变更事件，冻结而不是删除记录。"""
        key = (provider, connection_id, tool_name)
        with self._lock:
            existing = self._records.get(key)
            if existing is None:
                raise CapabilityError(f"unknown capability {key}")
            record = replace(
                existing,
                state=CapabilityState.REVIEW_REQUIRED,
                observed_at=observed_at,
                disabled_reason="tool disappeared from provider catalog",
            )
            self._records[key] = record
            return record

    def promote(
        self, *, provider: str, connection_id: str, tool_name: str, verified_by: str
    ) -> CapabilityRecord:
        """显式逐级晋级。跳级、对 REVIEW_REQUIRED/DISABLED 晋级都被拒绝。"""
        key = (provider, connection_id, tool_name)
        with self._lock:
            existing = self._records.get(key)
            if existing is None:
                raise CapabilityError(f"unknown capability {key}")
            if existing.state not in _PROMOTION_ORDER:
                raise CapabilityError(
                    f"cannot promote from {existing.state}; resolve review/disable first"
                )
            index = _PROMOTION_ORDER.index(existing.state)
            if index + 1 >= len(_PROMOTION_ORDER):
                raise CapabilityError("already ACTIVE")
            record = replace(existing, state=_PROMOTION_ORDER[index + 1], verified_by=verified_by)
            self._records[key] = record
            return record

    def assert_write_usable(
        self, *, provider: str, connection_id: str, tool_name: str, schema_hash: str
    ) -> None:
        """写路径的运行时检查：状态必须 ACTIVE 且 hash 与登记一致，否则 fail closed。"""
        key = (provider, connection_id, tool_name)
        with self._lock:
            record = self._records.get(key)
        if record is None:
            raise CapabilityError(f"capability {key} never registered; write denied")
        if record.state not in _WRITE_USABLE:
            raise CapabilityError(f"capability {key} in state {record.state}; write denied")
        if record.tool_schema_hash != schema_hash:
            raise CapabilityError(f"capability {key} schema hash mismatch at call time")
