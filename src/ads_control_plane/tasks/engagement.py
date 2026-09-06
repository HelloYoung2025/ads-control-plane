"""任务（Engagement）——"优化某款产品的广告"作为一等域对象（DEC-120）。

2026-08-28 Owner 需求（任务初始化）：以产品为入口——读取全部相关广告与表现
→ 给出优化建议（哪些该关、哪些该开）→ 制定任务计划 → 运行 → 回顾。

生命周期（只进不跳，每步产物入库可回溯）：

    DRAFT → DIAGNOSED → PLANNED → RUNNING → CLOSED
      │        │           │         │
      建任务    诊断报告      人选建议    绑定授权书/托管对象，
      (产品焦点) (全景+建议)   成计划     每次调整挂 engagement_id

诊断报告的生成器依赖真实读通道（P1 后接入）；本模块先冻结结构与不变量：
报告是**证据 + 建议**，不是执行——任何建议落地都要经人批准成计划。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, model_validator

from ads_control_plane.canonical.ids import CanonicalId
from ads_control_plane.canonical.money import Money
from ads_control_plane.identity.actor import ActorContext, PrincipalType
from ads_control_plane.tasks.selection import SelectionSet


class EngagementState(StrEnum):
    DRAFT = "DRAFT"
    DIAGNOSED = "DIAGNOSED"
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    CLOSED = "CLOSED"


class EngagementKind(StrEnum):
    INITIALIZE = "INITIALIZE"  # 任务初始化：全景诊断起步
    ADJUST = "ADJUST"  # 中途介入：对指定板块做调整


class Recommendation(StrEnum):
    """诊断建议动作闭集。KEEP 也要显式给出——"没建议"与"建议不动"是两回事。"""

    KEEP = "KEEP"
    PAUSE = "PAUSE"  # 该关
    ENABLE = "ENABLE"  # 该开
    ADJUST_BUDGET = "ADJUST_BUDGET"
    ADJUST_BID = "ADJUST_BID"
    NEGATE_TERMS = "NEGATE_TERMS"
    HARVEST_TERMS = "HARVEST_TERMS"  # 搜索词收割（加词+原组否定）


class EngagementError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ObjectDiagnosis(BaseModel):
    """诊断报告中的单对象条目：窗口表现 + 建议 + 人可核对的证据陈述。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    object_key: str  # "campaign:<id>" 等，与 custody 同一命名
    display_name: str
    window_spend: Money
    window_sales: Money
    clicks: int
    orders: int
    #: ACOS 以字符串十进制承载（分母为 0 时为 None，禁止编造 0%）。
    acos: str | None
    #: 领星策略托管状态原文（ads_strategy），非空即触发 TOOL_MANAGED（DEC-119）。
    lingxing_strategy: str | None
    recommendation: Recommendation
    #: 证据陈述：给人读的一句依据，必须来自窗口数据，不得空。
    evidence: str

    @model_validator(mode="after")
    def _non_empty_evidence(self) -> ObjectDiagnosis:
        if not self.evidence.strip():
            raise ValueError("every recommendation must carry evidence")
        return self


class DiagnosisReport(BaseModel):
    """全景诊断：任务焦点下的全部广告对象 + 表现 + 建议。生成即冻结。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    report_id: CanonicalId
    engagement_id: CanonicalId
    #: 数据窗口与数据源时刻——报告永远回答"基于何时的数据"。
    window_days: int
    source_as_of: datetime
    generated_at: datetime
    entries: tuple[ObjectDiagnosis, ...]
    #: 覆盖声明：报告扫过的对象总数（entries 之外被过滤的也要计数，禁止静默截断）。
    objects_scanned: int

    @model_validator(mode="after")
    def _coverage(self) -> DiagnosisReport:
        if self.objects_scanned < len(self.entries):
            raise ValueError("objects_scanned must cover all entries")
        return self


class PlannedAction(BaseModel):
    """计划条目 = 人从诊断建议中选中的一条（不许凭空出现建议之外的动作）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    object_key: str
    recommendation: Recommendation
    #: 人可修改的备注（如目标预算值留待提案阶段细化）。
    note: str = ""


class TaskEngagement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    engagement_id: CanonicalId
    organization_id: CanonicalId
    profile_external_id: str
    kind: EngagementKind
    #: 产品焦点：人写的检索意图（如 "HX02"），展开为对象集的规则在读侧实现。
    focus: str
    created_by_person_id: str
    created_at: datetime
    state: EngagementState = EngagementState.DRAFT
    diagnosis_report_id: uuid.UUID | None = None
    plan: tuple[PlannedAction, ...] = ()
    #: RUNNING 时绑定的授权书（持续策略用）；一次性动作走提案不经此字段。
    mandate_id: uuid.UUID | None = None
    closed_reason: str | None = None
    #: 勾选范围（可选，DEC-122/124）：人勾选的显式对象集。focus 是检索意图，
    #: scope 是已冻结的对象集合；给出时必须与任务同 profile。
    scope: SelectionSet | None = None

    @model_validator(mode="after")
    def _non_empty_focus(self) -> TaskEngagement:
        if not self.focus.strip():
            raise ValueError("engagement focus must be non-empty")
        return self

    @model_validator(mode="after")
    def _scope_same_profile(self) -> TaskEngagement:
        if self.scope is not None and self.scope.profile_external_id != self.profile_external_id:
            raise EngagementError(
                "SCOPE_PROFILE_MISMATCH", "scope selection belongs to another profile"
            )
        return self

    def attach_diagnosis(self, report: DiagnosisReport) -> TaskEngagement:
        if self.state is not EngagementState.DRAFT:
            raise EngagementError("ENGAGEMENT_NOT_DRAFT", f"state is {self.state}")
        if report.engagement_id != self.engagement_id:
            raise EngagementError("REPORT_MISMATCH", "report belongs to another engagement")
        return self.model_copy(
            update={
                "state": EngagementState.DIAGNOSED,
                "diagnosis_report_id": report.report_id,
            }
        )

    def approve_plan(
        self,
        approver: ActorContext,
        report: DiagnosisReport,
        selected: tuple[PlannedAction, ...],
    ) -> TaskEngagement:
        """人从诊断建议中圈选计划。计划条目必须能在报告里找到同对象同建议的出处。"""
        if approver.principal_type is not PrincipalType.HUMAN or not approver.human_person_id:
            raise EngagementError("HUMAN_REQUIRED", "plans are approved by humans only")
        if self.state is not EngagementState.DIAGNOSED:
            raise EngagementError("ENGAGEMENT_NOT_DIAGNOSED", f"state is {self.state}")
        if report.report_id != self.diagnosis_report_id:
            raise EngagementError("REPORT_MISMATCH", "plan must cite the attached report")
        if not selected:
            raise EngagementError("EMPTY_PLAN", "select at least one recommendation")
        recommended = {(e.object_key, e.recommendation) for e in report.entries}
        for action in selected:
            if (action.object_key, action.recommendation) not in recommended:
                raise EngagementError(
                    "ACTION_NOT_RECOMMENDED",
                    f"{action.recommendation} on {action.object_key} is not in the report",
                )
        return self.model_copy(update={"state": EngagementState.PLANNED, "plan": selected})

    def start_running(self, mandate_id: uuid.UUID | None = None) -> TaskEngagement:
        if self.state is not EngagementState.PLANNED:
            raise EngagementError("ENGAGEMENT_NOT_PLANNED", f"state is {self.state}")
        return self.model_copy(update={"state": EngagementState.RUNNING, "mandate_id": mandate_id})

    def close(self, closer: ActorContext, reason: str) -> TaskEngagement:
        if closer.principal_type is not PrincipalType.HUMAN or not closer.human_person_id:
            raise EngagementError("HUMAN_REQUIRED", "engagements are closed by humans only")
        if self.state is EngagementState.CLOSED:
            raise EngagementError("ENGAGEMENT_CLOSED", "already closed")
        return self.model_copy(
            update={"state": EngagementState.CLOSED, "closed_reason": reason or "closed"}
        )
