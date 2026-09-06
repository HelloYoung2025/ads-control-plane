"""候选集合审批 API（人类会话专用；M3 最小审批面，UI 后续套在其上）。

安全语义：
- ActorContext 只来自已验证 Bearer Token（AX-02）；AI token 会在域层 SoD 被拒（AX-05）。
- 批准必须携带 expected_hash——强制审批人对着看过的内容批（AX-07）。
- 跨组织的集合一律 404：不存在与无权访问不可区分（AX-16）。
- 错误码映射：SoD → 403；hash/状态/过期 → 409；未知 set → 404；无 token → 401。
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict

from ads_control_plane.api.errors import coded_detail, coded_detail_message
from ads_control_plane.api.mcp_tools.server import InMemoryActorTokenVerifier
from ads_control_plane.authorization.sod import SoDViolation
from ads_control_plane.canonical.ids import new_canonical_id
from ads_control_plane.canonical.money import Money
from ads_control_plane.identity.actor import ActorContext
from ads_control_plane.mirror.repository import SnapshotRepository
from ads_control_plane.mirror.snapshot import LEVEL_KEY_PREFIX
from ads_control_plane.strategies.mandate import (
    OBJECTIVE_DATA_REQUIREMENTS,
    AutomationMandate,
    MandateBounds,
    MandateObjective,
    MandateScope,
    MandateScopeKind,
    MandateState,
    MandateViolation,
    ObjectiveKind,
    RunWindow,
    assert_can_issue_mandate,
    assert_window_interval_compatible,
    issue_mandate,
)
from ads_control_plane.strategies.mandate_run import (
    NEEDS_ATTENTION,
    MandateRunOutcome,
    MandateRunRecord,
)
from ads_control_plane.strategies.negation import (
    CANDIDATE_SET_TTL_HOURS,
    CandidateSetError,
    CandidateSetState,
    NegationCandidateSet,
    NegationParameterPack,
    candidate_set_expired,
    render_bulk_csv,
    to_bulk_rows,
)
from ads_control_plane.strategies.store import (
    CandidateSetNotFound,
    InMemoryCandidateSetStore,
    InMemoryMandateRunLog,
    InMemoryMandateStore,
    MandateNotFound,
    StaleWrite,
)
from ads_control_plane.tasks.directive import ObjectLevel
from ads_control_plane.tasks.selection import SelectedObject, SelectionError, SelectionSet

SCOPE_KIND_INVALID = "SCOPE_KIND_INVALID"
SCOPE_LEVEL_INVALID = "SCOPE_LEVEL_INVALID"

#: 作用域勾选项的 level 取值（小写惯用形；大写枚举原文同样接受，与工作台一致）。
_LEVEL_ALIASES: dict[str, ObjectLevel] = {
    "campaign": ObjectLevel.CAMPAIGN,
    "ad_group": ObjectLevel.AD_GROUP,
    # 缺 "ad" 会让广告层作用域项拿到 SCOPE_LEVEL_INVALID「不认识这个层」，
    # 而正确的拒绝理由是 MANDATE_SCOPE_LEVEL_UNSUPPORTED「这个目标不能作用到这层」
    # ——对的结果、错的理由，人照着排查会去怀疑拼写。
    "ad": ObjectLevel.AD,
    "target": ObjectLevel.TARGET,
}

#: 层级 → 摘要里的人话量词。UI 直接显示服务端出的这句话，前端不再拼一次。
_LEVEL_LABELS: dict[ObjectLevel, str] = {
    ObjectLevel.CAMPAIGN: "广告活动",
    ObjectLevel.AD_GROUP: "广告组",
    ObjectLevel.AD: "广告",
    ObjectLevel.TARGET: "投放",
}


class ApproveRequest(BaseModel):
    expected_hash: str


class ScopeItemBody(BaseModel):
    """一个被点名的对象。

    profile_external_id 是这个对象**实际所属**的店铺（工作台带入勾选时知道自己
    是在哪个店铺勾的）。2026-08-29 排查（ui-3/mandate-2）：此前该字段不存在，
    服务端把授权书表单里填的店铺直接盖到每个勾选项上——人在 A 店勾了对象、
    表单里改成 B 店，就签出一份对象在 B 店根本不存在、每次运行 0 候选却永远
    显示 ACTIVE 的授权书，域层专门写的 SCOPE_PROFILE_MISMATCH 闸在 HTTP 路径
    上永不触发。现在勾选项自报所属店铺，与授权书店铺不一致时闸正常落下。
    缺省 None = 沿用授权书自己的 profile（手填 ID 的调用方旧行为不变）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    level: str
    external_id: str
    profile_external_id: str | None = None


class ScopeBody(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    items: tuple[ScopeItemBody, ...] = ()


class RunWindowBody(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    timezone: str
    start_hour: int
    end_hour: int


class IssueMandateRequest(BaseModel):
    """签发目标授权：目标 + 参数列表 + 配额与节奏 + 作用域与运行时段。

    响应回显全部合同内容供签发人核对。scope / run_window 都可缺省，缺省行为与
    新增它们之前逐字一致（整店 + 全天）。extra="forbid"：拼错的字段名必须报错，
    静默忽略会让「我明明填了运行时段」的授权书签成一份全天授权。
    """

    model_config = ConfigDict(extra="forbid")

    profile_external_id: str
    objective: ObjectiveKind
    statement: str
    lookback_days: int
    min_spend_amount: str
    currency: str
    min_clicks: int
    max_data_staleness_hours: int
    max_runs_per_day: int
    max_candidates_per_run: int
    valid_days: int
    run_interval_minutes: int = 1440
    #: 这份授权管哪些广告；缺省（None）= 整店。
    scope: ScopeBody | None = None
    #: 系统在哪些当地钟点允许跑；缺省（None）= 全天。
    run_window: RunWindowBody | None = None


def _parse_scope_level(raw: str) -> ObjectLevel:
    level = _LEVEL_ALIASES.get(raw.strip().lower())
    if level is None:
        raise HTTPException(status_code=422, detail=SCOPE_LEVEL_INVALID)
    return level


def _parse_scope_kind(raw: str) -> MandateScopeKind:
    try:
        return MandateScopeKind(raw.strip().upper())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=SCOPE_KIND_INVALID) from exc


def _build_scope(body: IssueMandateRequest) -> MandateScope | None:
    """请求体 → MandateScope。缺省 None（= 整店，与既有授权书同义）。

    勾选项自报所属店铺（见 ScopeItemBody 注释）；未报的沿用授权书的 profile。
    不做覆盖——覆盖会把「对象属于别的店」这一事实抹掉，让域层的
    SCOPE_PROFILE_MISMATCH 闸永不触发（2026-08-29 排查 ui-3/mandate-2）。
    kind 与 items 的相容性（OBJECTS 必须有勾选、PROFILE 不许带勾选）交给
    MandateScope 的构造校验，本函数不重复实现那两条判断。
    """
    if body.scope is None:
        return None
    selection: SelectionSet | None = None
    if body.scope.items:
        selection = SelectionSet(
            items=tuple(
                SelectedObject(
                    level=_parse_scope_level(item.level),
                    external_id=item.external_id,
                    profile_external_id=item.profile_external_id or body.profile_external_id,
                )
                for item in body.scope.items
            )
        )
    return MandateScope(kind=_parse_scope_kind(body.scope.kind), selection=selection)


def _build_run_window(body: IssueMandateRequest) -> RunWindow | None:
    if body.run_window is None:
        return None
    return RunWindow(
        timezone=body.run_window.timezone,
        start_hour=body.run_window.start_hour,
        end_hour=body.run_window.end_hour,
    )


def _scope_summary(scope: MandateScope | None) -> str:
    """作用域的人话摘要，如「整店」「3 个广告活动」「2 个广告活动 · 10 个广告组」。

    None 与 kind=PROFILE 都渲染成「整店」：既有授权书没有这个字段，它们本来就
    是整店授权，摘要里不该出现 null。去重按 (层级, external_id)，与 SelectionSet
    自己的上限判定同口径，避免同一对象被数两遍。
    """
    if scope is None or scope.selection is None:
        return "整店"
    counts: dict[ObjectLevel, int] = {}
    for level, _external_id in {(i.level, i.external_id) for i in scope.selection.items}:
        counts[level] = counts.get(level, 0) + 1
    return " · ".join(
        f"{counts[level]} 个{_LEVEL_LABELS[level]}" for level in ObjectLevel if level in counts
    )


def _run_window_summary(window: RunWindow | None) -> str | None:
    """运行时段的人话摘要，如「每天 02:00–18:00（Asia/Kuala_Lumpur）」。

    None = 没设窗口（不按钟点拦），摘要即 None——UI 据此显示「不限时段」而不是一句
    假的时段。时区名原样带出：不翻译成「吉隆坡时间」，因为 IANA 名才是合同里
    真正生效的那个值，人核对的必须是它。
    """
    if window is None:
        return None
    start, end, tz = window.start_hour, window.end_hour, window.timezone
    if start == end:  # 域层语义：起止同点 = 全天
        return f"不限时段（{tz}）"
    if start < end:
        return f"每天 {start:02d}:00–{end:02d}:00（{tz}）"
    return f"每天 {start:02d}:00 至次日 {end:02d}:00（{tz}）"


def _quota_day_summary(mandate: AutomationMandate) -> str:
    """「N 次/日」里那个「日」的边界，一句人话。

    只说时区不够（2026-08-30 排查）：22:00→次日 06:00 这类跨午夜时段里，当地午夜
    落在窗口正中间，配额日因此从窗口起点算，而不是从当地 0 点。悬停若只写
    「按 Asia/Shanghai 的当地日切换」，人会以为半夜过后配额重置——那正是这次修掉的
    行为，说法留在原处就成了一句描述旧缺陷的话。

    没设运行时段时更要小心措辞：这份授权没有声明过任何时区，只能按 UTC 切，而
    「当地 0 点」对一个 UTC+8 的卖家读起来就是他的 0 点——实际是他的早上 8 点
    （2026-08-30 排查）。签发表单默认就是「不限时段」，所以这是最常见的那一支。
    """
    tz = str(mandate.quota_timezone)
    window = mandate.run_window
    if window is None:
        return (
            "「日」按 UTC 切换（UTC 0 点换一次）——这份授权没设运行时段，"
            "也就没声明过任何时区；它与你所在时区的 0 点通常不是同一时刻"
        )
    if window.crosses_midnight:
        return f"「日」按 {tz} 计，每天 {window.start_hour:02d}:00 换一次——整夜算同一天"
    return f"「日」按 {tz} 的当地日切换（该时区 0 点换一次）"


#: 名称解析器：(level, external_id) → 镜像现值里的名称；解析不到 → None（不编造）。
NameOf = Callable[[ObjectLevel, str | None], str | None]


def _no_name(_level: ObjectLevel, _external_id: str | None) -> str | None:
    return None


def _scope_items(scope: MandateScope | None, name_of: NameOf) -> list[dict[str, Any]] | None:
    """作用域对象清单（含镜像名称）；整店授权为 None。

    2026-08-29 审计 #9：签发那一刻看得见名单，签完列表里只剩「勾选 2 个对象」——
    一周后判断该不该撤销、查重复授权、审计回溯全部变盲。清单是合同内容，必须回显。
    """
    if scope is None or scope.selection is None:
        return None
    return [
        {
            "level": item.level.value,
            "external_id": item.external_id,
            "name": name_of(item.level, item.external_id),
        }
        for item in scope.selection.items
    ]


#: 授权书卡片上回显几次运行。够看出「是一直这样还是刚开始这样」，又不至于把卡片撑成日志。
#: 只用于渲染 recent_runs——配额绝不在这上面数。
_RECENT_RUNS_SHOWN = 5

#: 取全量运行时的 limit。域层的 recent() 只接受 int，没有「不限」这个入参。
_ALL_RUNS = 1_000_000

#: 运行记录解析器：mandate_id → 该授权的**全部**运行（新的在前）。没有流水时恒为空。
#: 必须是全量，不能是截断过的展示列表（2026-09-06 排查）：配额是在这上面数出来的，
#: 而 recent_runs 只取前 _RECENT_RUNS_SHOWN 条。曾经这里返回 5 条，于是日上限 > 5 的
#: 授权跑满后，卡片仍写「今天还能跑 N 次」、悬停还承诺「现在发起不会被挡回」，
#: 而 MCP 面用未截断的 count_on_day 必回 RUN_BUDGET_EXCEEDED——同一份授权的同一个数，
#: 给人的那份和给 AI 的那份不一样，而它俩本来就是同一句话。截断只在渲染点做。
RunsOf = Callable[[uuid.UUID], tuple[MandateRunRecord, ...]]


def _no_runs(_mandate_id: uuid.UUID) -> tuple[MandateRunRecord, ...]:
    return ()


def _run_item(run: MandateRunRecord) -> dict[str, Any]:
    return {
        "ran_at": run.ran_at.isoformat(),
        "outcome": run.outcome.value,
        "candidate_count": run.candidate_count,
        "evaluated_ad_group_terms": run.evaluated_ad_group_terms,
        "distinct_search_terms": run.distinct_search_terms,
        "abstain_count": run.abstain_count,
        "asin_abstain_count": run.asin_abstain_count,
        #: 那几个 ASIN 是哪几个。卡片催人去领星「否定投放」动手，而 ALL_ASIN 这一路
        #: 不创建候选集合——不把词带出来，界面就只能说「这 3 个 ASIN」，永远说不出
        #: 是哪 3 个，人拿着一个数字去后台什么也做不了。
        "asin_abstain_terms": list(run.asin_abstain_terms),
        "scope_filtered_out": run.scope_filtered_out,
        "unjudged_ad_group_terms": run.unjudged_ad_group_terms,
        "unattributable_rows": run.unattributable_rows,
        "set_id": str(run.set_id) if run.set_id is not None else None,
        "error_code": run.error_code,
    }


def _mandate_summary(
    m: AutomationMandate,
    name_of: NameOf = _no_name,
    runs_of: RunsOf = _no_runs,
    now: datetime | None = None,
) -> dict[str, Any]:
    """授权书的合同内容 + **它到底跑成了什么样**。

    2026-08-30 排查 #23：后半截此前完全不存在。签发之后这张卡片再也不会变化——
    币种签错、店铺没接数据源、作用域把对象全挡掉、整批数据太旧，四种「这份授权
    根本跑不通」在界面上与「一切正常、这段时间确实没有该否的词」逐字同形：
    徽章「生效中」，待批空空如也。人得到的唯一信号是「没有新东西要批」。

    运行记录不是日志，是这份合同的履约事实：签了什么、跑了几次、每次结果如何。
    needs_attention 只在**最近一次**运行需要人动手时为真——一份昨天报错、今天
    跑通了的授权不该继续挂着红灯。

    「跑通了」还要求这次是**判断完整**的（2026-08-30 排查 #8/#16）：源侧有一批行
    读不出来时，那些 (广告组, 词) 这一轮根本没被判断过，而结局仍可能是 CANDIDATES
    或 NO_CANDIDATES——两者都是绿灯，两者都不该是。少提几个候选人看不出来，
    人只会看到「批完了，没别的了」。
    """
    runs = runs_of(m.mandate_id)
    latest = runs[0] if runs else None
    #: 「现在发起会不会被拒」——发起运行的是人（本系统不会到点自己跑），而这两个数
    #: 此前只在 MCP 响应里给 AI。人手上只有静态合同值「1 次/日」和「上次运行 14:32」，
    #: 要自己减出间隔、还要读懂跨午夜日界那句散文才知道答案。而被拒的尝试不进流水
    #: （记进去会让配额自耗），卡片纹丝不动——他连「刚才那次到底打到服务端没有」
    #: 都判断不出。同样的两个数，服务端算给 AI 也算给他。
    moment = now if now is not None else _utc_now()
    today = m.quota_day(moment)
    runs_today = sum(1 for r in runs if m.quota_day(r.ran_at) == today)
    next_allowed = (
        latest.ran_at + timedelta(minutes=m.bounds.run_interval_minutes) if latest else None
    )
    #: 日配额先用完时，最小间隔算出来的时刻是假的：到点发起必被 RUN_BUDGET_EXCEEDED
    #: 拒。两道闸并存，「最早可再发起」就得取两者里晚的那个，否则人守到那一刻再点
    #: 一次、再被拒一次，而同一格里还写着「今天的次数已用完」。
    remaining_today = max(0, m.bounds.max_runs_per_day - runs_today)
    if remaining_today == 0:
        day_start = m.next_quota_day_start(moment)
        next_allowed = day_start if next_allowed is None else max(next_allowed, day_start)
    return {
        "runs_today": runs_today,
        "runs_remaining_today": remaining_today,
        "next_run_allowed_at": next_allowed.isoformat() if next_allowed is not None else None,
        # 「从没跑过」与「跑过但没结果」是两件事：前者要看频次/时段是不是还没到点，
        # 后者要看 last_outcome。合成一个 null 会把两种处境揉成一种。
        "run_count_known": bool(runs),
        "last_run_at": latest.ran_at.isoformat() if latest else None,
        "last_outcome": latest.outcome.value if latest else None,
        "last_error_code": latest.error_code if latest else None,
        #: 空手而归 + 有弃权也要催（2026-09-06 排查）。「全部弃权」有 ALL_ABSTAINED
        #  兜着，**部分**弃权一路落到 NO_CANDIDATES，而那个码的界面文案是
        #  「不用做什么，这段窗口确实干净」——同一轮里可能正躺着这个店最大的一笔
        #  零转化花费，只是它那行数据太旧、判不了。判不了不是干净。
        #  只在 NO_CANDIDATES 上加这一条：产出了候选的那一轮，卡片已经在说
        #  「另有 N 个 ASIN 否不掉」，人手上本来就有活，不必再染一次琥珀。
        "needs_attention": latest is not None
        and (
            latest.outcome in NEEDS_ATTENTION
            or latest.unjudged_ad_group_terms > 0
            or latest.unattributable_rows > 0
            or (latest.outcome is MandateRunOutcome.NO_CANDIDATES and latest.abstain_count > 0)
        ),
        #: 上一轮有几条没判成——界面据此决定还能不能说「这段窗口确实干净」。
        "last_abstain_count": latest.abstain_count if latest else 0,
        "recent_runs": [_run_item(r) for r in runs[:_RECENT_RUNS_SHOWN]],
        "scope_items": _scope_items(m.scope, name_of),
        "mandate_id": str(m.mandate_id),
        "state": m.state.value,
        "profile_external_id": m.profile_external_id,
        "objective": m.objective.objective,
        "statement": m.objective.statement,
        "parameter_pack": m.parameter_pack.model_dump(mode="json"),
        "parameter_pack_hash": m.parameter_pack.content_hash(),
        "bounds": m.bounds.model_dump(mode="json"),
        "issued_by_person_id": m.issued_by_person_id,
        "issued_at": m.issued_at.isoformat(),
        "expires_at": m.expires_at.isoformat(),
        "revoked_by_person_id": m.revoked_by_person_id,
        # 人话摘要在服务端合成：作用域与时段是合同内容，人核对的那句话不该由
        # 前端各拼一份（拼错了没人发现，且换个客户端就换个说法）。
        "scope_summary": _scope_summary(m.scope),
        "run_window_summary": _run_window_summary(m.run_window),
        #: 结构化的作用域类型与时段，专供「照这份再签一份」把表单填回去。
        #  摘要那两句是给人读的，拼不回表单——而到期重签是这套设计里**必然**会发生的
        #  动作（授权最长 30 天、默认 7 天），此前界面对它零支持：过期行连「复制指令」
        #  都收起来，动作列只剩一个对死授权毫无意义的「撤销」。
        #: None 与 kind=PROFILE 都答 "PROFILE"，与 _scope_summary 同口径——那边
        #  两种情况都渲染成「整店」，这边答 null 就会让克隆把一份整店授权拨成
        #  「只管勾选的对象」而清单是空的，签发当场被 SCOPE_SELECTION_REQUIRED 拒。
        "scope_kind": (
            m.scope.kind.value if m.scope is not None else MandateScopeKind.PROFILE.value
        ),
        "run_window": (
            {
                "timezone": m.run_window.timezone,
                "start_hour": m.run_window.start_hour,
                "end_hour": m.run_window.end_hour,
            }
            if m.run_window is not None
            else None
        ),
        # 「N 次/日」里的「日」按哪个时区切。人核对的是卡片上那句话，而「一天」在
        # 没有时区的情况下会被按自己的钟点理解——UTC+8 的人读「3 次/日」，指的是
        # 他的一天。配额与运行时段现在共用同一个「天」，这里把它说出来。
        "quota_timezone": str(m.quota_timezone),
        # 时区不足以说清日界：跨午夜时段的配额日从窗口起点算，不从当地 0 点算。
        # 与 scope_summary / run_window_summary 同理，人话在服务端合成一份。
        "quota_day_summary": _quota_day_summary(m),
    }


def _summary(
    candidate_set: NegationCandidateSet,
    now: datetime,
    name_of: NameOf = _no_name,
    mandate_state: str | None = None,
    same_content_as: list[str] | None = None,
) -> dict[str, Any]:
    # 过期状态由服务端算（域层 approve 也是按同一个 TTL 拒的），不让前端各拼一份。
    # 2026-08-29 排查（approval-3）：此前 summary 不含过期信息，过期集合在界面上与
    # 能批的一模一样——按钮亮着、永远占着「待批」KPI，点了才 409 SET_EXPIRED。
    expires_at = candidate_set.generated_at + timedelta(hours=CANDIDATE_SET_TTL_HOURS)
    return {
        "set_id": str(candidate_set.set_id),
        "state": candidate_set.state.value,
        "set_hash": candidate_set.set_hash,
        # 「这两份是不是同一批发现」——set_hash 回答不了：每条候选的编号都进它，
        # 于是内容逐字相同的两次生成必得两个不同的 hash。人不会去逐词比对两张卡片。
        "content_fingerprint": candidate_set.content_fingerprint(),
        # None = 没查（单份查询不看别的集合），[] = 查了、没有同内容的。两者不同：
        # 把「没查」渲染成「没有重复」就是又一次把沉默说成好消息。
        "same_content_as": same_content_as,
        "generated_at": candidate_set.generated_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        # 仅对还停在 FROZEN 的集合有意义：已批/已拒的集合谈不上「过没过期」。
        # 判据来自域层（candidate_set_expired），不在这里另写一个：早一瞬说过期，
        # 人就会照着「拒绝后重新生成」赔掉一天配额，去换一份服务端本来还肯批的集合。
        "expired": candidate_set.state is CandidateSetState.FROZEN
        and candidate_set_expired(candidate_set.generated_at, now),
        "source": candidate_set.source,
        "candidate_count": len(candidate_set.candidates),
        # 这批是哪家店的。跨 profile 的集合不该存在，真出现就回 None 而不是挑一个。
        "profile_external_id": candidate_set.profile_external_id,
        # 出处授权书与它此刻的状态。撤销一份授权书时，它今早生成的集合原样留在
        # 「待批」里，与好集合逐字同形——同样的徽章、同样可点的「批准」按钮，
        # 卡片上没有任何线索指向来源。而撤销确认框刚跟人说过「AI 立即停止按它运行」。
        "mandate_id": str(candidate_set.mandate_id) if candidate_set.mandate_id else None,
        "mandate_state": mandate_state,
        # 截断前命中多少个。此前只进 MCP 返回值，签字的人看到的只有截断后的数字。
        "truncated_from": candidate_set.truncated_from,
        "asin_abstain_count": candidate_set.asin_abstain_count,
        #: 词表随集合一起冻结才说得出「是哪几个」。只回计数等于让人知道有钱在烧、
        #  却说不出烧在哪，而卡片正是在他签完字要去领星的那一刻提这件事。
        "asin_abstain_terms": list(candidate_set.asin_abstain_terms),
        #: 即席生成（mandate_id 为 None）的卡片上挂着一句「批准前请自己核对参数区间」
        #  ——而参数从来没送到过卡片上，人找一圈找不到，只能放弃核对直接批。
        #  授权书面早就回这个字段（前端渲染成「回看 30 天 · ≥25 点击 · …」），
        #  集合面漏了；参数随集合一起冻结，不是新事实。
        "parameter_pack": candidate_set.parameter_pack.model_dump(mode="json"),
        "candidates": [
            {
                "search_term": c.search_term,
                "ad_group_external_id": c.scope.entity_external_id,
                # 名称优先取候选自带的（与证据出自同一行、随集合一起冻结），拿不到
                # 才回落镜像现值。审计 #5 只做了后一半，而 2026-08-30 实测：真实店
                # 11,723 个活动、镜像默认只拉 300 个（2.5%），7 条候选 0 条解析得出
                # 名字——「两串 16 位数字让批准变盲签」原样还在，只是换了个原因。
                # 反过来（镜像优先）还会让另一个时点的名字盖掉审批人看过的那个。
                # 两者都没有时为 None，UI 退回显示 ID——不编造。
                "ad_group_name": (
                    c.ad_group_name or name_of(ObjectLevel.AD_GROUP, c.scope.entity_external_id)
                ),
                "campaign_external_id": c.scope.parent_refs.campaign_external_id,
                "campaign_name": (
                    c.campaign_name
                    or name_of(ObjectLevel.CAMPAIGN, c.scope.parent_refs.campaign_external_id)
                ),
                "spend": str(c.evidence.spend.amount),
                "currency": c.evidence.spend.currency,
                "clicks": c.evidence.clicks,
                "conversions": c.evidence.conversions,
                #: 不参与判定，只给人看——但 42 次点击来自 300 次曝光还是 6 万次，
                #: 指向的是相反的结论，而其余三个数在两种情形下逐字相同。
                "impressions": c.evidence.impressions,
                # 这三个时刻 CandidateEvidence 一直带着（negation.py:117），
                # 序列化时却被丢掉，于是卡片上「生成于今天」与「0 转化」并排出现，
                # 人读成「今天查的，这个词到今天一单没出」。真相是窗口右端已被归因
                # 滞后刻意往回推了几天——最近那几天根本没看。两个数字各自都对，
                # 摆在一起就把一个刻意的滞后变成了不存在，而窗口有多长恰恰是
                # 「该不该否定这个词」的关键前提。
                "window_start": c.evidence.window_start.isoformat(),
                "window_end": c.evidence.window_end.isoformat(),
                "data_as_of": c.evidence.data_as_of.isoformat(),
            }
            for c in candidate_set.candidates
        ],
        "approved_by_person_id": candidate_set.approved_by_person_id,
        #: 批准时刻。域层从一开始就在写它（approve()），只是一路没端出来，于是「已批」
        #: 列表只能按**生成**时间倒序，而人回头对账问的是「哪份是我刚批的」。
        #: 周日生成周二才批的那份会排在周一生成周一就批的下面，最上面那张不是最新批的。
        "approved_at": (
            candidate_set.approved_at.isoformat() if candidate_set.approved_at is not None else None
        ),
    }


def build_approval_app(
    store: InMemoryCandidateSetStore,
    verifier: InMemoryActorTokenVerifier,
    clock: Callable[[], datetime] | None = None,
    mandates: InMemoryMandateStore | None = None,
    snapshot_repo: SnapshotRepository | None = None,
    profile_currency: Callable[[str], str | None] | None = None,
    run_log: InMemoryMandateRunLog | None = None,
) -> FastAPI:
    app = FastAPI(title="ads-control-plane approval", docs_url=None, redoc_url=None)
    now_fn: Callable[[], datetime] = clock if clock is not None else _utc_now

    def runs_of(mandate_id: uuid.UUID) -> tuple[MandateRunRecord, ...]:
        """该授权的全部运行（新的在前）。没接流水时恒为空——空是真话，不编。

        全量而非 _RECENT_RUNS_SHOWN 条：配额在这上面数，展示才截断（见 RunsOf）。
        """
        if run_log is None:
            return ()
        return run_log.recent(mandate_id, _ALL_RUNS)

    def mandate_state_of(candidate_set: NegationCandidateSet) -> str | None:
        """集合出处授权书此刻的状态；即席生成或查不到时为 None。"""
        if mandates is None or candidate_set.mandate_id is None:
            return None
        try:
            return mandates.get(candidate_set.mandate_id).state.value
        except MandateNotFound:
            return None

    def summary_of(candidate_set: NegationCandidateSet) -> dict[str, Any]:
        return _summary(candidate_set, now_fn(), name_of, mandate_state_of(candidate_set))

    def name_of(level: ObjectLevel, external_id: str | None) -> str | None:
        """镜像现值名称解析（跨 profile 按 object_key 查最新快照）；缺镜像 → None。"""
        if snapshot_repo is None or not external_id:
            return None
        entries = snapshot_repo.history(LEVEL_KEY_PREFIX[level] + external_id)
        return entries[-1].name if entries else None

    def _assert_currency_matches_the_data(profile_external_id: str, currency: str) -> None:
        """签发期就拒币种不符，别让人签一份物理上跑不通的合同。

        运行期的闸在 negation.py:190（CURRENCY_MISMATCH）。放行到那里的后果不是
        「运行时报个错」——生成走的是 MCP 面，这个码在 Web 界面上一次都不会出现：
        授权书卡片一直显示「生效中」，「待批」页签一直空着并安慰人说候选会在
        生成之后出现在这里。人于是一直等，而每一块界面都在说一切正常。他分不清「查了没有浪费」「根本没查」和「这份授权从签下
        那一刻起就不可能跑通」。

        mandate.py:157 的 _scope_belongs_to_this_profile 为完全相同的病写了兄弟闸，
        理由逐字相同：「不一致时若放行，运行期的闸会让这份授权永远跑不出东西，
        而人看到的是一份状态 ACTIVE 的正常授权书。签发期就拒。」币种这一条一直没配上。

        查不到该店币种时不设闸：我们确实无法校验，装作能校验比不校验更坏。
        """
        if profile_currency is None:
            return
        actual = profile_currency(profile_external_id)
        if actual is None or actual == currency:
            return
        raise HTTPException(
            status_code=422,
            detail=coded_detail_message(
                "CURRENCY_MISMATCH",
                f"这个店铺的数据以 {actual} 结算，授权书填的是 {currency}；"
                f"把结算币种改成 {actual} 再签发",
            ),
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

    def _get_visible(set_id: uuid.UUID, actor: ActorContext) -> NegationCandidateSet:
        try:
            found = store.get(set_id)
        except CandidateSetNotFound as exc:
            raise HTTPException(status_code=404, detail="RESOURCE_UNAVAILABLE") from exc
        if found.organization_id != actor.organization_id:
            raise HTTPException(status_code=404, detail="RESOURCE_UNAVAILABLE")
        return found

    @app.get("/candidate-sets")
    def list_sets(
        actor: Annotated[ActorContext, Depends(current_actor)], state: str | None = None
    ) -> dict[str, Any]:
        try:
            # 大小写宽容：frozen 和 FROZEN 是同一个意思，没必要让调用方背枚举的大小写。
            parsed_state = CandidateSetState(state.strip().upper()) if state else None
        except ValueError as exc:
            # 2026-08-29 排查（approval-5）：写错状态名此前直接 500 裸文本，调用方
            # 分不清「我参数写错了」和「服务器坏了」。参数不合法是 422 的事。
            raise HTTPException(status_code=422, detail="CANDIDATE_STATE_INVALID") from exc
        sets = store.list_by_state(actor.organization_id, parsed_state)
        # 同内容的重复只在**这份清单里**认定：人看的就是这一屏。
        # 2026-08-30 实测（真实通道）：连续两次即席生成，第二次全部命中缓存、输入
        # 逐行相同，产出两份 7 条候选的 FROZEN 集合并排躺在待批里，set_hash 不同。
        # 界面把 set_hash 叫「内容指纹」，于是这两张卡片主动教人读成两批不同的发现。
        by_content: dict[str, list[str]] = {}
        for item in sets:
            by_content.setdefault(item.content_fingerprint(), []).append(str(item.set_id))
        return {
            "candidate_sets": [
                _summary(
                    item,
                    now_fn(),
                    name_of,
                    mandate_state_of(item),
                    same_content_as=[
                        other
                        for other in by_content[item.content_fingerprint()]
                        if other != str(item.set_id)
                    ],
                )
                for item in sets
            ]
        }

    def _commit(previous: NegationCandidateSet, updated: NegationCandidateSet) -> dict[str, Any]:
        """把域层算出的终态写回，且只在库里那一份还没被别人判过时才写。

        走到这里只可能有一种冲突：另一个会话在这几毫秒里已经批了或拒了同一批。
        那么此刻它确实不在 FROZEN——这与顺序发生时域层给的是同一个码，界面词典
        里已经有它（NOT_FROZEN），前端紧接着的 refreshAll 会把真实状态摆出来。
        """
        try:
            store.save_if_state_unchanged(previous, updated)
        except StaleWrite as exc:
            raise HTTPException(status_code=409, detail="NOT_FROZEN") from exc
        return summary_of(updated)

    @app.post("/candidate-sets/{set_id}/approve")
    def approve(
        set_id: uuid.UUID,
        body: ApproveRequest,
        actor: Annotated[ActorContext, Depends(current_actor)],
    ) -> dict[str, Any]:
        found = _get_visible(set_id, actor)
        if mandate_state_of(found) == MandateState.REVOKED.value:
            # 人刚在撤销确认框里读到「撤销后 AI 立即停止按它运行」。批准这份授权
            # 已经生出来的产物，执行的正是他判定为「签错了、要停掉」的那套参数。
            # 与 SET_EXPIRED 同规格 fail-closed：拒绝后重新生成，而不是让人在
            # 一张与好集合逐字同形的卡片上按下「批准」。
            raise HTTPException(
                status_code=409,
                detail=coded_detail_message(
                    "MANDATE_REVOKED",
                    "这批候选来自一份已被撤销的授权书；撤销时你要停的就是它。"
                    "先拒绝这一批，需要的话重新签发授权书再生成。",
                ),
            )
        try:
            approved = found.approve(actor, body.expected_hash, now_fn())
        except SoDViolation as exc:
            raise HTTPException(status_code=403, detail=exc.code) from exc
        except CandidateSetError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from exc
        return _commit(found, approved)

    @app.post("/candidate-sets/{set_id}/reject")
    def reject(
        set_id: uuid.UUID, actor: Annotated[ActorContext, Depends(current_actor)]
    ) -> dict[str, Any]:
        found = _get_visible(set_id, actor)
        try:
            rejected = found.reject(actor)
        except SoDViolation as exc:
            raise HTTPException(status_code=403, detail=exc.code) from exc
        except CandidateSetError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from exc
        return _commit(found, rejected)

    if mandates is not None:
        mandate_store = mandates

        @app.post("/mandates")
        def create_mandate(
            body: IssueMandateRequest, actor: Annotated[ActorContext, Depends(current_actor)]
        ) -> dict[str, Any]:
            # 授权判定必须排在**任何**参数校验之前。币种不符的 422 文案点名该店真实
            # 结算币种，而域层明令 AI 不能签发授权书——先校验参数就等于把这个端点
            # 变成一个币种探针，不该动手的人连参数错在哪都不该知道。
            # issue_mandate 内部同样会判（非 HTTP 调用方的保证），此处提前不削弱它。
            try:
                assert_can_issue_mandate(actor)
            except MandateViolation as exc:
                raise HTTPException(status_code=403, detail=exc.code) from exc
            _assert_currency_matches_the_data(body.profile_external_id, body.currency)
            #: 金额单独先解析（2026-09-06 排查）：Decimal 解析失败时 InvalidOperation
            #  的 str 是「[<class 'decimal.ConversionSyntax'>]」，原样进 message 就是
            #  一个 Python 内部异常类名，8 个可填参数一个都没点名。而这个字段是表单里
            #  唯一没有 type=number 的数值框，人填「1,234」或「$20」是常事。
            try:
                min_spend_amount = Decimal(body.min_spend_amount)
            except InvalidOperation as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "MIN_SPEND_NOT_A_NUMBER",
                        "message": (
                            "min_spend_amount 不是一个数：只填数字和小数点，"
                            "不要千分位逗号、货币符号或单位（例如 20.00）"
                        ),
                    },
                ) from exc
            try:
                pack = NegationParameterPack(
                    lookback_days=body.lookback_days,
                    min_spend=Money(amount=min_spend_amount, currency=body.currency),
                    min_clicks=body.min_clicks,
                    max_data_staleness_hours=body.max_data_staleness_hours,
                )
                bounds = MandateBounds(
                    max_runs_per_day=body.max_runs_per_day,
                    max_candidates_per_run=body.max_candidates_per_run,
                    valid_days=body.valid_days,
                    run_interval_minutes=body.run_interval_minutes,
                )
                objective = MandateObjective(objective=body.objective, statement=body.statement)
                scope = _build_scope(body)
                run_window = _build_run_window(body)
                # 「频次与限定时段不相容」是请求两个字段的组合形状问题，不是授权
                # 判定，所以在这里提前判一次好落 422。issue_mandate 内部同样会判
                # （非 HTTP 调用方的保证），此处提前不削弱它，只是让状态码说实话。
                assert_window_interval_compatible(run_window, bounds)
            except (SelectionError, MandateViolation) as exc:
                # 构造期 = 参数形状。作用域/时段/勾选集的校验器抛的是带 code 的自定义
                # 异常（不是 ValueError），具体码原样透出，不被压成笼统的
                # PARAMETER_REJECTED——人得知道是时区不认识还是勾选超了 200 个。
                raise HTTPException(status_code=422, detail=exc.code) from exc
            except (ValueError, InvalidOperation) as exc:
                raise HTTPException(
                    status_code=422, detail=coded_detail("PARAMETER_REJECTED", exc)
                ) from exc
            try:
                # 签发期 = 授权判定（AI 不可签、目标未就绪、作用域越权）→ 403。
                mandate = issue_mandate(
                    actor,
                    mandate_id=new_canonical_id(),
                    profile_external_id=body.profile_external_id,
                    objective=objective,
                    parameter_pack=pack,
                    bounds=bounds,
                    now=now_fn(),
                    scope=scope,
                    run_window=run_window,
                )
            except MandateViolation as exc:
                raise HTTPException(status_code=403, detail=exc.code) from exc
            mandate_store.save(mandate)
            return _mandate_summary(mandate, name_of, runs_of, now_fn())

        @app.get("/mandates")
        def list_mandates(
            actor: Annotated[ActorContext, Depends(current_actor)],
        ) -> dict[str, Any]:
            return {
                "mandates": [
                    _mandate_summary(m, name_of, runs_of, now_fn())
                    for m in mandate_store.list_for_org(actor.organization_id)
                ]
            }

        @app.get("/mandates/profile-currency")
        def read_profile_currency(
            _actor: Annotated[ActorContext, Depends(current_actor)],
            profile_external_id: str,
        ) -> dict[str, Any]:
            """这个店的数据用什么币种结算——签发前先告诉人，别让他撞 422 才知道。

            这个事实服务端一直握着（组合根注入的 profile_currency），签发闸也一直在
            用它 422 拒签并点名实际币种。界面却从另一条路取：同步白名单端点
            （/api/workbench/sync-profiles）。那条路答的是「哪些店允许被同步」，
            纯 Mock 部署下恒为空，于是币种字段恒说「服务端不知道这个店铺的数据币种，
            无法替你确认」——一句假话，而它下面紧跟着的后果是真的：填错会被拒。

            接了真实同步通道、但没开策略侧真实取数时更糟：每个真实店都落进这句
            「不知道」，USD 默认值原样留着，德国站签出的授权书每次运行都被
            CURRENCY_MISMATCH 拒，而那个码在 Web 界面上一次都不会出现。

            答不出就答 null——「不知道」本身是真话，只是不该在服务端知道时说。
            不返回店铺清单，只回答被问到的那一个：AX-16 不靠这个端点枚举资源。
            """
            return {
                "profile_external_id": profile_external_id,
                "currency": (profile_currency(profile_external_id) if profile_currency else None),
            }

        @app.get("/mandates/objectives")
        def list_objective_readiness(
            _actor: Annotated[ActorContext, Depends(current_actor)],
        ) -> dict[str, Any]:
            """各目标的就绪度事实：ready + 缺失的数据依赖清单。

            UI 靠它在**选目标那一刻**就说清「这个还不能签、缺哪几样」并藏掉无意义
            的打法卡片。2026-08-29 排查（ui-1/mandate-6/approval-1）：此前端点不存在，
            UI 永远走降级分支，4 个目标里 3 个必然被 403 拒、人却要填完整张表才知道。
            数据来源就是签发闸用的 OBJECTIVE_DATA_REQUIREMENTS——同一份事实，
            不另造快照。
            """
            return {
                "objectives": [
                    {
                        "objective": kind.value,
                        "ready": not missing,
                        "missing": list(missing),
                    }
                    for kind, missing in OBJECTIVE_DATA_REQUIREMENTS.items()
                ]
            }

        @app.post("/mandates/{mandate_id}/revoke")
        def revoke_mandate(
            mandate_id: uuid.UUID, actor: Annotated[ActorContext, Depends(current_actor)]
        ) -> dict[str, Any]:
            try:
                mandate = mandate_store.get(mandate_id)
            except MandateNotFound as exc:
                raise HTTPException(status_code=404, detail="RESOURCE_UNAVAILABLE") from exc
            if mandate.organization_id != actor.organization_id:
                raise HTTPException(status_code=404, detail="RESOURCE_UNAVAILABLE")
            try:
                revoked = mandate.revoke(actor)
            except MandateViolation as exc:
                raise HTTPException(status_code=403, detail=exc.code) from exc
            mandate_store.save(revoked)
            return _mandate_summary(revoked, name_of, runs_of, now_fn())

    @app.get("/candidate-sets/{set_id}/export.csv")
    def export_csv(
        set_id: uuid.UUID, actor: Annotated[ActorContext, Depends(current_actor)]
    ) -> Response:
        found = _get_visible(set_id, actor)
        try:
            csv_text = render_bulk_csv(
                to_bulk_rows(found),
                # 追加名称列（审计 #5）：运营在领星后台按名称导航，纯 ID 要逐行反查。
                name_of=lambda kind, ext: name_of(
                    ObjectLevel.CAMPAIGN if kind == "campaign" else ObjectLevel.AD_GROUP, ext
                ),
            )
        except CandidateSetError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from exc
        return Response(
            content=csv_text,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="negation-{set_id}.csv"',
            },
        )

    return app


def _utc_now() -> datetime:
    return datetime.now(UTC)
