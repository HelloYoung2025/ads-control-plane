"""Internal MCP 工具面测试（AX-02 / AX-05 / AX-16 在接口层的落点）。"""

from datetime import UTC, datetime, timedelta

from ads_control_plane.api.mcp_tools.server import (
    InMemoryActorTokenVerifier,
    build_internal_mcp,
)
from ads_control_plane.api.mcp_tools.service import ReadToolService
from ads_control_plane.authorization.model import Action, ClientType, Environment, Grant
from ads_control_plane.canonical.ids import new_canonical_id
from ads_control_plane.identity.actor import (
    ActorContext,
    AuthenticationStrength,
    PrincipalType,
    Role,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
ORG = new_canonical_id()
OTHER_ORG = new_canonical_id()


def ai_actor(org=ORG, expired: bool = False) -> ActorContext:
    # verifier 内部用真实时钟判过期，因此这里以真实当前时间为基准
    real_now = datetime.now(UTC)
    return ActorContext(
        principal_id=new_canonical_id(),
        principal_type=PrincipalType.AI_CLIENT,
        organization_id=org,
        roles=frozenset({Role.ANALYST}),
        human_initiator_person_id="alice",
        client_id="codex-1",
        session_id="s-1",
        authentication_strength=AuthenticationStrength.MFA,
        issued_at=real_now - timedelta(hours=2),
        expires_at=(real_now - timedelta(hours=1)) if expired else (real_now + timedelta(hours=8)),
    )


def read_grant(org=ORG, profiles: frozenset[str] | None = frozenset({"profile-A"})) -> Grant:
    return Grant(
        grant_id=new_canonical_id(),
        organization_id=org,
        environments=frozenset({Environment.STAGING}),
        actions=frozenset({Action.RESOURCE_READ}),
        client_types=frozenset({ClientType.MCP_AI}),
        profile_external_ids=profiles,
    )


def make_service(grants: list[Grant]) -> ReadToolService:
    return ReadToolService(environment=Environment.STAGING, grants=grants, denies=[])


class TestReadToolService:
    def test_whoami_reflects_server_side_identity_only(self) -> None:
        actor = ai_actor()
        result = make_service([read_grant()]).whoami(actor)
        assert result["principal_type"] == "AI_CLIENT"
        assert result["roles"] == ["ANALYST"]

    def test_scopes_show_only_own_org_grants(self) -> None:
        # 本组织 profile-A + 他组织 profile-B：后者不可见也不可探测
        service = make_service(
            [read_grant(), read_grant(org=OTHER_ORG, profiles=frozenset({"profile-B"}))]
        )
        result = service.list_authorized_scopes(ai_actor())
        assert result["profile_external_ids"] == ["profile-A"]

    def test_no_grant_yields_empty_scopes_not_error(self) -> None:
        # 自我描述工具：无任何范围的主体得到空列表（授权本身就是它展示的内容）
        service = make_service([])
        assert service.list_authorized_scopes(ai_actor())["profile_external_ids"] == []


class TestTokenVerifier:
    async def test_valid_token_maps_to_actor(self) -> None:
        verifier = InMemoryActorTokenVerifier()
        actor = ai_actor()
        verifier.register("tok-1", actor)
        access = await verifier.verify_token("tok-1")
        assert access is not None
        assert access.subject == str(actor.principal_id)

    async def test_unknown_and_expired_tokens_rejected(self) -> None:
        verifier = InMemoryActorTokenVerifier()
        verifier.register("tok-expired", ai_actor(expired=True))
        assert await verifier.verify_token("tok-unknown") is None
        assert await verifier.verify_token("tok-expired") is None


class TestServerSurface:
    async def test_only_read_and_draft_tools_exposed(self) -> None:
        server = build_internal_mcp(make_service([read_grant()]), InMemoryActorTokenVerifier())
        tools = {tool.name for tool in await server.list_tools()}
        assert tools == {"whoami", "list_authorized_scopes"}
        # 一般 AI 会话永不暴露 execute/submit/admin 类工具（AX-05 / §10.3F）
        forbidden = {"execute", "submit_proposal", "decide_approval", "kill", "credential"}
        assert not any(any(f in t for f in forbidden) for t in tools)
