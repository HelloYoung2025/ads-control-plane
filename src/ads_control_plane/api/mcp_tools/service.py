"""Internal MCP 只读工具的业务实现（M1 骨架，协议无关、可直接单测）。

服务端授权原则（AX-02/03/16）：
- ActorContext 只来自已验证的 Token（此处由传输层注入），永不来自工具参数；
- 每个工具先过 EffectiveAllow；
- "不存在"与"无权访问"对外统一为 RESOURCE_UNAVAILABLE，不可区分。

MCP 传输壳（mcp SDK >=2.1,<3，目标协议 2026-07-28，双版本握手验收）在
server.py 组装；本模块保持零 mcp 依赖以便测试与复用（REST 共享同一实现）。
"""

from __future__ import annotations

from typing import Any

from ads_control_plane.authorization.engine import evaluate
from ads_control_plane.authorization.model import (
    AccessRequest,
    Action,
    ClientType,
    Environment,
    ExplicitDeny,
    Grant,
)
from ads_control_plane.identity.actor import ActorContext


class ToolDenied(Exception):
    """统一拒绝：错误码稳定，不泄露资源是否存在（AX-16）。

    detail 是可选的**可行动补充**——「哪个参数、允许范围是什么」这类话。它谈的是
    调用方自己的输入和我们公开的白名单边界，两者都不是资源，因此不与 AX-16 冲突；
    构造 detail 的规矩写在 api/errors.py，只透我们自己写的校验语句。
    """

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ReadToolService:
    def __init__(
        self,
        *,
        environment: Environment,
        grants: list[Grant],
        denies: list[ExplicitDeny],
    ) -> None:
        self._environment = environment
        self._grants = grants
        self._denies = denies

    def _authorize(self, actor: ActorContext, action: Action) -> None:
        decision = evaluate(
            actor,
            AccessRequest(
                environment=self._environment,
                organization_id=actor.organization_id,
                action=action,
                client_type=ClientType.MCP_AI,
            ),
            self._grants,
            self._denies,
        )
        if not decision.allowed:
            raise ToolDenied("AUTH_SCOPE_DENIED")

    def whoami(self, actor: ActorContext) -> dict[str, Any]:
        """无副作用；返回服务端认定的身份，供客户端自检——不接受任何输入字段。"""
        return {
            "principal_id": str(actor.principal_id),
            "principal_type": actor.principal_type.value,
            "organization_id": str(actor.organization_id),
            "roles": sorted(role.value for role in actor.roles),
            "client_id": actor.client_id,
            "authentication_strength": actor.authentication_strength.value,
            "expires_at": actor.expires_at.isoformat(),
        }

    def list_authorized_scopes(self, actor: ActorContext) -> dict[str, Any]:
        """只返回该主体可见的资源范围；他人范围既不出现也不可探测。

        自我描述类工具（whoami / 本工具）只要求已认证：它展示的就是授权本身，
        没有任何范围的主体得到空列表而不是错误。资源读取类工具才走 _authorize。
        """
        profiles: set[str] = set()
        for grant in self._grants:
            if grant.organization_id != actor.organization_id:
                continue
            if Action.RESOURCE_READ not in grant.actions:
                continue
            if ClientType.MCP_AI not in grant.client_types:
                continue
            if grant.profile_external_ids:
                profiles.update(grant.profile_external_ids)
        return {
            "organization_id": str(actor.organization_id),
            "profile_external_ids": sorted(profiles),
            "note": "scopes reflect server-side grants only; client claims are ignored",
        }
