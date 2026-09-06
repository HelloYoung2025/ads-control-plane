"""策略工具的业务实现（协议无关，MCP 壳在 server.py 组装）。

AI（经 Codex 等客户端）可以做的事到"生成候选集合并冻结待批"为止（AX-05）：
- generate_negation_candidate_set：授权动作 = PROPOSAL_CREATE_DRAFT（AI 上限内）；
- list_negation_candidate_sets：授权动作 = RESOURCE_READ。
批准/拒绝不在本工具面——审批只属于人类会话（approval_api）。
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from datetime import datetime, timedelta
from typing import Any

from ads_control_plane.api.errors import denial_hint
from ads_control_plane.api.mcp_tools.service import ToolDenied
from ads_control_plane.authorization.engine import evaluate
from ads_control_plane.authorization.model import (
    AccessRequest,
    Action,
    ClientType,
    Environment,
    ExplicitDeny,
    Grant,
)
from ads_control_plane.canonical.ids import new_canonical_id
from ads_control_plane.canonical.money import Money
from ads_control_plane.identity.actor import ActorContext, PrincipalType
from ads_control_plane.strategies.mandate import (
    AutomationMandate,
    MandateViolation,
    assert_run_authorized,
)
from ads_control_plane.strategies.mandate_run import MandateRunOutcome, MandateRunRecord
from ads_control_plane.strategies.negation import (
    CANDIDATE_SET_TTL_HOURS,
    AbstainReason,
    CandidateSetError,
    CandidateSetState,
    NegationCandidateSet,
    NegationParameterPack,
    NegationRunResult,
    SearchTermRecord,
    candidate_set_expired,
    generate_negation_candidates,
)
from ads_control_plane.strategies.ports import (
    SearchTermFetch,
    SearchTermReadPort,
    SearchTermSourceError,
    UnjudgedGroup,
)
from ads_control_plane.strategies.store import (
    InMemoryCandidateSetStore,
    InMemoryMandateRunLog,
    InMemoryMandateStore,
    MandateNotFound,
)

#: 默认参数包取值（白名单内的保守档）。调用方覆盖仍受 NegationParameterPack 校验。
_DEFAULT_LOOKBACK_DAYS = 30
_DEFAULT_MIN_SPEND = "20.00"
_DEFAULT_CURRENCY = "USD"
_DEFAULT_MIN_CLICKS = 25
_DEFAULT_MAX_STALENESS_HOURS = 24

#: 响应里最多列几条弃权。candidates 早有 truncated_from 保护，abstains 一直没有。
#: 2026-08-30 实测这不是理论问题：真实店一批弃权 3815 条 × 约 134 字节 ≈ 512 KB，
#: MCP 传输当场断流，客户端只拿到 "SSE stream ended without a response" ——
#: 零信息，人连"是数据的问题还是服务器的问题"都判断不了。而这 3815 条弃权原因
#: 完全相同，信息量等于 1 条：列全了不是更诚实，是把一句话拆成三千遍还说不到位。
#: 真正要说清楚的是总数，由 abstain_count 恒如实给出。
_MAX_ABSTAINS_IN_RESPONSE = 50

#: 这些拒绝的补充说明可以原样带给调用方：它们讲的全是**这份授权自己的合同事实**
#: （最短间隔多少、距上次多久、下一个时段几点开、日配额几次），也正是调用方唯一
#: 能据以行动的那句话。其余码只回码——例如 SCOPE_MISMATCH 的 message 会说出
#: 「这份授权属于另一个组织」，那是 AX-16 要挡住的资源存在性。
_ACTIONABLE_VIOLATION_CODES = frozenset(
    {"RUN_TOO_SOON", "RUN_BUDGET_EXCEEDED", "OUTSIDE_RUN_WINDOW", "MANDATE_EXPIRED"}
)

#: 即席模式（不带 mandate_id）的单次候选上限。授权模式的上限来自合同
#: （bounds.max_candidates_per_run，白名单区间 [1, 200]），即席模式此前**没有上限**。
#: 后果不在机器这一侧：一次即席运行可以冻结出几百条候选，卡片上是一张几百行的表，
#: 而「批准」是一个按钮、一次点击。AX-07 要求被批准的内容就是被看见的内容——
#: 几百行没有人真的看过。取白名单上限本身（200）而不是更小的数：它是人签合同时
#: 能填的最大值，即席不该比人能授权的还宽，也不该比它更严而无从解释。
#: 与授权模式共用同一条路径：超出即截断并如实回报 truncated_from（卡片上写
#: 「本次共命中 N 个」），不静默丢弃。
_AD_HOC_MAX = 200


def _unjudged_within_scope(
    groups: tuple[UnjudgedGroup, ...], mandate: AutomationMandate | None
) -> int:
    """这些没被判断的组里，有几个落在这份授权圈定的范围内。

    不筛的后果是同一条运行记录上并排两个口径的数：「已评估 40 组」「作用域挡掉
    2700 组」是作用域口径，而「未判断 260 组」是全店口径。一份只管 1 个活动、
    自己范围内一切正常的授权书，会因为店里别处的坏数据永远挂着红灯——红灯的意义
    随之作废。反方向更糟：作用域内的组全部读不出来时，结局落到 SCOPE_EMPTY，
    界面叫人「改作用域后重签」，而作用域本来是对的。

    用的是和记录过滤同一个谓词（scope.covers），不另写一套匹配规则。

    一个活动 id 都拿不到的组（该组每一行都缺 campaign_id）按**落在圈内**算：
    我们排除不了它。这个方向是刻意选的——多说一次「有东西没判断」的代价是一次
    多余的提醒，漏说的代价是人把一份不完整的结论当成「这段窗口很干净」，
    而那正是这套计数存在的全部理由。UnjudgedGroup 的 docstring 写的是同一条。
    """
    if mandate is None or mandate.scope is None or mandate.scope.selection is None:
        return len(groups)
    scope = mandate.scope

    def in_scope(group: UnjudgedGroup) -> bool:
        if not group.campaign_external_ids:
            return True
        return any(
            scope.covers(ad_group_external_id=group.ad_group_external_id, campaign_external_id=c)
            for c in group.campaign_external_ids
        )

    return sum(1 for g in groups if in_scope(g))


def _within_mandate_scope(
    records: tuple[SearchTermRecord, ...], mandate: AutomationMandate
) -> tuple[tuple[SearchTermRecord, ...], int]:
    """按授权书作用域过滤策略输入。返回 (留下的记录, 被作用域挡掉的条数)。

    在**读到数据之后、生成候选之前**过滤，而不是生成完再筛：这样 evaluated_ad_group_terms
    与 abstains 描述的都是"这份授权真正看过的东西"，人核对时不会看到一份声称
    评估了整店、却只管 3 个活动的报告。

    整店作用域（scope=None 或 kind=PROFILE）恒等于不过滤，既有授权书行为逐字不变。

    没有这一层的后果不是"少一个功能"，是静默扩权：人签的是"只管这个活动"，
    系统照样在全店生成候选，而授权书状态 ACTIVE、摘要写着「1 个广告活动」。
    """
    scope = mandate.scope
    if scope is None or scope.selection is None:
        return records, 0
    kept = tuple(
        r
        for r in records
        if scope.covers(
            ad_group_external_id=r.scope.entity_external_id,
            campaign_external_id=r.scope.parent_refs.campaign_external_id,
        )
    )
    return kept, len(records) - len(kept)


def _empty_run_outcome(
    *,
    profile_has_data_source: bool,
    fetch: SearchTermFetch,
    unjudged_in_scope: int,
    result: NegationRunResult,
) -> MandateRunOutcome:
    """空手而归有七种不同的原因，对应人七种不同的下一步。

    此前它们在界面上是同一幅画面（徽章「生效中」+ 待批空态），而其中五种意味着
    这份授权**根本没跑通**。判定顺序即因果顺序：没接数据源 → 取回的行一条也读不出来
    → 没取到行 → 行都被作用域挡掉 → 浪费全是 ASIN 型（否不掉）→ 取到了但整批太旧
    → 真的没有该否的词。

    「读不出来」必须排在「没取到行」前面：源侧给了几千行、我们一条都聚合不出来时，
    len(records) 同样是 0，落到 NO_ROWS 就成了「这段时间没有数据」——而那句话会把人
    指向「拉长窗口」「确认在不在投放」这两个注定无效的动作，真正的原因（行的形状不对）
    在界面上一个字都不会出现。
    """
    if not profile_has_data_source:
        return MandateRunOutcome.NO_DATA_SOURCE
    if not fetch.records and not fetch.is_complete:
        return MandateRunOutcome.NO_USABLE_ROWS
    if not fetch.records:
        return MandateRunOutcome.NO_ROWS
    if result.evaluated_count == 0 and unjudged_in_scope > 0:
        # 作用域内的组全被丢掉，作用域外的组好好的：records 非空，上面那道闸不成立，
        # 而 SCOPE_EMPTY 会叫人「改作用域后重签」——作用域本来是对的，真实原因是
        # 这些组的行读不出来。照着改，问题分毫未动。
        return MandateRunOutcome.NO_USABLE_ROWS
    if result.evaluated_count == 0:
        return MandateRunOutcome.SCOPE_EMPTY
    if result.asin_abstain_count > 0 and result.asin_abstain_count == result.evaluated_count:
        # 必须排在 ALL_ABSTAINED 前面，且绝不能落到 NO_CANDIDATES。这一批每一条都是
        # 「花了钱、零转化」的浪费，只是本策略否不掉；NO_CANDIDATES 那句「这段窗口
        # 确实干净」会让人心安理得地什么都不做。
        return MandateRunOutcome.ALL_ASIN
    if len(result.abstains) == result.evaluated_count:
        return MandateRunOutcome.ALL_ABSTAINED
    return MandateRunOutcome.NO_CANDIDATES


class StrategyToolService:
    def __init__(
        self,
        *,
        environment: Environment,
        grants: list[Grant],
        denies: list[ExplicitDeny],
        search_terms: SearchTermReadPort,
        store: InMemoryCandidateSetStore,
        mandates: InMemoryMandateStore | None = None,
        run_log: InMemoryMandateRunLog | None = None,
        clock: Any = None,
    ) -> None:
        self._environment = environment
        self._grants = grants
        self._denies = denies
        self._search_terms = search_terms
        self._store = store
        self._mandates = mandates
        #: 授权运行流水。配额与最小间隔从它算，人也从它看「上次跑成什么样」。
        #: 组合根要把同一个实例也交给 approval_api，否则界面看不到运行。
        self._run_log = run_log if run_log is not None else InMemoryMandateRunLog()
        self._clock = clock  # Callable[[], datetime]；测试注入，None 时用真实时钟

    def _now(self) -> datetime:
        if self._clock is not None:
            now: datetime = self._clock()
            return now
        from datetime import UTC

        return datetime.now(UTC)

    def _authorize(self, actor: ActorContext, action: Action) -> None:
        decision = evaluate(
            actor,
            AccessRequest(
                environment=self._environment,
                organization_id=actor.organization_id,
                action=action,
                client_type=ClientType.MCP_AI,
            ),
            self._grants,
            self._denies,
        )
        if not decision.allowed:
            raise ToolDenied("AUTH_SCOPE_DENIED")

    def _resolve_pack(
        self,
        *,
        lookback_days: int | None,
        min_spend_amount: str | None,
        currency: str | None,
        min_clicks: int | None,
        max_data_staleness_hours: int | None,
    ) -> NegationParameterPack:
        try:
            return NegationParameterPack(
                lookback_days=lookback_days
                if lookback_days is not None
                else _DEFAULT_LOOKBACK_DAYS,
                # 不先 Decimal(...)：那样 "$20" / "20,00" 抛的是 decimal 的
                # ConversionSyntax，denial_hint 取到的是一串 Python 内部类名，
                # 调用方无从改起。交给 Money 校验，透出的是我们自己写的那句话。
                min_spend=Money(
                    amount=min_spend_amount  # type: ignore[arg-type]
                    if min_spend_amount is not None
                    else _DEFAULT_MIN_SPEND,
                    currency=currency if currency is not None else _DEFAULT_CURRENCY,
                ),
                min_clicks=min_clicks if min_clicks is not None else _DEFAULT_MIN_CLICKS,
                max_data_staleness_hours=max_data_staleness_hours
                if max_data_staleness_hours is not None
                else _DEFAULT_MAX_STALENESS_HOURS,
            )
        except (ValueError, ArithmeticError) as exc:
            raise ToolDenied("PARAMETER_REJECTED", denial_hint(exc)) from exc

    def _echo_quota(
        self, response: dict[str, Any], mandate: AutomationMandate | None, now: datetime
    ) -> None:
        """「下次什么时候能跑」「今天还剩几次」——AI 被拒后唯一想知道的两件事。

        必须在**每一条**消耗了配额的返回路径上说。此前只有产出候选那条说了，
        而空手而归的运行照样吃掉当天一次、照样刷新最小间隔：AI 看到 0 条候选，
        最自然的动作就是再跑一次，然后才撞上 RUN_TOO_SOON / RUN_BUDGET_EXCEEDED
        ——正是这两个数存在的意义（「等到 RUN_TOO_SOON 再说就晚了一轮」），
        却偏偏在最容易触发重试的那条路径上不说。

        调用点必须排在 _record_run 之后，count_on_day 才把这一次算进去。
        """
        if mandate is None:
            return
        remaining = max(
            0,
            mandate.bounds.max_runs_per_day
            - self._run_log.count_on_day(mandate.mandate_id, now, mandate.quota_day),
        )
        next_allowed = now + timedelta(minutes=mandate.bounds.run_interval_minutes)
        if remaining == 0:
            # 日配额已耗尽：间隔到了也照样被拒，说「最早 X 点」就是把人往一堵墙上指。
            next_allowed = max(next_allowed, mandate.next_quota_day_start(now))
        response["next_run_allowed_at"] = next_allowed.isoformat()
        response["runs_remaining_today"] = remaining

    def _record_run(
        self,
        mandate: AutomationMandate | None,
        now: datetime,
        outcome: MandateRunOutcome,
        *,
        result: NegationRunResult | None = None,
        fetch: SearchTermFetch | None = None,
        unjudged_in_scope: int = 0,
        scope_filtered_out: int = 0,
        set_id: uuid.UUID | None = None,
        error_code: str | None = None,
    ) -> None:
        """把这次运行如实记下来。

        只记授权运行：即席运行没有授权书可挂，也不受配额约束，记了没有归属也无人读。
        记录发生在**授权检查通过之后**——被配额/时段拒掉的那次不是一次运行，
        把它记成运行会让配额自己消耗自己，授权书永远解不开。
        """
        if mandate is None:
            return
        self._run_log.record(
            MandateRunRecord(
                run_id=new_canonical_id(),
                mandate_id=mandate.mandate_id,
                ran_at=now,
                outcome=outcome,
                evaluated_ad_group_terms=result.evaluated_count if result is not None else 0,
                distinct_search_terms=result.distinct_search_terms if result is not None else 0,
                candidate_count=len(result.candidates) if result is not None else 0,
                abstain_count=len(result.abstains) if result is not None else 0,
                asin_abstain_count=result.asin_abstain_count if result is not None else 0,
                asin_abstain_terms=(
                    tuple(
                        a.search_term
                        for a in result.abstains
                        if a.reason is AbstainReason.ASIN_NOT_A_KEYWORD
                    )
                    if result is not None
                    else ()
                ),
                scope_filtered_out=scope_filtered_out,
                unjudged_ad_group_terms=unjudged_in_scope,
                unattributable_rows=fetch.unattributable_rows if fetch else 0,
                set_id=set_id,
                error_code=error_code,
            )
        )

    def generate_negation_candidate_set(
        self,
        actor: ActorContext,
        *,
        profile_external_id: str,
        mandate_id: str | None = None,
        lookback_days: int | None = None,
        min_spend_amount: str | None = None,
        currency: str | None = None,
        min_clicks: int | None = None,
        max_data_staleness_hours: int | None = None,
    ) -> dict[str, Any]:
        """一次生成的完整过程，见 _generate 的 docstring。

        这一层只做一件事：给配额占位一个**保证会释放**的作用域。授权分支占到位
        之后，下面还有取数、多条 raise、多个 return——任何一条漏掉释放，这份授权
        就在本进程里被永久锁死，那比它要修的「超配额多跑一次」更糟。用 with 把
        释放钉在语言层面，而不是钉在「每条出口都记得调一次」上。
        """
        with ExitStack() as quota:
            return self._generate(
                quota,
                actor,
                profile_external_id=profile_external_id,
                mandate_id=mandate_id,
                lookback_days=lookback_days,
                min_spend_amount=min_spend_amount,
                currency=currency,
                min_clicks=min_clicks,
                max_data_staleness_hours=max_data_staleness_hours,
            )

    def _generate(
        self,
        quota: ExitStack,
        actor: ActorContext,
        *,
        profile_external_id: str,
        mandate_id: str | None = None,
        lookback_days: int | None = None,
        min_spend_amount: str | None = None,
        currency: str | None = None,
        min_clicks: int | None = None,
        max_data_staleness_hours: int | None = None,
    ) -> dict[str, Any]:
        """拉取搜索词绩效 → 证据门筛选 → 冻结候选集合待人批。

        两种模式（DEC-114）：
        - 即席：参数覆盖经白名单校验，越界即 PARAMETER_REJECTED；
        - 目标授权：传 mandate_id 后参数只来自签发合同，任何覆盖都是
          MANDATE_PARAMS_FORBIDDEN——"系统对着参数列表跑"的参数列表以合同为准。
        无候选时不创建集合（空集合无意义，也避免审批队列噪音）；返回里的
        profile_has_data_source 区分"没接数据源"与"查了没有"（runtime-2）。

        授权模式下还有两道边界闸：assert_run_authorized（配额/间隔/有效期/运行
        时段）与作用域过滤（_within_mandate_scope）。前者决定"现在能不能跑"，
        后者决定"能对哪些对象跑"——两者都不由调用方参数决定，只由签发合同决定。
        """
        self._authorize(actor, Action.PROPOSAL_CREATE_DRAFT)
        now = self._now()
        mandate = None
        if mandate_id is not None:
            overrides = (
                lookback_days,
                min_spend_amount,
                currency,
                min_clicks,
                max_data_staleness_hours,
            )
            if any(v is not None for v in overrides):
                raise ToolDenied("MANDATE_PARAMS_FORBIDDEN")
            if self._mandates is None:
                raise ToolDenied("MANDATE_UNKNOWN")
            try:
                parsed_mandate_id = uuid.UUID(mandate_id)
            except ValueError as exc:
                # 形状不对：多半是把界面上那 8 位缩写贴过来了。说清「要完整 36 位」，
                # 调用方才改得动——这句谈的是调用方自己的输入，不涉及资源是否存在。
                raise ToolDenied(
                    "MANDATE_UNKNOWN",
                    "mandate_id must be the full 36-character UUID (the mandate row in the "
                    "web UI shows only the first 8 characters; use its 「复制指令」 button)",
                ) from exc
            try:
                mandate = self._mandates.get(parsed_mandate_id)
            except MandateNotFound as exc:
                # 形状对但找不到。UI 词典此前只列了「已撤销」「不属于当前组织」两种
                # 成因——而这两种在服务端分别报 MANDATE_NOT_ACTIVE 与 SCOPE_MISMATCH，
                # 根本走不到这里。真正最常见的成因是授权书只存在于进程内存里，服务
                # 一重启就全没了；不说这句，AI 会照着词典把「可能被同事撤销了」讲给人听。
                raise ToolDenied(
                    "MANDATE_UNKNOWN",
                    "no such mandate in this server process; mandates live in memory and are "
                    "cleared on restart. Ask the human to re-issue it in the web UI and hand "
                    "you the new mandate_id. Do not fall back to ad-hoc generation: that runs "
                    "outside this mandate's scope, quota and interval",
                ) from exc
            # 配额与间隔从**运行流水**算，不再从候选集合反推。反推会让「币种签错」
            # 「没接数据源」「作用域全挡掉」「整批 ABSTAIN」「取数失败」这五种运行
            # 不消耗配额、不刷新间隔——恰恰是最该被拦住的情形可以被无限次触发，
            # 而每次触发对真实源是一轮多页读取。
            #: 数与判必须和「占位」在同一把锁里（run_log.claim）。只数不占的话，
            #  数完之后紧接着的是整整一轮取数（真实源十几秒），两个并发请求都在
            #  对方记流水之前数到 0，就都被放行：max_runs_per_day=1 的授权书一天
            #  跑两次，待批里出现两份孪生，而每一次对领星都是一轮 QPS=1 的多页读。
            authorized = mandate
            try:
                quota.enter_context(
                    self._run_log.claim(
                        mandate.mandate_id,
                        now,
                        mandate.quota_day,
                        lambda runs_today, last_run_at: assert_run_authorized(
                            authorized,
                            organization_id=actor.organization_id,
                            profile_external_id=profile_external_id,
                            runs_today=runs_today,
                            now=now,
                            last_run_at=last_run_at,
                        ),
                    )
                )
            except MandateViolation as exc:
                # 时间类拒绝的 message 是这份授权自己的合同事实（间隔多少、
                # 距上次多久、下一个时段几点开、配额几次），也正是调用方唯一能
                # 据以行动的那句话；丢掉它，AI 转给人的就只有一串裸码。
                # 其余码（SCOPE_MISMATCH / MANDATE_NOT_ACTIVE …）继续只回码：
                # 「mandate belongs to another organization」会告诉调用方这份授权
                # 存在但不属于他，那是 AX-16 要挡的资源存在性。
                detail = str(exc) if exc.code in _ACTIONABLE_VIOLATION_CODES else None
                raise ToolDenied(exc.code, detail) from exc
            pack = mandate.parameter_pack
        else:
            pack = self._resolve_pack(
                lookback_days=lookback_days,
                min_spend_amount=min_spend_amount,
                currency=currency,
                min_clicks=min_clicks,
                max_data_staleness_hours=max_data_staleness_hours,
            )
        # 取数之前先问"这个店接了没有"：取数结果为空回答不了这个问题。2026-08-29
        # 排查结论 runtime-2：未接入店铺跑出的 evaluated_ad_group_terms=0 曾与"查了确实没有
        # 浪费"逐字相同，读的人只会当成好消息。
        # 取数的一切失败都必须带码。真实数据源会超时、会被网关以 code=102 拒、会限流，
        # 而 _coded（server.py）只翻 ToolDenied——裸穿的异常在 MCP 面只剩一句
        # Error executing tool，调用方分不清「超时可重试」和「参数签错了永远不会成功」，
        # 于是去重试一个注定失败的调用，撞满 QPS=1 的限额。这正是 server.py 那段注释
        # 描述的、已经修过一次的病。
        try:
            profile_has_data_source = self._search_terms.has_profile(profile_external_id)
            # 没接数据源就不取数：对真实源来说那是一次白扔的网关调用（QPS=1 很贵），
            # 对 Mock 行为不变（未 seed 的 profile 本来就返回空元组）。
            fetch = (
                self._search_terms.fetch_search_term_performance(
                    profile_external_id, pack.lookback_days, now
                )
                if profile_has_data_source
                else SearchTermFetch()
            )
        except SearchTermSourceError as exc:
            self._record_run(mandate, now, MandateRunOutcome.SOURCE_ERROR, error_code=exc.code)
            raise ToolDenied(exc.code) from exc
        records = fetch.records
        scope_filtered_out = 0
        if mandate is not None:
            records, scope_filtered_out = _within_mandate_scope(records, mandate)
        # 「没被判断的组」也按同一个作用域筛：不筛就会把全店的坏数据算到一份只管
        # 1 个活动的授权书头上（见 _unjudged_within_scope）。
        unjudged_in_scope = _unjudged_within_scope(fetch.unjudged_groups, mandate)
        try:
            result = generate_negation_candidates(records, pack, now, new_canonical_id)
        except CandidateSetError as exc:
            # 域层带码错误（如 CURRENCY_MISMATCH）转 ToolDenied，让调用方拿到可查
            # 词典的码。裸穿会变成一句无标签的 Error executing tool（2026-08-29
            # 排查 mandate-1/approval-2），人分不清「币种签错」和「服务器坏了」。
            self._record_run(
                mandate,
                now,
                MandateRunOutcome.REJECTED,
                fetch=fetch,
                unjudged_in_scope=unjudged_in_scope,
                scope_filtered_out=scope_filtered_out,
                error_code=exc.code,
            )
            # CandidateSetError 的 message 谈的都是数据本身的形状（币种是哪两个、
            # 哪个词出现了两次），不涉及资源是否存在，可以原样带出。
            raise ToolDenied(exc.code, str(exc)) from exc
        truncated_from: int | None = None
        cap = mandate.bounds.max_candidates_per_run if mandate is not None else _AD_HOC_MAX
        if len(result.candidates) > cap:
            truncated_from = len(result.candidates)
            kept = sorted(result.candidates, key=lambda c: c.evidence.spend.amount, reverse=True)
            result = result.model_copy(update={"candidates": tuple(kept[:cap])})
        # ASIN 型排在前面再截断。两类弃权的可操作性不对等：STALE_DATA 全批同因、
        # 列一条和列五百条给人的信息一样多，而 ASIN 型每一条都是一个人要去领星手工
        # 否定的具体词。不排序的话，五百条过期弃权会把那几条真正要动手的挤出响应，
        # 只剩一个数——人知道"有 3 个"，却不知道是哪 3 个。sorted 稳定，组内原序不变。
        shown_abstains = sorted(
            result.abstains, key=lambda a: a.reason is not AbstainReason.ASIN_NOT_A_KEYWORD
        )[:_MAX_ABSTAINS_IN_RESPONSE]
        response: dict[str, Any] = {
            # 字段名从 `evaluated_terms` 改成现名（2026-08-30）：它数的一直是
            # (广告组 × 搜索词) 组合行，Mock 源下每个词只出现一次所以两者相等，
            # 名字的错处看不出来；接上真实源后同一个词投在几个广告组就是几行，
            # 而这个数唯一的读者是 AI，AI 唯一的动作是把它讲给人听——「已评估该店
            # 137 个搜索词」于是成了一句人无法察觉的假话。改名 + 另给去重后的词数，
            # 让两句话都能说真。
            "evaluated_ad_group_terms": result.evaluated_count,
            "distinct_search_terms": result.distinct_search_terms,
            "candidate_count": len(result.candidates),
            "abstains": [
                {"search_term": a.search_term, "reason": a.reason.value, "detail": a.detail}
                for a in shown_abstains
            ],
            "abstain_count": len(result.abstains),
            # 上面那个列表被截断时的原始条数，None = 没截断。candidates 早有同名
            # 保护（truncated_from），abstains 一直只能靠调用方自己比对两个长度才
            # 发现——而它恰恰更需要标记：排序是 ASIN 优先，ASIN 型是唯一要人去领星
            # 动手的那批，超过 50 条时后面那些在任何工具面都再也拿不回来。
            # 不标出来，AI 会把手里这 50 条当成全部念给人听。
            "abstains_truncated_from": (
                len(result.abstains) if len(result.abstains) > len(shown_abstains) else None
            ),
            # 弃权里有几条是「这笔钱确实在白烧，而我开不出能挡住它的否定词」。
            # 单列一个数是因为 abstains 会被截断而这个数恒如实，且它指向的下一步与
            # 其余弃权完全不同：不是等新数据，是人要去领星「否定投放」动手。
            # 它 > 0 时，candidate_count 再漂亮也不代表这个店已经处理完了。
            "asin_abstain_count": result.asin_abstain_count,
            "set_id": None,
            "set_hash": None,
            "mandate_id": mandate_id,
            "truncated_from": truncated_from,
            # 作用域挡掉了几条输入。0 与 None 语义不同：None = 没有作用域可言（即席
            # 模式），0 = 有作用域但一条都没挡。空手而归时人得能区分"店里本来就没有
            # 浪费"和"你圈的对象在这段数据里一条都没出现"。
            "scope_filtered_out": scope_filtered_out if mandate is not None else None,
            # False = 这个 profile 根本没接入数据源：上面的 0 是"没查"，不是"查了
            # 没有"。与 scope_filtered_out 同属"空手而归必须能归因"的一组字段
            # （2026-08-29 排查结论 runtime-2）。
            "profile_has_data_source": profile_has_data_source,
            # 这一轮**没被判断**的部分。它 > 0 时，上面的 candidate_count=0 只说明
            # 「在我看得懂的那部分里没有该否的词」，不说明这个店这段窗口没有浪费。
            # 少了这两个数，读响应的 AI 会把后一句讲给人听，而人无从察觉：
            # 响应里其余每个数字都正常，被丢掉的组连一个占位都没有。
            # 两个单位不同、互不重叠，绝不相加。
            # **作用域内**的未判断组数——与 evaluated_ad_group_terms / scope_filtered_out
            # 同一个口径。全店口径的那个在 source_accounting 里，两者不可混为一谈：
            # 一份只管 1 个活动的授权书，店里别处的坏数据与它无关。
            "unjudged_ad_group_terms": unjudged_in_scope,
            # 连广告组是谁都读不出来的行，恒为全店口径：说不出归属，也就说不出
            # 它落在哪份授权的范围里。
            "unattributable_rows": fetch.unattributable_rows,
            # 取数账目。放这里是为了让「拿到多少行、丢了什么」有唯一出处，
            # 而不是散落在实现的实例属性上等人回头去读（并发下会读到另一个店的）。
            # 取数账目（全店口径，与作用域无关）。四个行级计数把 source_total 填平：
            # source_total = skipped_summary_rows + duplicate_rows + unreadable_rows
            #                + usable_rows。少一格就会有一批行在账上凭空消失。
            "source_accounting": {
                "source_total": fetch.source_total,
                "skipped_summary_rows": fetch.skipped_summary_rows,
                "duplicate_rows": fetch.duplicate_rows,
                "unreadable_rows": fetch.unreadable_rows,
                "usable_rows": fetch.usable_rows,
                "unjudged_ad_group_terms_store_wide": fetch.unjudged_ad_group_terms,
                "served_from_cache": fetch.served_from_cache,
            },
            # 实际生效的门槛。没有它，「没有该否的词」这句话就没有适用范围可言：
            # 花了钱、零转化、但没到 min_spend / min_clicks 的词既不进 candidates 也
            # 不进 abstains，是被静默丢掉的——响应里连一个占位都没有。AI 于是只能把
            # candidate_count=0 讲成「这个店没有浪费花费」，而人无从察觉门槛的存在，
            # 更无从判断该不该把门槛调低。授权书模式下这几个数还是人亲手签的，
            # 讲给他听时必须报得出来。
            "applied_parameters": {
                "lookback_days": pack.lookback_days,
                "min_spend_amount": str(pack.min_spend.amount),
                "currency": pack.min_spend.currency,
                "min_clicks": pack.min_clicks,
                "max_data_staleness_hours": pack.max_data_staleness_hours,
            },
            "note": "approval requires a human session; this tool can only create frozen drafts",
        }
        if not result.candidates:
            # 空手而归有六种原因，服务端已经判好了——此前只喂给运行流水，响应里
            # 一个字都不给。读响应的 AI 于是只能看到 candidate_count: 0，把它讲成
            # 「这个店很干净」；而六种里有五种意味着这份授权根本没跑通。
            outcome = _empty_run_outcome(
                profile_has_data_source=profile_has_data_source,
                fetch=fetch,
                unjudged_in_scope=unjudged_in_scope,
                result=result,
            )
            response["outcome"] = outcome.value
            # 「只有 NO_CANDIDATES 才表示没有浪费」这句本身也是假的，而它恰恰是
            # 唯一被读的那句：NO_CANDIDATES 只排除了「候选」，排除不了同一轮里的
            # 弃权（数据太旧判不了、ASIN 型否不掉）、未判断组、以及没到门槛被静默
            # 丢掉的词。Web 界面 2026-09-06 已经为这句话补过限定（授权书卡片上那条
            # 「有 N 条没判成……它们不在这一轮的结论里」），工具面一直原样照说——
            # 同一句假话只修了人看的那一面，AI 转述给人的那一面没修。
            clean = outcome is MandateRunOutcome.NO_CANDIDATES
            caveats = []
            if result.abstains:
                caveats.append(
                    f"{len(result.abstains)} search terms were not judged at all "
                    f"(abstain_count; {result.asin_abstain_count} of them are ASINs this "
                    "strategy cannot negate)"
                )
            if unjudged_in_scope:
                caveats.append(f"{unjudged_in_scope} (ad_group, term) pairs in scope went unjudged")
            response["note"] = (
                f"no candidate set was created (outcome={outcome.value}); there is nothing to "
                "approve. Tell the human what this outcome means before saying anything about "
                "the account being clean. NO_CANDIDATES does NOT mean the account has no wasted "
                "spend: it means no term cleared the thresholds in `applied_parameters` "
                "(spend below min_spend_amount or clicks below min_clicks are dropped silently "
                "and appear nowhere in this response)."
                + (" In this run: " + "; ".join(caveats) + "." if clean and caveats else "")
            )
            self._record_run(
                mandate,
                now,
                outcome,
                result=result,
                fetch=fetch,
                unjudged_in_scope=unjudged_in_scope,
                scope_filtered_out=scope_filtered_out,
            )
            self._echo_quota(response, mandate, now)
            return response
        person_id = actor.human_person_id if actor.principal_type is PrincipalType.HUMAN else None
        candidate_set = NegationCandidateSet(
            set_id=new_canonical_id(),
            organization_id=actor.organization_id,
            parameter_pack=pack,
            candidates=result.candidates,
            generated_at=now,
            created_by_client_id=actor.client_id,
            created_by_person_id=person_id,
            source="AI" if actor.principal_type is PrincipalType.AI_CLIENT else "HUMAN",
            mandate_id=mandate.mandate_id if mandate is not None else None,
            truncated_from=truncated_from,
            asin_abstain_count=result.asin_abstain_count,
            asin_abstain_terms=tuple(
                a.search_term
                for a in result.abstains
                if a.reason is AbstainReason.ASIN_NOT_A_KEYWORD
            ),
        ).freeze()
        self._store.save(candidate_set)
        self._record_run(
            mandate,
            now,
            MandateRunOutcome.CANDIDATES,
            result=result,
            fetch=fetch,
            unjudged_in_scope=unjudged_in_scope,
            scope_filtered_out=scope_filtered_out,
            set_id=candidate_set.set_id,
        )
        response["set_id"] = str(candidate_set.set_id)
        response["set_hash"] = candidate_set.set_hash
        response["outcome"] = MandateRunOutcome.CANDIDATES.value
        # 这批什么时候作废。AI 转述时唯一能给出的时限，而人拿到的动作是「去批」。
        response["expires_at"] = (
            candidate_set.generated_at + timedelta(hours=CANDIDATE_SET_TTL_HOURS)
        ).isoformat()
        # 认重复用的那个值：内容相同的两次生成得到同一个指纹（set_hash 不会——
        # 候选编号进了它）。AI 拿它就能回答「这批是不是今天已经批过的那批」。
        response["content_fingerprint"] = candidate_set.content_fingerprint()
        # 内容逐字相同、仍在待批的其他集合。没有它，AI 每被问一次就多冻一份孪生，
        # 而每一份都要人去拒。
        response["same_content_as"] = [
            str(other.set_id)
            for other in self._store.list_by_state(actor.organization_id, CandidateSetState.FROZEN)
            if other.set_id != candidate_set.set_id
            and other.content_fingerprint() == candidate_set.content_fingerprint()
        ]
        self._echo_quota(response, mandate, now)
        response["candidates"] = [
            {
                "search_term": c.search_term,
                "ad_group_external_id": c.scope.entity_external_id,
                # 光有广告组 ID，AI 只能把一串 ID 念给人听。名称与活动归属都在
                # 候选里现成（REST 面早就在用），照给即可。
                "ad_group_name": c.ad_group_name,
                "campaign_external_id": c.scope.parent_refs.campaign_external_id,
                "campaign_name": c.campaign_name,
                "evidence": {
                    "spend": str(c.evidence.spend.amount),
                    "currency": c.evidence.spend.currency,
                    "clicks": c.evidence.clicks,
                    "conversions": c.evidence.conversions,
                    # 「这批数字统计的是哪一段」此前在全部三个出口上都不存在。
                    # 没有它，conversions: 0 会被读成「到今天为止零单」，
                    # 而窗口右端已被归因滞后刻意往回推了几天。
                    "window_start": c.evidence.window_start.isoformat(),
                    "window_end": c.evidence.window_end.isoformat(),
                    "data_as_of": c.evidence.data_as_of.isoformat(),
                },
            }
            for c in candidate_set.candidates
        ]
        return response

    def list_negation_candidate_sets(self, actor: ActorContext) -> dict[str, Any]:
        self._authorize(actor, Action.RESOURCE_READ)
        sets = self._store.list_by_state(actor.organization_id)
        # 人问 AI 「今天有什么要批」，AI 此前只能报出 uuid 与数量：说不出是哪家店、
        # 在哪份授权之下、有没有过 72 小时时效。三个字段都在手里，不给等于让 AI
        # 转述一段人无法行动的话。
        now = self._now()
        return {
            "candidate_sets": [
                {
                    "set_id": str(s.set_id),
                    "state": s.state.value,
                    "candidate_count": len(s.candidates),
                    # 这份清单不是那一轮浪费的全部：同轮还有几个 ASIN 否不掉。
                    # 少了它，AI 只能照着 candidate_count 说「这批 3 个词处理完就完了」。
                    "asin_abstain_count": s.asin_abstain_count,
                    #: 词表必须跟着计数一起给（2026-09-06 排查）。此前只回计数，于是
                    #  一批由 AI 生成的候选，AI 自己读不回来：没有 get_by_id，唯一吐出
                    #  词表的是 generate——而它改状态、消配额，默认打法（每天一次）下
                    #  产出这份集合的那次运行已经把当天配额用掉，重跑必撞
                    #  RUN_BUDGET_EXCEEDED；改走即席能拿到词，但会再冻一份内容相同的
                    #  待批集合，要人多拒一次。于是人在 AI 客户端里问「那个 ASIN 是
                    #  哪个词」「那 3 个词是什么」，AI 只能答「我看不到」。
                    #  这些字段 REST 面早就在算（approval_api._summary），只读、不消耗配额。
                    "asin_abstain_terms": list(s.asin_abstain_terms),
                    #: 名称要跟着 ID 一起给（2026-09-07 排查）。上面那条 note 把本工具
                    #  指定为 generate 的替代品，而 generate 的候选里带着
                    #  ad_group_name / campaign_name，这里只给两串外部 ID——人在领星
                    #  界面里是**按名称**找活动和广告组的，AI 只念得出 ID 就等于没答上。
                    #  名称与候选一起冻结（跟证据出自同一行），不是另取的现值。
                    "candidates": [
                        {
                            "search_term": c.search_term,
                            "ad_group_external_id": c.scope.entity_external_id,
                            "ad_group_name": c.ad_group_name,
                            "campaign_external_id": c.scope.parent_refs.campaign_external_id,
                            "campaign_name": c.campaign_name,
                            "match_type": c.match_type,
                        }
                        for c in s.candidates
                    ],
                    #: 截断前命中多少个：不给的话 AI 会把截断后的数字说成「全部」。
                    "truncated_from": s.truncated_from,
                    "parameter_pack": s.parameter_pack.model_dump(mode="json"),
                    "generated_at": s.generated_at.isoformat(),
                    "source": s.source,
                    "approved_by_person_id": s.approved_by_person_id,
                    "profile_external_id": s.profile_external_id,
                    "mandate_id": str(s.mandate_id) if s.mandate_id else None,
                    "expires_at": (
                        s.generated_at + timedelta(hours=CANDIDATE_SET_TTL_HOURS)
                    ).isoformat(),
                    "expired": s.state is CandidateSetState.FROZEN
                    and candidate_set_expired(s.generated_at, now),
                    "content_fingerprint": s.content_fingerprint(),
                }
                for s in sorted(sets, key=lambda s: s.generated_at, reverse=True)
            ],
            "note": (
                "only a human can approve, in the web UI 「待批」 tab. An expired set cannot be "
                "approved any more — it has to be rejected and regenerated. Sets sharing a "
                "content_fingerprint are the same batch of terms generated more than once: "
                "approving one is enough. This tool is read-only and costs no quota: use it to "
                "answer questions about a set you already generated (which terms, which ASINs "
                "could not be negated) instead of generating again — regenerating spends a run "
                "against the mandate's daily quota and creates a duplicate set a human then "
                "has to reject"
            ),
        }
