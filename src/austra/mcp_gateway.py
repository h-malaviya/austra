"""MCP boundary for external provider actions."""

import asyncio
import json
import sys
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


class ExternalToolError(RuntimeError):
    """An external MCP tool failed without exposing provider credentials."""


async def call_external_async(
    name: str, arguments: dict[str, Any], credentials: dict[str, str]
) -> dict[str, Any]:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "austra.external_mcp_server"],
        env=credentials,
    )
    try:
        async with (
            stdio_client(server) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            result = await session.call_tool(name, arguments)
    except Exception as error:
        raise ExternalToolError(f"External MCP tool {name} is unavailable") from error
    if result.isError:
        raise ExternalToolError(f"External MCP tool {name} failed")
    if not isinstance(result.structuredContent, dict):
        raise ExternalToolError(f"External MCP tool {name} returned invalid data")
    return result.structuredContent


def call_external(
    name: str, arguments: dict[str, Any], credentials: dict[str, str]
) -> dict[str, Any]:
    return asyncio.run(call_external_async(name, arguments, credentials))


async def remote_tools(url: str, token: str = "") -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with (
            httpx.AsyncClient(headers=headers, timeout=30.0) as client,
            streamable_http_client(url, http_client=client) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            found = []
            cursor = None
            while True:
                listed = await session.list_tools(cursor=cursor)
                found.extend(listed.tools)
                if len(found) > 100:
                    raise ExternalToolError("MCP server exposes too many tools")
                cursor = listed.nextCursor
                if not cursor:
                    break
    except ExternalToolError:
        raise
    except Exception as error:
        raise ExternalToolError("Could not connect to the MCP server") from error
    tools = [
        {
            "name": tool.name,
            "description": (tool.description or "")[:300],
            "input_schema": tool.inputSchema,
            "read_only": bool(tool.annotations and tool.annotations.readOnlyHint),
        }
        for tool in found
    ]
    if len(json.dumps(tools)) > 500_000:
        raise ExternalToolError("MCP tool catalog is too large")
    return tools


async def call_remote(url: str, token: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with (
            httpx.AsyncClient(headers=headers, timeout=60.0) as client,
            streamable_http_client(url, http_client=client) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(name, arguments)
    except Exception as error:
        raise ExternalToolError(f"MCP tool {name} is unavailable") from error
    if result.isError:
        raise ExternalToolError(f"MCP tool {name} failed")
    if isinstance(result.structuredContent, dict):
        return result.structuredContent
    return {"content": [item.model_dump(exclude_none=True) for item in result.content]}
