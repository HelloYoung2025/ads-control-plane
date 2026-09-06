"""授权模型：完整请求元组、单一 Grant 独立匹配（AX-03 / AX-04）。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from ads_control_plane.canonical.entity import CanonicalEntityRef, EntityType
from ads_control_plane.canonical.ids import CanonicalId
from ads_control_plane.canonical.money import Money


class Action(StrEnum):
    """原子动作词表（MVP 子集；禁止笼统的 ads.write）。"""

    RESOURCE_READ = "resource.read"
    METRICS_READ = "metrics.read"
    PROPOSAL_CREATE_DRAFT = "proposal.create_draft"
    PROPOSAL_SUBMIT = "proposal.submit"
    PROPOSAL_CANCEL = "proposal.cancel"
    APPROVAL_VIEW = "approval.view"
    APPROVAL_DECIDE = "approval.decide"
    EXECUTION_VIEW = "execution.view"
    EXECUTION_RECONCILE = "execution.reconcile"
    CAMPAIGN_DAILY_BUDGET_UPDATE = "campaign.daily_budget.update"
    TARGET_BID_UPDATE = "target.bid.update"


#: MVP 允许进入 Proposal 的写动作 → (实体类型, 字段) 白名单。其余一律 Initial-Deny。
WRITE_ACTION_ALLOWLIST: dict[Action, tuple[EntityType, str]] = {
    Action.CAMPAIGN_DAILY_BUDGET_UPDATE: (EntityType.CAMPAIGN, "daily_budget"),
    Action.TARGET_BID_UPDATE: (EntityType.TARGET, "bid"),
}


class Environment(StrEnum):
    DEVELOPMENT = "DEVELOPMENT"
    CI = "CI"
    STAGING = "STAGING"
    PRODUCTION = "PRODUCTION"


class ClientType(StrEnum):
    WEB = "WEB"
    MCP_AI = "MCP_AI"
    API = "API"
    INTERNAL_JOB = "INTERNAL_JOB"


class AccessRequest(BaseModel):
    """一次授权判定的完整请求元组。缺维度即无法匹配（fail closed），不做部分匹配。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: Environment
    organization_id: CanonicalId
    action: Action
    client_type: ClientType
    entity: CanonicalEntityRef | None = None
    field: str | None = None
    target_value: Money | None = None
    expected_before: Money | None = None


class Grant(BaseModel):
    """一条授权。单一 Grant 必须独立匹配整个请求元组（AX-04：禁止跨 Grant 拼接）。

    维度为 None 表示"该 Grant 不限制此维度"，但 action 与 organization 永远必须显式。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    grant_id: CanonicalId
    organization_id: CanonicalId
    environments: frozenset[Environment]
    actions: frozenset[Action]
    client_types: frozenset[ClientType]
    # 资源范围：None = 不限制该维度；写动作的 Grant 建议全部显式。
    marketplaces: frozenset[str] | None = None
    profile_external_ids: frozenset[str] | None = None
    entity_types: frozenset[EntityType] | None = None
    fields: frozenset[str] | None = None
    # 数值边界（写动作）：绝对值上限与单次变化上限，币种必须一致。
    max_absolute_value: Money | None = None
    max_single_delta: Money | None = None

    def matches(self, request: AccessRequest) -> bool:
        if request.organization_id != self.organization_id:
            return False
        if request.environment not in self.environments:
            return False
        if request.action not in self.actions:
            return False
        if request.client_type not in self.client_types:
            return False
        if self.marketplaces is not None and (
            request.entity is None or request.entity.marketplace not in self.marketplaces
        ):
            return False
        if self.profile_external_ids is not None and (
            request.entity is None
            or request.entity.profile_external_id not in self.profile_external_ids
        ):
            return False
        if self.entity_types is not None and (
            request.entity is None or request.entity.entity_type not in self.entity_types
        ):
            return False
        if self.fields is not None and (request.field is None or request.field not in self.fields):
            return False
        if self.max_absolute_value is not None:
            if request.target_value is None:
                return False
            if request.target_value.currency != self.max_absolute_value.currency:
                return False
            if request.target_value.amount > self.max_absolute_value.amount:
                return False
        if self.max_single_delta is not None:
            if request.target_value is None or request.expected_before is None:
                return False
            if request.expected_before.currency != self.max_single_delta.currency:
                return False
            delta = request.target_value.abs_delta(request.expected_before)
            if delta.amount > self.max_single_delta.amount:
                return False
        return True


class ExplicitDeny(BaseModel):
    """显式拒绝：匹配即拒，优先于任何 Allow。维度语义同 Grant，但匹配是"命中即拒"。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    deny_id: CanonicalId
    organization_id: CanonicalId
    actions: frozenset[Action] | None = None
    profile_external_ids: frozenset[str] | None = None
    entity_external_ids: frozenset[str] | None = None
    reason: str = ""

    def matches(self, request: AccessRequest) -> bool:
        if request.organization_id != self.organization_id:
            return False
        if self.actions is not None and request.action not in self.actions:
            return False
        if self.profile_external_ids is not None:
            if request.entity is None:
                return False
            if request.entity.profile_external_id not in self.profile_external_ids:
                return False
        if self.entity_external_ids is not None:
            if request.entity is None:
                return False
            if request.entity.entity_external_id not in self.entity_external_ids:
                return False
        return True


class DecisionOutcome(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class ReasonCode(StrEnum):
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    ACTOR_EXPIRED = "ACTOR_EXPIRED"
    ORG_MISMATCH = "ORG_MISMATCH"
    EXPLICIT_DENY = "EXPLICIT_DENY"
    NO_MATCHING_GRANT = "NO_MATCHING_GRANT"
    ROLE_INSUFFICIENT = "ROLE_INSUFFICIENT"
    ACTION_NOT_ALLOWLISTED = "ACTION_NOT_ALLOWLISTED"
    FIELD_MISMATCH = "FIELD_MISMATCH"
    SOD_VIOLATION = "SOD_VIOLATION"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    AUDIT_UNAVAILABLE = "AUDIT_UNAVAILABLE"
    LAYER_UNKNOWN = "LAYER_UNKNOWN"
    CLIENT_TYPE_FORBIDDEN = "CLIENT_TYPE_FORBIDDEN"
    ALLOWED = "ALLOWED"


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: DecisionOutcome
    reason_codes: tuple[ReasonCode, ...]
    matched_grant_id: CanonicalId | None = None
    matched_deny_id: CanonicalId | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome is DecisionOutcome.ALLOW
