"""授权边界原语：作用域（MandateScope）与运行窗口（RunWindow）。

2026-08-28 Owner 反馈落位：

- "签发授权书往往如果对已有的 campaign/group 或者广告进行设置，这里完全没有渠道
  可以这样" → MandateScope：一份授权可以只管人勾选的对象，而不是恒等于整店。
  勾选集直接复用 tasks/selection.py 的 SelectionSet，不新造选择器概念。
- "缺少运行的时间段…例如还有些广告在吉隆坡时间的凌晨 2 点到晚上 6 点"
  → RunWindow：系统在哪些**当地钟点**允许跑这份授权。

RunWindow 不是分时竞价。它只约束"什么时候检查"，不产生任何按时段变化的写值，
不改对象的 ads_strategy / is_apply_time，不与领星分时工具争控制权——DEC-119 的
"平台自任时段控制器 vs 委托领星分时"仍未裁决，本模块刻意不进入那个区域。

时区必须显式携带：绝不用服务器本地时区隐式解释"凌晨 2 点"。成员判定只看当地
钟点，因此 DST 切换当天窗口实际可能长 23 或 25 小时——这正是"按当地钟点工作"
的语义，不做补偿（补偿反而会让"凌晨 2 点"某天变成 1 点或 3 点）。

MandateViolation 定义在本模块而不是 mandate.py：mandate.py 依赖本模块（这两个类
是 AutomationMandate 的字段类型），反向 import 会成环。mandate.py 原样再导出，
`from ads_control_plane.strategies.mandate import MandateViolation` 一切照旧，
调用方的单条 `except MandateViolation` 仍能兜住本模块的全部拒绝。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, model_validator

from ads_control_plane.tasks.directive import ObjectLevel
from ads_control_plane.tasks.selection import SelectionSet

__all__ = [
    "MandateScope",
    "MandateScopeKind",
    "MandateViolation",
    "RunWindow",
]


class MandateViolation(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MandateScopeKind(StrEnum):
    """这份授权管哪些广告——二选一，必须显式落笔。

    "整店"是**扩大授权**的正向意思表示，所以不用"selection 为 None"隐式表达：
    审计要能区分"他确认要整店"和"他忘了填"。
    """

    PROFILE = "PROFILE"
    OBJECTS = "OBJECTS"


class MandateScope(BaseModel):
    """授权作用域。PROFILE = 整店；OBJECTS = 只管勾选集里的对象。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: MandateScopeKind
    #: kind=OBJECTS 时必填；kind=PROFILE 时必须为 None。
    #: 空集/跨 profile/超 200 由 SelectionSet 构造期自行拒绝（SELECTION_* 码），
    #: 本模型不重复实现那三条，只管"给没给"。
    selection: SelectionSet | None = None

    @model_validator(mode="after")
    def _contract(self) -> MandateScope:
        if self.kind is MandateScopeKind.OBJECTS and self.selection is None:
            raise MandateViolation(
                "SCOPE_SELECTION_REQUIRED",
                "scope kind OBJECTS requires a selection; pick objects or use kind PROFILE",
            )
        if self.kind is MandateScopeKind.PROFILE and self.selection is not None:
            raise MandateViolation(
                "MANDATE_SCOPE_CONFLICT",
                "scope kind PROFILE must not carry a selection; the two kinds are exclusive",
            )
        return self

    @property
    def profile_external_id(self) -> str | None:
        """勾选集所属 profile；整店作用域没有自带 profile（由授权书本身携带）。"""
        return None if self.selection is None else self.selection.profile_external_id

    @property
    def object_count(self) -> int:
        """作用域内被点名的对象数；整店为 0（0 不是"没有对象"，是"没有点名"）。"""
        return 0 if self.selection is None else len(self.selection.items)

    def covers(self, *, ad_group_external_id: str, campaign_external_id: str | None) -> bool:
        """候选（否定词按 AD_GROUP 落位）是否落在作用域内。整店恒真。

        只认两种命中：广告组被直接勾选，或它的父活动被勾选。TARGET 层不参与
        判定——搜索词记录不携带触发它的 target，向上取其广告组等于把"管这 3 个
        关键词"静默放大成"管这 3 个关键词所在的整个广告组"。
        """
        if self.kind is MandateScopeKind.PROFILE:
            return True
        if self.selection is None:  # pragma: no cover - 构造期校验已排除
            raise MandateViolation(
                "SCOPE_SELECTION_REQUIRED", "OBJECTS scope reached covers() without a selection"
            )
        keys = {(item.level, item.external_id) for item in self.selection.items}
        if (ObjectLevel.AD_GROUP, ad_group_external_id) in keys:
            return True
        return campaign_external_id is not None and (
            (ObjectLevel.CAMPAIGN, campaign_external_id) in keys
        )


class RunWindow(BaseModel):
    """运行窗口 = 系统在哪些**当地钟点**允许跑这份授权。

    钟点粒度取整小时（Owner 的例子就是整点）。区间左闭右开：start_hour 那一刻
    算开、end_hour 那一刻算关。start_hour == end_hour 表示全天（等价于不设窗口）；
    start_hour > end_hour 表示跨午夜（如 22 → 6 = 当地晚 10 点到次日早 6 点）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: IANA 时区名，如 "Asia/Kuala_Lumpur"。非法即拒，不回退服务器本地时区。
    timezone: str
    start_hour: int
    end_hour: int

    @model_validator(mode="after")
    def _contract(self) -> RunWindow:
        if not self.timezone.strip():
            raise MandateViolation("INVALID_TIMEZONE", "run window requires an IANA timezone name")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise MandateViolation(
                "INVALID_TIMEZONE",
                f"unknown IANA timezone {self.timezone!r}; the system will not guess a timezone",
            ) from exc
        if not 0 <= self.start_hour <= 23 or not 0 <= self.end_hour <= 23:
            raise MandateViolation(
                "RUN_WINDOW_INVALID", "start_hour and end_hour must both be within [0, 23]"
            )
        return self

    @property
    def is_all_day(self) -> bool:
        return self.start_hour == self.end_hour

    @property
    def crosses_midnight(self) -> bool:
        """这个时段**真的**跨过当地午夜吗。

        end_hour == 0 表示「到当天结束为止」：is_open_at 那一支退化成
        `hour >= start_hour`，22→0 就是 22:00–23:59，一分钟都没跨过午夜。
        光看 start > end 会把它判成跨午夜（2026-08-30 排查）——而配额日据此平移，
        卡片还会写「整夜算同一天」，对一个根本没有夜的时段说的。
        """
        return self.start_hour > self.end_hour and self.end_hour > 0

    def is_open_at(self, moment: datetime) -> bool:
        """moment 落在窗口内吗。naive datetime 一律拒绝——没有时区的时刻无法与
        当地钟点比较，猜一个时区就是把"凌晨 2 点"解释成别处的钟点。"""
        if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
            raise MandateViolation(
                "NAIVE_DATETIME_REJECTED",
                "run window membership requires a timezone-aware moment",
            )
        if self.is_all_day:
            return True
        hour = moment.astimezone(ZoneInfo(self.timezone)).hour
        if self.start_hour < self.end_hour:
            return self.start_hour <= hour < self.end_hour
        return hour >= self.start_hour or hour < self.end_hour

    def next_open_at(self, moment: datetime) -> datetime:
        """下一个窗口开启时刻（该时区的本地 datetime）。只用于拒绝提示：人最想
        知道的就是"那什么时候能跑"。DST 切换当天可能偏差一小时，提示语不做补偿。
        """
        if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
            raise MandateViolation(
                "NAIVE_DATETIME_REJECTED",
                "run window projection requires a timezone-aware moment",
            )
        local = moment.astimezone(ZoneInfo(self.timezone))
        today = local.replace(hour=self.start_hour, minute=0, second=0, microsecond=0)
        return today if local.hour < self.start_hour else today + timedelta(days=1)
