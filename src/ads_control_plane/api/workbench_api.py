"""对象工作台 API（/api/workbench）——镜像浏览、勾选预览、白名单同步、历史查看。

四个面（DEC-117/120/122 的 HTTP 投影）：
- GET  /objects：从镜像仓库读现值（repo.current），服务端分页 + 层级/父对象过滤
  + 浏览端筛选（name_contains / state / managed_only）与排序（sort_field / sort_dir）；
  镜像数据仅供浏览与选择，执行前由执行侧实时核对现值（本层不做任何写）。
- POST /preview：勾选项 → SelectionSet → 逐 selector 调 tasks/directive.build_preview，
  ExpansionPort 由镜像现值实现。**只产预览，不产生任何执行**；AI 身份可调（读侧），
  响应恒带 approval_required=true。这个常量的意思是「预览本身不是执行」，
  **不是**「后面还有一道本系统的审批」：工作台预览没有任何提交审批的入口，
  本系统也从不执行修改，要落地只能下载变更清单去领星后台手工改（2026-09-06 核实）。
- POST /sync：仅人身份可触发（AI → 403 HUMAN_REQUIRED）；LX_MCP_KEY 缺失 → 409
  fail-closed 显式报错；白名单店铺从 ADS_CP_SYNC_PROFILES 载入（缺省空 = 全拒）。
- GET  /history：repo.history 摘要，供「查看历史」。

安全语义沿用 approval_api：ActorContext 只来自已验证 Bearer Token（AX-02）；
错误一律带 SCREAMING_SNAKE code；本模块没有任何领星写工具调用路径。

注：本模块不用 `from __future__ import annotations`——路由函数的 Depends 引用
builder 闭包内的局部依赖，字符串化注解会让 FastAPI 解析不到它（approval_api 同理）。
"""

import os
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from ads_control_plane.adapters.lx_read import AUTH_SHOPS_TOOL_ID, LxMcpReadClient, LxReadError
from ads_control_plane.api.errors import coded_detail, coded_detail_message
from ads_control_plane.api.mcp_tools.server import InMemoryActorTokenVerifier
from ads_control_plane.identity.actor import ActorContext, PrincipalType
from ads_control_plane.mirror.repository import SnapshotRepository
from ads_control_plane.mirror.snapshot import AdObjectSnapshot
from ads_control_plane.mirror.sync import (
    LxReadPort,
    SyncEngine,
    SyncError,
    SyncRunReport,
    ToolCursor,
)
from ads_control_plane.tasks.directive import (
    AdjustmentIntent,
    AdjustmentKind,
    AdjustmentPreview,
    AffectedObject,
    DirectiveError,
    ObjectLevel,
    ObjectSelector,
    build_preview,
)
from ads_control_plane.tasks.selection import SelectedObject, SelectionError, SelectionSet

# ---------------------------------------------------------------- 常量与错误码

#: 服务端分页默认页长；上限与勾选上限同源（一次最多看/选 200）。
DEFAULT_PAGE_LENGTH = 25
MAX_PAGE_LENGTH = 200

#: dev 保守的同步分页上限默认值（每工具最多拉 3 页），env 可调。
DEFAULT_SYNC_MAX_PAGES = 3

ENV_LX_MCP_KEY = "LX_MCP_KEY"
ENV_LX_MCP_URL = "LX_MCP_URL"
ENV_SYNC_PROFILES = "ADS_CP_SYNC_PROFILES"
ENV_SYNC_MAX_PAGES = "ADS_CP_SYNC_MAX_PAGES"

LEVEL_INVALID = "LEVEL_INVALID"
SORT_FIELD_INVALID = "SORT_FIELD_INVALID"
SORT_DIR_INVALID = "SORT_DIR_INVALID"
HUMAN_REQUIRED = "HUMAN_REQUIRED"
LX_KEY_ABSENT = "LX_KEY_ABSENT"
LX_URL_ABSENT = "LX_URL_ABSENT"
SYNC_CONFIG_INVALID = "SYNC_CONFIG_INVALID"
#: 游标描述的那份镜像已经不在了（服务重启过，内存镜像与游标簿一起清空）。
SYNC_CURSOR_STALE = "SYNC_CURSOR_STALE"
PARAMETER_REJECTED = "PARAMETER_REJECTED"
PREVIEW_OBJECT_NOT_IN_MIRROR = "PREVIEW_OBJECT_NOT_IN_MIRROR"
PREVIEW_VALUE_UNAVAILABLE = "PREVIEW_VALUE_UNAVAILABLE"
PERF_BUCKET_INVALID = "PERF_BUCKET_INVALID"


class WorkbenchError(Exception):
    """工作台显式拒绝——带 code，供 HTTP 层映射，不静默降级。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


#: level 查询/请求体取值（小写惯用形；大写枚举原文同样接受）。
_LEVEL_ALIASES: dict[str, ObjectLevel] = {
    "campaign": ObjectLevel.CAMPAIGN,
    "ad_group": ObjectLevel.AD_GROUP,
    "ad": ObjectLevel.AD,
    "target": ObjectLevel.TARGET,
}


def _parse_level(raw: str) -> ObjectLevel:
    level = _LEVEL_ALIASES.get(raw.strip().lower())
    if level is None:
        raise HTTPException(status_code=400, detail=LEVEL_INVALID)
    return level


# ------------------------------------------------------- 浏览端筛选与排序（≠ selector 筛选）
#
# 下面这组参数只决定「表格显示哪些行、按什么顺序显示」。它们与
# tasks/directive.ObjectSelector 上同名的 name_contains / acos_over 等字段**不是**
# 同一回事：selector 的筛选是运行时再展开的晚绑定查询，而勾选集（tasks/selection.py）
# 离开工作台时永远只带人点过名的显式 external_ids。浏览端筛选不参与任何执行范围判定。

#: 排序字段白名单 → 镜像 metrics 键。name 走快照字段本身，不在 metrics 里。
_SORT_METRIC_KEYS: dict[str, str] = {
    "spend": "spends",
    "acos": "acos",
    "orders": "orders",
    "clicks": "clicks",
    "impressions": "impressions",
    "sales": "sales",
}
_SORT_FIELDS: frozenset[str] = frozenset({*_SORT_METRIC_KEYS, "name"})
_SORT_DIRS: frozenset[str] = frozenset({"asc", "desc"})


#: 绩效分桶：2026-08-29 领星 IA 实测（docs/evidence/lx-ads-ia-20260829.md §3）——
#: 领星投放页把 3 万多条投放直接分成「有成交/有点击无成交/有曝光无点击/无曝光」，
#: 这是运营找问题广告的第一反应（"花了钱没单的在哪"），比让人自己组合筛选快一个量级。
#: 四桶按漏斗互斥；判定所需指标缺失的行不落任何桶（缺失≠0，不冒充），但仍算「全部」。
PERF_BUCKETS: tuple[str, ...] = (
    "has_orders",
    "clicks_no_orders",
    "impressions_no_clicks",
    "no_impressions",
)


def _metric_int(snap: AdObjectSnapshot, key: str) -> int | None:
    """指标字符串 → int。缺失或解析不了 → None（绝不当 0 用）。"""
    raw = snap.metrics.get(key)
    if raw is None:
        return None
    try:
        return int(Decimal(str(raw).strip()))
    except (InvalidOperation, ValueError):
        return None


def _perf_bucket_of(snap: AdObjectSnapshot) -> str | None:
    orders = _metric_int(snap, "orders")
    clicks = _metric_int(snap, "clicks")
    impressions = _metric_int(snap, "impressions")
    if orders is not None and orders > 0:
        return "has_orders"
    if clicks is not None and clicks > 0:
        return "clicks_no_orders" if orders is not None else None
    if impressions is not None and impressions > 0:
        return "impressions_no_clicks" if clicks is not None else None
    if impressions is not None:
        return "no_impressions"
    return None


def _matches_filters(
    snap: AdObjectSnapshot,
    *,
    needle: str | None,
    state: str | None,
    managed_only: bool | None,
) -> bool:
    """浏览端行过滤。三项皆为 AND；任一项为 None 即不参与判定。

    name_contains 大小写不敏感，同时匹配 name / object_key / keyword_text——人在
    工作台里既按名字找也按 ID 找，只匹配 name 会让粘贴 ID 搜索静默落空。
    state 走精确相等：源侧原文，不做大小写折叠也不做同义词映射。
    managed_only=True 只看被领星策略托管的（ads_strategy 非空），False 只看未托管的。
    """
    if needle is not None:
        haystack = (snap.name or "", snap.object_key, snap.keyword_text or "")
        if not any(needle in field.lower() for field in haystack):
            return False
    if state is not None and snap.state != state:
        return False
    return managed_only is None or bool(snap.ads_strategy) is managed_only


def _sorted_rows(
    rows: list[AdObjectSnapshot], sort_field: str, descending: bool
) -> list[AdObjectSnapshot]:
    """按白名单字段排序；**缺失或不可解析的值恒排末尾**（asc/desc 都在末尾）。

    镜像 metrics 是源侧原样字符串（mirror/snapshot.py 的纪律：不做数值演绎），这里
    只在查询时把它解析为 Decimal 用于比较，解析结果不回写快照、不进历史。解析失败
    的行不当 0 处理、也不静默丢弃——把不可比较的值当 0 会让它冒充「最省钱的行」。
    组内以 object_key 兜底，保证同值行的顺序确定。
    """
    sortable: list[tuple[Any, AdObjectSnapshot]] = []
    unsortable: list[AdObjectSnapshot] = []
    for snap in rows:
        if sort_field == "name":
            raw_name = (snap.name or "").strip()
            if raw_name:
                sortable.append((raw_name.lower(), snap))
            else:
                unsortable.append(snap)
            continue
        raw = snap.metrics.get(_SORT_METRIC_KEYS[sort_field])
        try:
            value = Decimal(raw)  # type: ignore[arg-type]
        except (InvalidOperation, TypeError, ValueError):
            unsortable.append(snap)
            continue
        # 二轮审计：NaN 能构造成功但比较时抛 InvalidOperation——源侧一行脏数据会把
        # 整页排序打成 500。与解析失败同罪：归入不可比较末尾。±Infinity 可比较，放行。
        if value.is_nan():
            unsortable.append(snap)
        else:
            sortable.append((value, snap))
    sortable.sort(key=lambda pair: (pair[0], pair[1].object_key), reverse=descending)
    unsortable.sort(key=lambda snap: snap.object_key)
    return [snap for _, snap in sortable] + unsortable


# ---------------------------------------------------------------- 请求体模型


class PreviewItemBody(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    level: str
    external_id: str


class PreviewIntentBody(BaseModel):
    """怎么改：action 为 AdjustmentKind 白名单；value/percent 互斥性由域层校验。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: str
    value: str | None = None
    percent: int | None = None
    reason: str


class PreviewRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str
    items: tuple[PreviewItemBody, ...]
    intent: PreviewIntentBody


class ToolCursorBody(BaseModel):
    """单张报表的续拉状态（SyncRunReport.tool_cursors() 的线上形态）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    next_page: int | None = None
    rows_covered: int = 0
    #: 上游实际交付的行数。缺席即「老标签页的游标，答不上这个数」——见下方
    #: 重建处的兼容取值，那里必须挑不会让自动拉取空转的那一边。
    rows_seen: int | None = None
    source_total: int | None = None
    complete: bool = False


class SyncContinuation(BaseModel):
    """续拉游标：原样取自上一轮响应的 continuation 字段。

    窗口随游标一起回传，续拉的行才与已入库的行同属一个时间窗。

    next_pages 是老形态，保留可选是为了兼容浏览器里还握着上一版游标的标签页——
    本模型 frozen + extra="forbid"，删字段会让那种 POST 撞 pydantic 422，
    而 422 的裸校验错误体里没有前端能翻译的码。tools 缺席时回落到它，
    连带回落到它"已拉全的表会整张重拉"的老行为。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    report_date: str
    next_pages: dict[str, int] = {}
    tools: dict[str, ToolCursorBody] | None = None
    #: 游标所描述的那份镜像的纪元。镜像是纯内存的，重启即清空；而演示 token 是
    #: 固定串，重启后浏览器里的 Bearer 仍然有效，POST 照样 200。没有这个戳，
    #: 服务端会原样采信一份描述着「已不存在的 250 行」的游标：标着 complete 的表
    #: 一页都不拉，并把游标里的 rows_covered 抄进本轮 coverage 当作事实上报。
    #: 于是黄条写「广告组 250/250」看上去这层是齐的，人切过去表却是空的，
    #: 空态一句陈述句「该店铺镜像里没有广告组对象」——这句话是假的，
    #: 不是店里没有，是这轮压根没去拉。缺的还正是按花费降序排在最前面的那一段。
    #: 老标签页回传的游标没有这个字段，按不匹配处理（重新完整拉取是安全方向）。
    epoch: str | None = None


class SyncRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_id: str
    #: 省略 = 从头同步一轮；给出 = 从上轮停下的地方接着拉。
    continuation: SyncContinuation | None = None


# ---------------------------------------------------------------- 镜像展开端口


def _external_id(snap: AdObjectSnapshot) -> str:
    """object_key = "<prefix>:<external_id>"（构造校验保证形状）。"""
    return snap.object_key.split(":", 1)[1]


def _current_and_new(snap: AdObjectSnapshot, intent: AdjustmentIntent) -> tuple[str, str]:
    """按动作取现值字段并推导新值；数值动作缺现值 = 显式拒绝，不猜。

    SCALE_* 预览值按分位（0.01）四舍五入呈现；预览不是执行依据的最终值，
    执行前由执行侧对着源实时核对现值。
    """
    kind = intent.kind
    if kind in (AdjustmentKind.PAUSE, AdjustmentKind.ENABLE):
        new_state = "paused" if kind is AdjustmentKind.PAUSE else "enabled"
        return snap.state or "unknown", new_state
    if kind in (AdjustmentKind.SET_DAILY_BUDGET, AdjustmentKind.SCALE_DAILY_BUDGET):
        current = snap.daily_budget
        field_label = "daily_budget"
    else:  # SET_BID / SCALE_BID：独立竞价优先，其次组默认竞价
        current = snap.bid if snap.bid is not None else snap.default_bid
        field_label = "bid/default_bid"
    if current is None:
        # 消息会原样透到界面（2026-08-29 排查 workbench-3：此前「是哪一行」被算出来
        # 又丢掉，人只能逐个取消勾选二分查找）——所以写中文、带名称、说清怎么办。
        field_cn = "日预算" if field_label == "daily_budget" else "竞价（含组默认竞价）"
        raise WorkbenchError(
            PREVIEW_VALUE_UNAVAILABLE,
            f"「{snap.name or snap.object_key}」（{snap.object_key}）在镜像里没有"
            f"{field_cn}现值，整批预览已停——把它从勾选中移除后重试",
        )
    if kind in (AdjustmentKind.SET_DAILY_BUDGET, AdjustmentKind.SET_BID):
        assert intent.value is not None  # AdjustmentIntent 白名单校验已保证
        return str(current), str(Decimal(intent.value))
    assert intent.percent is not None  # SCALE_*：同上
    scaled = (current * (100 + intent.percent) / 100).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return str(current), str(scaled)


class MirrorExpansionPort:
    """镜像现值实现的 ExpansionPort：只消费显式 ID 选择器（勾选集的唯一产物）。

    勾选来自镜像表本身，所以勾选项在镜像中缺失是异常状态（同步竞态/过期），
    静默丢行会让人对着不完整的预览批准——因此显式拒绝（fail-loud）。
    """

    def __init__(self, repo: SnapshotRepository, intent: AdjustmentIntent) -> None:
        self._repo = repo
        self._intent = intent

    def expand(self, profile_external_id: str, selector: ObjectSelector) -> list[AffectedObject]:
        by_id: dict[str, AdObjectSnapshot] = {
            _external_id(snap): snap
            for snap in self._repo.current(profile_external_id, selector.level)
        }
        missing = [oid for oid in selector.external_ids if oid not in by_id]
        if missing:
            # 消息透到界面，人得知道是哪几个（2026-08-29 排查 workbench-3）。
            shown = "、".join(sorted(missing)[:5])
            more = f" 等 {len(missing)} 个" if len(missing) > 5 else ""
            raise WorkbenchError(
                PREVIEW_OBJECT_NOT_IN_MIRROR,
                f"勾选里有对象不在当前镜像中：{shown}{more}——先同步镜像再重试",
            )
        affected: list[AffectedObject] = []
        for oid in selector.external_ids:
            snap = by_id[oid]
            current_value, new_value = _current_and_new(snap, self._intent)
            affected.append(
                AffectedObject(
                    object_key=snap.object_key,
                    display_name=snap.name or snap.object_key,
                    current_value=current_value,
                    new_value=new_value,
                )
            )
        return affected


# ------------------------------------------------------- 序列化（Decimal→str，datetime→ISO）


def _dec_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _parent_names(snapshots: list[AdObjectSnapshot]) -> dict[str, str]:
    """object_key → name，只收可作父对象的两层（活动 / 广告组）且名称非空。

    取自同一次 repo.current(profile_id) 的全量结果，故与行数据同一时刻、不额外读库。
    """
    parentable = (ObjectLevel.CAMPAIGN, ObjectLevel.AD_GROUP)
    return {
        snap.object_key: snap.name for snap in snapshots if snap.level in parentable and snap.name
    }


def _object_row(snap: AdObjectSnapshot, names: dict[str, str] | None = None) -> dict[str, Any]:
    """行投影。父对象名称解析不到时留 None——UI 据此说「镜像里没有名称记录」，
    那句话必须为真：缺省 names（如历史面）时不返回名称字段，而不是返回一个假的 None。
    """
    row = {
        "object_key": snap.object_key,
        "name": snap.name,
        "state": snap.state,
        "daily_budget": _dec_str(snap.daily_budget),
        "default_bid": _dec_str(snap.default_bid),
        "bid": _dec_str(snap.bid),
        "keyword_text": snap.keyword_text,
        "match_type": snap.match_type,
        "targeting_type": snap.targeting_type,
        "ads_strategy": snap.ads_strategy,
        "is_apply_time": snap.is_apply_time,
        "parent_campaign_id": snap.parent_campaign_id,
        "parent_ad_group_id": snap.parent_ad_group_id,
        "metrics": dict(snap.metrics),
        "source_as_of": snap.source_as_of.isoformat(),
    }
    if names is not None:
        row["parent_campaign_name"] = (
            names.get(f"campaign:{snap.parent_campaign_id}") if snap.parent_campaign_id else None
        )
        row["parent_ad_group_name"] = (
            names.get(f"ad_group:{snap.parent_ad_group_id}") if snap.parent_ad_group_id else None
        )
    return row


def _history_row(snap: AdObjectSnapshot) -> dict[str, Any]:
    return {
        "recorded_at": snap.recorded_at.isoformat(),
        "source_as_of": snap.source_as_of.isoformat(),
        # 这一行的指标统计的是哪一段（#6，2026-08-30）。历史面板是**逐行比大小**的
        # 地方：两条快照并排，「花费 35.00 → 41.20」读起来就是「涨了」。而两轮同步
        # 的窗口可以不同（截断的同步隔天再开一轮，窗口右端跟着 as_of 走），此时
        # 变化里混着「窗口换了」这一项，屏幕上却只有一列时刻。None = 这行不是本进程
        # 同步来的，说不出窗口——不编。
        "report_date": snap.report_date,
        "name": snap.name,
        "state": snap.state,
        "daily_budget": _dec_str(snap.daily_budget),
        "default_bid": _dec_str(snap.default_bid),
        "bid": _dec_str(snap.bid),
        "keyword_text": snap.keyword_text,
        "ads_strategy": snap.ads_strategy,
        "is_apply_time": snap.is_apply_time,
        "metrics": dict(snap.metrics),
    }


def _preview_summary(preview: AdjustmentPreview) -> dict[str, Any]:
    return {
        "directive_id": str(preview.directive_id),
        "level": preview.selector.level.value,
        "expanded_at": preview.expanded_at.isoformat(),
        "affected": [
            {
                "object_key": row.object_key,
                "display_name": row.display_name,
                "current_value": row.current_value,
                "new_value": row.new_value,
            }
            for row in preview.affected
        ],
    }


def _coverage_by_level(report: SyncRunReport) -> dict[str, dict[str, Any]]:
    """把逐工具覆盖聚合到层级。TARGET 层由 targeting + keyword 两张报表共同构成，
    故两者的总数与已覆盖数相加、任一被截断即整层被截断。"""
    by_level: dict[str, dict[str, Any]] = {}
    for c in report.coverage:
        slot = by_level.setdefault(
            c.level, {"source_total": None, "rows_covered": 0, "truncated": False}
        )
        if c.source_total is not None:
            slot["source_total"] = (slot["source_total"] or 0) + c.source_total
        slot["rows_covered"] += c.rows_covered
        slot["truncated"] = slot["truncated"] or c.truncated
    return by_level


def _report_summary(report: SyncRunReport, mirror_epoch: str) -> dict[str, Any]:
    # coverage / truncated / continuation 是给人看「这轮拉全了没有」的唯一依据——
    # 没有它们，界面只能把拿到的行数当成店铺全貌播报（2026-08-29 排查的 P0）。
    return {
        "run_id": report.run_id,
        "profile_id": report.profile_id,
        "started_at": report.started_at.isoformat(),
        "finished_at": report.finished_at.isoformat(),
        "report_date": report.report_date,
        "per_level_rows": dict(report.per_level_rows),
        "truncated": report.truncated,
        "coverage": [
            {
                "tool_id": c.tool_id,
                "level": c.level,
                "source_total": c.source_total,
                "rows_covered": c.rows_covered,
                "truncated": c.truncated,
            }
            for c in report.coverage
        ],
        # 原样回传给 POST /sync 即可从中断处续拉；为 None 表示已无可续拉的。
        # tools 是权威游标（每张表都在里面，含已拉全的）；next_pages 是薄投影，
        # 只为让还握着上一版游标的标签页仍能解析——两者同源，不会互相矛盾。
        "continuation": (
            {
                "report_date": report.report_date,
                "epoch": mirror_epoch,
                "next_pages": report.next_pages(),
                "tools": {
                    tool_id: {
                        "next_page": cur.next_page,
                        "rows_covered": cur.rows_covered,
                        "rows_seen": cur.rows_seen,
                        "source_total": cur.source_total,
                        "complete": cur.complete,
                    }
                    for tool_id, cur in report.tool_cursors().items()
                },
            }
            # 门开在「还有页要拉」上，不开在 truncated 上。truncated 说的是
            # 「覆盖不足」，两件事不是一回事：上游提前给出空页时覆盖确实不足，
            # 但下一页并不存在，此时若还发 continuation，前端那个
            # `do{...}while(wb.continuation)` 就会空转到 300 轮硬上限才无声停下。
            if report.next_pages()
            else None
        ),
        "skipped_summary_rows": report.skipped_summary_rows,
        "decimal_parse_failures": report.decimal_parse_failures,
        "pages_fetched": report.pages_fetched,
        "catalog_versions": dict(report.catalog_versions),
        "schema_versions": dict(report.schema_versions),
    }


# ---------------------------------------------------------------- 同步侧 env 读取


def _allowed_profiles_from_env() -> tuple[str, ...]:
    """ADS_CP_SYNC_PROFILES 逗号分隔；缺省空 = 全拒（SyncEngine fail-closed）。"""
    raw = os.environ.get(ENV_SYNC_PROFILES, "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _max_pages_from_env() -> int:
    raw = os.environ.get(ENV_SYNC_MAX_PAGES)
    if raw is None or not raw.strip():
        return DEFAULT_SYNC_MAX_PAGES
    try:
        value = int(raw.strip(), 10)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=SYNC_CONFIG_INVALID) from exc
    if value < 1:
        raise HTTPException(status_code=409, detail=SYNC_CONFIG_INVALID)
    return value


def _default_read_port_factory(url: str, key: str) -> LxReadPort:
    """默认端口：领星只读 MCP 客户端（结构性防写，白名单外工具永不出网）。"""
    return LxMcpReadClient(url, key)


# ---------------------------------------------------------------- 路由


def build_workbench_router(
    repo: SnapshotRepository,
    verifier: InMemoryActorTokenVerifier,
    *,
    read_port_factory: Callable[[str, str], LxReadPort] | None = None,
    clock: Callable[[], datetime] | None = None,
    profile_currency: Callable[[str], str | None] | None = None,
) -> APIRouter:
    """组装对象工作台路由（前缀 /api/workbench），共享调用方传入的镜像仓库。

    read_port_factory(url, key) 仅测试注入假体用；缺省用 LxMcpReadClient。
    """
    router = APIRouter(prefix="/api/workbench")
    now_fn: Callable[[], datetime] = clock if clock is not None else _utc_now

    # 同步覆盖状态：profile_id → {ObjectLevel.value: {source_total, rows_covered, truncated}}。
    # 与镜像同生命周期（都在内存）。仅由 POST /sync 写入，供 /objects 如实回答
    # 「这张表是不是这个店的全部」。没有它，界面只能把镜像行数当成店铺全貌。
    coverage_state: dict[str, dict[str, dict[str, Any]]] = {}
    # 本次进程的镜像纪元。与 coverage_state / continuations / 镜像本身同生命周期，
    # 盖进每一份 continuation；重启后新纪元不认旧游标（见 SYNC_CURSOR_STALE）。
    mirror_epoch = str(uuid.uuid4())
    # profile_id → 该镜像所属的报表窗口（"YYYY-MM-DD - YYYY-MM-DD"）。
    report_windows: dict[str, str] = {}
    # profile_id → 上一轮截断同步的续拉游标；拉全即为 None。
    # 2026-08-29 审计 #4/#6：游标此前只活在页面 JS 内存里，刷新即丢——覆盖率行
    # 还在报「缺 1.1 万个」，续拉按钮却永远消失。服务端本就持有断点，随 /objects
    # 一并回传，页面重载后续拉条可恢复。
    continuations: dict[str, dict[str, Any] | None] = {}
    port_factory = (
        read_port_factory if read_port_factory is not None else _default_read_port_factory
    )

    def current_actor(
        authorization: Annotated[str | None, Header()] = None,
    ) -> ActorContext:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="AUTHENTICATION_REQUIRED")
        actor = verifier.actor_for(authorization.removeprefix("Bearer "))
        if actor is None:
            raise HTTPException(status_code=401, detail="AUTHENTICATION_REQUIRED")
        return actor

    @router.get("/objects")
    def list_objects(
        actor: Annotated[ActorContext, Depends(current_actor)],
        profile_id: str,
        level: str | None = None,
        page: Annotated[int, Query(ge=1)] = 1,
        length: Annotated[int, Query(ge=1, le=MAX_PAGE_LENGTH)] = DEFAULT_PAGE_LENGTH,
        parent_campaign_id: str | None = None,
        parent_ad_group_id: str | None = None,
        name_contains: str | None = None,
        state: str | None = None,
        managed_only: bool | None = None,
        perf_bucket: str | None = None,
        sort_field: str | None = None,
        sort_dir: str | None = None,
    ) -> dict[str, Any]:
        """镜像现值分页浏览。mirror_empty 指该 profile 整个镜像为空（层级过滤前）。

        筛选与排序都是**浏览端**行为（见上方分节注释），缺省全不生效——不传任何
        新参数时返回与既往逐字一致。顺序为：层级/父对象过滤 → 筛选 → 排序 → 分页；
        排序作用于筛选后的全集而不是当前页，否则「按花费排序」只排了一页等于骗人。
        非白名单的 sort_field / sort_dir 一律 400 显式拒绝——看不懂就当没传会返回比
        人要求更宽或顺序不符的结果集，而人以为筛过、排过了。
        """
        parsed_level = _parse_level(level) if level is not None else None
        if perf_bucket is not None and perf_bucket not in PERF_BUCKETS:
            # 与排序参数同一态度：看不懂就显式拒绝，静默忽略会让人以为筛过了。
            raise HTTPException(status_code=400, detail=PERF_BUCKET_INVALID)
        if sort_field is not None and sort_field not in _SORT_FIELDS:
            raise HTTPException(status_code=400, detail=SORT_FIELD_INVALID)
        if sort_dir is not None:
            if sort_dir not in _SORT_DIRS:
                raise HTTPException(status_code=400, detail=SORT_DIR_INVALID)
            if sort_field is None:
                # 单给方向不给字段什么也排不了；静默返回未排序的结果会让人以为排过了。
                raise HTTPException(status_code=400, detail=SORT_FIELD_INVALID)
        # 全空白的 name_contains 视为未提供（strip 后为空串）。
        needle = (name_contains.strip().lower() or None) if name_contains is not None else None
        all_current = repo.current(profile_id)
        rows = [
            snap
            for snap in all_current
            if (parsed_level is None or snap.level is parsed_level)
            and (parent_campaign_id is None or snap.parent_campaign_id == parent_campaign_id)
            and (parent_ad_group_id is None or snap.parent_ad_group_id == parent_ad_group_id)
            and _matches_filters(snap, needle=needle, state=state, managed_only=managed_only)
        ]
        # 分桶计数对「分桶前」的筛选后全集算：快捷条上的数字要回答的是
        # 「当前筛选下各桶各有多少」，桶自身筛选不参与，否则点一下数字全变。
        bucket_counts: dict[str, int] = {"all": len(rows), **{b: 0 for b in PERF_BUCKETS}}
        for snap in rows:
            bucket = _perf_bucket_of(snap)
            if bucket is not None:
                bucket_counts[bucket] += 1
        if perf_bucket is not None:
            rows = [snap for snap in rows if _perf_bucket_of(snap) == perf_bucket]
        if sort_field is not None:
            rows = _sorted_rows(rows, sort_field, descending=sort_dir == "desc")
        start = (page - 1) * length
        # 父对象名称取自同一份 all_current（层级过滤前的全量），因此浏览「投放」层
        # 时也能显示所属活动/广告组的名字——只发 ID 会让人对着一串 c-1 认不出是哪条。
        names = _parent_names(all_current)
        # 这批行到底来自哪几个窗口——从行本身数，不从同步端那个每轮无条件覆写的
        # profile 级全局值读。#13：截断的同步隔天再开一轮新的，没被重拉到的行仍带着
        # 上一个窗口的花费，而表头写着新窗口；人对着一个写死的窗口把整张表排序、
        # 比大小，行与行之间根本不可比，且屏幕上没有一个字提示过这件事。
        # 统计范围是**过滤后的全部行**而不是当前页：排序与「总计 N 行」都按它算。
        windows = sorted({snap.report_date for snap in rows if snap.report_date is not None})
        unknown_window_rows = sum(1 for snap in rows if snap.report_date is None)
        return {
            "rows": [_object_row(snap, names) for snap in rows[start : start + length]],
            "total": len(rows),
            "bucket_counts": bucket_counts,
            "mirror_empty": not all_current,
            # 「镜像里这层有多少行」不等于「店里这层有多少个对象」。没有这一项，界面只能
            # 把前者当后者播报（2026-08-29 排查的 P0）。None = 本进程没同步过这个 profile
            # （演示种子数据即如此），此时无从断言覆盖率，界面也不该假装知道。
            "level_coverage": coverage_state.get(profile_id, {}).get(
                parsed_level.value if parsed_level else "", None
            ),
            # 这批行是哪个时间窗的。人得知道眼前的花费/ACOS 是哪几天的合计。
            # 只在**这批行确实同属一个窗口**时给出；口径不齐时为 None，让界面去说
            # 「不可横向比较」，而不是挑一个窗口印上去当成全体的标签。
            "report_date": (windows[0] if len(windows) == 1 and unknown_window_rows == 0 else None),
            # 实际出现的窗口清单（空 = 这批行都不是同步来的，如演示种子）。
            "report_windows": windows,
            # 有多少行说不出自己是哪个窗口的。它与「窗口不止一个」是两种不同的不齐，
            # 合成一个布尔会让界面只能说一句含糊的话。
            "report_window_unknown_rows": unknown_window_rows,
            # 有效的续拉游标（None = 已拉全或没同步过）：页面刷新后据此恢复
            # 「继续拉取」入口，而不是让人对着缺口数字干瞪眼（审计 #4/#6）。
            "sync_continuation": continuations.get(profile_id),
        }

    @router.post("/preview")
    def preview_adjustment(
        body: PreviewRequest, actor: Annotated[ActorContext, Depends(current_actor)]
    ) -> dict[str, Any]:
        """勾选 + 意图 → 逐对象「现值 → 新值」预览。只产预览，不产生任何执行。"""
        try:
            kind = AdjustmentKind(body.intent.action)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=coded_detail(PARAMETER_REJECTED, exc)
            ) from exc
        try:
            intent = AdjustmentIntent(
                kind=kind,
                value=body.intent.value,
                percent=body.intent.percent,
                reason=body.intent.reason,
            )
            selected = tuple(
                SelectedObject(
                    level=_parse_level(item.level),
                    external_id=item.external_id,
                    profile_external_id=body.profile_id,
                )
                for item in body.items
            )
        except (ValueError, ArithmeticError) as exc:
            # 域层白名单拒绝（幅度越界/值形状/空 reason）——含 pydantic 包裹的 ValueError
            # 与 Decimal 的 InvalidOperation；错误码沿用既有 PARAMETER_REJECTED。
            raise HTTPException(
                status_code=422, detail=coded_detail(PARAMETER_REJECTED, exc)
            ) from exc
        try:
            selection = SelectionSet(items=selected)
        except SelectionError as exc:
            raise HTTPException(status_code=422, detail=exc.code) from exc
        port = MirrorExpansionPort(repo, intent)
        previews: list[AdjustmentPreview] = []
        try:
            for selector in selection.to_selectors():
                previews.append(
                    build_preview(
                        directive_id=uuid.uuid4(),
                        engagement_id=None,
                        profile_external_id=selection.profile_external_id,
                        selector=selector,
                        intent=intent,
                        port=port,
                        now=now_fn(),
                    )
                )
        except (WorkbenchError, DirectiveError) as exc:
            # 结构化 detail：code 归词典，message 直接给人看（含是哪个对象、怎么办）。
            # UI 的 api() 已按 {code, message} 解析（serverMessage）。只回 code 会把
            # 服务端刚算出的「是哪一行」丢掉（2026-08-29 排查 workbench-3）。
            raise HTTPException(
                status_code=409, detail={"code": exc.code, "message": str(exc)}
            ) from exc
        return {
            "profile_id": selection.profile_external_id,
            "intent": {
                "action": intent.kind.value,
                "value": intent.value,
                "percent": intent.percent,
                "reason": intent.reason,
            },
            "previews": [_preview_summary(p) for p in previews],
            "affected_total": sum(len(p.affected) for p in previews),
            # 常量提示：预览是读侧动作，本身不是执行。注意它不承诺本系统里还有一道
            # 审批——工作台预览没有提交审批的入口，落地是人到领星后台手工改。
            "approval_required": True,
        }

    @router.post("/sync")
    def trigger_sync(
        body: SyncRequest, actor: Annotated[ActorContext, Depends(current_actor)]
    ) -> dict[str, Any]:
        """人触发一轮白名单镜像同步。AI → 403；key 缺失 → 409（fail-closed 显式报错）。"""
        if actor.principal_type is not PrincipalType.HUMAN:
            raise HTTPException(status_code=403, detail=HUMAN_REQUIRED)
        key = os.environ.get(ENV_LX_MCP_KEY, "").strip()
        if not key:
            raise HTTPException(status_code=409, detail=LX_KEY_ABSENT)
        url = os.environ.get(ENV_LX_MCP_URL, "").strip()
        if not url:
            raise HTTPException(status_code=409, detail=LX_URL_ABSENT)
        max_pages = _max_pages_from_env()
        cont = body.continuation
        if cont is not None and cont.epoch != mirror_epoch:
            # 游标描述的那份镜像已经不在了。原样采信它的后果是：标着 complete 的表
            # 一页都不拉，而游标里的 rows_covered 被抄进本轮 coverage 当作事实上报
            # ——黄条写「广告组 250/250」，人切过去表却是空的。宁可让人重新拉一遍。
            raise HTTPException(
                status_code=409,
                detail=coded_detail_message(
                    SYNC_CURSOR_STALE,
                    "服务重启过，内存镜像与断点一起清空了；这个断点描述的那份数据已经不在。"
                    "请点「同步镜像」重新完整拉取。",
                ),
            )
        try:
            engine = SyncEngine(port_factory(url, key), repo, _allowed_profiles_from_env())
            report = engine.run(
                body.profile_id,
                max_pages=max_pages,
                # tools 是权威游标；没有它（老标签页回传的老游标）才回落到 next_pages，
                # 连带回落到"已拉全的表整张重拉"的老行为——老游标解析得了但修不好。
                tool_cursors=(
                    {
                        tool_id: ToolCursor(
                            next_page=c.next_page,
                            rows_covered=c.rows_covered,
                            # 老标签页的游标没有这个数。此处必须猜，而两个方向不
                            # 等价：猜低（当成上游还欠着行）会让空页分支每轮把
                            # next_page 推一格，自动拉取空转到 300 轮硬上限才无声
                            # 停下；猜「上游已交付够数」最多让这一轮少一个重试入口，
                            # 人还有「同步镜像」重新完整拉。取不会空转的那一边。
                            rows_seen=(
                                c.rows_seen
                                if c.rows_seen is not None
                                else (c.source_total or c.rows_covered)
                            ),
                            source_total=c.source_total,
                            complete=c.complete,
                        )
                        for tool_id, c in cont.tools.items()
                    }
                    if cont and cont.tools
                    else None
                ),
                start_pages=(cont.next_pages if cont and not cont.tools else None),
                report_date=cont.report_date if cont else None,
            )
        except SyncError as exc:
            # 白名单拒绝（空白名单/profile 不在名单）——授权性质，403。
            raise HTTPException(status_code=403, detail=exc.code) from exc
        except LxReadError as exc:
            # 上游网关/信封失败——502，code 原样透传给 UI 词典。
            raise HTTPException(status_code=502, detail=exc.code) from exc
        coverage_state[body.profile_id] = _coverage_by_level(report)
        report_windows[body.profile_id] = report.report_date
        summary = _report_summary(report, mirror_epoch)
        continuations[body.profile_id] = summary.get("continuation")
        return summary

    @router.get("/history")
    def object_history(
        object_key: str, actor: Annotated[ActorContext, Depends(current_actor)]
    ) -> dict[str, Any]:
        """对象快照历史（recorded_at 升序）；未知 object_key 即空 entries——无历史是事实。"""
        return {
            "object_key": object_key,
            "entries": [_history_row(snap) for snap in repo.history(object_key)],
        }

    # 店铺名录缓存：profile_id → {"alias", "country"}（ad_auth_shops 一次拉全）。
    # 名称只是展示增强：拿不到（未配通道/上游失败）就退回裸 ID，绝不因此让白名单
    # 回显缺席（fail-open）；成功才缓存，失败下次再试。
    shop_directory_cache: dict[str, dict[str, str | None]] = {}
    shop_directory_loaded = False

    def _shop_directory() -> dict[str, dict[str, str | None]]:
        nonlocal shop_directory_loaded
        if shop_directory_loaded:
            return shop_directory_cache
        key = os.environ.get(ENV_LX_MCP_KEY, "").strip()
        url = os.environ.get(ENV_LX_MCP_URL, "").strip()
        if not key or not url:
            return {}  # 演示/未配通道：没有名称来源，不算失败也不缓存
        try:
            page = port_factory(url, key).fetch_page(AUTH_SHOPS_TOOL_ID, {})
        except LxReadError:
            return {}
        raw_rows = page.get("rows")
        rows = raw_rows if isinstance(raw_rows, list | tuple) else ()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            pid = str(row.get("profile_id") or "").strip()
            if not pid:
                continue
            alias = str(row.get("alias") or "").strip() or None
            country = str(row.get("country") or "").strip() or None
            shop_directory_cache[pid] = {"alias": alias, "country": country}
        # 二轮审计：成功信封但零有效行（上游抖动/schema 漂移）不算成功——此时置
        # loaded 会把空名录缓存到进程死亡，下拉永远退化成裸 ID 且刷新无效。
        # 有真实行才进入终态；空结果按可重试处理，下次请求再拉。
        if shop_directory_cache:
            shop_directory_loaded = True
        return shop_directory_cache

    @router.get("/sync-profiles")
    def sync_profiles(
        actor: Annotated[ActorContext, Depends(current_actor)],
    ) -> dict[str, Any]:
        """同步白名单（ADS_CP_SYNC_PROFILES）回显 + 店铺名录，供 UI 下拉；空 = 未配置全拒。

        2026-08-29 Owner 反馈：下拉里一串 16 位数字 ID，人认不出哪家店。alias/country
        来自领星 ad_auth_shops（领星 ERP 里人就是靠店铺别名认店的）；名录不可得时
        两字段为 None，UI 退回显示 ID。
        """
        directory = _shop_directory()
        return {
            "profiles": [
                {
                    "profile_id": pid,
                    "alias": (directory.get(pid) or {}).get("alias"),
                    "country": (directory.get(pid) or {}).get("country"),
                    # 签发表单据此自动带出结算币种。此前币种是自由文本、默认写死
                    # USD，而界面手里明明存着站点——人被要求手填一个系统已经知道
                    # 的事实，填错了就签出一份运行期恒被 CURRENCY_MISMATCH 拒、
                    # 界面上却一直显示「生效中」的授权书。
                    "currency": profile_currency(pid) if profile_currency else None,
                }
                for pid in _allowed_profiles_from_env()
            ]
        }

    return router


def _utc_now() -> datetime:
    return datetime.now(UTC)
