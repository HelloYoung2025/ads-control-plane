"""任务层（DEC-120/121）测试：控制权互斥、任务生命周期、中途介入指令。"""

from datetime import UTC, datetime, timedelta

import pytest

from ads_control_plane.authorization.custody import (
    DEFAULT_HUMAN_PRIORITY_COOLDOWN,
    CustodyState,
    CustodyViolation,
    ObjectCustody,
)
from ads_control_plane.canonical.ids import new_canonical_id
from ads_control_plane.canonical.money import Money
from ads_control_plane.identity.actor import (
    ActorContext,
    AuthenticationStrength,
    PrincipalType,
    Role,
)
from ads_control_plane.tasks.directive import (
    AdjustmentIntent,
    AdjustmentKind,
    AffectedObject,
    DirectiveError,
    ObjectLevel,
    ObjectSelector,
    build_preview,
)
from ads_control_plane.tasks.engagement import (
    DiagnosisReport,
    EngagementError,
    EngagementKind,
    EngagementState,
    ObjectDiagnosis,
    PlannedAction,
    Recommendation,
    TaskEngagement,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
ORG = new_canonical_id()
ENG_ID = new_canonical_id()


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
        roles=frozenset({Role.APPROVER, Role.ANALYST}),
        client_id="c-1",
        session_id="s-1",
        authentication_strength=AuthenticationStrength.MFA,
        issued_at=real_now,
        expires_at=real_now + timedelta(hours=8),
        **extra,
    )


def custody(**update) -> ObjectCustody:
    base = ObjectCustody(
        organization_id=ORG, profile_external_id="profile-A", object_key="campaign:c-1"
    )
    return base.model_copy(update=update) if update else base


class TestCustody:
    def test_unmanaged_blocks_ai(self) -> None:
        with pytest.raises(CustodyViolation) as e:
            custody().assert_ai_may_act(ENG_ID, NOW)
        assert e.value.code == "OBJECT_NOT_CLAIMED"

    def test_human_claims_then_ai_may_act(self) -> None:
        claimed = custody().claim_for_ai(actor(PrincipalType.HUMAN), ENG_ID, NOW)
        assert claimed.state is CustodyState.AI_MANAGED
        claimed.assert_ai_may_act(ENG_ID, NOW)  # 不抛

    def test_ai_cannot_claim(self) -> None:
        with pytest.raises(CustodyViolation) as e:
            custody().claim_for_ai(actor(PrincipalType.AI_CLIENT), ENG_ID, NOW)
        assert e.value.code == "HUMAN_REQUIRED"

    def test_other_engagement_blocked(self) -> None:
        claimed = custody().claim_for_ai(actor(PrincipalType.HUMAN), ENG_ID, NOW)
        with pytest.raises(CustodyViolation) as e:
            claimed.assert_ai_may_act(new_canonical_id(), NOW)
        assert e.value.code == "OBJECT_CLAIMED_ELSEWHERE"

    def test_external_human_change_backs_off(self) -> None:
        claimed = custody().claim_for_ai(actor(PrincipalType.HUMAN), ENG_ID, NOW)
        yielded = claimed.note_external_human_change(NOW, "budget changed in Lingxing UI")
        assert yielded.state is CustodyState.HUMAN_PRIORITY
        assert yielded.human_priority_until == NOW + DEFAULT_HUMAN_PRIORITY_COOLDOWN
        with pytest.raises(CustodyViolation) as e:
            yielded.assert_ai_may_act(ENG_ID, NOW + timedelta(hours=1))
        assert e.value.code == "OBJECT_HUMAN_PRIORITY"

    def test_cooldown_expiry_still_requires_reclaim(self) -> None:
        yielded = custody().note_external_human_change(NOW, "manual edit")
        after = NOW + DEFAULT_HUMAN_PRIORITY_COOLDOWN + timedelta(minutes=1)
        with pytest.raises(CustodyViolation) as e:
            yielded.assert_ai_may_act(ENG_ID, after)
        assert e.value.code == "OBJECT_NOT_CLAIMED"
        reclaimed = yielded.claim_for_ai(actor(PrincipalType.HUMAN), ENG_ID, after)
        reclaimed.assert_ai_may_act(ENG_ID, after)

    def test_human_takeover_has_no_expiry(self) -> None:
        claimed = custody().claim_for_ai(actor(PrincipalType.HUMAN), ENG_ID, NOW)
        taken = claimed.human_take_over(actor(PrincipalType.HUMAN))
        assert taken.human_priority_until is None
        with pytest.raises(CustodyViolation) as e:
            taken.assert_ai_may_act(ENG_ID, NOW + timedelta(days=365))
        assert e.value.code == "OBJECT_HUMAN_PRIORITY"

    def test_tool_managed_wins_over_claim(self) -> None:
        frozen = custody().note_tool_managed("TimingTactics")
        with pytest.raises(CustodyViolation) as e:
            frozen.claim_for_ai(actor(PrincipalType.HUMAN), ENG_ID, NOW)
        assert e.value.code == "OBJECT_TOOL_MANAGED"


def diagnosis_entry(key: str, rec: Recommendation) -> ObjectDiagnosis:
    return ObjectDiagnosis(
        object_key=key,
        display_name=f"HX02 {key}",
        window_spend=Money(amount="120.00", currency="USD"),
        window_sales=Money(amount="300.00", currency="USD"),
        clicks=200,
        orders=9,
        acos="0.40",
        lingxing_strategy=None,
        recommendation=rec,
        evidence="ACOS 40% over 30d window with 9 orders",
    )


def report(entries: tuple[ObjectDiagnosis, ...], eng_id=ENG_ID) -> DiagnosisReport:
    return DiagnosisReport(
        report_id=new_canonical_id(),
        engagement_id=eng_id,
        window_days=30,
        source_as_of=NOW - timedelta(hours=6),
        generated_at=NOW,
        entries=entries,
        objects_scanned=len(entries),
    )


def engagement() -> TaskEngagement:
    return TaskEngagement(
        engagement_id=ENG_ID,
        organization_id=ORG,
        profile_external_id="profile-A",
        kind=EngagementKind.INITIALIZE,
        focus="HX02",
        created_by_person_id="boss",
        created_at=NOW,
    )


class TestEngagement:
    def test_lifecycle_draft_to_running(self) -> None:
        rep = report((diagnosis_entry("campaign:c-1", Recommendation.PAUSE),))
        eng = engagement().attach_diagnosis(rep)
        assert eng.state is EngagementState.DIAGNOSED
        planned = eng.approve_plan(
            actor(PrincipalType.HUMAN),
            rep,
            (PlannedAction(object_key="campaign:c-1", recommendation=Recommendation.PAUSE),),
        )
        assert planned.state is EngagementState.PLANNED
        running = planned.start_running(mandate_id=new_canonical_id())
        assert running.state is EngagementState.RUNNING

    def test_plan_must_cite_report(self) -> None:
        rep = report((diagnosis_entry("campaign:c-1", Recommendation.PAUSE),))
        eng = engagement().attach_diagnosis(rep)
        with pytest.raises(EngagementError) as e:
            eng.approve_plan(
                actor(PrincipalType.HUMAN),
                rep,
                (
                    PlannedAction(
                        object_key="campaign:c-1",
                        recommendation=Recommendation.ADJUST_BUDGET,  # 报告没建议这个
                    ),
                ),
            )
        assert e.value.code == "ACTION_NOT_RECOMMENDED"

    def test_ai_cannot_approve_plan(self) -> None:
        rep = report((diagnosis_entry("campaign:c-1", Recommendation.KEEP),))
        eng = engagement().attach_diagnosis(rep)
        with pytest.raises(EngagementError) as e:
            eng.approve_plan(
                actor(PrincipalType.AI_CLIENT),
                rep,
                (PlannedAction(object_key="campaign:c-1", recommendation=Recommendation.KEEP),),
            )
        assert e.value.code == "HUMAN_REQUIRED"

    def test_foreign_report_rejected(self) -> None:
        foreign = report(
            (diagnosis_entry("campaign:c-1", Recommendation.PAUSE),), eng_id=new_canonical_id()
        )
        with pytest.raises(EngagementError) as e:
            engagement().attach_diagnosis(foreign)
        assert e.value.code == "REPORT_MISMATCH"

    def test_evidence_required(self) -> None:
        with pytest.raises(ValueError):
            ObjectDiagnosis(
                object_key="campaign:c-1",
                display_name="x",
                window_spend=Money(amount="1.00", currency="USD"),
                window_sales=Money(amount="0.00", currency="USD"),
                clicks=1,
                orders=0,
                acos=None,
                lingxing_strategy=None,
                recommendation=Recommendation.PAUSE,
                evidence="  ",
            )


class FakeExpansion:
    def __init__(self, rows: list[AffectedObject]) -> None:
        self._rows = rows

    def expand(self, profile_external_id: str, selector: ObjectSelector) -> list[AffectedObject]:
        return list(self._rows)


def row(key: str) -> AffectedObject:
    return AffectedObject(
        object_key=key, display_name=key, current_value="25.00", new_value="20.00"
    )


class TestDirective:
    def test_selector_requires_ids_or_filter(self) -> None:
        with pytest.raises(ValueError):
            ObjectSelector(level=ObjectLevel.CAMPAIGN)

    def test_selector_ids_and_filters_exclusive(self) -> None:
        with pytest.raises(ValueError):
            ObjectSelector(level=ObjectLevel.CAMPAIGN, external_ids=("c-1",), name_contains="HX02")

    def test_intent_whitelist(self) -> None:
        with pytest.raises(ValueError):
            AdjustmentIntent(kind=AdjustmentKind.PAUSE, value="10", reason="stop bleeding")
        with pytest.raises(ValueError):
            AdjustmentIntent(kind=AdjustmentKind.SCALE_BID, percent=80, reason="too big")
        with pytest.raises(ValueError):
            AdjustmentIntent(kind=AdjustmentKind.SET_BID, value="-1", reason="negative")
        AdjustmentIntent(kind=AdjustmentKind.SCALE_DAILY_BUDGET, percent=-20, reason="cut")

    def test_preview_expands_objects(self) -> None:
        preview = build_preview(
            directive_id=new_canonical_id(),
            engagement_id=ENG_ID,
            profile_external_id="profile-A",
            selector=ObjectSelector(level=ObjectLevel.CAMPAIGN, name_contains="HX02"),
            intent=AdjustmentIntent(
                kind=AdjustmentKind.SET_DAILY_BUDGET, value="20.00", reason="cap spend"
            ),
            port=FakeExpansion([row("campaign:c-1"), row("campaign:c-2")]),
            now=NOW,
        )
        assert len(preview.affected) == 2
        assert preview.affected[0].current_value == "25.00"

    def test_empty_match_is_explicit_error(self) -> None:
        with pytest.raises(DirectiveError) as e:
            build_preview(
                directive_id=new_canonical_id(),
                engagement_id=None,
                profile_external_id="profile-A",
                selector=ObjectSelector(level=ObjectLevel.CAMPAIGN, name_contains="nope"),
                intent=AdjustmentIntent(kind=AdjustmentKind.PAUSE, reason="x"),
                port=FakeExpansion([]),
                now=NOW,
            )
        assert e.value.code == "SELECTOR_MATCHED_NOTHING"

    def test_too_broad_is_explicit_error(self) -> None:
        rows = [row(f"campaign:c-{i}") for i in range(201)]
        with pytest.raises(DirectiveError) as e:
            build_preview(
                directive_id=new_canonical_id(),
                engagement_id=None,
                profile_external_id="profile-A",
                selector=ObjectSelector(level=ObjectLevel.CAMPAIGN, name_contains="c"),
                intent=AdjustmentIntent(kind=AdjustmentKind.PAUSE, reason="mass pause"),
                port=FakeExpansion(rows),
                now=NOW,
            )
        assert e.value.code == "SELECTOR_TOO_BROAD"
