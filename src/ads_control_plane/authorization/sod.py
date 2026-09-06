"""职责分离（AX-05 / AX-08）：请求级冲突矩阵，按不可变 human_person_id 判定。

不依赖"有多少个角色对象"——同一个人换两个登录账号也不能绕过。
"""

from __future__ import annotations

from ads_control_plane.identity.actor import ActorContext, PrincipalType


class SoDViolation(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def check_can_approve(
    approver: ActorContext,
    proposal_creator_person_id: str | None,
    proposal_submitter_person_id: str | None,
) -> None:
    """审批冲突矩阵。违反即抛 SoDViolation，调用方必须让其冒泡为 DENY。"""
    # AI 永远不能审批（AX-05），即使被授予 APPROVER 角色也阻断。
    if approver.principal_type is not PrincipalType.HUMAN:
        raise SoDViolation("AI_CANNOT_APPROVE", "only HUMAN principals may approve")
    person = approver.human_person_id
    if not person:
        raise SoDViolation("APPROVER_IDENTITY_UNKNOWN", "approver has no human_person_id")
    if proposal_creator_person_id and person == proposal_creator_person_id:
        raise SoDViolation("CREATOR_CANNOT_APPROVE", "proposal creator cannot approve it")
    if proposal_submitter_person_id and person == proposal_submitter_person_id:
        raise SoDViolation("SUBMITTER_CANNOT_APPROVE", "proposal submitter cannot approve it")


def check_can_submit(
    submitter: ActorContext,
    draft_created_by_client_id: str | None,
) -> None:
    """AI 不得对自己创建的 draft 执行正式提交（AX-05）；提交必须来自人类会话。"""
    if submitter.principal_type is PrincipalType.AI_CLIENT:
        raise SoDViolation(
            "AI_CANNOT_SUBMIT",
            "AI clients may only create drafts; submission requires a human session",
        )
    if submitter.principal_type is PrincipalType.SERVICE_ACCOUNT:
        raise SoDViolation(
            "SERVICE_ACCOUNT_CANNOT_SUBMIT",
            "service accounts cannot submit proposals in MVP",
        )
    _ = draft_created_by_client_id  # 保留参数：未来 AutomationGrant 匹配时使用
