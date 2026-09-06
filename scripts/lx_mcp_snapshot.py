"""LX-MCP 工具面快照核实脚本（DEC-009）。

在 **密钥持有者本人电脑** 上运行；密钥只经环境变量进入进程，
不落盘、不打印、不进入输出文件。输出仅含工具元数据（名称、schema 哈希、
注解、描述截断），用于回答"本租户 LX-MCP 到底有哪些广告写工具"。

用法：
    export LX_MCP_URL="<你的 LX-MCP endpoint>"
    export LX_MCP_KEY="<新轮换的 key，绝不粘贴到任何聊天/文档>"
    export LX_MCP_KEY_HEADER="X-Mcp-Key"   # 可选，默认 X-Mcp-Key
    uv run python scripts/lx_mcp_snapshot.py

产物：lx_mcp_snapshot_<UTC时间>.json —— 请人工确认内容后再交给评审方。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

#: 名称疑似写语义的启发式标记——仅供人工核对排序，不构成能力判定。
_WRITE_HINTS = ("create", "update", "delete", "set", "add", "remove", "adjust", "pause", "enable")


def _schema_hash(schema: Any) -> str:
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def capture(url: str, key_header: str, key: str) -> dict[str, Any]:
    headers = {key_header: key}
    async with (
        httpx2.AsyncClient(headers=headers) as http,
        streamable_http_client(url, http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        init_dump = init.model_dump(mode="json")
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            from mcp.types import PaginatedRequestParams

            params = PaginatedRequestParams(cursor=cursor) if cursor else None
            page = await session.list_tools(params=params)
            for tool in page.tools:
                name = tool.name
                description = tool.description or ""
                tools.append(
                    {
                        "name": name,
                        "input_schema_sha256": _schema_hash(tool.input_schema),
                        "description_sha256": hashlib.sha256(
                            description.encode("utf-8")
                        ).hexdigest(),
                        "description_head": description[:200],
                        "annotations": tool.annotations.model_dump(mode="json")
                        if tool.annotations
                        else None,
                        "likely_write_by_name": any(h in name.lower() for h in _WRITE_HINTS),
                    }
                )
            cursor = page.next_cursor
            if not cursor:
                break
        return {
            "captured_at": datetime.now(UTC).isoformat(),
            "purpose": "DEC-009 snapshot-conflict verification; metadata only, no credentials",
            "endpoint_host_only": httpx2.URL(url).host,
            "server_info": init_dump.get("server_info") or init_dump.get("serverInfo"),
            "protocol_version": init_dump.get("protocol_version")
            or init_dump.get("protocolVersion"),
            "tool_count": len(tools),
            "likely_write_tool_count": sum(1 for t in tools if t["likely_write_by_name"]),
            "tools": sorted(tools, key=lambda t: str(t["name"])),
        }


def main() -> int:
    url = os.environ.get("LX_MCP_URL")
    key = os.environ.get("LX_MCP_KEY")
    key_header = os.environ.get("LX_MCP_KEY_HEADER", "X-Mcp-Key")
    if not url or not key:
        print("缺少 LX_MCP_URL 或 LX_MCP_KEY 环境变量；不要把 key 写进任何文件。")
        return 2
    snapshot = asyncio.run(capture(url, key_header, key))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = f"lx_mcp_snapshot_{stamp}.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False, indent=2)
    print(f"工具总数: {snapshot['tool_count']}")
    print(f"名称疑似写语义: {snapshot['likely_write_tool_count']}（仅启发式，需人工核对）")
    print(f"快照已写入: {out_path}（只含元数据；请自查后再外发）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
