import json
from base64 import urlsafe_b64decode
from email import message_from_bytes, policy

import pytest
from fastapi.testclient import TestClient

from austra.access import Access
from austra.agent import AgentReply, ExternalCall
from austra.external_mcp_server import gmail_send_email
from austra.files import Files
from austra.mcp_connections import McpConnections
from austra.mcp_gateway import ExternalToolError, call_external
from austra.web import create_app


def test_local_provider_server_uses_mcp_protocol_without_a_provider_call():
    assert call_external("provider_health", {}, {}) == {"ready": True}
    with pytest.raises(ExternalToolError, match="failed"):
        call_external("unknown_tool", {}, {})


def test_connected_gmail_send_uses_verified_builtin_tool(tmp_path, monkeypatch):
    access = Access(tmp_path)
    access.save(
        "gmail",
        {
            "GMAIL_CLIENT_ID": "client",
            "GMAIL_CLIENT_SECRET": "secret",
            "GMAIL_REFRESH_TOKEN": "refresh",
            "GMAIL_SENDER_EMAIL": "sender@example.com",
        },
    )
    calls = []

    def provider(name, arguments, credentials):
        calls.append((name, arguments, set(credentials)))
        return {"sent": True, "message_id": "gmail-id"}

    monkeypatch.setattr("austra.mcp_connections.call_external", provider)
    connections = McpConnections(tmp_path, access)
    assert [tool["name"] for tool in connections.list()["gmail"]["tools"]] == ["send_email"]
    with pytest.raises(ValueError, match="input schema"):
        connections.call("gmail", "send_email", {"to": "recipient@example.com"})
    result = connections.call(
        "gmail",
        "send_email",
        {"to": "recipient@example.com", "subject": "Hello", "body": "Message"},
    )
    assert result == {"sent": True, "message_id": "gmail-id"}
    assert calls == [
        (
            "gmail_send_email",
            {"to": "recipient@example.com", "subject": "Hello", "body": "Message"},
            {"GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_SENDER_EMAIL"},
        )
    ]


def test_gmail_send_formats_message_and_reports_missing_permission(monkeypatch):
    requests = []

    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload

        def raise_for_status(self):
            assert self.status_code < 400

        def json(self):
            return self.payload

    def post(url, **kwargs):
        requests.append((url, kwargs))
        return (
            Response(200, {"access_token": "token"})
            if len(requests) == 1
            else Response(200, {"id": "sent-id"})
        )

    monkeypatch.setattr("austra.external_mcp_server.httpx.post", post)
    monkeypatch.setenv("GMAIL_CLIENT_ID", "client")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "refresh")
    monkeypatch.setenv("GMAIL_SENDER_EMAIL", "sender@example.com")
    assert gmail_send_email("recipient@example.com", "Hello", "Message") == {
        "sent": True,
        "message_id": "sent-id",
    }
    encoded = requests[1][1]["json"]["raw"]
    message = message_from_bytes(urlsafe_b64decode(encoded))
    assert message["From"] == "sender@example.com"
    assert message["To"] == "recipient@example.com"
    assert message["Subject"] == "Hello"
    assert message.get_payload(decode=True).decode().strip() == "Message"

    requests.clear()

    def denied(url, **kwargs):
        requests.append((url, kwargs))
        return Response(200, {"access_token": "token"}) if len(requests) == 1 else Response(403, {})

    monkeypatch.setattr("austra.external_mcp_server.httpx.post", denied)
    assert gmail_send_email("recipient@example.com", "Hello", "Message")["sent"] is False


def test_gmail_send_includes_pdf_attachment(monkeypatch):
    requests = []

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    def post(url, **kwargs):
        requests.append((url, kwargs))
        return Response({"access_token": "token"} if len(requests) == 1 else {"id": "sent-id"})

    monkeypatch.setattr("austra.external_mcp_server.httpx.post", post)
    for key, value in {
        "GMAIL_CLIENT_ID": "client",
        "GMAIL_CLIENT_SECRET": "secret",
        "GMAIL_REFRESH_TOKEN": "refresh",
        "GMAIL_SENDER_EMAIL": "sender@example.com",
    }.items():
        monkeypatch.setenv(key, value)
    from base64 import b64encode

    assert (
        gmail_send_email(
            "recipient@example.com",
            "Hello",
            "Message",
            "requirement.pdf",
            b64encode(b"%PDF-test").decode("ascii"),
        )["sent"]
        is True
    )
    message = message_from_bytes(
        urlsafe_b64decode(requests[1][1]["json"]["raw"]), policy=policy.default
    )
    attachment = next(message.iter_attachments())
    assert attachment.get_filename() == "requirement.pdf"
    assert attachment.get_content_type() == "application/pdf"
    assert attachment.get_payload(decode=True) == b"%PDF-test"


def test_gmail_expired_refresh_token_has_actionable_error(monkeypatch):
    class Response:
        status_code = 400

        def json(self):
            return {"error": "invalid_grant"}

    monkeypatch.setattr("austra.external_mcp_server.httpx.post", lambda *args, **kwargs: Response())
    for key in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"):
        monkeypatch.setenv(key, "test")
    from austra.external_mcp_server import _gmail_access_token

    with pytest.raises(ValueError, match="Reconnect Gmail"):
        _gmail_access_token()


def test_gmail_send_resolves_pdf_from_current_chat(tmp_path, monkeypatch):
    access = Access(tmp_path)
    access.save(
        "gmail",
        {
            "GMAIL_CLIENT_ID": "client",
            "GMAIL_CLIENT_SECRET": "secret",
            "GMAIL_REFRESH_TOKEN": "refresh",
            "GMAIL_SENDER_EMAIL": "sender@example.com",
        },
    )
    files = Files(tmp_path)
    chat = files.create_chat("Send the PDF", "Admin")
    saved = files.add_generated_attachment_message(
        chat["id"], "PDF ready", "report.pdf", b"%PDF-test"
    )
    digest = saved["messages"][-1]["attachments"][0]["id"]
    calls = []
    monkeypatch.setattr(
        "austra.mcp_connections.call_external",
        lambda name, arguments, credentials: (
            calls.append((name, arguments)) or {"sent": True, "message_id": "sent-id"}
        ),
    )
    connections = McpConnections(tmp_path, access)
    arguments = {
        "to": "recipient@example.com",
        "subject": "Report",
        "body": "Attached",
        "attachment_id": digest,
    }
    with pytest.raises(ValueError, match="unavailable"):
        connections.call("gmail", "send_email", arguments)
    assert connections.call("gmail", "send_email", arguments, chat_id=chat["id"])["sent"]
    assert calls[0][0] == "gmail_send_email"
    assert calls[0][1]["attachment_name"] == "report.pdf"
    assert calls[0][1]["attachment_base64"] == "JVBERi10ZXN0"


def test_admin_mail_request_generates_and_attaches_material_pdf(tmp_path, monkeypatch):
    sent = []

    def planner(_files, _access, _chat_id):
        return AgentReply(
            reply="I will send the requested report.",
            external_calls=[
                ExternalCall(
                    connection="gmail",
                    tool="send_email",
                    arguments_json=json.dumps(
                        {
                            "to": "recipient@example.com",
                            "subject": "Material needs",
                            "body": "See attached report.",
                            "attachment_id": "material_requirements_pdf",
                        }
                    ),
                )
            ],
        )

    monkeypatch.setattr("austra.web.build_material_plan", lambda _files: {"as_of": []})
    monkeypatch.setattr("austra.web.requirement_pdf", lambda _plan: b"%PDF-test")
    monkeypatch.setattr(
        "austra.mcp_connections.call_external",
        lambda name, arguments, credentials: (
            sent.append(arguments) or {"sent": True, "message_id": "sent-id"}
        ),
    )
    client = TestClient(create_app(tmp_path, planner=planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    Access(tmp_path).save(
        "gmail",
        {
            "GMAIL_CLIENT_ID": "client",
            "GMAIL_CLIENT_SECRET": "secret",
            "GMAIL_REFRESH_TOKEN": "refresh",
            "GMAIL_SENDER_EMAIL": "sender@example.com",
        },
    )
    response = client.post("/api/chats", json={"body": "Send the material requirements PDF"})
    assert response.status_code == 201
    messages = response.json()["chat"]["messages"]
    assert messages[-2]["attachments"][0]["name"] == "material-requirements.pdf"
    assert "message_id" in messages[-1]["body"]
    assert sent[0]["attachment_name"] == "material-requirements.pdf"


def test_expired_gmail_send_reports_reconnect_and_allows_retry(tmp_path, monkeypatch):
    def planner(_files, _access, _chat_id):
        return AgentReply(
            reply="Sending the email.",
            external_calls=[
                ExternalCall(
                    connection="gmail",
                    tool="send_email",
                    arguments_json=json.dumps(
                        {
                            "to": "recipient@example.com",
                            "subject": "Hello",
                            "body": "Message",
                        }
                    ),
                )
            ],
        )

    def expired(_name, _arguments, _credentials):
        raise ExternalToolError(
            "Gmail authorization expired or was revoked. Reconnect Gmail on Connections."
        )

    monkeypatch.setattr("austra.mcp_connections.call_external", expired)
    client = TestClient(create_app(tmp_path, planner=planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    Access(tmp_path).save(
        "gmail",
        {
            "GMAIL_CLIENT_ID": "client",
            "GMAIL_CLIENT_SECRET": "secret",
            "GMAIL_REFRESH_TOKEN": "refresh",
            "GMAIL_SENDER_EMAIL": "sender@example.com",
        },
    )
    response = client.post("/api/chats", json={"body": "Send the email"})
    assert "Reconnect Gmail on Connections" in response.json()["chat"]["messages"][-1]["body"]
    assert list((tmp_path / "external_calls").iterdir()) == []


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
