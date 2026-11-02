import json

import pytest
from fastapi.testclient import TestClient

from austra.agent import AgentReply, ExternalCall
from austra.files import Files
from austra.mcp_gateway import ExternalToolError, call_external
from austra.web import create_app


def test_local_provider_server_uses_mcp_protocol_without_a_provider_call():
    assert call_external("provider_health", {}, {}) == {"ready": True}
    with pytest.raises(ExternalToolError, match="failed"):
        call_external("unknown_tool", {}, {})


def test_remote_mcp_setup_discovers_tools_and_keeps_token_private(tmp_path, monkeypatch):
    discovered = []
    called = []

    async def tools(url, token=""):
        discovered.append((url, token))
        return [
            {
                "name": "send_message",
                "description": "Send a message",
                "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
                "read_only": False,
            }
        ]

    async def call(url, token, name, arguments):
        called.append((url, token, name, arguments))
        return {"sent": True}

    monkeypatch.setattr("austra.mcp_connections.remote_tools", tools)
    monkeypatch.setattr("austra.mcp_connections.call_remote", call)
    client = TestClient(create_app(tmp_path))
    assert client.get("/api/mcp/connections").status_code == 401
    client.post("/api/setup", json={"password": "long private passphrase"})
    assert (
        client.post(
            "/api/mcp/connections",
            json={"name": "teams", "url": "https://teams.example/mcp", "token": "private-token"},
        ).status_code
        == 200
    )
    listed = client.get("/api/mcp/connections").json()
    assert listed["teams"]["tools"][0]["name"] == "send_message"
    assert "private-token" not in json.dumps(listed)
    assert "private-token" not in (tmp_path / "mcp_connections.json").read_text()
    assert (
        client.post(
            "/api/mcp/connections/teams/tools/not_verified",
            json={"arguments": {"text": "hello"}},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/mcp/connections/teams/tools/send_message",
            json={"arguments": {"text": 42}},
        ).status_code
        == 400
    )
    assert client.post(
        "/api/mcp/connections/teams/tools/send_message",
        json={"arguments": {"text": "hello"}},
    ).json() == {"sent": True}
    assert discovered == [("https://teams.example/mcp", "private-token")]
    assert called == [
        ("https://teams.example/mcp", "private-token", "send_message", {"text": "hello"})
    ]
    assert "private-token" not in json.dumps(client.get("/api/audit").json())
    assert client.delete("/api/mcp/connections/teams").json() == {"removed": True}
    assert "MCP_TEAMS_TOKEN" not in (tmp_path / ".env").read_text()


def test_remote_mcp_rejects_non_https_and_duplicate_names(tmp_path, monkeypatch):
    async def tools(_url, _token=""):
        return [{"name": "read", "description": "", "input_schema": {}, "read_only": True}]

    monkeypatch.setattr("austra.mcp_connections.remote_tools", tools)
    client = TestClient(create_app(tmp_path))
    client.post("/api/setup", json={"password": "long private passphrase"})
    assert (
        client.post(
            "/api/mcp/connections", json={"name": "teams", "url": "http://teams.example/mcp"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/mcp/connections", json={"name": "teams", "url": "https://teams.example/mcp"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/mcp/connections", json={"name": "teams", "url": "https://other.example/mcp"}
        ).status_code
        == 400
    )


def test_admin_chat_invokes_verified_mcp_tool_once(tmp_path, monkeypatch):
    calls = []

    async def tools(_url, _token=""):
        return [{"name": "send_message", "description": "", "input_schema": {}, "read_only": False}]

    async def call(_url, _token, name, arguments):
        calls.append((name, arguments))
        return {"sent": True}

    monkeypatch.setattr("austra.mcp_connections.remote_tools", tools)
    monkeypatch.setattr("austra.mcp_connections.call_remote", call)

    def planner(_files, _access, _chat_id):
        return AgentReply(
            reply="I will send the requested message.",
            external_calls=[
                ExternalCall(
                    connection="teams",
                    tool="send_message",
                    arguments_json='{"text":"Hello"}',
                )
            ],
        )

    client = TestClient(create_app(tmp_path, planner=planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    client.post(
        "/api/mcp/connections",
        json={"name": "teams", "url": "https://teams.example/mcp", "token": "private-token"},
    )
    created = client.post("/api/chats", json={"body": "Send Hello in Teams"}).json()
    assert 'send_message: {"sent": true}' in created["chat"]["messages"][-1]["body"]
    chat_id = created["chat"]["id"]
    client.post(f"/api/chats/{chat_id}/retry")
    assert calls == [("send_message", {"text": "Hello"})]

    slack_chat = Files(tmp_path).create_chat("Send Hello in Teams", "U123", "slack")
    blocked = client.post(f"/api/chats/{slack_chat['id']}/retry").json()
    assert "require an administrator chat" in blocked["chat"]["messages"][-1]["body"]
    assert len(calls) == 1
