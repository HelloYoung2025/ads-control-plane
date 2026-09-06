"""审批（AX-07 / AX-08）：审批绑定 Proposal Hash，内容变化即失效；审批人不能编辑。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from ads_control_plane.canonical.ids import CanonicalId
from ads_control_plane.proposals.model import Proposal


class ApprovalOutcome(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    approval_id: CanonicalId
    proposal_id: CanonicalId
    proposal_hash: str
    approver_principal_id: CanonicalId
    approver_person_id: str
    outcome: ApprovalOutcome
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    comment: str = ""


class ApprovalInvalid(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def assert_approval_valid(approval: ApprovalDecision, proposal: Proposal, now: datetime) -> None:
    """执行前的审批有效性复核。任何一条不满足即拒绝，不得沿用旧审批。"""
    if approval.outcome is not ApprovalOutcome.APPROVED:
        raise ApprovalInvalid("NOT_APPROVED", "decision is not an approval")
    if approval.proposal_id != proposal.proposal_id:
        raise ApprovalInvalid("PROPOSAL_MISMATCH", "approval bound to a different proposal")
    if proposal.proposal_hash is None:
        raise ApprovalInvalid("PROPOSAL_NOT_FROZEN", "proposal was never frozen")
    if approval.proposal_hash != proposal.proposal_hash:
        raise ApprovalInvalid(
            "HASH_MISMATCH",
            "proposal content changed after approval; re-approval required",
        )
    if approval.proposal_hash != proposal.compute_hash():
        raise ApprovalInvalid(
            "CONTENT_DRIFT",
            "stored hash does not match recomputed content; tampering suspected",
        )
    if approval.revoked_at is not None and approval.revoked_at <= now:
        raise ApprovalInvalid("REVOKED", "approval was revoked")
    if now >= approval.expires_at:
        raise ApprovalInvalid("APPROVAL_EXPIRED", "approval TTL elapsed; re-approval required")
    if now >= proposal.valid_until:
        raise ApprovalInvalid("PROPOSAL_EXPIRED", "proposal TTL elapsed")
