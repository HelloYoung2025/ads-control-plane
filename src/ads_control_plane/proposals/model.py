"""Proposal 聚合（AX-06 / AX-07）。

生产写的唯一入口形态：绝对目标值 + expected_before + 冻结 Hash。
"相对加 0.01"只允许出现在人类解释文本里，永不进入执行链路。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from ads_control_plane.canonical.entity import CanonicalEntityRef
from ads_control_plane.canonical.ids import CanonicalId
from ads_control_plane.canonical.money import Money

#: Hash 规范化方式版本。变更序列化规则必须提升此版本，旧 Hash 不做跨版本比较。
CANONICALIZATION_VERSION = "sha256-jsonc1"


class ProposalState(StrEnum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVAL_SATISFIED = "APPROVAL_SATISFIED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


_LEGAL_TRANSITIONS: dict[ProposalState, frozenset[ProposalState]] = {
    ProposalState.DRAFT: frozenset({ProposalState.VALIDATED, ProposalState.CANCELLED}),
    ProposalState.VALIDATED: frozenset(
        {ProposalState.PENDING_APPROVAL, ProposalState.CANCELLED, ProposalState.EXPIRED}
    ),
    ProposalState.PENDING_APPROVAL: frozenset(
        {
            ProposalState.APPROVAL_SATISFIED,
            ProposalState.REJECTED,
            ProposalState.EXPIRED,
            ProposalState.CANCELLED,
        }
    ),
    ProposalState.APPROVAL_SATISFIED: frozenset({ProposalState.EXPIRED, ProposalState.CANCELLED}),
    ProposalState.REJECTED: frozenset(),
    ProposalState.EXPIRED: frozenset(),
    ProposalState.CANCELLED: frozenset(),
}


class IllegalTransition(Exception):
    pass


def assert_legal_transition(current: ProposalState, target: ProposalState) -> None:
    if target not in _LEGAL_TRANSITIONS[current]:
        raise IllegalTransition(f"proposal transition {current} -> {target} is illegal")


class ObservedValue(BaseModel):
    """写前观察值：带快照引用与观察时间，供写前重读比对。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: Money
    snapshot_id: CanonicalId
    observed_at: datetime


class ChangeItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    change_item_id: CanonicalId
    entity: CanonicalEntityRef
    field: str
    expected_before: ObservedValue
    absolute_target: Money
    risk_tier: str = "R2"

    @model_validator(mode="after")
    def _same_currency(self) -> ChangeItem:
        if self.expected_before.value.currency != self.absolute_target.currency:
            raise ValueError(
                "expected_before and absolute_target must share one currency; "
                "cross-currency changes are a separate, denied action"
            )
        return self


class Proposal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: CanonicalId
    organization_id: CanonicalId
    state: ProposalState
    created_by_person_id: str | None
    created_by_client_id: str
    source: str  # "AI" | "HUMAN"
    change_items: tuple[ChangeItem, ...]
    valid_until: datetime
    # 冻结后填充；DRAFT 阶段为 None。
    proposal_hash: str | None = None

    @model_validator(mode="after")
    def _non_empty_items(self) -> Proposal:
        if not self.change_items:
            raise ValueError("proposal must contain at least one change item")
        return self

    def compute_hash(self) -> str:
        """对规范排序后的完整 change_items 计算内容 Hash（单对象也是一项数组）。"""
        items: list[dict[str, Any]] = [item.model_dump(mode="json") for item in self.change_items]
        items.sort(key=lambda d: str(d["change_item_id"]))
        payload = {
            "canonicalization": CANONICALIZATION_VERSION,
            "organization_id": str(self.organization_id),
            "change_items": items,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def frozen_copy(self, target_state: ProposalState) -> Proposal:
        """提交冻结：进入审批前固化 Hash。之后任何内容变化都会使 Hash 不匹配。"""
        assert_legal_transition(self.state, target_state)
        return self.model_copy(update={"state": target_state, "proposal_hash": self.compute_hash()})

    def with_state(self, target_state: ProposalState) -> Proposal:
        assert_legal_transition(self.state, target_state)
        return self.model_copy(update={"state": target_state})
