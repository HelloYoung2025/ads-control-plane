"""域层（DEC-122/124/125）测试：勾选集、策略包状态机、退出守卫、任务勾选范围。"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ads_control_plane.canonical.ids import new_canonical_id
from ads_control_plane.identity.actor import (
    ActorContext,
    AuthenticationStrength,
    PrincipalType,
    Role,
)
from ads_control_plane.strategies.bundle import BundleError, BundleStatus, StrategyBundle
from ads_control_plane.strategies.exit_guard import ExitGuard, ExitPolicy, ExitVerdict
from ads_control_plane.tasks.directive import MAX_AFFECTED_OBJECTS, ObjectLevel
from ads_control_plane.tasks.engagement import EngagementError, EngagementKind, TaskEngagement
from ads_control_plane.tasks.selection import SelectedObject, SelectionError, SelectionSet

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
ORG = new_canonical_id()


def actor(principal_type: PrincipalType) -> ActorContext:
    real_now = datetime.now(UTC)
    extra = (
        {"human_person_id": "boss"}
        if principal_type is PrincipalType.HUMAN
        else {"human_initiator_person_id": "boss"}
    )
    return ActorContext(
        principal_id=new_canonical_id(),
        principal_type=principal_type,
        organization_id=ORG,
        roles=frozenset({Role.APPROVER, Role.OPERATOR}),
        client_id="c-1",
        session_id="s-1",
        authentication_strength=AuthenticationStrength.MFA,
        issued_at=real_now,
        expires_at=real_now + timedelta(hours=8),
        **extra,
    )


def selected(level: ObjectLevel, external_id: str, profile: str = "profile-A") -> SelectedObject:
    return SelectedObject(level=level, external_id=external_id, profile_external_id=profile)


def selection(*keys: tuple[ObjectLevel, str], profile: str = "profile-A") -> SelectionSet:
    return SelectionSet(items=tuple(selected(lv, ext, profile) for lv, ext in keys))


def bundle(**overrides: object) -> StrategyBundle:
    fields: dict[str, object] = {
        "bundle_id": new_canonical_id(),
        "name": "HX02 wasted-spend cleanup",
        "profile_external_id": "profile-A",
        "mandate_ids": ("mandate-1",),
        "selection": selection((ObjectLevel.CAMPAIGN, "c-1")),
        "exit_policy": ExitPolicy(max_cumulative_spend=Decimal("100.00")),
        "created_by": "boss",
        "created_at": NOW,
        "notes": "意图：仅工作日白天运行（注记，不做调度，DEC-119）",
    }
    fields.update(overrides)
    return StrategyBundle(**fields)  # type: ignore[arg-type]


class TestSelectionSet:
    def test_empty_selection_rejected(self) -> None:
        with pytest.raises(SelectionError) as e:
            SelectionSet(items=())
        assert e.value.code == "SELECTION_EMPTY"

    def test_mixed_profile_rejected(self) -> None:
        with pytest.raises(SelectionError) as e:
            SelectionSet(
                items=(
                    selected(ObjectLevel.CAMPAIGN, "c-1", "profile-A"),
                    selected(ObjectLevel.CAMPAIGN, "c-2", "profile-B"),
                )
            )
        assert e.value.code == "SELECTION_MIXED_PROFILE"

    def test_over_limit_rejected(self) -> None:
        items = tuple(
            selected(ObjectLevel.TARGET, f"t-{i}") for i in range(MAX_AFFECTED_OBJECTS + 1)
        )
        with pytest.raises(SelectionError) as e:
            SelectionSet(items=items)
        assert e.value.code == "SELECTION_TOO_BROAD"

    def test_duplicates_count_once_at_limit(self) -> None:
        # 400 个条目、去重后恰好 200 个对象：上限作用于去重后的对象数，允许。
        items = tuple(
            selected(ObjectLevel.TARGET, f"t-{i % MAX_AFFECTED_OBJECTS}") for i in range(400)
        )
        sel = SelectionSet(items=items)
        (only,) = sel.to_selectors()
        assert len(only.external_ids) == MAX_AFFECTED_OBJECTS

    def test_blank_ids_rejected(self) -> None:
        with pytest.raises(ValueError):
            SelectedObject(level=ObjectLevel.CAMPAIGN, external_id="  ", profile_external_id="p")
        with pytest.raises(ValueError):
            SelectedObject(level=ObjectLevel.CAMPAIGN, external_id="c-1", profile_external_id="")

    def test_to_selectors_groups_by_level(self) -> None:
        sel = selection(
            (ObjectLevel.CAMPAIGN, "c-1"),
            (ObjectLevel.TARGET, "kw-9"),  # keyword 并入 TARGET 层
            (ObjectLevel.CAMPAIGN, "c-2"),
            (ObjectLevel.CAMPAIGN, "c-1"),  # 重复勾选
            (ObjectLevel.AD_GROUP, "g-7"),
        )
        selectors = {s.level: s for s in sel.to_selectors()}
        assert set(selectors) == {ObjectLevel.CAMPAIGN, ObjectLevel.AD_GROUP, ObjectLevel.TARGET}
        assert selectors[ObjectLevel.CAMPAIGN].external_ids == ("c-1", "c-2")
        assert selectors[ObjectLevel.AD_GROUP].external_ids == ("g-7",)
        assert selectors[ObjectLevel.TARGET].external_ids == ("kw-9",)
        # 产物是显式 ID 型选择器：无任何筛选字段（满足 ObjectSelector 互斥合同）。
        assert all(s.name_contains is None for s in sel.to_selectors())

    def test_profile_accessor(self) -> None:
        assert selection((ObjectLevel.CAMPAIGN, "c-1")).profile_external_id == "profile-A"


class TestStrategyBundle:
    def test_requires_at_least_one_mandate(self) -> None:
        with pytest.raises(BundleError) as e:
            bundle(mandate_ids=())
        assert e.value.code == "BUNDLE_NO_MANDATES"

    def test_selection_profile_mismatch_rejected(self) -> None:
        with pytest.raises(BundleError) as e:
            bundle(selection=selection((ObjectLevel.CAMPAIGN, "c-9"), profile="profile-B"))
        assert e.value.code == "SCOPE_PROFILE_MISMATCH"

    def test_activate_by_human(self) -> None:
        active = bundle().activate(actor(PrincipalType.HUMAN))
        assert active.status is BundleStatus.ACTIVE

    def test_ai_cannot_activate(self) -> None:
        with pytest.raises(BundleError) as e:
            bundle().activate(actor(PrincipalType.AI_CLIENT))
        assert e.value.code == "HUMAN_REQUIRED"

    def test_suspend_records_reason_then_human_resumes(self) -> None:
        active = bundle().activate(actor(PrincipalType.HUMAN))
        suspended = active.suspend("STOP_LOSS_TRIGGERED: spend 100.00 >= cap 100.00")
        assert suspended.status is BundleStatus.SUSPENDED
        assert suspended.suspended_reason is not None
        assert "STOP_LOSS_TRIGGERED" in suspended.suspended_reason
        # SUSPENDED 后恢复必须是人；AI 不能自我复跑。
        with pytest.raises(BundleError) as e:
            suspended.activate(actor(PrincipalType.AI_CLIENT))
        assert e.value.code == "HUMAN_REQUIRED"
        resumed = suspended.activate(actor(PrincipalType.HUMAN))
        assert resumed.status is BundleStatus.ACTIVE
        assert resumed.suspended_reason is None

    def test_suspend_requires_active(self) -> None:
        with pytest.raises(BundleError) as e:
            bundle().suspend("nothing running")
        assert e.value.code == "BUNDLE_NOT_ACTIVE"

    def test_activate_when_already_active_rejected(self) -> None:
        active = bundle().activate(actor(PrincipalType.HUMAN))
        with pytest.raises(BundleError) as e:
            active.activate(actor(PrincipalType.HUMAN))
        assert e.value.code == "BUNDLE_NOT_ACTIVATABLE"

    def test_ai_cannot_close(self) -> None:
        with pytest.raises(BundleError) as e:
            bundle().close(actor(PrincipalType.AI_CLIENT))
        assert e.value.code == "HUMAN_REQUIRED"

    def test_close_from_any_state_and_closed_is_terminal(self) -> None:
        assert bundle().close(actor(PrincipalType.HUMAN)).status is BundleStatus.CLOSED
        suspended = bundle().activate(actor(PrincipalType.HUMAN)).suspend("stop loss")
        closed = suspended.close(actor(PrincipalType.HUMAN))
        assert closed.status is BundleStatus.CLOSED
        with pytest.raises(BundleError) as e:
            closed.close(actor(PrincipalType.HUMAN))
        assert e.value.code == "BUNDLE_CLOSED"
        with pytest.raises(BundleError) as e:
            closed.activate(actor(PrincipalType.HUMAN))
        assert e.value.code == "BUNDLE_NOT_ACTIVATABLE"
        with pytest.raises(BundleError) as e:
            closed.suspend("too late")
        assert e.value.code == "BUNDLE_NOT_ACTIVE"

    def test_blank_name_rejected(self) -> None:
        with pytest.raises(ValueError):
            bundle(name="   ")


class TestExitGuard:
    def test_all_none_policy_never_triggers(self) -> None:
        verdict = ExitGuard.evaluate(ExitPolicy(), Decimal("999999"), Decimal("999999"))
        assert verdict is ExitVerdict.NONE

    def test_stop_loss_boundary_inclusive(self) -> None:
        policy = ExitPolicy(max_cumulative_spend=Decimal("100.00"))
        assert ExitGuard.evaluate(policy, Decimal("99.99"), Decimal("0")) is ExitVerdict.NONE
        assert (
            ExitGuard.evaluate(policy, Decimal("100.00"), Decimal("0"))
            is ExitVerdict.STOP_LOSS_TRIGGERED
        )

    def test_target_boundary_inclusive(self) -> None:
        policy = ExitPolicy(target_wasted_spend_removed=Decimal("50"))
        assert ExitGuard.evaluate(policy, Decimal("0"), Decimal("49.99")) is ExitVerdict.NONE
        assert ExitGuard.evaluate(policy, Decimal("0"), Decimal("50")) is ExitVerdict.TARGET_REACHED

    def test_stop_loss_wins_when_both_hit(self) -> None:
        policy = ExitPolicy(
            max_cumulative_spend=Decimal("100"), target_wasted_spend_removed=Decimal("50")
        )
        assert (
            ExitGuard.evaluate(policy, Decimal("100"), Decimal("50"))
            is ExitVerdict.STOP_LOSS_TRIGGERED
        )

    def test_thresholds_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            ExitPolicy(max_cumulative_spend=Decimal("0"))
        with pytest.raises(ValueError):
            ExitPolicy(target_wasted_spend_removed=Decimal("-1"))


def engagement(scope: SelectionSet | None = None) -> TaskEngagement:
    return TaskEngagement(
        engagement_id=new_canonical_id(),
        organization_id=ORG,
        profile_external_id="profile-A",
        kind=EngagementKind.INITIALIZE,
        focus="HX02",
        created_by_person_id="boss",
        created_at=NOW,
        scope=scope,
    )


class TestEngagementScope:
    def test_scope_defaults_to_none(self) -> None:
        assert engagement().scope is None

    def test_scope_same_profile_accepted(self) -> None:
        eng = engagement(scope=selection((ObjectLevel.CAMPAIGN, "c-1")))
        assert eng.scope is not None
        assert eng.scope.profile_external_id == "profile-A"

    def test_scope_profile_mismatch_rejected(self) -> None:
        with pytest.raises(EngagementError) as e:
            engagement(scope=selection((ObjectLevel.CAMPAIGN, "c-1"), profile="profile-B"))
        assert e.value.code == "SCOPE_PROFILE_MISMATCH"
