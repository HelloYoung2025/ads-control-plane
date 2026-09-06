"""策略包（StrategyBundle）——编排层对授权书的引用容器（DEC-124/125）。

2026-08-28 Owner 裁决默认：策略包不是新的授权来源，而是编排层对既有授权书
（AutomationMandate，≥1 张）的引用 + 人勾选的作用范围 + 退出策略。授权边界
仍由授权书自身裁定；包被暂停/关闭不撤销任何授权书。

生命周期（激活/关闭只能由人发起；暂停可由 ExitGuard 判定或人触发）：

    DRAFT → ACTIVE ⇄ SUSPENDED        任一非 CLOSED 状态 --close(人)--> CLOSED
              （SUSPENDED 恢复必须由人再 activate，不存在自动复跑）

时段等运行意图仅落 notes 注记、只供人读，不做任何调度（DEC-119 未裁决，
不建时段调度器）。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from ads_control_plane.canonical.ids import CanonicalId
from ads_control_plane.identity.actor import ActorContext, PrincipalType
from ads_control_plane.strategies.exit_guard import ExitPolicy
from ads_control_plane.tasks.selection import SelectionSet


class BundleStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    CLOSED = "CLOSED"


class BundleError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class StrategyBundle(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    bundle_id: CanonicalId
    name: str
    profile_external_id: str
    #: 引用的授权书（≥1）；包只引用不签发，越界判定始终由授权书自身完成。
    mandate_ids: tuple[str, ...]
    #: 人勾选的作用范围；None = 尚未圈定（DRAFT 期可重建后补）。
    selection: SelectionSet | None = None
    exit_policy: ExitPolicy
    status: BundleStatus = BundleStatus.DRAFT
    created_by: str
    created_at: datetime
    #: 时段等运行意图的注记，只供人读；永不进入调度或执行判定（DEC-119）。
    notes: str | None = None
    #: 最近一次暂停的依据（ExitGuard verdict 原文或人写原因）；恢复时清空。
    suspended_reason: str | None = None

    @model_validator(mode="after")
    def _contract(self) -> StrategyBundle:
        if not self.name.strip():
            raise ValueError("bundle name must be non-empty (audit readability)")
        if not self.mandate_ids:
            raise BundleError(
                "BUNDLE_NO_MANDATES",
                "a bundle references at least one mandate; it is never an authorization source",
            )
        if any(not mandate_id.strip() for mandate_id in self.mandate_ids):
            # 空白 id 是空引用：能过 ≥1 检查却指向不存在的授权书，等同没有引用。
            raise BundleError(
                "BUNDLE_NO_MANDATES",
                "mandate ids must be non-blank; a blank id references no mandate",
            )
        if self.selection is not None and (
            self.selection.profile_external_id != self.profile_external_id
        ):
            raise BundleError(
                "SCOPE_PROFILE_MISMATCH",
                "bundle selection belongs to another profile",
            )
        return self

    def activate(self, actor: ActorContext) -> StrategyBundle:
        """激活（DRAFT）或恢复（SUSPENDED）。启动运行是授权扩大——只能由人发起
        （AX-05 同源：AI 不能启动自己的运行），SUSPENDED 后的恢复同理。"""
        if actor.principal_type is not PrincipalType.HUMAN or not actor.human_person_id:
            raise BundleError("HUMAN_REQUIRED", "bundles are activated by humans only")
        if self.status not in (BundleStatus.DRAFT, BundleStatus.SUSPENDED):
            raise BundleError("BUNDLE_NOT_ACTIVATABLE", f"state is {self.status}")
        return self.model_copy(update={"status": BundleStatus.ACTIVE, "suspended_reason": None})

    def suspend(self, reason: str) -> StrategyBundle:
        """暂停 = 回到人。由 ExitGuard 判定或人触发均可，故不做人别校验；
        恢复必须由人 activate。绝不在此撤销授权书——撤销是人的意思表示
        （mandate.revoke），语义不同。"""
        if self.status is not BundleStatus.ACTIVE:
            raise BundleError("BUNDLE_NOT_ACTIVE", f"state is {self.status}")
        return self.model_copy(
            update={"status": BundleStatus.SUSPENDED, "suspended_reason": reason or "suspended"}
        )

    def close(self, actor: ActorContext) -> StrategyBundle:
        """关闭：任一非 CLOSED 状态皆可，由人发起，关闭后不可复用（重开新包）。"""
        if actor.principal_type is not PrincipalType.HUMAN or not actor.human_person_id:
            raise BundleError("HUMAN_REQUIRED", "bundles are closed by humans only")
        if self.status is BundleStatus.CLOSED:
            raise BundleError("BUNDLE_CLOSED", "already closed")
        return self.model_copy(update={"status": BundleStatus.CLOSED})
