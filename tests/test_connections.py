import asyncio
import json
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from austra.access import CONNECTORS, GUIDES, Access
from austra.agent import ConnectionCheckError
from austra.connectors import exchange_gmail_code, verify_gmail, verify_slack
from austra.slack_socket import receive_events
from austra.web import create_app


def test_connection_tests_are_authenticated_and_audited_without_secrets(tmp_path):
    checked = []

    def verify_slack_values(values):
        checked.append(values)
        if values["SLACK_BOT_TOKEN"] == "bad-token":
            raise ConnectionCheckError("Slack rejected the bot token.", 400)

    client = TestClient(create_app(tmp_path, connector_verifiers={"slack": verify_slack_values}))
    assert client.post("/api/connections/slack/test").status_code == 401
    client.post("/api/setup", json={"password": "long private passphrase"})
    assert client.post("/api/connections/slack/test").status_code == 400
    client.post(
        "/api/credentials/slack",
        json={
            "values": {
                "SLACK_SIGNING_SECRET": "private-signing-secret",
                "SLACK_BOT_TOKEN": "bad-token",
                "SLACK_ALLOWED_TEAM_ID": "T123",
                "SLACK_APP_TOKEN": "xapp-test-token",
            }
        },
    )
    assert client.post("/api/connections/slack/test").status_code == 400
    client.post("/api/credentials/slack", json={"values": {"SLACK_BOT_TOKEN": "good-token"}})
    assert client.post("/api/connections/slack/test").json() == {"connected": True}
    assert checked[-1]["SLACK_BOT_TOKEN"] == "good-token"
    config = client.get("/api/config").json()
    assert config["fields"]["slack"] == [
        "SLACK_SIGNING_SECRET",
        "SLACK_BOT_TOKEN",
        "SLACK_ALLOWED_TEAM_ID",
        "SLACK_APP_TOKEN",
    ]
    assert "good-token" not in json.dumps(config)
    audit = client.get("/api/audit").json()
    assert [event["outcome"] for event in audit if event["action"] == "connection.tested"] == [
        "success",
        "failed",
    ]
    assert "private-signing-secret" not in json.dumps(audit)
    assert "good-token" not in json.dumps(audit)


def test_slack_verifier_checks_bot_and_workspace(monkeypatch):
    called = []

    def call(name, arguments, credentials):
        called.append((name, arguments, credentials))
        return {"team_id": "T1"} if name == "slack_verify" else {"url": "wss://example"}

    monkeypatch.setattr("austra.connectors.call_external", call)
    values = {
        "SLACK_BOT_TOKEN": "token",
        "SLACK_ALLOWED_TEAM_ID": "T1",
        "SLACK_APP_TOKEN": "xapp-test-token",
    }
    verify_slack(values)
    assert [item[0] for item in called] == ["slack_verify", "slack_socket_url"]
    try:
        verify_slack({**values, "SLACK_ALLOWED_TEAM_ID": "T2"})
    except ConnectionCheckError as error:
        assert "different workspace" in str(error)
    else:
        raise AssertionError("Workspace mismatch must fail")


def test_slack_socket_acknowledges_before_processing_event(tmp_path):
    access = Access(tmp_path)
    access.save("slack", {"SLACK_APP_TOKEN": "xapp-test-token"})
    received = []

    class WebSocket:
        def __init__(self):
            self.messages = iter(
                [
                    json.dumps(
                        {
                            "type": "events_api",
                            "envelope_id": "envelope-1",
                            "payload": {"type": "event_callback", "event_id": "Ev123"},
                        }
                    ),
                    json.dumps({"type": "disconnect", "reason": "refresh_requested"}),
                ]
            )
            self.acks = []

        async def recv(self):
            return next(self.messages)

        async def send(self, message):
            self.acks.append(json.loads(message))

    websocket = WebSocket()

    def on_event(payload):
        assert websocket.acks == [{"envelope_id": "envelope-1"}]
        received.append(payload["event_id"])

    asyncio.run(receive_events(websocket, "xapp-test-token", access, on_event))
    assert received == ["Ev123"]


def test_gmail_verifier_refreshes_token_and_matches_mailbox(monkeypatch):
    called = []

    def call(name, arguments, credentials):
        called.append((name, arguments, credentials))
        return {"email": "admin@example.com"}

    monkeypatch.setattr("austra.connectors.call_external", call)
    values = {
        "GMAIL_CLIENT_ID": "client",
        "GMAIL_CLIENT_SECRET": "secret",
        "GMAIL_REFRESH_TOKEN": "refresh",
        "GMAIL_SENDER_EMAIL": "admin@example.com",
    }
    verify_gmail(values)
    assert called == [("gmail_verify", {}, values)]
    try:
        verify_gmail({**values, "GMAIL_SENDER_EMAIL": "other@example.com"})
    except ConnectionCheckError as error:
        assert "does not match" in str(error)
    else:
        raise AssertionError("Mailbox mismatch must fail")


def test_new_connector_uses_catalog_and_verifier_without_new_routes(tmp_path, monkeypatch):
    monkeypatch.setitem(CONNECTORS, "example", ("EXAMPLE_TOKEN",))
    monkeypatch.setitem(
        GUIDES, "example", {"title": "Example", "url": "https://example.com", "steps": "Connect"}
    )
    checked = []
    client = TestClient(
        create_app(
            tmp_path,
            connector_verifiers={"example": lambda values: checked.append(values["EXAMPLE_TOKEN"])},
        )
    )
    client.post("/api/setup", json={"password": "long private passphrase"})
    assert client.get("/api/config").json()["fields"]["example"] == ["EXAMPLE_TOKEN"]
    client.post("/api/credentials/example", json={"values": {"EXAMPLE_TOKEN": "private"}})
    assert client.post("/api/connections/example/test").json() == {"connected": True}
    assert checked == ["private"]


def test_gmail_oauth_flow_hides_refresh_token_and_rejects_replayed_state(tmp_path, monkeypatch):
    client = TestClient(create_app(tmp_path, connector_verifiers={"gmail": lambda _values: None}))
    assert client.post("/api/connections/gmail/start").status_code == 401
    client.post("/api/setup", json={"password": "long private passphrase"})
    assert client.post("/api/connections/gmail/start").status_code == 400
    client.post(
        "/api/credentials/gmail",
        json={"values": {"GMAIL_CLIENT_ID": "client-id", "GMAIL_CLIENT_SECRET": "client-secret"}},
    )
    config = client.get("/api/config").json()
    assert config["fields"]["gmail"] == ["GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET"]
    assert config["connectors"]["gmail"] is False
    started = client.post("/api/connections/gmail/start").json()
    query = parse_qs(urlsplit(started["authorization_url"]).query)
    assert query["client_id"] == ["client-id"]
    assert query["access_type"] == ["offline"]
    assert query["redirect_uri"] == [config["gmail_redirect_uri"]]
    assert query["scope"] == ["https://www.googleapis.com/auth/gmail.metadata"]

    def exchange(code, client_id, client_secret, redirect_uri):
        assert (code, client_id, client_secret, redirect_uri) == (
            "one-time-code",
            "client-id",
            "client-secret",
            config["gmail_redirect_uri"],
        )
        return "private-refresh-token", "admin@example.com"

    monkeypatch.setattr("austra.web.exchange_gmail_code", exchange)
    state = query["state"][0]
    completed = client.get(
        "/api/connections/gmail/callback",
        params={"state": state, "code": "one-time-code"},
        follow_redirects=False,
    )
    assert completed.status_code == 303
    assert completed.headers["location"] == "/?connection=gmail&oauth=connected"
    assert client.get("/api/config").json()["connectors"]["gmail"] is True
    assert client.post("/api/connections/gmail/test").json() == {"connected": True}
    assert client.get("/api/connections/gmail/callback", params={"state": state}).status_code == 400
    assert "private-refresh-token" not in json.dumps(client.get("/api/config").json())
    assert "private-refresh-token" not in json.dumps(client.get("/api/audit").json())


def test_gmail_code_exchange_uses_authorization_code_without_manual_token(monkeypatch):
    def call(name, arguments, credentials):
        assert name == "gmail_exchange_code"
        assert arguments == {"code": "code", "redirect_uri": "http://localhost/callback"}
        assert credentials == {"GMAIL_CLIENT_ID": "id", "GMAIL_CLIENT_SECRET": "secret"}
        return {"refresh_token": "refresh", "email": "admin@example.com"}

    monkeypatch.setattr("austra.connectors.call_external", call)
    assert exchange_gmail_code("code", "id", "secret", "http://localhost/callback") == (
        "refresh",
        "admin@example.com",
    )
