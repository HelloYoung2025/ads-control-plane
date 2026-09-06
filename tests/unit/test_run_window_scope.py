"""运行窗口（RunWindow）与授权作用域（MandateScope）的域层合同测试。

覆盖 Owner 2026-08-28 的两条反馈：
- "缺少运行的时间段…例如还有些广告在吉隆坡时间的凌晨 2 点到晚上 6 点"
- "签发授权书往往如果对已有的 campaign/group 或者广告进行设置，这里完全没有渠道"

以及一条向后兼容断言：不带 scope / run_window 的授权书行为与今天逐字相同。
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ads_control_plane.canonical.ids import new_canonical_id
from ads_control_plane.canonical.money import Money
from ads_control_plane.identity.actor import (
    ActorContext,
    AuthenticationStrength,
    PrincipalType,
    Role,
)
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
    assert_window_interval_compatible,
    issue_mandate,
)
from ads_control_plane.strategies.negation import NegationParameterPack
from ads_control_plane.tasks.directive import ObjectLevel
from ads_control_plane.tasks.selection import SelectedObject, SelectionError, SelectionSet

KL = "Asia/Kuala_Lumpur"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
ORG = new_canonical_id()


def human() -> ActorContext:
    real_now = datetime.now(UTC)
    return ActorContext(
        principal_id=new_canonical_id(),
        principal_type=PrincipalType.HUMAN,
        organization_id=ORG,
        roles=frozenset({Role.APPROVER}),
        client_id="c-1",
        session_id="s-1",
        authentication_strength=AuthenticationStrength.MFA,
        issued_at=real_now,
        expires_at=real_now + timedelta(hours=8),
        human_person_id="boss",
    )


def pack() -> NegationParameterPack:
    return NegationParameterPack(
        lookback_days=30,
        min_spend=Money(amount="20.00", currency="USD"),
        min_clicks=25,
        max_data_staleness_hours=24,
    )


def bounds(**overrides: int) -> MandateBounds:
    params: dict[str, int] = {
        "max_runs_per_day": 2,
        "max_candidates_per_run": 50,
        "valid_days": 14,
        "run_interval_minutes": 1440,
    }
    params.update(overrides)
    return MandateBounds(**params)


def selection(*pairs: tuple[ObjectLevel, str], profile: str = "profile-A") -> SelectionSet:
    return SelectionSet(
        items=tuple(
            SelectedObject(level=level, external_id=ext, profile_external_id=profile)
            for level, ext in pairs
        )
    )


def objects_scope(*pairs: tuple[ObjectLevel, str], profile: str = "profile-A") -> MandateScope:
    return MandateScope(kind=MandateScopeKind.OBJECTS, selection=selection(*pairs, profile=profile))


def make_mandate(
    *,
    scope: MandateScope | None = None,
    run_window: RunWindow | None = None,
    objective: ObjectiveKind = ObjectiveKind.WASTED_SPEND_REMOVED,
    **bound_overrides: int,
) -> AutomationMandate:
    return issue_mandate(
        human(),
        mandate_id=new_canonical_id(),
        profile_external_id="profile-A",
        objective=MandateObjective(objective=objective, statement="清除无效搜索词花费"),
        parameter_pack=pack(),
        bounds=bounds(**bound_overrides),
        now=NOW,
        scope=scope,
        run_window=run_window,
    )


def at_kl(hour: int, minute: int = 0) -> datetime:
    """吉隆坡当地时刻（UTC+8，无夏令时）。"""
    return datetime(2026, 8, 28, hour, minute, tzinfo=ZoneInfo(KL))


# ------------------------------------------------------------------ RunWindow 构造合同


class TestRunWindowContract:
    def test_accepts_kuala_lumpur(self) -> None:
        window = RunWindow(timezone=KL, start_hour=2, end_hour=18)
        assert window.timezone == KL
        assert not window.is_all_day
        assert not window.crosses_midnight

    def test_unknown_timezone_rejected(self) -> None:
        with pytest.raises(MandateViolation) as exc:
            RunWindow(timezone="Asia/Kuala_Lumpar", start_hour=2, end_hour=18)
        assert exc.value.code == "INVALID_TIMEZONE"

    def test_blank_timezone_rejected(self) -> None:
        """时区不可省：系统不会拿服务器所在地的钟点替人解释"凌晨 2 点"。"""
        with pytest.raises(MandateViolation) as exc:
            RunWindow(timezone="   ", start_hour=2, end_hour=18)
        assert exc.value.code == "INVALID_TIMEZONE"

    def test_path_like_timezone_rejected(self) -> None:
        """ZoneInfo 对越界 key 抛 ValueError 而非 ZoneInfoNotFoundError，同样兜住。"""
        with pytest.raises(MandateViolation) as exc:
            RunWindow(timezone="../../etc/passwd", start_hour=2, end_hour=18)
        assert exc.value.code == "INVALID_TIMEZONE"

    @pytest.mark.parametrize(("start", "end"), [(24, 6), (-1, 6), (2, 24), (2, -1)])
    def test_hour_out_of_range_rejected(self, start: int, end: int) -> None:
        with pytest.raises(MandateViolation) as exc:
            RunWindow(timezone="UTC", start_hour=start, end_hour=end)
        assert exc.value.code == "RUN_WINDOW_INVALID"

    def test_equal_hours_means_all_day(self) -> None:
        window = RunWindow(timezone=KL, start_hour=0, end_hour=0)
        assert window.is_all_day is True

    def test_frozen(self) -> None:
        window = RunWindow(timezone=KL, start_hour=2, end_hour=18)
        with pytest.raises(ValueError, match="frozen"):
            window.start_hour = 5  # type: ignore[misc]


# ------------------------------------------------------------------ RunWindow 成员判定


class TestRunWindowMembership:
    def test_daytime_window_open_inside(self) -> None:
        window = RunWindow(timezone=KL, start_hour=2, end_hour=18)
        assert window.is_open_at(at_kl(11)) is True

    def test_daytime_window_closed_outside(self) -> None:
        window = RunWindow(timezone=KL, start_hour=2, end_hour=18)
        assert window.is_open_at(at_kl(22)) is False
        assert window.is_open_at(at_kl(1, 59)) is False

    def test_start_hour_is_inclusive_end_hour_is_exclusive(self) -> None:
        """开窗时刻算开、闭窗时刻算关——区间左闭右开，明确断言避免日后漂移。"""
        window = RunWindow(timezone=KL, start_hour=2, end_hour=18)
        assert window.is_open_at(at_kl(2, 0)) is True
        assert window.is_open_at(at_kl(17, 59)) is True
        assert window.is_open_at(at_kl(18, 0)) is False

    def test_crosses_midnight_true_table(self) -> None:
        """22 → 06：晚 10 点到次日早 6 点。"""
        window = RunWindow(timezone=KL, start_hour=22, end_hour=6)
        assert window.crosses_midnight is True
        assert window.is_open_at(at_kl(23)) is True
        assert window.is_open_at(at_kl(0, 30)) is True
        assert window.is_open_at(at_kl(5, 59)) is True
        assert window.is_open_at(at_kl(12)) is False

    def test_crosses_midnight_boundaries(self) -> None:
        window = RunWindow(timezone=KL, start_hour=22, end_hour=6)
        assert window.is_open_at(at_kl(22, 0)) is True
        assert window.is_open_at(at_kl(21, 59)) is False
        assert window.is_open_at(at_kl(6, 0)) is False

    def test_a_window_ending_at_midnight_does_not_cross_it(self) -> None:
        """end_hour == 0 = 「到当天结束为止」，一分钟都没跨过午夜。

        光看 start > end 会把 22→0 判成跨午夜，而 is_open_at 那一支退化成
        `hour >= 22`——22:00–23:59。配额日据此平移，卡片还会写「整夜算同一天」，
        对一个根本没有夜的时段说的（2026-08-30 排查）。
        """
        window = RunWindow(timezone=KL, start_hour=22, end_hour=0)
        assert window.crosses_midnight is False
        assert window.is_open_at(at_kl(22, 0)) is True
        assert window.is_open_at(at_kl(23, 59)) is True
        assert window.is_open_at(at_kl(0, 30)) is False
        # 真的跨午夜的那种仍然是 True，别把这条修成一刀切。
        assert RunWindow(timezone=KL, start_hour=22, end_hour=6).crosses_midnight is True

    def test_all_day_window_open_every_hour(self) -> None:
        window = RunWindow(timezone=KL, start_hour=9, end_hour=9)
        assert all(window.is_open_at(at_kl(h)) for h in range(24))

    def test_membership_uses_window_timezone_not_utc(self) -> None:
        """P4 锚点：同一个 UTC 时刻，按吉隆坡钟点判定，不按服务器/UTC 钟点。"""
        window = RunWindow(timezone=KL, start_hour=2, end_hour=18)
        utc_1900 = datetime(2026, 8, 28, 19, 0, tzinfo=UTC)  # = 吉隆坡次日 03:00
        assert utc_1900.hour == 19  # UTC 钟点在窗口外
        assert window.is_open_at(utc_1900) is True  # 当地钟点在窗口内

    def test_naive_datetime_rejected(self) -> None:
        window = RunWindow(timezone=KL, start_hour=2, end_hour=18)
        with pytest.raises(MandateViolation) as exc:
            window.is_open_at(datetime(2026, 8, 28, 11, 0))
        assert exc.value.code == "NAIVE_DATETIME_REJECTED"

    def test_naive_rejected_even_for_all_day_window(self) -> None:
        """全天窗口也不放行 naive：无时区的时刻本身就不可用于钟点比较。"""
        window = RunWindow(timezone=KL, start_hour=0, end_hour=0)
        with pytest.raises(MandateViolation) as exc:
            window.is_open_at(datetime(2026, 8, 28, 11, 0))
        assert exc.value.code == "NAIVE_DATETIME_REJECTED"

    def test_next_open_at_same_day_and_next_day(self) -> None:
        window = RunWindow(timezone=KL, start_hour=2, end_hour=18)
        before = window.next_open_at(at_kl(1))
        assert (before.day, before.hour) == (28, 2)
        after = window.next_open_at(at_kl(20))
        assert (after.day, after.hour) == (29, 2)

    def test_next_open_at_rejects_naive(self) -> None:
        window = RunWindow(timezone=KL, start_hour=2, end_hour=18)
        with pytest.raises(MandateViolation) as exc:
            window.next_open_at(datetime(2026, 8, 28, 11, 0))
        assert exc.value.code == "NAIVE_DATETIME_REJECTED"


# ------------------------------------------------------------------ MandateScope 合同


class TestMandateScopeContract:
    def test_objects_without_selection_rejected(self) -> None:
        with pytest.raises(MandateViolation) as exc:
            MandateScope(kind=MandateScopeKind.OBJECTS)
        assert exc.value.code == "SCOPE_SELECTION_REQUIRED"

    def test_profile_with_selection_rejected(self) -> None:
        """整店与勾选清单只能选一个；审计要能区分"确认要整店"和"忘了填"。"""
        with pytest.raises(MandateViolation) as exc:
            MandateScope(
                kind=MandateScopeKind.PROFILE,
                selection=selection((ObjectLevel.CAMPAIGN, "c-1")),
            )
        assert exc.value.code == "MANDATE_SCOPE_CONFLICT"

    def test_empty_selection_rejected_by_selection_set(self) -> None:
        """空集在 SelectionSet 构造期就被拒（SELECTION_EMPTY），本模型不重复实现。"""
        with pytest.raises(SelectionError) as exc:
            SelectionSet(items=())
        assert exc.value.code == "SELECTION_EMPTY"

    def test_profile_scope_reports_no_objects(self) -> None:
        scope = MandateScope(kind=MandateScopeKind.PROFILE)
        assert scope.object_count == 0
        assert scope.profile_external_id is None

    def test_objects_scope_reports_profile_and_count(self) -> None:
        scope = objects_scope((ObjectLevel.CAMPAIGN, "c-1"), (ObjectLevel.AD_GROUP, "ag-1"))
        assert scope.object_count == 2
        assert scope.profile_external_id == "profile-A"


class TestMandateScopeCovers:
    def test_profile_scope_covers_everything(self) -> None:
        scope = MandateScope(kind=MandateScopeKind.PROFILE)
        assert scope.covers(ad_group_external_id="ag-anything", campaign_external_id=None) is True

    def test_covers_ad_group_directly(self) -> None:
        scope = objects_scope((ObjectLevel.AD_GROUP, "ag-1"))
        assert scope.covers(ad_group_external_id="ag-1", campaign_external_id="c-9") is True

    def test_covers_via_parent_campaign(self) -> None:
        scope = objects_scope((ObjectLevel.CAMPAIGN, "c-1"))
        assert scope.covers(ad_group_external_id="ag-unknown", campaign_external_id="c-1") is True

    def test_does_not_cover_unrelated_object(self) -> None:
        scope = objects_scope((ObjectLevel.CAMPAIGN, "c-1"), (ObjectLevel.AD_GROUP, "ag-1"))
        assert scope.covers(ad_group_external_id="ag-2", campaign_external_id="c-2") is False

    def test_does_not_cover_when_campaign_ref_missing(self) -> None:
        scope = objects_scope((ObjectLevel.CAMPAIGN, "c-1"))
        assert scope.covers(ad_group_external_id="ag-1", campaign_external_id=None) is False

    def test_ad_group_id_does_not_match_campaign_level_entry(self) -> None:
        """层级参与 key：ID 撞名不构成命中。"""
        scope = objects_scope((ObjectLevel.CAMPAIGN, "x-1"))
        assert scope.covers(ad_group_external_id="x-1", campaign_external_id=None) is False


# ------------------------------------------------------------------ 授权书字段与签发


class TestMandateScopeAndWindowFields:
    def test_defaults_are_backward_compatible(self) -> None:
        """不传 scope / run_window 的授权书 = 整店 + 全天，与今天逐字相同。"""
        mandate = make_mandate()
        assert mandate.scope is None
        assert mandate.run_window is None
        assert_run_authorized(
            mandate,
            organization_id=ORG,
            profile_external_id="profile-A",
            runs_today=0,
            now=NOW,
        )

    def test_issue_carries_scope_and_window(self) -> None:
        mandate = make_mandate(
            scope=objects_scope((ObjectLevel.AD_GROUP, "ag-1")),
            run_window=RunWindow(timezone=KL, start_hour=2, end_hour=18),
        )
        assert mandate.scope is not None and mandate.scope.object_count == 1
        assert mandate.run_window is not None and mandate.run_window.timezone == KL

    def test_scope_profile_mismatch_rejected(self) -> None:
        """一份授权只管一个店铺：勾选集的 profile 与授权书不一致，签发即拒。"""
        with pytest.raises(MandateViolation) as exc:
            make_mandate(scope=objects_scope((ObjectLevel.CAMPAIGN, "c-1"), profile="profile-B"))
        assert exc.value.code == "SCOPE_PROFILE_MISMATCH"

    def test_scope_profile_match_accepted(self) -> None:
        mandate = make_mandate(scope=objects_scope((ObjectLevel.CAMPAIGN, "c-1")))
        assert mandate.scope is not None
        assert mandate.scope.profile_external_id == "profile-A"

    def test_target_level_scope_rejected(self) -> None:
        """投放层作用域在数据上无法判定包含关系——签发期拒，而不是运行期空转。"""
        with pytest.raises(MandateViolation) as exc:
            make_mandate(scope=objects_scope((ObjectLevel.TARGET, "kw-1")))
        assert exc.value.code == "MANDATE_SCOPE_LEVEL_UNSUPPORTED"

    def test_campaign_and_ad_group_levels_accepted(self) -> None:
        mandate = make_mandate(
            scope=objects_scope((ObjectLevel.CAMPAIGN, "c-1"), (ObjectLevel.AD_GROUP, "ag-1"))
        )
        assert mandate.scope is not None and mandate.scope.object_count == 2

    def test_unready_objective_still_wins_over_scope_rejection(self) -> None:
        """未就绪目标 + 坏作用域 → 仍先报 OBJECTIVE_NOT_READY（教育路径优先）。"""
        with pytest.raises(MandateViolation) as exc:
            make_mandate(
                objective=ObjectiveKind.CLEARANCE_VELOCITY,
                scope=objects_scope((ObjectLevel.TARGET, "kw-1")),
            )
        assert exc.value.code == "OBJECTIVE_NOT_READY"

    def test_scope_and_window_do_not_shift_parameter_pack_hash(self) -> None:
        """作用域与时段是授权边界，不是策略参数：不得混进参数包 hash。"""
        plain = make_mandate()
        scoped = make_mandate(
            scope=objects_scope((ObjectLevel.AD_GROUP, "ag-1")),
            run_window=RunWindow(timezone=KL, start_hour=2, end_hour=18),
        )
        assert scoped.parameter_pack.content_hash() == plain.parameter_pack.content_hash()


# ------------------------------------------------------------------ 运行闸


class TestRunWindowGate:
    def test_run_inside_window_allowed(self) -> None:
        mandate = make_mandate(run_window=RunWindow(timezone=KL, start_hour=2, end_hour=18))
        assert_run_authorized(
            mandate,
            organization_id=ORG,
            profile_external_id="profile-A",
            runs_today=0,
            now=at_kl(11),
        )

    def test_run_outside_window_rejected(self) -> None:
        mandate = make_mandate(run_window=RunWindow(timezone=KL, start_hour=2, end_hour=18))
        with pytest.raises(MandateViolation) as exc:
            assert_run_authorized(
                mandate,
                organization_id=ORG,
                profile_external_id="profile-A",
                runs_today=0,
                now=at_kl(22),
            )
        assert exc.value.code == "OUTSIDE_RUN_WINDOW"

    def test_rejection_message_names_next_opening(self) -> None:
        """拒绝要说人话下一句：什么时候能跑。丢掉它等于把 fail-closed 做成哑巴。"""
        mandate = make_mandate(run_window=RunWindow(timezone=KL, start_hour=2, end_hour=18))
        with pytest.raises(MandateViolation) as exc:
            assert_run_authorized(
                mandate,
                organization_id=ORG,
                profile_external_id="profile-A",
                runs_today=0,
                now=at_kl(22),
            )
        assert "2026-08-29T02:00:00+08:00" in str(exc.value)
        assert KL in str(exc.value)

    def test_owner_kuala_lumpur_scenario(self) -> None:
        """Owner 原例：吉隆坡 02:00–18:00。UTC 03:00 = 当地 11:00 放行；
        UTC 14:00 = 当地 22:00 拒绝。"""
        mandate = make_mandate(run_window=RunWindow(timezone=KL, start_hour=2, end_hour=18))
        assert_run_authorized(
            mandate,
            organization_id=ORG,
            profile_external_id="profile-A",
            runs_today=0,
            now=datetime(2026, 8, 28, 3, 0, tzinfo=UTC),
        )
        with pytest.raises(MandateViolation) as exc:
            assert_run_authorized(
                mandate,
                organization_id=ORG,
                profile_external_id="profile-A",
                runs_today=0,
                now=datetime(2026, 8, 28, 14, 0, tzinfo=UTC),
            )
        assert exc.value.code == "OUTSIDE_RUN_WINDOW"

    def test_cross_midnight_window_allows_early_morning_run(self) -> None:
        mandate = make_mandate(run_window=RunWindow(timezone=KL, start_hour=22, end_hour=6))
        assert_run_authorized(
            mandate,
            organization_id=ORG,
            profile_external_id="profile-A",
            runs_today=0,
            now=at_kl(1),
        )

    def test_all_day_window_never_blocks(self) -> None:
        mandate = make_mandate(run_window=RunWindow(timezone=KL, start_hour=0, end_hour=0))
        for hour in (0, 6, 13, 23):
            assert_run_authorized(
                mandate,
                organization_id=ORG,
                profile_external_id="profile-A",
                runs_today=0,
                now=at_kl(hour),
            )

    def test_budget_is_reported_before_window(self) -> None:
        """顺序断言（D10）：配额耗尽时说"02:00 再来"是误导，先报配额。"""
        mandate = make_mandate(
            max_runs_per_day=1, run_window=RunWindow(timezone=KL, start_hour=2, end_hour=18)
        )
        with pytest.raises(MandateViolation) as exc:
            assert_run_authorized(
                mandate,
                organization_id=ORG,
                profile_external_id="profile-A",
                runs_today=1,
                now=at_kl(22),
            )
        assert exc.value.code == "RUN_BUDGET_EXCEEDED"

    def test_interval_is_reported_before_window(self) -> None:
        mandate = make_mandate(run_window=RunWindow(timezone=KL, start_hour=2, end_hour=18))
        with pytest.raises(MandateViolation) as exc:
            assert_run_authorized(
                mandate,
                organization_id=ORG,
                profile_external_id="profile-A",
                runs_today=0,
                now=at_kl(22),
                last_run_at=at_kl(21),
            )
        assert exc.value.code == "RUN_TOO_SOON"

    def test_expiry_is_reported_before_window(self) -> None:
        mandate = make_mandate(
            valid_days=1, run_window=RunWindow(timezone=KL, start_hour=2, end_hour=18)
        )
        with pytest.raises(MandateViolation) as exc:
            assert_run_authorized(
                mandate,
                organization_id=ORG,
                profile_external_id="profile-A",
                runs_today=0,
                now=NOW + timedelta(days=3),
            )
        assert exc.value.code == "MANDATE_EXPIRED"

    def test_window_gate_rejects_naive_moment_when_reached(self) -> None:
        """窗口闸自身对 naive 时刻 fail-closed（不猜时区）。

        本条只管 RunWindow 这一层。闸链整体对 naive now 的带码拒绝由
        assert_run_authorized 最前端负责（V3 审查补齐，见
        tests/unit/test_v3_security_anchors.py 的
        test_naive_now_is_a_coded_rejection_not_a_bare_typeerror）——
        在那之前，更早的 expires_at 比较会先抛裸 TypeError。
        """
        mandate = make_mandate(run_window=RunWindow(timezone=KL, start_hour=2, end_hour=18))
        assert mandate.run_window is not None
        with pytest.raises(MandateViolation) as exc:
            mandate.run_window.is_open_at(datetime(2026, 8, 28, 11, 0))
        assert exc.value.code == "NAIVE_DATETIME_REJECTED"


# ------------------------------------------------------------------ 窗口 × 频次 相容性


class TestWindowIntervalCompatibility:
    def test_drifting_interval_with_window_rejected_at_issuance(self) -> None:
        """100 分钟 + 固定时段 = 运行时刻逐日漂移，几天后永远进不了窗口。"""
        with pytest.raises(MandateViolation) as exc:
            make_mandate(
                run_interval_minutes=100,
                run_window=RunWindow(timezone=KL, start_hour=2, end_hour=18),
            )
        assert exc.value.code == "RUN_WINDOW_INCOMPATIBLE"

    def test_drifting_interval_without_window_is_fine(self) -> None:
        mandate = make_mandate(run_interval_minutes=100)
        assert mandate.bounds.run_interval_minutes == 100

    def test_drifting_interval_with_all_day_window_is_fine(self) -> None:
        mandate = make_mandate(
            run_interval_minutes=100,
            run_window=RunWindow(timezone=KL, start_hour=0, end_hour=0),
        )
        assert mandate.run_window is not None and mandate.run_window.is_all_day

    @pytest.mark.parametrize("interval", [60, 360, 720, 1440, 4320, 10080])
    def test_ui_offered_intervals_all_compatible(self, interval: int) -> None:
        """UI 频次下拉提供的六个值都必须能配限定时段，否则界面会造出必拒组合。"""
        assert_window_interval_compatible(
            RunWindow(timezone=KL, start_hour=2, end_hour=18),
            bounds(run_interval_minutes=interval),
        )

    def test_none_window_short_circuits(self) -> None:
        assert_window_interval_compatible(None, bounds(run_interval_minutes=100))
