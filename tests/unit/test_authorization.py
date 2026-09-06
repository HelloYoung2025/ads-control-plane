"""AX-02/03/04/05/08 公理测试。

覆盖红队场景：RT-01（跨 Profile 越权）、RT-03（宽 key 被低权限借用）、
AUTH-01（Viewer 请求写）、AUTH-07（自批）、
Grant 拼接（Profile A 读 + Profile B 写 ≠ Profile A 写）。
"""

from datetime import UTC, datetime, timedelta

import pytest

from ads_control_plane.authorization.engine import LayerStatus, evaluate
from ads_control_plane.authorization.model import (
    AccessRequest,
    Action,
    ClientType,
    Environment,
    ExplicitDeny,
    Grant,
    ReasonCode,
)
from ads_control_plane.authorization.sod import SoDViolation, check_can_approve, check_can_submit
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

ORG = new_canonical_id()
CONN = new_canonical_id()
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def make_actor(
    *,
    principal_type: PrincipalType = PrincipalType.HUMAN,
    roles: frozenset[Role],
    person: str | None = "person-a",
    initiator: str | None = None,
    org=ORG,
) -> ActorContext:
    return ActorContext(
        principal_id=new_canonical_id(),
        principal_type=principal_type,
        organization_id=org,
        roles=roles,
        human_person_id=person if principal_type is PrincipalType.HUMAN else None,
        human_initiator_person_id=initiator,
        client_id="client-1",
        session_id="sess-1",
        authentication_strength=AuthenticationStrength.MFA,
        #: 会话有效期锚在**真实墙钟**上，不是这个文件里那个固定的 NOW。
        #  ActorContext.is_expired() 默认拿 datetime.now(UTC) 比（identity/actor.py），
        #  而注入的 clock 只管域层判定。钉在固定日历日上，这个令牌就是一颗定时炸弹：
        #  过了那一刻，全绿的测试会在某次与代码无关的运行里突然 401。
        #  2026-09-07 真的炸过一次：test_mandate_scope_api 那条在 20:00 UTC 从绿变红，
        #  而 diff 里一个相关改动都没有——排查花掉的时间远超写对它的成本。
        issued_at=datetime.now(UTC) - timedelta(minutes=5),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def entity(profile: str = "profile-A", entity_id: str = "t-1") -> CanonicalEntityRef:
    return CanonicalEntityRef(
        organization_id=ORG,
        provider=Provider.MOCK,
        provider_connection_id=CONN,
        marketplace="US",
        shop_external_id="shop-1",
        profile_external_id=profile,
        ad_product=AdProduct.SP,
        entity_type=EntityType.TARGET,
        entity_external_id=entity_id,
        parent_refs=ParentRefs(campaign_external_id="c-1", ad_group_external_id="ag-1"),
    )


def bid_update_request(profile: str = "profile-A", target: str = "0.83") -> AccessRequest:
    return AccessRequest(
        environment=Environment.STAGING,
        organization_id=ORG,
        action=Action.TARGET_BID_UPDATE,
        client_type=ClientType.WEB,
        entity=entity(profile),
        field="bid",
        target_value=Money(amount=target, currency="USD"),
        expected_before=Money(amount="0.82", currency="USD"),
    )


def write_grant(profile: str = "profile-A") -> Grant:
    return Grant(
        grant_id=new_canonical_id(),
        organization_id=ORG,
        environments=frozenset({Environment.STAGING}),
        actions=frozenset({Action.TARGET_BID_UPDATE}),
        client_types=frozenset({ClientType.WEB}),
        profile_external_ids=frozenset({profile}),
        entity_types=frozenset({EntityType.TARGET}),
        fields=frozenset({"bid"}),
        max_absolute_value=Money(amount="5.00", currency="USD"),
        max_single_delta=Money(amount="0.10", currency="USD"),
    )


def read_grant(profile: str) -> Grant:
    return Grant(
        grant_id=new_canonical_id(),
        organization_id=ORG,
        environments=frozenset({Environment.STAGING}),
        actions=frozenset({Action.RESOURCE_READ}),
        client_types=frozenset({ClientType.WEB}),
        profile_external_ids=frozenset({profile}),
    )


OPERATOR = make_actor(roles=frozenset({Role.OPERATOR}))


class TestEffectiveAllow:
    def test_happy_path_single_grant_matches_full_tuple(self) -> None:
        decision = evaluate(OPERATOR, bid_update_request(), [write_grant()], [], now=NOW)
        assert decision.allowed
        assert decision.matched_grant_id is not None

    def test_rt01_cross_profile_denied_without_provider_call(self) -> None:
        # Shop/Profile A 的授权不能写 Profile B 的对象
        decision = evaluate(
            OPERATOR,
            bid_update_request(profile="profile-B"),
            [write_grant("profile-A")],
            [],
            now=NOW,
        )
        assert not decision.allowed
        assert ReasonCode.NO_MATCHING_GRANT in decision.reason_codes

    def test_no_grant_stitching_read_a_plus_write_b(self) -> None:
        # AX-04：Profile A 的读 Grant + Profile B 的写 Grant 不能拼出 Profile A 写
        grants = [read_grant("profile-A"), write_grant("profile-B")]
        decision = evaluate(OPERATOR, bid_update_request(profile="profile-A"), grants, [], now=NOW)
        assert not decision.allowed
        assert ReasonCode.NO_MATCHING_GRANT in decision.reason_codes

    def test_auth01_viewer_cannot_write(self) -> None:
        viewer = make_actor(roles=frozenset({Role.VIEWER}))
        decision = evaluate(viewer, bid_update_request(), [write_grant()], [], now=NOW)
        assert not decision.allowed
        assert ReasonCode.ROLE_INSUFFICIENT in decision.reason_codes

    def test_explicit_deny_wins_over_matching_grant(self) -> None:
        deny = ExplicitDeny(
            deny_id=new_canonical_id(),
            organization_id=ORG,
            profile_external_ids=frozenset({"profile-A"}),
            reason="frozen during promo",
        )
        decision = evaluate(OPERATOR, bid_update_request(), [write_grant()], [deny], now=NOW)
        assert not decision.allowed
        assert ReasonCode.EXPLICIT_DENY in decision.reason_codes
        assert decision.matched_deny_id == deny.deny_id

    def test_expired_actor_denied(self) -> None:
        #: 这一条测的是**域层**过期规则，判定时刻是显式传进去的 now——所以它的
        #  会话有效期必须跟着同一个 NOW，而不是 make_actor 那个锚在墙钟上的窗口
        #  （那个窗口是为「令牌验证按真实时间走」准备的，见 make_actor 的注释）。
        #  在这里就地声明，两种时间各归各位。
        expiring = OPERATOR.model_copy(update={"expires_at": NOW + timedelta(hours=1)})
        decision = evaluate(
            expiring, bid_update_request(), [write_grant()], [], now=NOW + timedelta(hours=2)
        )
        assert not decision.allowed
        assert ReasonCode.ACTOR_EXPIRED in decision.reason_codes

    def test_cross_org_denied(self) -> None:
        other_org_actor = make_actor(roles=frozenset({Role.OPERATOR}), org=new_canonical_id())
        decision = evaluate(other_org_actor, bid_update_request(), [write_grant()], [], now=NOW)
        assert not decision.allowed
        assert ReasonCode.ORG_MISMATCH in decision.reason_codes

    def test_delta_exceeding_grant_bound_denied(self) -> None:
        decision = evaluate(
            OPERATOR, bid_update_request(target="1.83"), [write_grant()], [], now=NOW
        )
        assert not decision.allowed  # delta 1.01 > 0.10

    def test_unknown_runtime_layer_fails_closed(self) -> None:
        class UnknownLayer:
            def check(self, request: AccessRequest) -> tuple[LayerStatus, ReasonCode]:
                return LayerStatus.UNKNOWN, ReasonCode.AUDIT_UNAVAILABLE

        decision = evaluate(
            OPERATOR, bid_update_request(), [write_grant()], [], [UnknownLayer()], now=NOW
        )
        assert not decision.allowed
        assert ReasonCode.LAYER_UNKNOWN in decision.reason_codes

    def test_kill_switch_layer_denies(self) -> None:
        class KillLayer:
            def check(self, request: AccessRequest) -> tuple[LayerStatus, ReasonCode]:
                return LayerStatus.DENY, ReasonCode.KILL_SWITCH_ACTIVE

        decision = evaluate(
            OPERATOR, bid_update_request(), [write_grant()], [], [KillLayer()], now=NOW
        )
        assert not decision.allowed
        assert ReasonCode.KILL_SWITCH_ACTIVE in decision.reason_codes


class TestAIClientCeiling:
    def test_rt03_ai_client_cannot_reach_write_action_even_with_grant(self) -> None:
        ai = make_actor(
            principal_type=PrincipalType.AI_CLIENT,
            roles=frozenset({Role.OPERATOR}),  # 即使被错误授予 Operator 角色
            person=None,
            initiator="person-a",
        )
        request = AccessRequest(
            environment=Environment.STAGING,
            organization_id=ORG,
            action=Action.TARGET_BID_UPDATE,
            client_type=ClientType.MCP_AI,
            entity=entity(),
            field="bid",
            target_value=Money(amount="0.83", currency="USD"),
            expected_before=Money(amount="0.82", currency="USD"),
        )
        decision = evaluate(ai, request, [write_grant()], [], now=NOW)
        assert not decision.allowed
        assert ReasonCode.CLIENT_TYPE_FORBIDDEN in decision.reason_codes

    def test_ai_client_can_read_and_draft(self) -> None:
        ai = make_actor(
            principal_type=PrincipalType.AI_CLIENT,
            roles=frozenset({Role.ANALYST}),
            person=None,
            initiator="person-a",
        )
        request = AccessRequest(
            environment=Environment.STAGING,
            organization_id=ORG,
            action=Action.PROPOSAL_CREATE_DRAFT,
            client_type=ClientType.MCP_AI,
        )
        grant = Grant(
            grant_id=new_canonical_id(),
            organization_id=ORG,
            environments=frozenset({Environment.STAGING}),
            actions=frozenset({Action.PROPOSAL_CREATE_DRAFT}),
            client_types=frozenset({ClientType.MCP_AI}),
        )
        assert evaluate(ai, request, [grant], [], now=NOW).allowed


class TestSoD:
    def test_auth07_creator_cannot_approve_own_proposal(self) -> None:
        approver = make_actor(roles=frozenset({Role.APPROVER}), person="person-a")
        with pytest.raises(SoDViolation) as exc:
            check_can_approve(approver, "person-a", None)
        assert exc.value.code == "CREATOR_CANNOT_APPROVE"

    def test_different_person_can_approve(self) -> None:
        approver = make_actor(roles=frozenset({Role.APPROVER}), person="person-b")
        check_can_approve(approver, "person-a", "person-a")  # 不抛异常

    def test_ai_cannot_approve_even_if_granted(self) -> None:
        ai = make_actor(
            principal_type=PrincipalType.AI_CLIENT,
            roles=frozenset({Role.APPROVER}),
            person=None,
            initiator="person-a",
        )
        with pytest.raises(SoDViolation) as exc:
            check_can_approve(ai, "person-x", None)
        assert exc.value.code == "AI_CANNOT_APPROVE"

    def test_ai_cannot_submit_proposal(self) -> None:
        ai = make_actor(
            principal_type=PrincipalType.AI_CLIENT,
            roles=frozenset({Role.ANALYST}),
            person=None,
            initiator="person-a",
        )
        with pytest.raises(SoDViolation) as exc:
            check_can_submit(ai, "client-1")
        assert exc.value.code == "AI_CANNOT_SUBMIT"

    def test_same_person_via_two_logins_still_blocked(self) -> None:
        # 同一自然人换第二个登录账号（不同 principal_id）也不能自批
        approver_second_login = make_actor(roles=frozenset({Role.APPROVER}), person="person-a")
        with pytest.raises(SoDViolation):
            check_can_approve(approver_second_login, "person-a", None)
