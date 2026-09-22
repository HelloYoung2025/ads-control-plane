"""一个只读工具的回环 MCP 组件：SFW → http://127.0.0.1:8790/mcp → find_wasted_search_terms。

- **只有一个工具、没有参数**。店铺、门槛、导出目录全部来自配置文件；模型没有机会编一个
  profile_id 进来（工具参数不含身份）。
- **Bearer 校验是一层纯 ASGI 中间件**，不 import starlette，也不用 SDK 的 AuthSettings /
  TokenVerifier——那条路要 issuer_url 占位符并挂一套 OAuth 元数据端点，本组件只在回环上
  服务一个客户端，一个口令就够。口令每次请求都从配置文件现读：配置换了口令不用重启，
  而口令读不到时**一律 401**（fail closed）。只读 sfw_bearer 这一个字段而不是整份配置：
  店铺表配坏了，请求仍能进到工具，工具再用完整校验把那句「配置错误：…」说给人听——
  否则人听到的只有「工具没连上」，管理员要猜。
- **配置错误不让进程退出**（施工计划 §8 攻击 13）：launchd 会不停重启一个启动即死的进程，
  而模型面对一个不存在的工具只会即兴发挥。进程照常常驻，工具固定返回
  ToolError("配置错误：<一句中文>，找管理员")——mcp 2.1.1 会在前面加一段
  「Error executing tool find_wasted_search_terms: 」（2026-09-19 实测），我们那句话原样跟在后面。
- 工具函数是**同步**的：SDK 把它放进工作线程（mcp/server/mcpserver/utilities/func_metadata.py），
  领星客户端里的 asyncio.run 不会撞上服务端的事件循环。
"""

from __future__ import annotations

import hmac
import logging
import sys
import threading
import tomllib
from collections.abc import Awaitable, Callable, MutableMapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ads_control_plane.sfw.config import ConfigError, PackConfig, check_private_file, load_config
from ads_control_plane.sfw.service import build_source, run_all, summarize
from ads_control_plane.strategies.ports import SearchTermReadPort

logger = logging.getLogger("ads_control_plane.sfw")

SERVER_NAME = "amazon-ads"
#: 第二次调用最多在锁上等多久；超了就用一句人话打发，不让它静默等到工具超时。
LOCK_WAIT_SECONDS = 30.0

TOOL_NAME = "find_wasted_search_terms"

#: 模型纪律。同一份文字放三处：工具 description（tools/list 必达）、MCPServer instructions
#: （initialize 字段）、项目里的 AGENTS.md（assets/AGENTS.md，测试钉着逐字相同）。
DISCIPLINE = "\n".join(
    [
        "你在 SFW 里帮一个不懂技术的人找「花了钱却没出单」的搜索词。规矩：",
        "1. 收到「找出所有店铺里花了钱却没出单的搜索词，做成否定词表。」这句话，"
        "只调用一次 find_wasted_search_terms，不带参数，一轮对话只调一次。",
        "2. 不运行任何命令，不读、不写、不改任何文件，不自己算门槛，不重试。",
        "3. 回答只用工具返回的文字，原样转述，链接照抄，不增不减，不解释门槛怎么来的。",
        "4. 工具不存在或调用失败时，只回一句「工具没连上，找管理员」；"
        "但报错里带「配置错误：」时，把那句话原样念出来。",
        "5. 永远不说「已批准」「已生效」「已上传」「会定时跑」「每天自动」。"
        "否定词只在人把 CSV 交给领星之后才生效，那一步不是你做的。",
    ]
)
INSTRUCTIONS = DISCIPLINE
TOOL_DESCRIPTION = DISCIPLINE

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_UNAUTHORIZED_BODY = b'{"error":"unauthorized"}'


def _utc_now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ Bearer


class BearerMiddleware:
    """纯 ASGI：只放行 `Authorization: Bearer <secret>`，缺失 / 不符 / 口令读不到 → 401 JSON。

    secret_source 每次请求调一次，返回 None 表示「现在没有口令可比」——那就谁也不放行。
    比较用 hmac.compare_digest：口令是 SFW 与本组件之间唯一的凭据，不给计时侧信道留缝。
    lifespan 原样透传（SDK 的会话管理器靠它启动）；其余非 http 的 scope 直接关掉。
    """

    def __init__(self, app: ASGIApp, secret_source: Callable[[], str | None]) -> None:
        self.app = app
        self._secret_source = secret_source

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            # 本组件没有 WebSocket 面；不认识的 scope 一律关掉，不往里放。
            await send({"type": "websocket.close", "code": 1008})
            return
        if not self._authorized(scope):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(_UNAUTHORIZED_BODY)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": _UNAUTHORIZED_BODY})
            return
        await self.app(scope, receive, send)

    def _authorized(self, scope: Scope) -> bool:
        secret = self._secret_source()
        if not secret:
            return False
        header = next((v for k, v in scope.get("headers", ()) if k == b"authorization"), b"")
        scheme, _, token = header.partition(b" ")
        if scheme.lower() != b"bearer":  # RFC 6750：scheme 大小写不敏感；口令本身逐字节比
            logger.warning(
                "拒绝了一个不带 Bearer 口令的请求（%s %s）", scope.get("method"), scope.get("path")
            )
            return False
        if not hmac.compare_digest(token.strip(), secret.encode("utf-8")):
            logger.warning(
                "拒绝了一个口令不符的请求：SFW 登记的 secret 与 config.toml 的 sfw_bearer 不一致"
            )
            return False
        return True


def _bearer_of(config_path: Path, *, expect_uid: int | None) -> str | None:
    """从配置文件里只读 sfw_bearer。文件不私有、不存在、不是 TOML、字段空着 → None（全部 401）。"""
    try:
        check_private_file(config_path, expect_uid=expect_uid)
        document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (ConfigError, OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        logger.warning("读不到 sfw_bearer，所有请求按未授权处理：%s", exc)
        return None
    bearer = document.get("sfw_bearer")
    if not isinstance(bearer, str) or not bearer:
        logger.warning("配置文件 %s 里没有 sfw_bearer，所有请求按未授权处理", config_path)
        return None
    return bearer


# ------------------------------------------------------------------ MCP 服务


class _SourceHolder:
    """跨调用复用数据源：取数缓存住在源实例里，源换了缓存就没了。

    配置每次调用都重读（改了店铺表不用重启）；配置一变就重建数据源——旧绑定表不能再用。
    """

    def __init__(self, factory: Callable[[PackConfig], SearchTermReadPort]) -> None:
        self._factory = factory
        self._built_for: PackConfig | None = None
        self._source: SearchTermReadPort | None = None

    def for_config(self, cfg: PackConfig) -> SearchTermReadPort:
        if self._source is None or cfg != self._built_for:
            self._source = self._factory(cfg)
            self._built_for = cfg
        return self._source


def build_server(
    config_path: Path,
    *,
    expect_uid: int | None,
    now_fn: Callable[[], datetime] = _utc_now,
    source_factory: Callable[[PackConfig], SearchTermReadPort] | None = None,
) -> MCPServer[Any]:
    """唯一的工具。ConfigError → ToolError("配置错误：…，找管理员")；其余带码错误逐店进文本。"""
    server: MCPServer[Any] = MCPServer(name=SERVER_NAME, instructions=INSTRUCTIONS)
    sources = _SourceHolder(source_factory if source_factory is not None else build_source)
    # 同步工具函数由 SDK 放进工作线程跑：两个对话同时敲 /fd 就是两个线程。一把锁让第二个
    # 等第一个跑完再拿缓存，而不是两边并发打领星（QPS=1）、各建一个数据源。
    run_lock = threading.Lock()

    @server.tool(name=TOOL_NAME, description=TOOL_DESCRIPTION)
    def find_wasted_search_terms() -> str:
        if not run_lock.acquire(timeout=LOCK_WAIT_SECONDS):
            return "上一次查询还在跑，等它出结果；出来之后敲 /new 回车，再敲 /fd 回车回车。"
        try:
            try:
                cfg = load_config(config_path, expect_uid=expect_uid)
            except ConfigError as exc:
                raise ToolError(f"配置错误：{exc}，找管理员") from exc
            # 不走 service.run_once：它每次现建数据源，而这里要跨调用复用（取数缓存住在源实例里）。
            runs = run_all(cfg, sources.for_config(cfg), now=now_fn())
            logger.info(
                "find_wasted_search_terms：%s",
                "，".join(f"{run.store.nickname}={run.outcome.value}" for run in runs),
            )
            return summarize(runs, cfg)
        finally:
            run_lock.release()

    return server


def build_app(
    config_path: Path,
    *,
    expect_uid: int | None,
    no_auth: bool = False,
    now_fn: Callable[[], datetime] = _utc_now,
    source_factory: Callable[[PackConfig], SearchTermReadPort] | None = None,
) -> ASGIApp:
    """streamable_http_app(host="127.0.0.1") 外包一层 BearerMiddleware；no_auth 时不包。"""
    server = build_server(
        config_path, expect_uid=expect_uid, now_fn=now_fn, source_factory=source_factory
    )
    app: ASGIApp = server.streamable_http_app(host="127.0.0.1")
    if no_auth:
        logger.warning(
            "NO AUTH：--no-auth 已开启，本机任何进程都能调用 %s，只准用于排障，排完就关",
            TOOL_NAME,
        )
        return app
    return BearerMiddleware(app, lambda: _bearer_of(config_path, expect_uid=expect_uid))


def serve(config_path: Path, *, port: int, no_auth: bool, expect_uid: int | None) -> None:
    """常驻回环。配置错误不退出：工具那里报错，进程留着（launchd KeepAlive 不会空转重启）。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    app = build_app(config_path, expect_uid=expect_uid, no_auth=no_auth)
    logger.info("amazon-ads 在 http://127.0.0.1:%d/mcp 待命；配置 %s", port, config_path)
    uvicorn.run(app, host="127.0.0.1", port=port, log_config=None)
