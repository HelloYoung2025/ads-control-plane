"""Internal MCP 传输壳（mcp SDK >=2.1,<3；目标协议 2026-07-28）。

原则：
- 工具处理函数从已验证的 Bearer Token 解析 ActorContext（AX-02）；
  工具参数中的任何身份字段一律不存在/不接受。
- 一般 AI 会话工具面 = 只读 + 草案（§10.3）；无 execute/submit/admin 工具。
- Streamable HTTP 独立进程部署；session id 永不承载授权语义。

生产部署将 InMemoryActorTokenVerifier 换成公司 OIDC introspection；
本模块的组装逻辑保持不变。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ads_control_plane.api.mcp_tools.service import ReadToolService, ToolDenied
from ads_control_plane.api.mcp_tools.strategy_service import StrategyToolService
from ads_control_plane.identity.actor import ActorContext


def _coded(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """把服务层的带码拒绝翻译成 MCP 的"故意拒绝"，而不是让它当崩溃流出去。

    SDK 对两类异常的处理截然不同：ToolError 是"工具有意拒绝"，消息原样送到调用方；
    其他异常一律当崩溃，消息留在服务端、调用方只看到 "Error executing tool <name>"。
    ToolDenied 不继承 SDK 类型（service 层刻意协议无关），于是每一条拒绝——
    AUTH_SCOPE_DENIED / MANDATE_UNKNOWN / RUN_BUDGET_EXCEEDED / OUTSIDE_RUN_WINDOW……
    ——都走了崩溃那条路，码被吞掉。

    后果不是"少一句提示"：调用方分不清"这是策略拒绝"和"服务器坏了"，会把一条
    有意的拒绝当瞬时故障重试；同时每一次正常的边界拒绝都被记成一次崩溃，真崩溃
    的信号被淹掉。ToolDenied 的码本来就是设计成对外稳定、不泄露资源存在性的
    （AX-16），暴露它正是原意。翻译放在 MCP 壳层——service 层继续不认识 SDK。
    """
    try:
        return call()
    except ToolDenied as exc:
        # 码在前，可行动补充在后：调用方按前缀匹配码的老写法不受影响，而人（和 AI）
        # 终于能看到「哪个参数、允许范围是什么」，不必靠试。
        raise ToolError(f"{exc.code}: {exc.detail}" if exc.detail else exc.code) from exc


class InMemoryActorTokenVerifier(TokenVerifier):
    """MVP 验证器：不透明 token → 服务端 ActorContext 映射。

    token 由公司网关签发（audience-bound、短期、降权），此处只做查表；
    生产实现改为 OIDC token introspection / JWT 验签，接口不变。
    """

    def __init__(self) -> None:
        self._by_token: dict[str, ActorContext] = {}

    def register(self, token: str, actor: ActorContext) -> None:
        self._by_token[token] = actor

    def actor_for(self, token: str) -> ActorContext | None:
        actor = self._by_token.get(token)
        if actor is None or actor.is_expired():
            return None
        return actor

    async def verify_token(self, token: str) -> AccessToken | None:
        actor = self.actor_for(token)
        if actor is None:
            return None
        return AccessToken(
            token=token,
            client_id=actor.client_id,
            scopes=sorted(role.value for role in actor.roles),
            expires_at=int(actor.expires_at.timestamp()),
            subject=str(actor.principal_id),
        )


def build_internal_mcp(
    service: ReadToolService,
    verifier: InMemoryActorTokenVerifier,
    *,
    strategy: StrategyToolService | None = None,
    issuer_url: str = "https://auth.internal.example.invalid",
    resource_server_url: str = "https://ads-mcp.internal.example.invalid/mcp",
) -> MCPServer:
    """组装 Internal MCP。默认 URL 是不可路由占位符，生产部署必须显式传入。"""
    from mcp.server.auth.settings import AuthSettings

    server = MCPServer(
        name="ads-control-plane-internal",
        instructions=(
            "Company internal ads read/draft surface for Amazon advertising. All authorization "
            "is enforced server-side; identity claims inside tool arguments are ignored. "
            "This server never changes any ad: it drafts negative-keyword candidates that a "
            "human approves in the web UI and then applies by hand in the Lingxing console. "
            "Nothing here runs on a schedule — every run happens because a person asked you "
            "for one, so relay outcomes back to that person in their own language, including "
            "the ones where nothing was produced."
        ),
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=issuer_url,  # type: ignore[arg-type]
            resource_server_url=resource_server_url,  # type: ignore[arg-type]
        ),
    )

    def _current_actor() -> ActorContext:
        access = get_access_token()
        actor = verifier.actor_for(access.token) if access is not None else None
        if actor is None:
            raise ToolDenied("AUTHENTICATION_REQUIRED")
        return actor

    @server.tool(name="whoami", description="Return the server-side identity of the caller.")
    def whoami() -> dict[str, Any]:
        return _coded(lambda: service.whoami(_current_actor()))

    @server.tool(
        name="list_authorized_scopes",
        description="List resource scopes visible to the caller. Other scopes are not probeable.",
    )
    def list_authorized_scopes() -> dict[str, Any]:
        return _coded(lambda: service.list_authorized_scopes(_current_actor()))

    if strategy is not None:
        strategy_service = strategy

        @server.tool(
            name="generate_negation_candidate_set",
            description=(
                "Generate a frozen NEG_EXACT candidate set: search terms that spent money "
                "over the lookback window without a single attributed order. The set appears "
                "in the human web UI under 「待批」 (state FROZEN) and expires 72h after "
                "generation; only a human can approve it there, and approval is impossible "
                "through this surface.\n"
                "mandate_id: the full 36-character UUID of an ACTIVE mandate, from the mandate "
                "row in the web UI (its 「复制指令」 button copies a ready-made instruction). "
                "Under a mandate, pass no other parameters — they come from the signed contract, "
                "and the mandate's scope, daily quota and minimum interval all apply. Without "
                "mandate_id the run is ad-hoc: whitelist-bounded parameters, no quota, no "
                "interval, no scope — prefer a mandate whenever the human has one.\n"
                'min_spend_amount is a plain decimal string like "20.00" (no currency sign, '
                "no thousands separator); currency is a 3-letter code and must match the "
                "store's own currency; lookback_days 7-90; min_clicks >= 10; "
                "max_data_staleness_hours 1-72.\n"
                "A response with candidate_count 0 is NOT proof the account is clean: read "
                "`outcome` first. Even NO_CANDIDATES does not mean the account is clean — it "
                "means nothing cleared the thresholds echoed in `applied_parameters`; terms "
                "under min_spend_amount or min_clicks are dropped silently and appear nowhere "
                "in the response, and `abstain_count` / `unjudged_ad_group_terms` cover more "
                "spend this run could not judge. When you report a NO_CANDIDATES run, state "
                "the thresholds that were in force, not just the verdict. If "
                "`same_content_as` is non-empty, this exact batch is already waiting for the "
                "human; tell them to approve that one instead of generating again.\n"
                "`asin_abstain_count` > 0 means some search terms burned money with no orders "
                "but are ASINs, not keywords: the NEGATIVE_EXACT keywords this tool drafts "
                "cannot block them. Always relay those terms (they are listed first in "
                "`abstains`) and say the human must negate them as product targets in Lingxing "
                "「否定投放」. Approving the candidate set does not cover them. "
                "If `abstains_truncated_from` is not null, the `abstains` list is PARTIAL "
                "(it holds the first 50, ASINs first) — say so plainly instead of presenting "
                "it as the full list, and note that the terms beyond it cannot be retrieved "
                "from any tool on this surface; a mandate scoped to fewer campaigns evaluates "
                "fewer terms, so its list fits."
            ),
        )
        def generate_negation_candidate_set(
            profile_external_id: str,
            mandate_id: str | None = None,
            lookback_days: int | None = None,
            min_spend_amount: str | None = None,
            currency: str | None = None,
            min_clicks: int | None = None,
            max_data_staleness_hours: int | None = None,
        ) -> dict[str, Any]:
            return _coded(
                lambda: strategy_service.generate_negation_candidate_set(
                    _current_actor(),
                    profile_external_id=profile_external_id,
                    mandate_id=mandate_id,
                    lookback_days=lookback_days,
                    min_spend_amount=min_spend_amount,
                    currency=currency,
                    min_clicks=min_clicks,
                    max_data_staleness_hours=max_data_staleness_hours,
                )
            )

        @server.tool(
            name="list_negation_candidate_sets",
            description=(
                "List candidate sets of the caller's organization: state, counts, which store "
                "and mandate each came from, and whether a FROZEN one has passed its 72h "
                "deadline (expired ones can no longer be approved, only rejected and "
                "regenerated). Use this to answer 「有什么要批」 without generating anything."
            ),
        )
        def list_negation_candidate_sets() -> dict[str, Any]:
            return _coded(lambda: strategy_service.list_negation_candidate_sets(_current_actor()))

    return server
