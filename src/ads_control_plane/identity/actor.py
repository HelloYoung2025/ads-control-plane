"""ActorContext：服务端签发的调用者上下文（安全公理 AX-02）。

客户端在工具参数/请求体里自报的 actor_id、role、organization 一律不可信；
本模型的实例只能由网关在验证凭据后构建。API 层必须丢弃请求中出现的同名字段。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from ads_control_plane.canonical.ids import CanonicalId


class PrincipalType(StrEnum):
    HUMAN = "HUMAN"
    AI_CLIENT = "AI_CLIENT"
    SERVICE_ACCOUNT = "SERVICE_ACCOUNT"


class Role(StrEnum):
    """MVP 角色目录（DEC-101：18 角色塌缩为 6，SoD 靠请求级冲突矩阵而非角色数量）。"""

    VIEWER = "VIEWER"
    ANALYST = "ANALYST"
    OPERATOR = "OPERATOR"
    APPROVER = "APPROVER"
    ADMIN = "ADMIN"
    AUDITOR = "AUDITOR"


class AuthenticationStrength(StrEnum):
    PASSWORD = "PASSWORD"
    MFA = "MFA"
    SERVICE_CREDENTIAL = "SERVICE_CREDENTIAL"


class ActorContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    principal_id: CanonicalId
    principal_type: PrincipalType
    organization_id: CanonicalId
    roles: frozenset[Role]
    # SoD 依据不可变的人（HR subject），不是登录名；HUMAN 必填。
    human_person_id: str | None = None
    # AI_CLIENT 必须记录背后的人类发起者（委托链），无人类发起者的 AI 调用只能是服务账户身份。
    human_initiator_person_id: str | None = None
    client_id: str
    session_id: str
    authentication_strength: AuthenticationStrength
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def _validate_identity_chain(self) -> ActorContext:
        if self.principal_type is PrincipalType.HUMAN and not self.human_person_id:
            raise ValueError("HUMAN principal requires human_person_id")
        if self.principal_type is PrincipalType.AI_CLIENT and not self.human_initiator_person_id:
            raise ValueError("AI_CLIENT requires human_initiator_person_id (delegation chain)")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        return self

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or datetime.now(UTC)) >= self.expires_at

    def acting_person_id(self) -> str | None:
        """用于 SoD 判定的自然人：HUMAN 是本人，AI_CLIENT 是委托人。"""
        if self.principal_type is PrincipalType.HUMAN:
            return self.human_person_id
        if self.principal_type is PrincipalType.AI_CLIENT:
            return self.human_initiator_person_id
        return None
