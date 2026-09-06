"""追加式审计账本（AX-09）：Provider 提交前必须预写成功；账本不可用 → 停写。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class AuditUnavailable(Exception):
    """审计不可用。调用方唯一正确的处理是放弃本次生产写（fail closed）。"""


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    event_time: datetime
    event_type: str
    actor_principal_id: str
    actor_type: str
    organization_id: str
    resource_path: str
    action: str
    decision: str
    payload: dict[str, Any] = field(default_factory=dict)


class InMemoryAuditLedger:
    """内存追加账本。fail_next 用于故障注入（测试 EXE-08：审计不可用即停写）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[AuditEvent] = []
        self.fail_next: bool = False

    def append(self, event: AuditEvent) -> None:
        with self._lock:
            if self.fail_next:
                self.fail_next = False
                raise AuditUnavailable("audit ledger write failed")
            self._events.append(event)

    def events(self) -> tuple[AuditEvent, ...]:
        with self._lock:
            return tuple(self._events)

    # 故意不提供 update/delete：append-only 是接口层的承诺，不只是约定。
