"""EffectiveAllow 判定引擎（AX-03/AX-04）。

各授权层的 AND：任一层 Deny 或 Unknown → 拒绝（fail closed）；Explicit Deny 永远优先；
必须存在单一 Grant 独立匹配完整请求元组，禁止跨 Grant 拼接。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from ads_control_plane.authorization.model import (
    WRITE_ACTION_ALLOWLIST,
    AccessRequest,
    Action,
    ClientType,
    Decision,
    DecisionOutcome,
    ExplicitDeny,
    Grant,
    ReasonCode,
)
from ads_control_plane.identity.actor import ActorContext, PrincipalType, Role

#: 角色 → 可执行动作。写动作对角色的要求：Operator 提交执行类，Approver 审批。
_ROLE_ACTIONS: dict[Role, frozenset[Action]] = {
    Role.VIEWER: frozenset({Action.RESOURCE_READ, Action.METRICS_READ}),
    Role.ANALYST: frozenset(
        {
            Action.RESOURCE_READ,
            Action.METRICS_READ,
            Action.PROPOSAL_CREATE_DRAFT,
            Action.PROPOSAL_CANCEL,
        }
    ),
    Role.OPERATOR: frozenset(
        {
            Action.RESOURCE_READ,
            Action.METRICS_READ,
            Action.PROPOSAL_CREATE_DRAFT,
            Action.PROPOSAL_SUBMIT,
            Action.PROPOSAL_CANCEL,
            Action.EXECUTION_VIEW,
            Action.EXECUTION_RECONCILE,
            Action.CAMPAIGN_DAILY_BUDGET_UPDATE,
            Action.TARGET_BID_UPDATE,
        }
    ),
    Role.APPROVER: frozenset(
        {Action.RESOURCE_READ, Action.METRICS_READ, Action.APPROVAL_VIEW, Action.APPROVAL_DECIDE}
    ),
    Role.ADMIN: frozenset(
        {Action.RESOURCE_READ, Action.METRICS_READ, Action.EXECUTION_VIEW, Action.APPROVAL_VIEW}
    ),
    Role.AUDITOR: frozenset(
        {Action.RESOURCE_READ, Action.METRICS_READ, Action.EXECUTION_VIEW, Action.APPROVAL_VIEW}
    ),
}

#: AI 客户端可执行的动作上限（AX-05：无审批、无提交、无执行类动作）。
_AI_CLIENT_ACTION_CEILING: frozenset[Action] = frozenset(
    {
        Action.RESOURCE_READ,
        Action.METRICS_READ,
        Action.PROPOSAL_CREATE_DRAFT,
        Action.EXECUTION_VIEW,
    }
)


class LayerStatus(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    UNKNOWN = "UNKNOWN"


class RuntimeLayer(Protocol):
    """运行时前置条件层（kill switch、审计健康等）。UNKNOWN 与 DENY 同效（fail closed）。"""

    def check(self, request: AccessRequest) -> tuple[LayerStatus, ReasonCode]: ...


def evaluate(
    actor: ActorContext,
    request: AccessRequest,
    grants: list[Grant],
    denies: list[ExplicitDeny],
    runtime_layers: list[RuntimeLayer] | None = None,
    now: datetime | None = None,
) -> Decision:
    def deny(*codes: ReasonCode, deny_id: object = None) -> Decision:
        return Decision(
            outcome=DecisionOutcome.DENY,
            reason_codes=tuple(codes),
            matched_deny_id=deny_id,  # type: ignore[arg-type]
        )

    # 1. 主体有效
    if actor.is_expired(now):
        return deny(ReasonCode.ACTOR_EXPIRED)
    # 2. 组织一致（跨组织请求直接拒绝，不进入资源解析）
    if actor.organization_id != request.organization_id:
        return deny(ReasonCode.ORG_MISMATCH)
    # 3. Explicit Deny 优先于一切 Allow
    for d in denies:
        if d.matches(request):
            return deny(ReasonCode.EXPLICIT_DENY, deny_id=d.deny_id)
    # 4. AI 客户端动作上限（AX-05），先于角色判定
    if actor.principal_type is PrincipalType.AI_CLIENT:
        if request.action not in _AI_CLIENT_ACTION_CEILING:
            return deny(ReasonCode.CLIENT_TYPE_FORBIDDEN)
        if request.client_type is not ClientType.MCP_AI:
            return deny(ReasonCode.CLIENT_TYPE_FORBIDDEN)
    # 5. 角色允许该动作
    if not any(request.action in _ROLE_ACTIONS.get(role, frozenset()) for role in actor.roles):
        return deny(ReasonCode.ROLE_INSUFFICIENT)
    # 6. 写动作必须在 (实体类型, 字段) 白名单内
    if request.action in WRITE_ACTION_ALLOWLIST:
        expected_entity_type, expected_field = WRITE_ACTION_ALLOWLIST[request.action]
        if request.entity is None or request.entity.entity_type is not expected_entity_type:
            return deny(ReasonCode.ACTION_NOT_ALLOWLISTED)
        if request.field != expected_field:
            return deny(ReasonCode.FIELD_MISMATCH)
    # 7. 单一 Grant 独立匹配完整元组（禁止拼接）
    matched = next((g for g in grants if g.matches(request)), None)
    if matched is None:
        return deny(ReasonCode.NO_MATCHING_GRANT)
    # 8. 运行时层：任何 UNKNOWN/DENY → 拒绝
    for layer in runtime_layers or []:
        status, code = layer.check(request)
        if status is LayerStatus.DENY:
            return deny(code)
        if status is LayerStatus.UNKNOWN:
            return deny(ReasonCode.LAYER_UNKNOWN, code)

    return Decision(
        outcome=DecisionOutcome.ALLOW,
        reason_codes=(ReasonCode.ALLOWED,),
        matched_grant_id=matched.grant_id,
    )
