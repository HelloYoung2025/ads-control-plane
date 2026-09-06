"""广告对象现值快照（AdObjectSnapshot）——镜像层的最小事实单元。

数据源是四张报表（docs/evidence/lx-v3-feasibility-20260828.md §1）：预算在
campaign 层、组默认竞价在 group 层、独立竞价与词文本在 targeting/keyword 层。
keyword 并入 TARGET 层，用 keyword_text 区分，不扩 ObjectLevel 枚举。

纪律：
- object_key 与 custody 的对象标识同构（"campaign:<id>" / "ad_group:<id>" / "target:<id>"），
  前缀必须与 level 一致，否则拒绝构造（OBJECT_KEY_LEVEL_MISMATCH）。
- 时间戳一律 timezone-aware UTC，naive datetime 拒收（NAIVE_DATETIME_REJECTED）。
- 窗口指标（spends/acos 等）原样字符串进 metrics，不做数值演绎——镜像只陈述
  源侧现值，不制造二手结论。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType

from ads_control_plane.tasks.directive import ObjectLevel


class SnapshotError(Exception):
    """快照构造违规——带 code 的显式拒绝，不静默降级。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


#: object_key 前缀与对象层级的同构映射（与 authorization/custody.py 的约定一致）。
LEVEL_KEY_PREFIX: Mapping[ObjectLevel, str] = {
    ObjectLevel.CAMPAIGN: "campaign:",
    ObjectLevel.AD_GROUP: "ad_group:",
    ObjectLevel.AD: "ad:",
    ObjectLevel.TARGET: "target:",
}


@dataclass(frozen=True, kw_only=True)
class AdObjectSnapshot:
    """单个广告对象在某一时刻的现值快照（append-only，见 repository.py）。"""

    #: "campaign:<id>" / "ad_group:<id>" / "target:<id>"（外部 ID 原文）。
    object_key: str
    level: ObjectLevel
    profile_id: str
    #: 领星店铺 sid（报表行携带时记录）。
    sid: str | None = None
    #: 展示名：campaign_name / ad_group_name / expression / keyword_text。
    name: str | None = None
    state: str | None = None
    #: 日预算现值（campaign 层报表 budget 字段）。
    daily_budget: Decimal | None = None
    #: 组默认竞价（group/targeting/keyword 层报表 default_bid 字段）。
    default_bid: Decimal | None = None
    #: 独立竞价现值（targeting/keyword 层报表 bid 字段）。
    bid: Decimal | None = None
    #: keyword 并入 TARGET 层的区分字段。
    keyword_text: str | None = None
    match_type: str | None = None
    #: 投放方式（manual/auto，campaign/group/keyword 报表 targeting_type 字段）。
    #: 2026-08-29 领星 IA 实测：领星行内 [手动]/[自动] 徽标就用这一维度，运营靠它
    #: 一眼分清「自动跑量的」和「手动圈词的」——见 docs/evidence/lx-ads-ia-20260829.md。
    targeting_type: str | None = None
    #: 领星策略工具标记（TOOL_MANAGED 打标数据源之一）。
    ads_strategy: str | None = None
    #: 分时托管标志（报表 int/bool 兼容提取）。
    is_apply_time: bool | None = None
    parent_campaign_id: str | None = None
    parent_ad_group_id: str | None = None
    #: 窗口指标原样字符串（spends/sales/acos/orders/clicks/impressions），不做数值演绎。
    metrics: Mapping[str, str] = field(default_factory=dict)
    #: 这行指标统计的是哪一段（"YYYY-MM-DD - YYYY-MM-DD"，领星 report_date 原文）。
    #: 2026-08-30 排查 #13：此前快照不带它，于是「这张表是哪个窗口的」只能由同步端
    #: 记一份 profile 级的全局值，而那个值每轮无条件覆写。截断的同步隔天再开一轮新的，
    #: 没被重拉到的行仍带着上一个窗口的花费，表头却写着新窗口——人对着一个写死的
    #: 窗口把整张表排序、比大小，而行与行之间根本不可比。窗口是**每一行的属性**，
    #: 记在行上才能如实回答。None = 这行不是同步来的（如演示种子），不编。
    report_date: str | None = None
    #: 源侧现值时刻（本次拉取时刻，tz-aware UTC）。
    source_as_of: datetime
    #: 本地记录时刻（tz-aware UTC）；仓库按它取最新/排历史。
    recorded_at: datetime
    catalog_version: str
    schema_version: str

    def __post_init__(self) -> None:
        # 防篡改：metrics 复制进只读视图。快照是 append-only 历史的事实单元，
        # 若与调用方共享同一个可变 dict，调用方事后改 dict 即静默改写历史。
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))
        if not self.profile_id.strip():
            raise SnapshotError("PROFILE_ID_REQUIRED", "snapshot requires a non-empty profile_id")
        prefix = LEVEL_KEY_PREFIX[self.level]
        if not self.object_key.startswith(prefix) or len(self.object_key) <= len(prefix):
            raise SnapshotError(
                "OBJECT_KEY_LEVEL_MISMATCH",
                f"object_key {self.object_key!r} must start with {prefix!r} "
                f"and carry a non-empty external id for level {self.level}",
            )
        timestamps = (("source_as_of", self.source_as_of), ("recorded_at", self.recorded_at))
        for label, value in timestamps:
            if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
                raise SnapshotError(
                    "NAIVE_DATETIME_REJECTED",
                    f"{label} must be timezone-aware (UTC); got naive {value.isoformat()}",
                )
