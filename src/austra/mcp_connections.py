"""Validated, reusable remote MCP connection catalog."""

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from austra.access import Access
from austra.mcp_gateway import call_remote, remote_tools

NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{1,31}\Z")
MICROSOFT_LEARN_URL = "https://learn.microsoft.com/api/mcp"


def _valid_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username


def _validate_tools(tools: list[dict[str, Any]]) -> None:
    if not tools:
        raise ValueError("MCP server exposes no tools")
    try:
        for tool in tools:
            Draft202012Validator.check_schema(tool["input_schema"])
    except (KeyError, SchemaError) as error:
        raise ValueError("MCP server returned an invalid tool schema") from error


class McpConnections:
    def __init__(self, root: str | Path, access: Access) -> None:
        self.path = Path(root) / "mcp_connections.json"
        self.access = access

    def list(self) -> dict[str, dict[str, Any]]:
        stored = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        return {
            "microsoft_learn": {
                "url": MICROSOFT_LEARN_URL,
                "tools": [],
                "built_in": True,
            },
            **stored,
        }

    def _save(self, connections: dict[str, dict[str, Any]]) -> None:
        connections.pop("microsoft_learn", None)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(connections, indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def add(self, name: str, url: str, token: str = "") -> dict[str, Any]:
        if not NAME_PATTERN.fullmatch(name) or name == "microsoft_learn":
            raise ValueError("Invalid MCP connection name")
        if not _valid_url(url):
            raise ValueError("MCP server URL must use HTTPS")
        connections = self.list()
        if name in connections:
            raise ValueError("MCP connection already exists")
        tools = asyncio.run(remote_tools(url, token))
        _validate_tools(tools)
        if token:
            self.access.save_mcp_token(name, token)
        connection = {"url": url, "tools": tools, "built_in": False}
        connections[name] = connection
        self._save(connections)
        return connection

    def test(self, name: str) -> dict[str, Any]:
        connections = self.list()
        if name not in connections:
            raise KeyError(name)
        connection = connections[name]
        tools = asyncio.run(remote_tools(connection["url"], self.access.mcp_token(name)))
        _validate_tools(tools)
        if not connection["built_in"]:
            connection["tools"] = tools
            self._save(connections)
        return {"url": connection["url"], "tools": tools, "built_in": connection["built_in"]}

    def call(self, name: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        connections = self.list()
        if name not in connections:
            raise KeyError(name)
        connection = connections[name]
        tools = (
            asyncio.run(remote_tools(connection["url"], self.access.mcp_token(name)))
            if connection["built_in"]
            else connection["tools"]
        )
        tool = next((tool for tool in tools if tool["name"] == tool_name), None)
        if tool is None:
            raise ValueError("Tool is not in the verified MCP catalog")
        try:
            validator = Draft202012Validator(tool["input_schema"])
            validator.validate(arguments)
        except (SchemaError, ValidationError) as error:
            raise ValueError("Tool arguments do not match the MCP input schema") from error
        return asyncio.run(
            call_remote(connection["url"], self.access.mcp_token(name), tool_name, arguments)
        )

    def remove(self, name: str) -> None:
        connections = self.list()
        if name not in connections or connections[name]["built_in"]:
            raise KeyError(name)
        connections.pop(name)
        self._save(connections)
        self.access.delete_mcp_token(name)
