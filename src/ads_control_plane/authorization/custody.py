"""对象控制权（custody）——"同一广告对象同一时刻只有一个控制器"（DEC-119/121）。

2026-08-28 Owner 需求：人类操作时 AI 不能操作，AI 操作时人类不能操作。
诚实的物理边界：人在领星/Amazon 后台的操作平台无法实时阻止（那不是我们的
界面），只能经操作日志滞后检测。因此互斥被实现为一个不对称的状态机：

- 对 AI 的约束是**硬的**：每次 AI 动作前查 custody，非 AI_MANAGED 即拒（fail-closed）。
- 对人的约束是**软的**：平台界面上 AI 托管对象要求先接管；带外人工变更被
  日志检测到后，对象自动让位给人（HUMAN_PRIORITY 冷却期），AI 停手。

控制权四层优先级（高者胜）：
    TOOL_MANAGED（领星策略托管，DEC-119）> HUMAN_PRIORITY > AI_MANAGED > UNMANAGED
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from ads_control_plane.identity.actor import ActorContext, PrincipalType

#: 检测到带外人工变更后，AI 对该对象停手的默认冷却时长（DEC-121 待 Owner 定值）。
DEFAULT_HUMAN_PRIORITY_COOLDOWN = timedelta(hours=72)


class CustodyState(StrEnum):
    UNMANAGED = "UNMANAGED"  # 无主：AI 可提案（不可自动执行），人可随意
    AI_MANAGED = "AI_MANAGED"  # 任务托管中：AI 在授权内动作；界面上人须先接管
    HUMAN_PRIORITY = "HUMAN_PRIORITY"  # 人工优先：检测到人工变更/人工接管，AI 停手
    TOOL_MANAGED = "TOOL_MANAGED"  # 领星策略托管（RuleEngine/StepBudget/TimingTactics）


class CustodyViolation(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ObjectCustody(BaseModel):
    """单个广告对象（campaign/adGroup/target）的控制权记录。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    organization_id: uuid.UUID
    profile_external_id: str
    #: 对象标识："campaign:<id>" / "ad_group:<id>" / "target:<id>"（外部 ID 原文）。
    object_key: str
    state: CustodyState = CustodyState.UNMANAGED
    #: AI_MANAGED 时绑定的任务；其余状态为 None。
    engagement_id: uuid.UUID | None = None
    #: HUMAN_PRIORITY 的冷却截止；None 表示需人显式归还。
    human_priority_until: datetime | None = None
    #: 最近一次状态变化的依据（审计可读）。
    reason: str = ""

    def assert_ai_may_act(self, engagement_id: uuid.UUID, now: datetime) -> None:
        """AI 执行任何动作前的硬闸。非本任务托管即拒——拒绝含义是"回到人"。"""
        if self.state is CustodyState.TOOL_MANAGED:
            raise CustodyViolation(
                "OBJECT_TOOL_MANAGED",
                f"{self.object_key} is managed by a Lingxing strategy tool; "
                "takeover requires owner decision (DEC-119)",
            )
        if self.state is CustodyState.HUMAN_PRIORITY:
            if self.human_priority_until is None or now < self.human_priority_until:
                raise CustodyViolation(
                    "OBJECT_HUMAN_PRIORITY",
                    f"{self.object_key} is under human priority"
                    + (
                        f" until {self.human_priority_until.isoformat()}"
                        if self.human_priority_until
                        else " until explicitly released"
                    ),
                )
            # 冷却已过：视同无主，仍要求先重新托管。
            raise CustodyViolation(
                "OBJECT_NOT_CLAIMED", f"{self.object_key} cooled down but is not re-claimed"
            )
        if self.state is CustodyState.UNMANAGED:
            raise CustodyViolation(
                "OBJECT_NOT_CLAIMED", f"{self.object_key} is not claimed by any engagement"
            )
        if self.engagement_id != engagement_id:
            raise CustodyViolation(
                "OBJECT_CLAIMED_ELSEWHERE",
                f"{self.object_key} is managed by another engagement",
            )

    def claim_for_ai(
        self, claimer: ActorContext, engagement_id: uuid.UUID, now: datetime
    ) -> ObjectCustody:
        """人把对象交给某个任务托管。托管是授权扩大——只能由人发起（AX-05 同源）。"""
        if claimer.principal_type is not PrincipalType.HUMAN or not claimer.human_person_id:
            raise CustodyViolation("HUMAN_REQUIRED", "only humans assign AI custody")
        if self.state is CustodyState.TOOL_MANAGED:
            raise CustodyViolation(
                "OBJECT_TOOL_MANAGED", "release the Lingxing strategy tool first (DEC-119)"
            )
        if self.state is CustodyState.AI_MANAGED and self.engagement_id != engagement_id:
            raise CustodyViolation(
                "OBJECT_CLAIMED_ELSEWHERE", "already managed by another engagement"
            )
        if self.state is CustodyState.HUMAN_PRIORITY and (
            self.human_priority_until is None or now < self.human_priority_until
        ):
            # 人当然可以随时把自己的优先权交还——这一分支就是显式归还。
            pass
        return self.model_copy(
            update={
                "state": CustodyState.AI_MANAGED,
                "engagement_id": engagement_id,
                "human_priority_until": None,
                "reason": f"claimed by {claimer.human_person_id}",
            }
        )

    def note_external_human_change(self, observed_at: datetime, detail: str) -> ObjectCustody:
        """操作日志检测到带外人工变更：对象自动让位给人（human-wins backoff）。"""
        return self.model_copy(
            update={
                "state": CustodyState.HUMAN_PRIORITY,
                "engagement_id": None,
                "human_priority_until": observed_at + DEFAULT_HUMAN_PRIORITY_COOLDOWN,
                "reason": f"external human change detected: {detail[:120]}",
            }
        )

    def note_tool_managed(self, detail: str) -> ObjectCustody:
        """读侧发现对象被领星策略工具托管（ads_strategy 非空）：冻结不碰。"""
        return self.model_copy(
            update={
                "state": CustodyState.TOOL_MANAGED,
                "engagement_id": None,
                "human_priority_until": None,
                "reason": f"lingxing strategy tool: {detail[:120]}",
            }
        )

    def human_take_over(self, taker: ActorContext) -> ObjectCustody:
        """人显式接管（无冷却截止：直到人归还）。任何一个人即可——收权从简。"""
        if taker.principal_type is not PrincipalType.HUMAN or not taker.human_person_id:
            raise CustodyViolation("HUMAN_REQUIRED", "only humans take over custody")
        return self.model_copy(
            update={
                "state": CustodyState.HUMAN_PRIORITY,
                "engagement_id": None,
                "human_priority_until": None,
                "reason": f"taken over by {taker.human_person_id}",
            }
        )
