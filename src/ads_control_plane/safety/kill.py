"""Kill Switch（AX-13）：单调 Epoch 停写；恢复走独立协议，不是普通 toggle。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime


class KillResumeError(Exception):
    pass


@dataclass
class KillRecord:
    epoch: int
    scope: str  # "GLOBAL" 或 "connection:<id>" 等
    active: bool
    activated_by_person_id: str
    reason: str
    activated_at: datetime


@dataclass
class InMemoryKillStore:
    """内存实现，语义与未来 PG 表一致：epoch 全局单调递增，同一权威时钟。"""

    _lock: threading.Lock = field(default_factory=threading.Lock)
    _epoch: int = 0
    _records: list[KillRecord] = field(default_factory=list)

    def current_epoch(self) -> int:
        with self._lock:
            return self._epoch

    def is_killed(self, scope: str = "GLOBAL") -> bool:
        with self._lock:
            for record in reversed(self._records):
                if record.scope in ("GLOBAL", scope):
                    return record.active
            return False

    def activate(self, *, scope: str, person_id: str, reason: str) -> int:
        """任何有权限的单人可激活（停下永远比继续容易）。返回新 epoch。"""
        with self._lock:
            self._epoch += 1
            self._records.append(
                KillRecord(
                    epoch=self._epoch,
                    scope=scope,
                    active=True,
                    activated_by_person_id=person_id,
                    reason=reason,
                    activated_at=datetime.now(UTC),
                )
            )
            return self._epoch

    def resume(
        self,
        *,
        scope: str,
        approver_a_person_id: str,
        approver_b_person_id: str,
        incident_ref: str,
    ) -> int:
        """恢复需要两个不同自然人，且激活者不能单独解除；产生更高的 epoch。

        Kill 前签发的 Intent 绑定旧 epoch，恢复后自动全部失效（epoch 不匹配），
        不存在"恢复后补执行旧任务"。
        """
        with self._lock:
            last_active = next(
                (r for r in reversed(self._records) if r.scope in ("GLOBAL", scope) and r.active),
                None,
            )
            if last_active is None:
                raise KillResumeError("no active kill for scope")
            if approver_a_person_id == approver_b_person_id:
                raise KillResumeError("resume requires two distinct human persons")
            solo_activator = {approver_a_person_id, approver_b_person_id} == {
                last_active.activated_by_person_id
            }
            if solo_activator:
                raise KillResumeError("activator cannot solely resume")
            if not incident_ref:
                raise KillResumeError("resume requires incident reference")
            self._epoch += 1
            self._records.append(
                KillRecord(
                    epoch=self._epoch,
                    scope=scope,
                    active=False,
                    activated_by_person_id=approver_a_person_id,
                    reason=f"resume:{incident_ref}",
                    activated_at=datetime.now(UTC),
                )
            )
            return self._epoch
