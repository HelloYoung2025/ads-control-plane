"""目标授权书（AutomationMandate）——"批准"的目标级形态（DEC-114）。

2026-08-28 业务 Owner 提出：批准做成"填写目标 → 形成参数列表 → 系统对着参数列表
优化调整"。本模型即该合同的域对象：

    目标（objective，必须是已定义的目标函数，DEC-113）
      → 参数列表（parameter pack，白名单闭集，签发时冻结）
      → 有界授权（配额 + 有效期 + 单人可撤销）

当前消费点 = 候选自动生成（读侧）。执行侧自动化不在本模型：那需要 Gate 3
授权与写通道合同，届时以新字段扩展并重签，不隐式继承。
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import StrEnum
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, model_validator

from ads_control_plane.canonical.ids import CanonicalId
from ads_control_plane.identity.actor import ActorContext, PrincipalType
from ads_control_plane.strategies.negation import NegationParameterPack

# 授权边界原语住在 run_window.py（AutomationMandate 的字段类型，反向依赖会成环）。
# 用显式别名再导出，保持 `from ...strategies.mandate import MandateViolation` 的既有
# import 路径不变，同时满足 mypy strict 的 no_implicit_reexport。
from ads_control_plane.strategies.run_window import MandateScope as MandateScope
from ads_control_plane.strategies.run_window import MandateScopeKind as MandateScopeKind
from ads_control_plane.strategies.run_window import MandateViolation as MandateViolation
from ads_control_plane.strategies.run_window import RunWindow as RunWindow
from ads_control_plane.tasks.directive import ObjectLevel

#: 授权有效期上限：到期必须由人重签，不存在长生不老的自动化授权。
MAX_MANDATE_VALID_DAYS = 30


class MandateState(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class ObjectiveKind(StrEnum):
    """业务目标场景（2026-08-28 Owner 需求：清仓 / 打新品 / 推高销量 + 已有的降无效花费）。

    枚举是白名单：系统知道这些目标存在，但每个目标有数据地基就绪条件
    （OBJECTIVE_DATA_REQUIREMENTS）——地基未就绪的目标签发时 fail-closed，
    并明确说缺什么，而不是假装能优化。
    """

    WASTED_SPEND_REMOVED = "WASTED_SPEND_REMOVED"
    CLEARANCE_VELOCITY = "CLEARANCE_VELOCITY"  # 清仓：出货速度优先，容忍放宽 ACOS
    LAUNCH_RAMP = "LAUNCH_RAMP"  # 打新品：曝光/订单爬坡，预算上限约束
    SALES_GROWTH = "SALES_GROWTH"  # 推高销量：销量最大化 s.t. 盈亏约束


#: 每个目标函数可运行前必须就绪的数据依赖（空元组 = 已就绪）。
#: 依赖接入后在此翻转并配套测试；条目对应 decision register 的 DEC-116。
OBJECTIVE_DATA_REQUIREMENTS: dict[ObjectiveKind, tuple[str, ...]] = {
    ObjectiveKind.WASTED_SPEND_REMOVED: (),
    ObjectiveKind.CLEARANCE_VELOCITY: (
        "fba_inventory_feed",  # 库存/cover_days 数据源未接入
        "clearance_unit_loss_cap",  # 单件可接受亏损口径未冻结
    ),
    ObjectiveKind.LAUNCH_RAMP: (
        "ramp_definition",  # "爬坡"的可计算定义未冻结
        "attribution_maturity_calibration",  # 归因成熟期未实测校准
    ),
    ObjectiveKind.SALES_GROWTH: (
        "unit_economics_baseline",  # 单位经济口径未经财务签核（DEC-020/021）
        "breakeven_acos_definition",
    ),
}

#: 每个目标允许的作用域层级。否定词按广告组落位，而搜索词记录不携带触发它的
#: target，因此 TARGET 层作用域在数据上无法判定包含关系——不可实现即不可选。
#: 若放行 TARGET，授权书能签发却永远选不中任何候选（运行期才发现的静默失效）。
OBJECTIVE_SCOPE_LEVELS: dict[ObjectiveKind, frozenset[ObjectLevel]] = {
    ObjectiveKind.WASTED_SPEND_REMOVED: frozenset({ObjectLevel.CAMPAIGN, ObjectLevel.AD_GROUP}),
    ObjectiveKind.CLEARANCE_VELOCITY: frozenset({ObjectLevel.CAMPAIGN, ObjectLevel.AD_GROUP}),
    ObjectiveKind.LAUNCH_RAMP: frozenset({ObjectLevel.CAMPAIGN, ObjectLevel.AD_GROUP}),
    ObjectiveKind.SALES_GROWTH: frozenset({ObjectLevel.CAMPAIGN, ObjectLevel.AD_GROUP}),
}


class MandateObjective(BaseModel):
    """目标声明。objective 必须是已定义的目标函数枚举——"把广告优化好"不可计算，拒收。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    objective: ObjectiveKind
    #: 人写的目标陈述，只供人读与审计回溯；永不进入任何执行判定。
    statement: str

    @model_validator(mode="after")
    def _non_empty_statement(self) -> MandateObjective:
        if not self.statement.strip():
            raise ValueError("objective statement must be non-empty (audit readability)")
        return self


class MandateBounds(BaseModel):
    """运行配额与节奏。越界不是错误分支，是"回到人"的信号。

    run_interval_minutes 的下限是数据物理决定的：搜索词报告等输入按小时/天刷新，
    比数据刷新更快的运行读到的是同一份缓存，只产生重复决策不产生新信息。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_runs_per_day: int
    max_candidates_per_run: int
    valid_days: int
    #: 两次运行的最小间隔（分钟）。白名单 [60, 10080]；否词类日级数据建议 1440。
    run_interval_minutes: int = 1440

    @model_validator(mode="after")
    def _whitelist(self) -> MandateBounds:
        if not 1 <= self.max_runs_per_day <= 24:
            raise ValueError("max_runs_per_day must be within [1, 24]")
        if not 1 <= self.max_candidates_per_run <= 200:
            raise ValueError("max_candidates_per_run must be within [1, 200]")
        if not 1 <= self.valid_days <= MAX_MANDATE_VALID_DAYS:
            raise ValueError(f"valid_days must be within [1, {MAX_MANDATE_VALID_DAYS}]")
        if not 60 <= self.run_interval_minutes <= 10080:
            raise ValueError(
                "run_interval_minutes must be within [60, 10080]: sub-hourly runs re-read "
                "the same cached data and produce duplicate decisions, not new information"
            )
        return self


class AutomationMandate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mandate_id: CanonicalId
    organization_id: CanonicalId
    profile_external_id: str
    objective: MandateObjective
    #: "参数列表"合同本体，自包含：授权模式下的运行只使用它，不接受调用方覆盖，
    #: 因此结构上不存在参数漂移。参数要变 = 撤销后重签。
    parameter_pack: NegationParameterPack
    bounds: MandateBounds
    issued_by_person_id: str
    issued_at: datetime
    expires_at: datetime
    #: 这份授权管哪些广告。None = 整店（与 kind=PROFILE 等价），保持既有授权书
    #: 与既有调用点的行为逐字不变。作用域与时段是**授权边界**不是策略参数，因此
    #: 都不进入 parameter_pack.content_hash()——混进去会让既有 hash 全部漂移。
    scope: MandateScope | None = None
    #: 系统在哪些当地钟点允许跑这份授权。None = 全天（不施加额外限制）。
    run_window: RunWindow | None = None
    state: MandateState = MandateState.ACTIVE
    revoked_by_person_id: str | None = None

    @property
    def quota_timezone(self) -> tzinfo:
        """「一天最多跑几次」里的「天」按哪个时区切。

        合同上「一天最多 N 次」与「只在当地 2 点到 18 点跑」是同一张纸上的两句话，
        人读的时候「一天」当然是同一个「天」。此前配额按 UTC 日切、时段按当地钟点，
        两个「天」相差整整一个时区偏移：UTC+8 的店在当地早上 8 点换一次配额，于是
        当地同一天里可以跑到 2N 次，而卡片上写着 N 次/日（2026-08-30 排查 #2/#9）。

        没设运行时段的授权书没有声明过任何时区，此时只能用 UTC——这是「我们不知道
        当地是几点」的如实表达，不是默认值。界面据 quota_timezone 说明日切口径，
        免得人按自己的钟点理解这个 N。
        """
        return ZoneInfo(self.run_window.timezone) if self.run_window is not None else UTC

    def quota_day(self, moment: datetime) -> date:
        """这一刻算在哪个「配额日」里。配额判定按这个值分组，不按当地日历日。

        跨午夜的运行时段（22:00 → 次日 06:00，run_window 明确支持）里，当地午夜落在
        窗口**正中间**：按当地日历日切，一个连续的运行时段被劈成两个配额日，
        「1 次/日」的授权当晚 22:10 跑一次、次日 04:20 再跑一次，两次都放行。
        而卡片上并排写着「每天 22:00 至次日 06:00」与「1 次/日」——人核对这两句话，
        读出来是每晚一次（2026-08-30 排查）。

        修法是让配额日从**窗口自己的起点**开始算：把当地时刻减去 start_hour 再取日期，
        22:10 与次日 04:20 就落在同一个配额日里。这才是「配额与运行时段共用同一个天」
        这句话真正成立的样子。

        只有**真的跨午夜**的窗口才平移。09→17 这类窗口里当地午夜落在窗口之外，
        当地日历日就是人心里的那个「天」；全天窗口（start == end，允许写成 5→5）
        更是如此——按它的 start_hour 平移会把日界挪到当地早上 5 点，而写 5→5 的人
        表达的是「不限时段」，他心里的一天仍从 0 点开始。平移一个不需要平移的窗口，
        修的是一个不存在的问题，换来的是一个人猜不到的日界。
        """
        local = moment.astimezone(self.quota_timezone)
        window = self.run_window
        if window is None or not window.crosses_midnight:
            return local.date()
        return (local - timedelta(hours=window.start_hour)).date()

    def next_quota_day_start(self, moment: datetime) -> datetime:
        """下一个配额日从哪一刻开始——也就是配额用完之后最早能再发起的时刻。

        必须与 quota_day 共用同一套平移规则，否则两个「天」又会错开：跨午夜窗口的
        配额日从 start_hour 起算，日界就在当地 start_hour，不在当地午夜。

        存在的理由：「最早几点可再发起」此前一律按最小间隔算。日配额先用完时那个
        时刻是假的——到点发起必被 RUN_BUDGET_EXCEEDED 拒，而界面和 AI 都照着它说
        「最早 13:00 可再发起」。人会守到 13:00 再点一次、再被拒一次，而卡片上同时
        还写着「今天的次数已用完」——两句话没有一句告诉他到底该等到什么时候。
        """
        window = self.run_window
        shift = (
            timedelta(hours=window.start_hour)
            if window is not None and window.crosses_midnight
            else timedelta(0)
        )
        next_day = self.quota_day(moment) + timedelta(days=1)
        local_start = datetime.combine(next_day, time(0, 0), tzinfo=self.quota_timezone) + shift
        return local_start.astimezone(UTC)

    @model_validator(mode="after")
    def _scope_belongs_to_this_profile(self) -> AutomationMandate:
        """一份授权只管一个店铺：勾选集的 profile 必须与授权书的 profile 一致。

        不一致时若放行，运行期的 profile 闸（SCOPE_MISMATCH）会让这份授权永远
        跑不出东西，而人看到的是一份状态 ACTIVE 的正常授权书。签发期就拒。
        """
        selection = self.scope.selection if self.scope is not None else None
        if selection is not None and selection.profile_external_id != self.profile_external_id:
            raise MandateViolation(
                "SCOPE_PROFILE_MISMATCH",
                f"scope selection covers profile {selection.profile_external_id!r} but the "
                f"mandate covers {self.profile_external_id!r}; one mandate covers one profile",
            )
        return self

    def revoke(self, revoker: ActorContext) -> AutomationMandate:
        """撤销：单个人即可、无需理由字段之外的仪式（收权从简）。AI 也不能撤——
        撤销是人的意思表示；系统侧的紧急停用走 kill switch，语义不同。"""
        if revoker.principal_type is not PrincipalType.HUMAN or not revoker.human_person_id:
            raise MandateViolation("HUMAN_REQUIRED", "only humans revoke mandates")
        return self.model_copy(
            update={
                "state": MandateState.REVOKED,
                "revoked_by_person_id": revoker.human_person_id,
            }
        )


def assert_can_issue_mandate(issuer: ActorContext) -> str:
    """签发 = 授权扩大，只能来自人类会话（AX-05 同源：AI 不能给自己发授权）。

    单独抽出来是为了让 HTTP 面能在**碰参数之前**先判这一闸。此前签发端点先跑
    币种校验、再跑授权判定，而币种不符的 422 文案点名该店真实结算币种——于是
    域层明令不能签发的 AI 身份，照样能拿这个端点当币种探针，一个 profile 一发地
    问出别家店以什么结算。判定顺序即信息泄露顺序：不该动手的人，连参数错在哪
    都不该知道。

    过闸后返回签发人的 person_id：这一闸判的就是「有没有这个人」，把它交出来，
    调用点就不必再写一次同样的判空。
    """
    if issuer.principal_type is not PrincipalType.HUMAN or not issuer.human_person_id:
        raise MandateViolation("AI_CANNOT_ISSUE_MANDATE", "mandates are issued by humans only")
    return issuer.human_person_id


def issue_mandate(
    issuer: ActorContext,
    *,
    mandate_id: uuid.UUID,
    profile_external_id: str,
    objective: MandateObjective,
    parameter_pack: NegationParameterPack,
    bounds: MandateBounds,
    now: datetime,
    scope: MandateScope | None = None,
    run_window: RunWindow | None = None,
) -> AutomationMandate:
    """签发 = 授权扩大，只能来自人类会话（AX-05 同源：AI 不能给自己发授权）。

    数据地基未就绪的目标在签发时即拒绝（fail-closed），并明确列出缺什么——
    系统不接受"先签着、数据以后再说"的授权。

    scope=None 等价整店、run_window=None 等价全天：既有调用点不传即行为不变。
    """
    issued_by = assert_can_issue_mandate(issuer)
    missing = OBJECTIVE_DATA_REQUIREMENTS[objective.objective]
    if missing:
        raise MandateViolation(
            "OBJECTIVE_NOT_READY",
            f"objective {objective.objective} is defined but its data foundation is not ready; "
            f"missing: {', '.join(missing)} (see DEC-116)",
        )
    if scope is not None and scope.selection is not None:
        allowed = OBJECTIVE_SCOPE_LEVELS[objective.objective]
        rejected = sorted({i.level.value for i in scope.selection.items if i.level not in allowed})
        if rejected:
            raise MandateViolation(
                "MANDATE_SCOPE_LEVEL_UNSUPPORTED",
                f"objective {objective.objective} cannot scope to levels: {', '.join(rejected)}; "
                f"allowed levels are {', '.join(sorted(x.value for x in allowed))}",
            )
    assert_window_interval_compatible(run_window, bounds)
    return AutomationMandate(
        mandate_id=mandate_id,
        organization_id=issuer.organization_id,
        profile_external_id=profile_external_id,
        objective=objective,
        parameter_pack=parameter_pack,
        bounds=bounds,
        issued_by_person_id=issued_by,
        issued_at=now,
        expires_at=now + timedelta(days=bounds.valid_days),
        scope=scope,
        run_window=run_window,
    )


def assert_window_interval_compatible(run_window: RunWindow | None, bounds: MandateBounds) -> None:
    """签发期相容性：限定时段 + 不整除 24 小时的间隔 = 最静默的失效模式。

    run_interval_minutes 白名单是 [60, 10080] 的任意整数，裸 API 可以传 100。
    100 分钟间隔 + 一个固定的每日时段，运行时刻每天漂移 40 分钟，几天后就再也
    落不进窗口——授权书状态 ACTIVE、没有任何错误，但永远不再运行。签发期一个
    取模就能结构性消除，运行期再发现时人已经损失了若干天。
    """
    if run_window is None or run_window.is_all_day:
        return
    interval = bounds.run_interval_minutes
    if 1440 % interval != 0 and interval % 1440 != 0:
        raise MandateViolation(
            "RUN_WINDOW_INCOMPATIBLE",
            f"interval {interval}min drifts against a fixed daily window and will eventually "
            "miss it entirely; use an interval that divides or multiplies 1440",
        )


def assert_run_authorized(
    mandate: AutomationMandate,
    *,
    organization_id: uuid.UUID,
    profile_external_id: str,
    runs_today: int,
    now: datetime,
    last_run_at: datetime | None = None,
) -> None:
    """每次自动运行前的完整边界检查。任一不满足即拒绝——拒绝的含义是"回到人"。

    检查顺序即下列顺序，运行窗口**追加在最末**：配额耗尽时告诉人"下一个窗口
    02:00 开"是误导（到了 02:00 配额可能仍然是耗尽的），先报配额更诚实。

    naive now 在最前面就拒：下面每一条闸都要拿它跟 tz-aware 的时刻比较，裸比较
    抛的是 TypeError（HTTP 面 500），拒绝的理由丢失了。带码拒绝不改变"不放行"
    这个结果，只是把它说清楚。
    """
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise MandateViolation(
            "NAIVE_DATETIME_REJECTED",
            "run authorization requires a timezone-aware moment; the system will not "
            "interpret a bare clock reading in the server's local timezone",
        )
    if mandate.state is not MandateState.ACTIVE:
        raise MandateViolation("MANDATE_NOT_ACTIVE", f"mandate state is {mandate.state}")
    if now >= mandate.expires_at:
        raise MandateViolation("MANDATE_EXPIRED", "mandate expired; re-issue required")
    if mandate.organization_id != organization_id:
        raise MandateViolation("SCOPE_MISMATCH", "mandate belongs to another organization")
    if mandate.profile_external_id != profile_external_id:
        raise MandateViolation("SCOPE_MISMATCH", "mandate does not cover this profile")
    if runs_today >= mandate.bounds.max_runs_per_day:
        raise MandateViolation("RUN_BUDGET_EXCEEDED", "daily run budget exhausted")
    if last_run_at is not None:
        elapsed = now - last_run_at
        interval = timedelta(minutes=mandate.bounds.run_interval_minutes)
        if elapsed < interval:
            raise MandateViolation(
                "RUN_TOO_SOON",
                f"last run was {elapsed} ago; contract interval is "
                f"{mandate.bounds.run_interval_minutes} minutes",
            )
    window = mandate.run_window
    if window is not None and not window.is_open_at(now):
        raise MandateViolation(
            "OUTSIDE_RUN_WINDOW",
            f"now is outside the authorized run window "
            f"{window.start_hour:02d}:00-{window.end_hour:02d}:00 ({window.timezone}); "
            f"next opens at {window.next_open_at(now).isoformat()}",
        )
