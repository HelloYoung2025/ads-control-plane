"""中途介入调整（AdjustmentDirective）——"选对象、说改什么、看预览、再批准"（DEC-120）。

2026-08-28 Owner 需求：运行中想对某些 campaign/广告组调整，但"没有一个选择
对象，无法告诉系统哪些地方要改、改什么、怎么改"。本模块把这句话变成合同：

    selector（选哪些）+ intent（改什么/怎么改，白名单闭集）
      → expand 成受影响对象预览（现值 → 新值，逐对象）
      → 冻结为预览清单，人对着预览批准（复用集合审批的 Hash 语义）

展开依赖读通道（ExpansionPort，P1 接真实报表）；本模块冻结合同与不变量：
预览为空不是成功，是 SELECTOR_MATCHED_NOTHING——选择器没选中任何东西必须明说。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from ads_control_plane.canonical.ids import CanonicalId

#: 单次指令可命中的对象数上限——中途介入是手术刀不是推土机。
MAX_AFFECTED_OBJECTS = 200

#: 相对调整的白名单幅度（百分比），超出即拒。
MAX_RELATIVE_STEP_PERCENT = 50


class ObjectLevel(StrEnum):
    CAMPAIGN = "CAMPAIGN"
    AD_GROUP = "AD_GROUP"
    #: 「广告」层：投放在广告组里的具体商品（ASIN/SKU 一条一行）。领星六层模型的
    #: 第 4 层，与 TARGET（投放：词/定向）并列挂在广告组下，不是它的父或子。
    #: 2026-08-29 Owner 反馈工作台缺这一层；数据源 ad_campaign_product_report。
    AD = "AD"
    TARGET = "TARGET"


class AdjustmentKind(StrEnum):
    """改什么——白名单闭集，与实测写工具一一对应（docs/ad-control-plan.md §3）。"""

    PAUSE = "PAUSE"
    ENABLE = "ENABLE"
    SET_DAILY_BUDGET = "SET_DAILY_BUDGET"  # campaign 层
    SCALE_DAILY_BUDGET = "SCALE_DAILY_BUDGET"  # 相对 ±%
    SET_BID = "SET_BID"  # target / adGroup 默认竞价
    SCALE_BID = "SCALE_BID"


class DirectiveError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ObjectSelector(BaseModel):
    """选哪些：显式 ID 列表，或名称筛选 + 指标条件（二选一，禁止都不给）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    level: ObjectLevel
    #: 显式对象外部 ID 列表（与筛选互斥地至少给一个）。
    external_ids: tuple[str, ...] = ()
    #: 名称模糊筛选（如 "HX02"）。
    name_contains: str | None = None
    #: 指标条件（窗口内），全部可选：如 acos_over="0.40" 只选 ACOS>40% 的。
    acos_over: str | None = None
    spend_over: str | None = None
    orders_at_most: int | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> ObjectSelector:
        filters = (self.name_contains, self.acos_over, self.spend_over, self.orders_at_most)
        has_filter = any(v is not None for v in filters)
        if not self.external_ids and not has_filter:
            raise ValueError("selector must carry explicit ids or at least one filter")
        if self.external_ids and has_filter:
            raise ValueError("explicit ids and filters are mutually exclusive; pick one")
        return self


class AdjustmentIntent(BaseModel):
    """怎么改：动作 + 值。绝对值与相对值二选一，白名单校验在此层完成。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: AdjustmentKind
    #: 绝对值（SET_*）：十进制字符串；PAUSE/ENABLE 必须为 None。
    value: str | None = None
    #: 相对百分比（SCALE_*）：-50..50 的整数，0 无意义即拒。
    percent: int | None = None
    #: 人写的动机，进审计与提案说明——不参与执行判定。
    reason: str

    @model_validator(mode="after")
    def _whitelist(self) -> AdjustmentIntent:
        if not self.reason.strip():
            raise ValueError("adjustment reason must be non-empty (audit readability)")
        if self.kind in (AdjustmentKind.PAUSE, AdjustmentKind.ENABLE):
            if self.value is not None or self.percent is not None:
                raise ValueError(f"{self.kind} carries no value")
            return self
        if self.kind in (AdjustmentKind.SET_DAILY_BUDGET, AdjustmentKind.SET_BID):
            if self.value is None or self.percent is not None:
                raise ValueError(f"{self.kind} requires an absolute value only")
            if Decimal(self.value) <= 0:
                raise ValueError("absolute value must be positive")
            return self
        # SCALE_*
        if self.percent is None or self.value is not None:
            raise ValueError(f"{self.kind} requires percent only")
        if self.percent == 0 or abs(self.percent) > MAX_RELATIVE_STEP_PERCENT:
            raise ValueError(f"percent must be within ±{MAX_RELATIVE_STEP_PERCENT} and non-zero")
        return self


class AffectedObject(BaseModel):
    """预览行：这个对象、现值、执行后的新值——人对着它批。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    object_key: str
    display_name: str
    current_value: str  # 现值原文（状态/预算/竞价），来自读通道
    new_value: str


class ExpansionPort(Protocol):
    """selector → 命中对象清单。P1 接真实报表实现；测试用内存假体。"""

    def expand(
        self, profile_external_id: str, selector: ObjectSelector
    ) -> list[AffectedObject]: ...


class AdjustmentPreview(BaseModel):
    """冻结的展开结果：受影响对象全清单。空清单不可构造（SELECTOR_MATCHED_NOTHING）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    directive_id: CanonicalId
    engagement_id: CanonicalId | None
    profile_external_id: str
    selector: ObjectSelector
    intent: AdjustmentIntent
    affected: tuple[AffectedObject, ...]
    expanded_at: datetime


def build_preview(
    *,
    directive_id: uuid.UUID,
    engagement_id: uuid.UUID | None,
    profile_external_id: str,
    selector: ObjectSelector,
    intent: AdjustmentIntent,
    port: ExpansionPort,
    now: datetime,
) -> AdjustmentPreview:
    """展开选择器为预览。命中为空/超上限都是显式错误，不静默。"""
    affected = port.expand(profile_external_id, selector)
    if not affected:
        raise DirectiveError(
            "SELECTOR_MATCHED_NOTHING",
            "selector matched no objects; refine ids or filters",
        )
    if len(affected) > MAX_AFFECTED_OBJECTS:
        raise DirectiveError(
            "SELECTOR_TOO_BROAD",
            f"selector matched {len(affected)} objects (max {MAX_AFFECTED_OBJECTS}); "
            "narrow the filter or split the directive",
        )
    return AdjustmentPreview(
        directive_id=directive_id,
        engagement_id=engagement_id,
        profile_external_id=profile_external_id,
        selector=selector,
        intent=intent,
        affected=tuple(affected),
        expanded_at=now,
    )
