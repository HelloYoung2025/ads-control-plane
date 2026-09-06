"""本地演示 /mcp 冒烟客户端——Codex 等外部 MCP 客户端接入前的自检工具。

先起服务：uv run python scripts/serve_local_demo.py --port 8788
再自检：  uv run python scripts/smoke_mcp_client.py [--port 8788] [--token demo-codex-token]

做三件事并打印实测结果：initialize（协议版本）、list_tools（工具名清单）、
call_tool("whoami")（服务端认定的身份 JSON）。全部通过则退出码 0。
LOCAL DEMO ONLY：token 为固定 demo token，全部 Mock，无任何真实凭据。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def smoke(url: str, token: str) -> None:
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
        streamable_http_client(url, http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        init_dump = init.model_dump(mode="json")
        # 2.1.x 实测协商 2025-11-25（由首请求形态决定），属预期，不是配置错误。
        print(f"protocol_version: {init_dump.get('protocol_version')}")

        tools = (await session.list_tools()).tools
        print(f"tools ({len(tools)}): {', '.join(sorted(t.name for t in tools))}")

        result = await session.call_tool("whoami", {})
        if result.is_error:
            raise SystemExit(f"whoami 调用被拒：{result.content!r}")
        payload: Any = getattr(result, "structured_content", None)
        if payload is None:
            payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
        print("whoami:")
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="本地演示 /mcp 冒烟自检（纯 Mock token）")
    parser.add_argument(
        "--port", type=int, default=8788, help="serve_local_demo 的端口（默认 8788）"
    )
    parser.add_argument("--token", default="demo-codex-token", help="demo Bearer token")
    args = parser.parse_args()
    asyncio.run(smoke(f"http://127.0.0.1:{args.port}/mcp", args.token))
    print("smoke ok")


if __name__ == "__main__":
    main()
