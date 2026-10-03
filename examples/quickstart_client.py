"""Minimal MCP client: remember one thing, recall it, print the result.

Start the MCP server first (see docs/quickstart.md), then run:

    python examples/quickstart_client.py
    python examples/quickstart_client.py http://localhost:8765/mcp

It uses only the `mcp` package that Common AI Memory already depends on.
"""
from __future__ import annotations

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8765/mcp"


def payload(result):
    data = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if data is None and result.content:
        data = json.loads(result.content[0].text)
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    return data


async def main() -> None:
    async with streamable_http_client(URL) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            tools = sorted(tool.name for tool in (await session.list_tools()).tools)
            print("tools:", ", ".join(tools))
            saved = payload(await session.call_tool("remember", {
                "content": "Quickstart: I prefer jasmine tea in the afternoon.",
                "category": "preference/food",
            }))
            print("remembered:", saved["id"])
            found = payload(await session.call_tool("recall", {"query": "jasmine tea"}))
            print("recalled:", [item["id"] for item in found])
            assert any(item["id"] == saved["id"] for item in found), "the new memory should be recalled"


if __name__ == "__main__":
    asyncio.run(main())
