"""目标授权（AutomationMandate，DEC-114）测试：签发 SoD、边界检查、授权驱动的生成。"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from ads_control_plane.api.mcp_tools.service import ToolDenied
from ads_control_plane.api.mcp_tools.strategy_service import StrategyToolService
from ads_control_plane.authorization.model import Action, ClientType, Environment, Grant
from ads_control_plane.canonical.entity import (
    AdProduct,
    CanonicalEntityRef,
    EntityType,
    ParentRefs,
    Provider,
)
from ads_control_plane.canonical.ids import new_canonical_id
from ads_control_plane.canonical.money import Money
from ads_control_plane.identity.actor import (
    ActorContext,
    AuthenticationStrength,
    PrincipalType,
    Role,
)
from ads_control_plane.providers.mock.search_terms import MockSearchTermSource
from ads_control_plane.strategies.mandate import (
    AutomationMandate,
    MandateBounds,
    MandateObjective,
    MandateScope,
    MandateScopeKind,
    MandateViolation,
    ObjectiveKind,
    RunWindow,
    assert_run_authorized,
    issue_mandate,
)
from ads_control_plane.strategies.mandate_run import MandateRunOutcome, MandateRunRecord
from ads_control_plane.strategies.negation import NegationParameterPack, SearchTermRecord
from ads_control_plane.strategies.ports import (
    SearchTermFetch,
    SearchTermReadPort,
    UnjudgedGroup,
)
from ads_control_plane.strategies.store import (
    InMemoryCandidateSetStore,
    InMemoryMandateRunLog,
    InMemoryMandateStore,
)
from ads_control_plane.tasks.directive import ObjectLevel
from ads_control_plane.tasks.selection import SelectedObject, SelectionSet

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
ORG = new_canonical_id()


def actor(principal_type: PrincipalType, org=ORG) -> ActorContext:
    real_now = datetime.now(UTC)
    extra = (
        {"human_person_id": "boss"}
        if principal_type is PrincipalType.HUMAN
        else {"human_initiator_person_id": "boss"}
    )
    return ActorContext(
        principal_id=new_canonical_id(),
        principal_type=principal_type,
        organization_id=org,
        roles=frozenset({Role.APPROVER, Role.ANALYST}),
        client_id="c-1",
        session_id="s-1",
        authentication_strength=AuthenticationStrength.MFA,
        issued_at=real_now,
        expires_at=real_now + timedelta(hours=8),
        **extra,
    )


def pack() -> NegationParameterPack:
    return NegationParameterPack(
        lookback_days=30,
        min_spend=Money(amount="20.00", currency="USD"),
        min_clicks=25,
        max_data_staleness_hours=24,
    )


def objective() -> MandateObjective:
    return MandateObjective(
        objective="WASTED_SPEND_REMOVED", statement="每月压降 profile-A 的无效搜索词花费"
    )


def bounds(**overrides: int) -> MandateBounds:
    params = {"max_runs_per_day": 2, "max_candidates_per_run": 50, "valid_days": 14}
    params.update(overrides)
    return MandateBounds(**params)


def make_mandate(issuer: ActorContext | None = None, **bound_overrides: int):
    return issue_mandate(
        issuer or actor(PrincipalType.HUMAN),
        mandate_id=new_canonical_id(),
        profile_external_id="profile-A",
        objective=objective(),
        parameter_pack=pack(),
        bounds=bounds(**bound_overrides),
        now=NOW,
    )


class TestQuotaDayIsTheLocalDay:
    """「一天最多 N 次」的「天」，必须和「只在当地几点到几点跑」的「天」是同一个。

    2026-08-30 排查 #2/#9：配额此前按 UTC 日切，运行时段按 IANA 当地钟点，两个
    「天」相差整整一个时区偏移。UTC+8 的店在当地早上 8 点换一次配额——当地同一天里
    可以跑到 2N 次，而卡片上写着 N 次/日。人核对的是卡片上那句话。
    """

    def _log_and_mandate(self) -> tuple[InMemoryMandateRunLog, AutomationMandate]:
        mandate = issue_mandate(
            actor(PrincipalType.HUMAN),
            mandate_id=new_canonical_id(),
            profile_external_id="profile-A",
            objective=objective(),
            parameter_pack=pack(),
            bounds=bounds(max_runs_per_day=1),
            now=NOW,
            run_window=RunWindow(timezone="Asia/Shanghai", start_hour=0, end_hour=0),
        )
        return InMemoryMandateRunLog(), mandate

    def _record(self, log: InMemoryMandateRunLog, mandate_id, at: datetime) -> None:
        log.record(
            MandateRunRecord(
                run_id=new_canonical_id(),
                mandate_id=mandate_id,
                ran_at=at,
                outcome=MandateRunOutcome.NO_CANDIDATES,
                evaluated_ad_group_terms=1,
                distinct_search_terms=1,
                candidate_count=0,
                abstain_count=0,
                scope_filtered_out=0,
            )
        )

    def test_two_runs_straddling_the_utc_midnight_are_one_local_day(self) -> None:
        log, mandate = self._log_and_mandate()
        # 当地 2026-08-30 07:30 与 08:30（UTC+8）：同一个当地日，跨了 UTC 日界。
        first = datetime(2026, 8, 29, 23, 30, tzinfo=UTC)
        second = datetime(2026, 8, 30, 0, 30, tzinfo=UTC)
        self._record(log, mandate.mandate_id, first)
        assert log.count_on_day(mandate.mandate_id, second, mandate.quota_day) == 1
        with pytest.raises(MandateViolation) as exc:
            assert_run_authorized(
                mandate,
                organization_id=ORG,
                profile_external_id="profile-A",
                runs_today=log.count_on_day(mandate.mandate_id, second, mandate.quota_day),
                now=second,
                last_run_at=first,
            )
        assert exc.value.code == "RUN_BUDGET_EXCEEDED"
        # 按 UTC 日切它会是 0——那正是此前放行第二次的原因。
        assert log.count_on_day(mandate.mandate_id, second, lambda m: m.astimezone(UTC).date()) == 0

    def test_a_new_local_day_does_restore_the_budget(self) -> None:
        """反向也必须成立：换了当地日，配额确实回来了，否则配额永远解不开。"""
        log, mandate = self._log_and_mandate()
        self._record(log, mandate.mandate_id, datetime(2026, 8, 29, 23, 30, tzinfo=UTC))
        next_local_day = datetime(2026, 8, 30, 16, 30, tzinfo=UTC)  # 当地 8/31 00:30
        assert log.count_on_day(mandate.mandate_id, next_local_day, mandate.quota_day) == 0

    def _night_mandate(self) -> tuple[InMemoryMandateRunLog, AutomationMandate]:
        """22:00 → 次日 06:00（Asia/Shanghai），每天最多 1 次，最小间隔 6 小时。"""
        mandate = issue_mandate(
            actor(PrincipalType.HUMAN),
            mandate_id=new_canonical_id(),
            profile_external_id="profile-A",
            objective=objective(),
            parameter_pack=pack(),
            bounds=bounds(max_runs_per_day=1, run_interval_minutes=360),
            now=NOW,
            run_window=RunWindow(timezone="Asia/Shanghai", start_hour=22, end_hour=6),
        )
        return InMemoryMandateRunLog(), mandate

    def test_one_night_is_one_quota_day_even_though_midnight_splits_it(self) -> None:
        """跨午夜的运行时段里，当地午夜落在窗口**正中间**。

        按当地日历日切配额，一个连续的运行时段被劈成两个配额日：当地 22:10 跑一次
        （日界 8/30）、次日 04:20 再跑一次（日界 8/31），两次的 runs_today 都是 0，
        间隔 6.2h 也够，时段还开着——全部放行。而卡片上并排写着「每天 22:00 至次日
        06:00」与「1 次/日」，人核对这两句话读出来的是每晚一次；实际每晚两次，
        每次都是对领星生产 API 的一整轮多页读取。配额存在的理由正是拦住这个。
        """
        log, mandate = self._night_mandate()
        first = datetime(2026, 8, 29, 14, 10, tzinfo=UTC)  # 当地 8/29 22:10
        second = datetime(2026, 8, 29, 20, 20, tzinfo=UTC)  # 当地 8/30 04:20
        # 前提：两次都确实落在运行时段内，否则这条测的就不是配额了。
        assert mandate.run_window is not None
        assert mandate.run_window.is_open_at(first)
        assert mandate.run_window.is_open_at(second)
        self._record(log, mandate.mandate_id, first)
        assert log.count_on_day(mandate.mandate_id, second, mandate.quota_day) == 1
        with pytest.raises(MandateViolation) as exc:
            assert_run_authorized(
                mandate,
                organization_id=ORG,
                profile_external_id="profile-A",
                runs_today=log.count_on_day(mandate.mandate_id, second, mandate.quota_day),
                now=second,
                last_run_at=first,
            )
        assert exc.value.code == "RUN_BUDGET_EXCEEDED"
        # 按当地**日历日**切它会是 0——那正是同一晚能跑两次的原因。
        assert (
            log.count_on_day(
                mandate.mandate_id,
                second,
                lambda m: m.astimezone(mandate.quota_timezone).date(),
            )
            == 0
        )

    def test_the_next_night_gets_its_budget_back(self) -> None:
        """反向必须成立：换了一晚，配额确实回来了，否则这份授权只能跑一次。"""
        log, mandate = self._night_mandate()
        self._record(log, mandate.mandate_id, datetime(2026, 8, 29, 14, 10, tzinfo=UTC))
        next_night = datetime(2026, 8, 30, 14, 10, tzinfo=UTC)  # 当地 8/30 22:10
        assert log.count_on_day(mandate.mandate_id, next_night, mandate.quota_day) == 0

    def test_a_daytime_window_is_unaffected_by_the_shift(self) -> None:
        """不跨午夜的窗口不受影响：窗口内每一刻减去同一个偏移，仍落在同一天。"""
        mandate = issue_mandate(
            actor(PrincipalType.HUMAN),
            mandate_id=new_canonical_id(),
            profile_external_id="profile-A",
            objective=objective(),
            parameter_pack=pack(),
            bounds=bounds(max_runs_per_day=1),
            now=NOW,
            run_window=RunWindow(timezone="Asia/Shanghai", start_hour=9, end_hour=17),
        )
        morning = datetime(2026, 8, 30, 1, 30, tzinfo=UTC)  # 当地 09:30
        afternoon = datetime(2026, 8, 30, 8, 30, tzinfo=UTC)  # 当地 16:30
        assert mandate.quota_day(morning) == mandate.quota_day(afternoon)
        # 而次日同一钟点必须是另一个配额日。
        assert mandate.quota_day(morning + timedelta(days=1)) != mandate.quota_day(morning)

    def _dst_mandate(self, tz: str) -> AutomationMandate:
        return issue_mandate(
            actor(PrincipalType.HUMAN),
            mandate_id=new_canonical_id(),
            profile_external_id="profile-A",
            objective=objective(),
            parameter_pack=pack(),
            bounds=bounds(max_runs_per_day=1),
            now=NOW,
            run_window=RunWindow(timezone=tz, start_hour=22, end_hour=6),
        )

    def test_a_night_that_crosses_a_dst_jump_is_still_one_quota_day(self) -> None:
        """夏令时那一夜同样是一夜。

        平移用的是**当地墙上时钟**减法（aware datetime 减 timedelta 在 Python 里不做
        跨 DST 归一），这正是这里要的语义：日界钉在当地 22:00 这个钟点上，与那一天
        实际有 23 还是 25 小时无关。若有人把它改写成先转 UTC 再减，spring-forward
        那夜的日界会漂一小时，而这种漂移在没有 DST 的时区里永远看不出来。

        2026-03-08 America/New_York：当地 02:00 直接跳到 03:00，那一天只有 23 小时。
        """
        mandate = self._dst_mandate("America/New_York")
        spring = [
            datetime(2026, 3, 8, 3, 10, tzinfo=UTC),  # 当地 3/7 22:10 EST（窗口刚开）
            datetime(2026, 3, 8, 6, 30, tzinfo=UTC),  # 当地 3/8 01:30 EST（跳变前）
            datetime(2026, 3, 8, 8, 30, tzinfo=UTC),  # 当地 3/8 04:30 EDT（跳变后）
        ]
        days = {mandate.quota_day(m) for m in spring}
        assert len(days) == 1, f"同一夜被切成了多个配额日：{sorted(days)}"
        # 反向、也是真正能分辨两种写法的那一刻：跳变当天的**夜里** 22:30。
        # 墙钟减法把它平移到当地 3/8 00:30 → 配额日 3/8，与前一夜（3/7）分开——对的。
        # 先转 UTC 再减会得到 3/7，把两个不同的夜合成同一个配额日：1 次/日的授权在
        # 跳变次日整夜跑不了，而卡片上写着「每晚一次」，且一年只错这一次，没人会想到 DST。
        previous_night = datetime(2026, 3, 8, 3, 30, tzinfo=UTC)  # 当地 3/7 22:30 EST
        transition_night = datetime(2026, 3, 9, 2, 30, tzinfo=UTC)  # 当地 3/8 22:30 EDT
        assert mandate.quota_day(previous_night) != mandate.quota_day(transition_night)

    def test_the_repeated_hour_of_a_fall_back_night_is_one_quota_day(self) -> None:
        """fall-back 那夜当地 01:30 出现两次（EDT 一次、EST 一次），仍是同一夜。

        2026-11-01 America/New_York：当地 02:00 退回 01:00，那一天有 25 小时。
        """
        mandate = self._dst_mandate("America/New_York")
        autumn = [
            datetime(2026, 11, 1, 2, 10, tzinfo=UTC),  # 当地 10/31 22:10 EDT
            datetime(2026, 11, 1, 5, 30, tzinfo=UTC),  # 当地 01:30 EDT（第一次）
            datetime(2026, 11, 1, 6, 30, tzinfo=UTC),  # 当地 01:30 EST（第二次）
            datetime(2026, 11, 1, 9, 30, tzinfo=UTC),  # 当地 04:30 EST
        ]
        days = {mandate.quota_day(m) for m in autumn}
        assert len(days) == 1, f"同一夜被切成了多个配额日：{sorted(days)}"

    def test_a_timezone_whose_offset_is_not_a_whole_hour_still_works(self) -> None:
        """+05:45 这类偏移不影响日界：平移量是当地钟点上的整小时，与偏移无关。"""
        mandate = self._dst_mandate("Asia/Kathmandu")
        assert mandate.quota_day(datetime(2026, 8, 29, 16, 20, tzinfo=UTC)) == mandate.quota_day(
            datetime(2026, 8, 29, 22, 40, tzinfo=UTC)
        )

    def test_a_mandate_without_a_run_window_counts_in_utc(self) -> None:
        """没设运行时段的授权书没有声明过任何时区，只能按 UTC 切——这是「我们不知道
        当地是几点」的如实表达。界面据 quota_timezone 说明日切口径。"""
        assert make_mandate().quota_timezone is UTC


class TestMandateDomain:
    def test_ai_cannot_issue(self) -> None:
        with pytest.raises(MandateViolation) as exc:
            make_mandate(issuer=actor(PrincipalType.AI_CLIENT))
        assert exc.value.code == "AI_CANNOT_ISSUE_MANDATE"

    def test_expired_mandate_refuses_runs(self) -> None:
        mandate = make_mandate(valid_days=1)
        with pytest.raises(MandateViolation) as exc:
            assert_run_authorized(
                mandate,
                organization_id=ORG,
                profile_external_id="profile-A",
                runs_today=0,
                now=NOW + timedelta(days=2),
            )
        assert exc.value.code == "MANDATE_EXPIRED"

    def test_revoked_mandate_refuses_runs_and_ai_cannot_revoke(self) -> None:
        mandate = make_mandate()
        with pytest.raises(MandateViolation):
            mandate.revoke(actor(PrincipalType.AI_CLIENT))
        revoked = mandate.revoke(actor(PrincipalType.HUMAN))
        with pytest.raises(MandateViolation) as exc:
            assert_run_authorized(
                revoked,
                organization_id=ORG,
                profile_external_id="profile-A",
                runs_today=0,
                now=NOW,
            )
        assert exc.value.code == "MANDATE_NOT_ACTIVE"

    def test_daily_run_budget(self) -> None:
        mandate = make_mandate(max_runs_per_day=1)
        with pytest.raises(MandateViolation) as exc:
            assert_run_authorized(
                mandate,
                organization_id=ORG,
                profile_external_id="profile-A",
                runs_today=1,
                now=NOW,
            )
        assert exc.value.code == "RUN_BUDGET_EXCEEDED"

    def test_scope_mismatch(self) -> None:
        mandate = make_mandate()
        with pytest.raises(MandateViolation) as exc:
            assert_run_authorized(
                mandate,
                organization_id=ORG,
                profile_external_id="profile-B",
                runs_today=0,
                now=NOW,
            )
        assert exc.value.code == "SCOPE_MISMATCH"

    def test_bounds_whitelist(self) -> None:
        with pytest.raises(ValueError, match="valid_days"):
            bounds(valid_days=31)

    def test_sub_hourly_interval_rejected(self) -> None:
        """ "10 分钟一次"落在白名单外：比数据刷新更快的运行只产生重复决策。"""
        with pytest.raises(ValueError, match="run_interval_minutes"):
            bounds(run_interval_minutes=10)

    def test_run_too_soon_within_interval(self) -> None:
        mandate = make_mandate(run_interval_minutes=1440)
        with pytest.raises(MandateViolation) as exc:
            assert_run_authorized(
                mandate,
                organization_id=ORG,
                profile_external_id="profile-A",
                runs_today=1,
                now=NOW + timedelta(hours=2),
                last_run_at=NOW,
            )
        assert exc.value.code == "RUN_TOO_SOON"

    def test_run_allowed_after_interval(self) -> None:
        mandate = make_mandate(run_interval_minutes=60)
        assert_run_authorized(
            mandate,
            organization_id=ORG,
            profile_external_id="profile-A",
            runs_today=1,
            now=NOW + timedelta(hours=2),
            last_run_at=NOW,
        )

    def test_unready_objective_fails_closed_at_issuance(self) -> None:
        """清仓/新品/推销量已在枚举内，但数据地基未就绪——签发即拒并列出缺什么。"""
        with pytest.raises(MandateViolation) as exc:
            issue_mandate(
                actor(PrincipalType.HUMAN),
                mandate_id=new_canonical_id(),
                profile_external_id="profile-A",
                objective=MandateObjective(
                    objective=ObjectiveKind.CLEARANCE_VELOCITY, statement="清掉滞销库存"
                ),
                parameter_pack=pack(),
                bounds=bounds(),
                now=NOW,
            )
        assert exc.value.code == "OBJECTIVE_NOT_READY"
        assert "fba_inventory_feed" in str(exc.value)


def record(term: str, spend: str) -> SearchTermRecord:
    return SearchTermRecord(
        scope=CanonicalEntityRef(
            organization_id=ORG,
            provider=Provider.MOCK,
            provider_connection_id=new_canonical_id(),
            marketplace="US",
            shop_external_id="shop-1",
            profile_external_id="profile-A",
            ad_product=AdProduct.SP,
            entity_type=EntityType.AD_GROUP,
            entity_external_id="ag-1",
            parent_refs=ParentRefs(campaign_external_id="c-1"),
        ),
        search_term=term,
        clicks=40,
        conversions=0,
        spend=Money(amount=spend, currency="USD"),
        window_start=NOW - timedelta(days=30),
        window_end=NOW - timedelta(days=1),
        data_as_of=NOW - timedelta(hours=2),
    )


class PartlyUnreadableSource:
    """接了数据源、也取回了行，但其中一部分读不出来。

    Mock 源造不出这个形态：它 seed 什么就返回什么，账目恒为 0。而真实源每一轮
    都可能丢掉一些 (广告组, 词)——这正是「0 条候选」会不会被读成「这个店干净」
    的分界。
    """

    def __init__(
        self,
        records: tuple[SearchTermRecord, ...] = (),
        *,
        unjudged: int = 0,
        unattributable: int = 0,
        unjudged_ad_group: str = "ag-1",
        unjudged_campaign: str | None = "c-1",
    ) -> None:
        self._fetch = SearchTermFetch(
            records=records,
            # 未判断的组带身份：调用方要按作用域筛它们。默认落在整店授权的范围内。
            # unjudged_campaign=None 表示这个组连活动 id 都读不出来（源侧每一行都缺
            # campaign_id）——真实源确实会产出这种组，而它是作用域筛的边界情形。
            unjudged_groups=tuple(
                UnjudgedGroup(
                    ad_group_external_id=f"{unjudged_ad_group}-{i}",
                    campaign_external_ids=() if unjudged_campaign is None else (unjudged_campaign,),
                )
                for i in range(unjudged)
            ),
            unattributable_rows=unattributable,
        )

    def has_profile(self, profile_external_id: str) -> bool:
        return True

    def fetch_search_term_performance(
        self, profile_external_id: str, lookback_days: int, as_of: datetime
    ) -> SearchTermFetch:
        del profile_external_id, lookback_days, as_of
        return self._fetch


class TestMandateDrivenGeneration:
    def make_service(
        self, source: SearchTermReadPort, clock: Callable[[], datetime] | None = None
    ) -> tuple[
        StrategyToolService,
        InMemoryCandidateSetStore,
        InMemoryMandateStore,
        InMemoryMandateRunLog,
    ]:
        store = InMemoryCandidateSetStore()
        mandates = InMemoryMandateStore()
        run_log = InMemoryMandateRunLog()
        service = StrategyToolService(
            environment=Environment.STAGING,
            grants=[
                Grant(
                    grant_id=new_canonical_id(),
                    organization_id=ORG,
                    environments=frozenset({Environment.STAGING}),
                    actions=frozenset({Action.PROPOSAL_CREATE_DRAFT}),
                    client_types=frozenset({ClientType.MCP_AI}),
                )
            ],
            denies=[],
            search_terms=source,
            store=store,
            mandates=mandates,
            run_log=run_log,
            clock=clock or (lambda: NOW),
        )
        return service, store, mandates, run_log

    def test_generation_under_mandate_records_provenance(self) -> None:
        source = MockSearchTermSource()
        source.seed("profile-A", [record("cheap widget", "35.00")])
        service, store, mandates, _log = self.make_service(source)
        mandate = make_mandate()
        mandates.save(mandate)
        result = service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id="profile-A",
            mandate_id=str(mandate.mandate_id),
        )
        assert result["candidate_count"] == 1
        saved = store.list_by_state(ORG)[0]
        assert saved.mandate_id == mandate.mandate_id

    def test_parameter_override_forbidden_under_mandate(self) -> None:
        source = MockSearchTermSource()
        source.seed("profile-A", [record("cheap widget", "35.00")])
        service, _, mandates, _log = self.make_service(source)
        mandate = make_mandate()
        mandates.save(mandate)
        with pytest.raises(ToolDenied) as exc:
            service.generate_negation_candidate_set(
                actor(PrincipalType.AI_CLIENT),
                profile_external_id="profile-A",
                mandate_id=str(mandate.mandate_id),
                min_clicks=30,
            )
        assert exc.value.code == "MANDATE_PARAMS_FORBIDDEN"

    def test_unknown_mandate_rejected(self) -> None:
        service, _, _, _log = self.make_service(MockSearchTermSource())
        with pytest.raises(ToolDenied) as exc:
            service.generate_negation_candidate_set(
                actor(PrincipalType.AI_CLIENT),
                profile_external_id="profile-A",
                mandate_id=str(new_canonical_id()),
            )
        assert exc.value.code == "MANDATE_UNKNOWN"

    def test_a_mandate_id_from_a_previous_process_says_the_mandates_were_cleared(self) -> None:
        """重启后拿旧 mandate_id 来的 AI，要收到「重启清空了」而不是一句裸码。

        授权书只存在进程内存里，而改环境变量、加白名单店铺、更新代码都要重启——
        这是操作者被 runbook 明确要求去做的日常动作。重启后 Codex 会话里还记着
        昨天的 mandate_id，重发一次此前得到的整条回复就是 `MANDATE_UNKNOWN` 五个字。
        界面词典给这个码列的两个成因（已被撤销、不属于当前组织）恰恰都产生不了它：
        那两种在服务端分别是 MANDATE_NOT_ACTIVE 与 SCOPE_MISMATCH。于是 AI 只能
        照词典转述「可能被同事撤销了」，人去界面一看空空如也，先怀疑的是同事。

        另一半同样要钉住：不许把「改走即席模式」当绕路——即席不在任何授权书之下，
        不受作用域、配额与最小间隔约束，那正是这份授权存在的意义。
        """
        service, _, _, _log = self.make_service(MockSearchTermSource())
        with pytest.raises(ToolDenied) as exc:
            service.generate_negation_candidate_set(
                actor(PrincipalType.AI_CLIENT),
                profile_external_id="profile-A",
                mandate_id=str(new_canonical_id()),  # 形状合法、进程里没有：重启后的样子
            )
        detail = exc.value.detail or ""
        assert "cleared on restart" in detail and "re-issue" in detail
        assert "ad-hoc" in detail

    def test_an_eight_character_mandate_id_is_told_it_is_too_short(self) -> None:
        """界面表格只印 8 位前缀，人最容易贴给 AI 的就是它。

        服务端做的是 uuid.UUID() 精确解析，8 位必然失败。与「进程里没这份授权」
        共用一个错误码没问题（两者都不该泄露资源是否存在），但补充说明必须分开：
        一个要人去重签，另一个只要人取完整 ID，两个动作完全不同。
        """
        service, _, _, _log = self.make_service(MockSearchTermSource())
        with pytest.raises(ToolDenied) as exc:
            service.generate_negation_candidate_set(
                actor(PrincipalType.AI_CLIENT),
                profile_external_id="profile-A",
                mandate_id="bd72d354",
            )
        assert exc.value.code == "MANDATE_UNKNOWN"
        assert "36-character" in (exc.value.detail or "")

    def test_daily_budget_enforced_via_run_log(self) -> None:
        source = MockSearchTermSource()
        source.seed("profile-A", [record("cheap widget", "35.00")])
        service, _, mandates, _log = self.make_service(source)
        mandate = make_mandate(max_runs_per_day=1)
        mandates.save(mandate)
        first = service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id="profile-A",
            mandate_id=str(mandate.mandate_id),
        )
        assert first["set_id"] is not None
        with pytest.raises(ToolDenied) as exc:
            service.generate_negation_candidate_set(
                actor(PrincipalType.AI_CLIENT),
                profile_external_id="profile-A",
                mandate_id=str(mandate.mandate_id),
            )
        assert exc.value.code == "RUN_BUDGET_EXCEEDED"

    def test_the_quota_day_is_the_local_day_at_the_real_call_site(self) -> None:
        """配额时区必须在**生产调用点**上被证明生效，不只在 store 的单测里。

        2026-08-30 排查：TestQuotaDayIsTheLocalDay 自己调 count_on_day 并自己传
        mandate.quota_timezone，于是它证明的是「store 会按传进去的时区切日」——
        把 strategy_service 那一行的 quota_timezone 改回 UTC，整个套件仍然全绿，
        而这正是缺陷所在的那一行。一道只在测试里被正确调用的闸，等于没有闸。

        场景是真实的：UTC+8 的店，当地同一天的早 07:30 与 08:30 跑两次。按当地日
        切是第 2 次（超出 1 次/日，必须拒）；按 UTC 日切分属两天，第 2 次会被放行
        ——而卡片上写着「1 次/日」，人核对的是卡片上那句话。
        """
        source = MockSearchTermSource()
        source.seed("profile-A", [record("cheap widget", "35.00")])
        # 当地 08-29 07:30 与 08:30（Asia/Shanghai）：同一当地日，跨了 UTC 日界。
        # 两个时刻都在 record() 的 data_as_of 之后 24 小时内，免得整批 ABSTAIN
        # 把配额这道闸遮住——那样这条测试会因为一个无关的理由变绿。
        moments = [
            datetime(2026, 8, 28, 23, 30, tzinfo=UTC),
            datetime(2026, 8, 29, 0, 30, tzinfo=UTC),
        ]
        service, _store, mandates, run_log = self.make_service(source, clock=lambda: moments[0])
        mandate = issue_mandate(
            actor(PrincipalType.HUMAN),
            mandate_id=new_canonical_id(),
            profile_external_id="profile-A",
            objective=objective(),
            parameter_pack=pack(),
            # 两次相隔恰好 60 分钟 = 合约允许的最小间隔，间隔闸放行；
            # 第二次若被拒，只可能是被**配额**拒的。
            bounds=bounds(max_runs_per_day=1, run_interval_minutes=60),
            now=NOW,
            run_window=RunWindow(timezone="Asia/Shanghai", start_hour=0, end_hour=0),
        )
        mandates.save(mandate)
        first = service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id="profile-A",
            mandate_id=str(mandate.mandate_id),
        )
        assert first["set_id"] is not None

        moments[0] = moments[1]
        with pytest.raises(ToolDenied) as exc:
            service.generate_negation_candidate_set(
                actor(PrincipalType.AI_CLIENT),
                profile_external_id="profile-A",
                mandate_id=str(mandate.mandate_id),
            )
        assert exc.value.code == "RUN_BUDGET_EXCEEDED"
        # 钉住这条测试真正在测什么：按 UTC 日切，第二次的 runs_today 是 0（会放行）。
        assert (
            run_log.count_on_day(mandate.mandate_id, moments[1], lambda m: m.astimezone(UTC).date())
            == 0
        )
        assert run_log.count_on_day(mandate.mandate_id, moments[1], mandate.quota_day) == 1

    def test_second_run_within_interval_rejected(self) -> None:
        source = MockSearchTermSource()
        source.seed("profile-A", [record("cheap widget", "35.00")])
        service, _, mandates, _log = self.make_service(source)
        mandate = make_mandate(max_runs_per_day=2, run_interval_minutes=1440)
        mandates.save(mandate)
        first = service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id="profile-A",
            mandate_id=str(mandate.mandate_id),
        )
        assert first["set_id"] is not None
        with pytest.raises(ToolDenied) as exc:
            service.generate_negation_candidate_set(
                actor(PrincipalType.AI_CLIENT),
                profile_external_id="profile-A",
                mandate_id=str(mandate.mandate_id),
            )
        assert exc.value.code == "RUN_TOO_SOON"

    def test_run_that_yields_nothing_still_consumes_the_budget(self) -> None:
        """配额此前从候选集合数上算，于是**跑不出候选的运行不消耗配额**。

        而跑不出候选正是配置错了的表现：币种签错、店铺没接数据源、作用域把对象全
        挡掉、整批数据太旧。也就是说最该被拦住的情形，节流完全不生效——
        max_runs_per_day=1 的授权书可以被无限次触发，每次对真实源都是一轮多页读取
        （QPS=1）。这条测的就是那个洞。
        """
        source = MockSearchTermSource()
        source.seed("profile-A", [])  # 接了数据源，但这个窗口一行都没有
        service, store, mandates, run_log = self.make_service(source)
        mandate = make_mandate(max_runs_per_day=1)
        mandates.save(mandate)
        first = service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id="profile-A",
            mandate_id=str(mandate.mandate_id),
        )
        assert first["set_id"] is None
        assert store.list_by_state(ORG) == ()  # 没有任何集合可供反推
        assert run_log.count_on_day(mandate.mandate_id, NOW, mandate.quota_day) == 1
        with pytest.raises(ToolDenied) as exc:
            service.generate_negation_candidate_set(
                actor(PrincipalType.AI_CLIENT),
                profile_external_id="profile-A",
                mandate_id=str(mandate.mandate_id),
            )
        assert exc.value.code == "RUN_BUDGET_EXCEEDED"

    def test_failed_run_is_recorded_with_its_code_and_consumes_the_budget(self) -> None:
        """币种签错：调用方拿到 CURRENCY_MISMATCH，而人此前在界面上什么都看不到。

        授权书徽章仍是「生效中」、待批仍是空的——与「查了，确实没有浪费」逐字同形。
        运行流水把这次失败连同错误码记下来，界面才有得可说。
        """
        source = MockSearchTermSource()
        eur = record("cheap widget", "35.00").model_copy(
            update={"spend": Money(amount="35.00", currency="EUR")}
        )
        source.seed("profile-A", [eur])
        service, _, mandates, run_log = self.make_service(source)
        mandate = make_mandate(max_runs_per_day=1)
        mandates.save(mandate)
        with pytest.raises(ToolDenied) as exc:
            service.generate_negation_candidate_set(
                actor(PrincipalType.AI_CLIENT),
                profile_external_id="profile-A",
                mandate_id=str(mandate.mandate_id),
            )
        assert exc.value.code == "CURRENCY_MISMATCH"
        latest = run_log.latest(mandate.mandate_id)
        assert latest is not None
        assert latest.outcome is MandateRunOutcome.REJECTED
        assert latest.error_code == "CURRENCY_MISMATCH"
        # 失败同样消耗配额：重试一个注定失败的调用不该是免费的。
        with pytest.raises(ToolDenied) as second:
            service.generate_negation_candidate_set(
                actor(PrincipalType.AI_CLIENT),
                profile_external_id="profile-A",
                mandate_id=str(mandate.mandate_id),
            )
        assert second.value.code == "RUN_BUDGET_EXCEEDED"

    def test_the_four_look_alike_failures_get_four_different_outcomes(self) -> None:
        """四种「待批空空如也」必须区分得开，否则人不知道该改什么。

        每种对应一个不同的下一步：去接数据源 / 换窗口 / 改作用域重签 / 等新数据。
        """
        seen: dict[str, MandateRunOutcome] = {}

        # 1) 店铺没接数据源
        service, _, mandates, run_log = self.make_service(MockSearchTermSource())
        m1 = make_mandate()
        mandates.save(m1)
        service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id="profile-A",
            mandate_id=str(m1.mandate_id),
        )
        latest = run_log.latest(m1.mandate_id)
        assert latest is not None
        seen["no_data_source"] = latest.outcome

        # 2) 接了，但这个窗口一行都没有
        empty = MockSearchTermSource()
        empty.seed("profile-A", [])
        service, _, mandates, run_log = self.make_service(empty)
        m2 = make_mandate()
        mandates.save(m2)
        service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id="profile-A",
            mandate_id=str(m2.mandate_id),
        )
        latest = run_log.latest(m2.mandate_id)
        assert latest is not None
        seen["no_rows"] = latest.outcome

        # 3) 有行，但作用域把它们全挡掉了
        scoped = MockSearchTermSource()
        scoped.seed("profile-A", [record("cheap widget", "35.00")])
        service, _, mandates, run_log = self.make_service(scoped)
        m3 = issue_mandate(
            actor(PrincipalType.HUMAN),
            mandate_id=new_canonical_id(),
            profile_external_id="profile-A",
            objective=objective(),
            parameter_pack=pack(),
            bounds=bounds(),
            now=NOW,
            scope=MandateScope(
                kind=MandateScopeKind.OBJECTS,
                selection=SelectionSet(
                    items=(
                        SelectedObject(
                            level=ObjectLevel.AD_GROUP,
                            external_id="ag-other",
                            profile_external_id="profile-A",
                        ),
                    )
                ),
            ),
        )
        mandates.save(m3)
        result = service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id="profile-A",
            mandate_id=str(m3.mandate_id),
        )
        assert result["scope_filtered_out"] == 1
        latest = run_log.latest(m3.mandate_id)
        assert latest is not None
        seen["scope_empty"] = latest.outcome

        # 4) 有行、在作用域内，但整批数据都太旧
        stale = MockSearchTermSource()
        stale.seed(
            "profile-A",
            [
                record("cheap widget", "35.00").model_copy(
                    update={"data_as_of": NOW - timedelta(days=9)}
                )
            ],
        )
        service, _, mandates, run_log = self.make_service(stale)
        m4 = make_mandate()
        mandates.save(m4)
        service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id="profile-A",
            mandate_id=str(m4.mandate_id),
        )
        latest = run_log.latest(m4.mandate_id)
        assert latest is not None
        seen["all_abstained"] = latest.outcome

        assert seen == {
            "no_data_source": MandateRunOutcome.NO_DATA_SOURCE,
            "no_rows": MandateRunOutcome.NO_ROWS,
            "scope_empty": MandateRunOutcome.SCOPE_EMPTY,
            "all_abstained": MandateRunOutcome.ALL_ABSTAINED,
        }
        # 四个结局互不相同——合并任意两个都会让人拿不准该改什么。
        assert len(set(seen.values())) == 4

    def test_rows_we_could_not_read_are_not_reported_as_an_empty_window(self) -> None:
        """取回了行、一条也读不出来，与「这段窗口一行数据都没有」是两件事。

        两者的 len(records) 都是 0，此前都落到 NO_ROWS，而 NO_ROWS 把人指向
        「确认这家店在不在投放」「换更长的回看天数」——两个注定无效的动作：
        行本来就有，是形状不对。真正的原因在界面上一个字都不会出现。
        """
        service, _, mandates, run_log = self.make_service(
            PartlyUnreadableSource(unjudged=3, unattributable=2)
        )
        m = make_mandate()
        mandates.save(m)
        result = service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id="profile-A",
            mandate_id=str(m.mandate_id),
        )
        latest = run_log.latest(m.mandate_id)
        assert latest is not None
        assert latest.outcome is MandateRunOutcome.NO_USABLE_ROWS
        assert latest.outcome is not MandateRunOutcome.NO_ROWS
        assert result["unjudged_ad_group_terms"] == 3
        assert result["unattributable_rows"] == 2

    def _scoped_mandate(self, ad_group: str):
        return issue_mandate(
            actor(PrincipalType.HUMAN),
            mandate_id=new_canonical_id(),
            profile_external_id="profile-A",
            objective=objective(),
            parameter_pack=pack(),
            bounds=bounds(),
            now=NOW,
            scope=MandateScope(
                kind=MandateScopeKind.OBJECTS,
                selection=SelectionSet(
                    items=(
                        SelectedObject(
                            level=ObjectLevel.AD_GROUP,
                            external_id=ad_group,
                            profile_external_id="profile-A",
                        ),
                    )
                ),
            ),
        )

    def _campaign_scoped_mandate(self, campaign: str):
        """按**活动**圈定——这是最常见的圈法，也是 covers() 唯一需要活动 id 的那支。"""
        return issue_mandate(
            actor(PrincipalType.HUMAN),
            mandate_id=new_canonical_id(),
            profile_external_id="profile-A",
            objective=objective(),
            parameter_pack=pack(),
            bounds=bounds(),
            now=NOW,
            scope=MandateScope(
                kind=MandateScopeKind.OBJECTS,
                selection=SelectionSet(
                    items=(
                        SelectedObject(
                            level=ObjectLevel.CAMPAIGN,
                            external_id=campaign,
                            profile_external_id="profile-A",
                        ),
                    )
                ),
            ),
        )

    def test_a_campaign_scoped_mandate_still_sees_its_own_unjudged_groups(self) -> None:
        """按活动圈定时，作用域筛只能靠 UnjudgedGroup 上的活动 id。

        covers() 在没有活动 id 时只认「广告组被直接勾选」，而按活动圈定的授权
        一个广告组都没勾。所以只要这个组的活动 id 丢了，它对这份授权就彻底消失：
        缺口计数 0 → needs_attention 假 → 琥珀行不出现 → 悬停印「全部判断完毕」。
        真实发生的事是他圈的活动下有一个组整组没被判断，而他一个字都收不到。
        """
        source = PartlyUnreadableSource(
            records=(record("cheap widget", "35.00"),),
            unjudged=2,
            unjudged_ad_group="ag-in-campaign",
            unjudged_campaign="c-1",  # record 的默认活动，也是下面圈定的那个
        )
        service, _, mandates, run_log = self.make_service(source)
        m = self._campaign_scoped_mandate("c-1")
        mandates.save(m)
        result = service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id="profile-A",
            mandate_id=str(m.mandate_id),
        )
        assert result["unjudged_ad_group_terms"] == 2
        latest = run_log.latest(m.mandate_id)
        assert latest is not None
        assert latest.unjudged_ad_group_terms == 2

    def test_a_group_with_no_campaign_id_at_all_counts_as_possibly_in_scope(self) -> None:
        """连活动 id 都读不出来的组，按「排除不了它落在圈里」算。

        方向是刻意选的：多说一次「有东西没判断」的代价是一次多余的提醒；
        漏说的代价是人把一份不完整的结论当成「这段窗口很干净」，而这套计数
        存在的全部理由就是拦住后者。
        """
        source = PartlyUnreadableSource(
            records=(record("cheap widget", "35.00"),),
            unjudged=1,
            unjudged_ad_group="ag-faceless",
            unjudged_campaign=None,  # 该组每一行都缺 campaign_id
        )
        service, _, mandates, run_log = self.make_service(source)
        m = self._campaign_scoped_mandate("c-1")
        mandates.save(m)
        result = service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id="profile-A",
            mandate_id=str(m.mandate_id),
        )
        assert result["unjudged_ad_group_terms"] == 1
        latest = run_log.latest(m.mandate_id)
        assert latest is not None
        assert latest.unjudged_ad_group_terms == 1

    def test_bad_data_outside_the_scope_does_not_light_up_this_mandate(self) -> None:
        """一份只管 1 个广告组的授权书，不该因为店里别处的坏数据永远挂着红灯。

        「已评估 / 作用域挡掉」是作用域口径，「未判断」此前是全店口径——两个分母
        并排写在同一条运行记录上。店里 260 组读不出来、而这份授权圈的广告组一切
        正常时，卡片会写「上次跑通了，但没判断完：260 组没有被判断过」，人去查一份
        完全健康的授权书；而红灯天天亮着，红灯的意义随之作废。
        """
        source = PartlyUnreadableSource(
            records=(record("cheap widget", "35.00"),),  # 落在 ag-1（record 的默认广告组）
            unjudged=3,
            unjudged_ad_group="somewhere-else",  # 坏组在别的广告组下
            unjudged_campaign="other-campaign",
        )
        service, _, mandates, run_log = self.make_service(source)
        m = self._scoped_mandate("ag-1")
        mandates.save(m)
        result = service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id="profile-A",
            mandate_id=str(m.mandate_id),
        )
        assert result["unjudged_ad_group_terms"] == 0  # 作用域内一个都没丢
        assert result["source_accounting"]["unjudged_ad_group_terms_store_wide"] == 3
        latest = run_log.latest(m.mandate_id)
        assert latest is not None
        assert latest.unjudged_ad_group_terms == 0

    def test_bad_data_inside_the_scope_is_not_reported_as_an_empty_scope(self) -> None:
        """反过来：作用域内的组全读不出来时，不许说「圈的对象没有数据」。

        records 非空（店里别处正常），作用域过滤后 evaluated_count == 0，此前结局落到
        SCOPE_EMPTY——界面据此叫人「改作用域后重签」。而作用域本来是对的，真实原因是
        这些组的行读不出来。人照做会改一个正确的作用域，问题分毫未动。
        """
        source = PartlyUnreadableSource(
            records=(record("cheap widget", "35.00"),),  # 落在 ag-1，不在作用域内
            unjudged=2,
            unjudged_ad_group="ag-scoped",
            unjudged_campaign="c-1",
        )
        service, _, mandates, run_log = self.make_service(source)
        m = self._scoped_mandate("ag-scoped-0")  # 命中第一个坏组
        mandates.save(m)
        result = service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id="profile-A",
            mandate_id=str(m.mandate_id),
        )
        assert result["unjudged_ad_group_terms"] == 1
        latest = run_log.latest(m.mandate_id)
        assert latest is not None
        assert latest.outcome is MandateRunOutcome.NO_USABLE_ROWS
        assert latest.outcome is not MandateRunOutcome.SCOPE_EMPTY

    def test_a_clean_looking_run_says_how_much_it_never_judged(self) -> None:
        """0 条候选 + 有东西没被判断 = 「在我看得懂的那部分里没有」，不是「这个店干净」。

        这两句话对人的意思完全相反，而响应里其余每个数字都正常：被丢掉的组
        连一个占位都没有。读响应的是 AI，AI 唯一的动作是把它讲给人听。
        """
        source = PartlyUnreadableSource(
            records=(record("cheap widget", "1.00"),),  # 花费远低于门槛 → 不产候选
            unjudged=7,
        )
        service, _, mandates, run_log = self.make_service(source)
        m = make_mandate()
        mandates.save(m)
        result = service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id="profile-A",
            mandate_id=str(m.mandate_id),
        )
        assert result["candidate_count"] == 0
        assert result["unjudged_ad_group_terms"] == 7
        latest = run_log.latest(m.mandate_id)
        assert latest is not None
        # 结局仍是「没有该否的词」——它对读得懂的那部分是真话；但记录必须同时带上
        # 「有 7 组没被判断过」，界面据此不把这一轮报成绿灯。
        assert latest.outcome is MandateRunOutcome.NO_CANDIDATES
        assert latest.unjudged_ad_group_terms == 7

    def test_successful_run_links_back_to_the_set_it_produced(self) -> None:
        source = MockSearchTermSource()
        source.seed("profile-A", [record("cheap widget", "35.00")])
        service, _, mandates, run_log = self.make_service(source)
        mandate = make_mandate()
        mandates.save(mandate)
        result = service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id="profile-A",
            mandate_id=str(mandate.mandate_id),
        )
        latest = run_log.latest(mandate.mandate_id)
        assert latest is not None
        assert latest.outcome is MandateRunOutcome.CANDIDATES
        assert str(latest.set_id) == result["set_id"]
        assert latest.candidate_count == 1
        assert latest.error_code is None

    def test_ad_hoc_run_is_not_recorded_against_any_mandate(self) -> None:
        """即席运行没有授权书可挂：记了没有归属，也不该消耗任何人的配额。"""
        source = MockSearchTermSource()
        source.seed("profile-A", [record("cheap widget", "35.00")])
        service, _, mandates, run_log = self.make_service(source)
        mandate = make_mandate()
        mandates.save(mandate)
        service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT), profile_external_id="profile-A"
        )
        assert run_log.latest(mandate.mandate_id) is None
        assert run_log.count_on_day(mandate.mandate_id, NOW, mandate.quota_day) == 0

    def test_ad_hoc_runs_are_capped_too_and_say_so(self) -> None:
        """即席模式此前没有任何候选上限，而注释声称有（2026-08-30 排查 #10）。

        后果不在机器这一侧：一次即席运行可以冻结出几百条候选，卡片上是一张几百行
        的表，而「批准」是一个按钮、一次点击。AX-07 要求被批准的内容就是被看见的
        内容——几百行没有人真的看过。截断必须如实回报，不静默丢弃。
        """
        from ads_control_plane.api.mcp_tools import strategy_service as svc

        source = MockSearchTermSource()
        source.seed(
            "profile-A",
            [record(f"waste term {i:03d}", f"{100 + i}.00") for i in range(svc._AD_HOC_MAX + 3)],
        )
        service, store, _mandates, _log = self.make_service(source)
        result = service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id="profile-A",
            lookback_days=30,
            min_spend_amount="20.00",
            currency="USD",
            min_clicks=25,
            max_data_staleness_hours=24,
        )
        assert result["truncated_from"] == svc._AD_HOC_MAX + 3
        assert result["candidate_count"] == svc._AD_HOC_MAX
        # 留下的是花费最高的那些——与授权模式同一条路径、同一个排序。
        saved = store.list_by_state(ORG)[0]
        assert len(saved.candidates) == svc._AD_HOC_MAX
        assert saved.truncated_from == svc._AD_HOC_MAX + 3

    def test_truncation_keeps_highest_spend(self) -> None:
        source = MockSearchTermSource()
        source.seed(
            "profile-A",
            [record("small waste", "25.00"), record("big waste", "90.00")],
        )
        service, store, mandates, _log = self.make_service(source)
        mandate = make_mandate(max_candidates_per_run=1)
        mandates.save(mandate)
        result = service.generate_negation_candidate_set(
            actor(PrincipalType.AI_CLIENT),
            profile_external_id="profile-A",
            mandate_id=str(mandate.mandate_id),
        )
        assert result["truncated_from"] == 2
        assert result["candidate_count"] == 1
        saved = store.list_by_state(ORG)[0]
        assert saved.candidates[0].search_term == "big waste"


class TestNextQuotaDayStartUsesTheSameDayAsTheQuota:
    """配额日什么时候翻篇，必须和 quota_day 的口径逐字一致。

    这个时刻的唯一用途是回答「今天的次数用完了，那什么时候能再跑」。它若按当地
    午夜算、而配额日按 start_hour 平移过，两个「天」又会错开一个 start_hour——
    人守到那一刻发起，仍旧撞 RUN_BUDGET_EXCEEDED。
    """

    def test_a_plain_window_rolls_over_at_local_midnight(self) -> None:
        m = issue_mandate(
            actor(PrincipalType.HUMAN),
            mandate_id=new_canonical_id(),
            profile_external_id="profile-A",
            objective=objective(),
            parameter_pack=pack(),
            bounds=bounds(),
            now=NOW,
            run_window=RunWindow(timezone="Asia/Shanghai", start_hour=9, end_hour=17),
        )
        moment = datetime(2026, 8, 28, 6, 0, tzinfo=UTC)  # 当地 14:00
        start = m.next_quota_day_start(moment)
        assert m.quota_day(start) == m.quota_day(moment) + timedelta(days=1)
        # 差一微秒就还在今天——边界必须是恰好翻篇的那一刻，不是「第二天某个时候」。
        assert m.quota_day(start - timedelta(microseconds=1)) == m.quota_day(moment)

    def test_a_window_across_midnight_rolls_over_at_its_own_start_hour(self) -> None:
        m = issue_mandate(
            actor(PrincipalType.HUMAN),
            mandate_id=new_canonical_id(),
            profile_external_id="profile-A",
            objective=objective(),
            parameter_pack=pack(),
            bounds=bounds(),
            now=NOW,
            run_window=RunWindow(timezone="Asia/Shanghai", start_hour=22, end_hour=6),
        )
        moment = datetime(2026, 8, 28, 20, 10, tzinfo=UTC)  # 当地次日 04:10，在窗口里
        start = m.next_quota_day_start(moment)
        assert m.quota_day(start) == m.quota_day(moment) + timedelta(days=1)
        assert m.quota_day(start - timedelta(microseconds=1)) == m.quota_day(moment)
        # 日界落在当地 22:00（窗口起点），不是当地午夜。
        assert start.astimezone(m.quota_timezone).hour == 22


def test_the_card_does_not_promise_a_time_the_daily_quota_will_still_refuse() -> None:
    """配额用完之后，卡片上「最早几点可再发起」不许报最小间隔算出的时刻。

    两道闸并存：最小间隔和日配额。此前这个字段只按间隔算，于是同一格里并排出现
    「今天的次数已用完」和「最早 13:00 可再发起」——人守到 13:00 点一次、被
    RUN_BUDGET_EXCEEDED 拒一次，两句话没有一句告诉他到底该等到什么时候。
    而被拒的尝试故意不进流水（记进去会自耗配额），卡片纹丝不动，他连刚才那次
    有没有打到服务端都判断不出。
    """
    from ads_control_plane.api.approval_api import _mandate_summary

    m = issue_mandate(
        actor(PrincipalType.HUMAN),
        mandate_id=new_canonical_id(),
        profile_external_id="profile-A",
        objective=objective(),
        parameter_pack=pack(),
        # 1 次/日、间隔 1 小时：跑完这一次，1 小时后间隔就到了，配额却要等到明天。
        bounds=MandateBounds(
            max_runs_per_day=1,
            max_candidates_per_run=50,
            valid_days=14,
            run_interval_minutes=60,
        ),
        now=NOW,
    )
    ran_at = NOW
    run = MandateRunRecord(
        run_id=new_canonical_id(),
        mandate_id=m.mandate_id,
        ran_at=ran_at,
        outcome=MandateRunOutcome.CANDIDATES,
        evaluated_ad_group_terms=9,
        distinct_search_terms=9,
        candidate_count=3,
        abstain_count=0,
        scope_filtered_out=0,
        set_id=new_canonical_id(),
    )
    summary = _mandate_summary(m, runs_of=lambda _id: (run,), now=ran_at + timedelta(minutes=5))

    assert summary["runs_remaining_today"] == 0
    next_allowed = datetime.fromisoformat(summary["next_run_allowed_at"])
    assert next_allowed > ran_at + timedelta(minutes=60), "配额已空，间隔到了也跑不了"
    assert next_allowed == m.next_quota_day_start(ran_at), "该报的是配额日翻篇的那一刻"
