"""Validated, reusable remote MCP connection catalog."""

import asyncio
import json
import re
from base64 import b64encode
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from austra.access import Access
from austra.files import Files
from austra.mcp_gateway import ExternalToolError, call_external, call_remote, remote_tools

NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{1,31}\Z")
MICROSOFT_LEARN_URL = "https://learn.microsoft.com/api/mcp"
GMAIL_TOOL = {
    "name": "send_email",
    "description": (
        "Send an email from the connected Gmail account, optionally with one PDF. "
        "Use attachment_id for a PDF already in this chat, or material_requirements_pdf "
        "to generate the current material requirements PDF."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["to", "subject", "body"],
        "properties": {
            "to": {"type": "string", "minLength": 1},
            "subject": {"type": "string", "minLength": 1},
            "body": {"type": "string", "minLength": 1},
            "attachment_id": {
                "type": "string",
                "pattern": "^([0-9a-f]{64}|material_requirements_pdf)$",
            },
        },
    },
    "read_only": False,
}


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
            **(
                {"gmail": {"url": "Built-in Gmail", "tools": [GMAIL_TOOL], "built_in": True}}
                if self.access.configured().get("gmail", False)
                else {}
            ),
            **stored,
        }

    def _save(self, connections: dict[str, dict[str, Any]]) -> None:
        connections.pop("microsoft_learn", None)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        temporary.write_text(json.dumps(connections, indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def add(self, name: str, url: str, token: str = "") -> dict[str, Any]:
        if not NAME_PATTERN.fullmatch(name) or name in {"microsoft_learn", "gmail"}:
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
        if name == "gmail":
            verified = call_external("gmail_verify", {}, self._gmail_credentials())
            if (
                verified.get("email", "").casefold()
                != self.access.get("GMAIL_SENDER_EMAIL").casefold()
            ):
                raise ExternalToolError("Gmail account does not match the saved sender")
            return connection
        tools = asyncio.run(remote_tools(connection["url"], self.access.mcp_token(name)))
        _validate_tools(tools)
        if not connection["built_in"]:
            connection["tools"] = tools
            self._save(connections)
        return {"url": connection["url"], "tools": tools, "built_in": connection["built_in"]}

    def call(
        self, name: str, tool_name: str, arguments: dict[str, Any], *, chat_id: str = ""
    ) -> dict[str, Any]:
        connections = self.list()
        if name not in connections:
            raise KeyError(name)
        connection = connections[name]
        tools = (
            asyncio.run(remote_tools(connection["url"], self.access.mcp_token(name)))
            if name == "microsoft_learn"
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
        if name == "gmail":
            provider_arguments = {
                key: value for key, value in arguments.items() if key != "attachment_id"
            }
            attachment_id = arguments.get("attachment_id")
            if attachment_id:
                if not chat_id or attachment_id == "material_requirements_pdf":
                    raise ValueError("PDF attachment is unavailable in this chat")
                path, filename = Files(self.path.parent).attachment(chat_id, attachment_id)
                content = path.read_bytes()
                if (
                    not filename.lower().endswith(".pdf")
                    or not content.startswith(b"%PDF-")
                    or len(content) > 1_000_000
                ):
                    raise ValueError("Only saved PDFs up to 1 MB can be emailed")
                provider_arguments["attachment_name"] = filename
                provider_arguments["attachment_base64"] = b64encode(content).decode("ascii")
            result = call_external(
                "gmail_send_email", provider_arguments, self._gmail_credentials()
            )
            if result.get("sent") is not True:
                raise ExternalToolError(
                    result.get("error") or "Gmail did not confirm the email was sent."
                )
            return result
        return asyncio.run(
            call_remote(connection["url"], self.access.mcp_token(name), tool_name, arguments)
        )

    def _gmail_credentials(self) -> dict[str, str]:
        return {
            name: self.access.get(name)
            for name in (
                "GMAIL_CLIENT_ID",
                "GMAIL_CLIENT_SECRET",
                "GMAIL_REFRESH_TOKEN",
                "GMAIL_SENDER_EMAIL",
            )
        }

    def remove(self, name: str) -> None:
        connections = self.list()
        if name not in connections or connections[name]["built_in"]:
            raise KeyError(name)
        connections.pop(name)
        self._save(connections)
        self.access.delete_mcp_token(name)
