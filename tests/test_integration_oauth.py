"""Platform owned Gmail consent uses a static callback and tenant bound state."""

import asyncio
import hashlib
import os
from base64 import urlsafe_b64encode
from io import BytesIO
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet
from docx import Document
from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session
from psycopg import sql

from austra.agent import AgentReply
from austra.connectors import GMAIL_SCOPE
from austra.external_mcp_server import mcp
from austra.integrations.gmail_content import reply_content
from austra.integrations.google_workspace import (
    GOOGLE_WORKSPACE_ENDPOINTS,
    GOOGLE_WORKSPACE_SCOPE,
    WorkspaceConsentError,
    exchange_code,
)
from austra.integrations.secrets import ConnectionSecrets
from austra.mcp_gateway import ExternalToolError
from austra.platform.auth import password_hash
from austra.web import create_app


def test_reply_content_extracts_only_the_matched_message_and_document(monkeypatch):
    def encoded(text):
        return urlsafe_b64encode(text.encode()).decode()

    def fetch(url, **_options):
        if "/attachments/" in url:
            body = {"data": encoded("Quote reference: Q-100\nLead time: 7 days")}
        else:
            body = {"id": "reply-1", "threadId": "approved-thread", "payload": {"parts": [
                {"mimeType": "text/plain", "body": {"data": encoded("Quoted unit price: USD 12.50")}},
                {"filename": "offer.txt", "body": {"attachmentId": "quote-1", "size": 44}},
                {"filename": "executable.exe", "body": {"attachmentId": "blocked", "size": 80}},
            ]}}
        assert not url.endswith("/blocked")
        return httpx.Response(200, request=httpx.Request("GET", url), json=body)

    monkeypatch.setattr("austra.integrations.gmail_content.httpx.get", fetch)
    evidence = reply_content("fixture", "reply-1", "approved-thread")
    assert evidence["quoted_terms"] == {
        "quote_reference": "Q-100", "lead_time_days": "7", "quoted_unit_price": "USD 12.50",
    }
    assert [item["status"] for item in evidence["attachments"]] == ["extracted", "review_needed"]
    with pytest.raises(ValueError, match="did not match"):
        reply_content("fixture", "reply-1", "different-thread")

    async def check_tool_output():
        async with create_connected_server_and_client_session(mcp) as client:
            result = await client.call_tool("gmail_read_reply_content", {
                "message_id": "reply-1", "thread_id": "approved-thread",
            })
            assert not result.isError
            assert result.structuredContent == evidence

    monkeypatch.setattr("austra.external_mcp_server._gmail_access_token", lambda: "fixture")
    asyncio.run(check_tool_output())


@pytest.mark.parametrize("over_limit", [False, True])
def test_reply_content_checks_word_table_after_introductory_paragraphs(monkeypatch, over_limit):
    document = Document()
    for index in range(15):
        document.add_paragraph(f"Quote notes for criterion {index}.")
    if over_limit:
        document.add_paragraph("L" * 12_001)
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "SKU", "Quantity"
    table.cell(1, 0).text, table.cell(1, 1).text = "PK-C500", "200 NOS"
    contents = BytesIO()
    document.save(contents)

    def fetch(url, **_options):
        return httpx.Response(200, request=httpx.Request("GET", url), json={
            "id": "reply-1", "threadId": "approved-thread", "payload": {
                "filename": "quote.docx", "body": {"data": urlsafe_b64encode(contents.getvalue()).decode()},
            },
        })

    monkeypatch.setattr("austra.integrations.gmail_content.httpx.get", fetch)
    attachment, = reply_content("fixture", "reply-1", "approved-thread")["attachments"]
    if over_limit:
        assert attachment["status"] == "review_needed"
        assert "excerpts" not in attachment
    else:
        assert attachment["status"] == "extracted"
        assert "PK-C500 | 200 NOS" in attachment["excerpts"][-1]["text"]


def test_gmail_reply_uses_the_sent_thread_and_original_recipient(monkeypatch):
    def message(identifier, sender, date, reference="", labels=()):
        headers = {"From": sender, "In-Reply-To": reference, "Message-ID": f"<{identifier}@example.com>",
                   "Subject": "Re: Demo purchase request"}
        return {"id": identifier, "internalDate": str(date), "labelIds": list(labels),
                "payload": {"headers": [{"name": name, "value": value} for name, value in headers.items()]}}

    calls = []
    reference = "<sent@example.com>"
    messages = [
        message("sent", "sender@example.com", 1, labels=("SENT",)),
        message("other-sender", "other@example.com", 2, reference),
        message("same-subject", "buyer@example.com", 3, "<another@example.com>"),
    ]

    def provider_get(url, **options):
        calls.append((url, options["params"]))
        if url.endswith("/messages/sent"):
            body = {"threadId": "thread"}
        else:
            body = {"messages": messages}
        return httpx.Response(200, request=httpx.Request("GET", url), json=body)

    monkeypatch.setattr("austra.external_mcp_server._gmail_access_token", lambda: "fixture")
    monkeypatch.setattr("austra.external_mcp_server.httpx.get", provider_get)
    async def check_replies():
        arguments = {"message_id": "sent", "recipient": "buyer@example.com"}
        async with create_connected_server_and_client_session(mcp) as client:
            waiting = await client.call_tool("gmail_find_reply", arguments)
            assert not waiting.isError
            assert waiting.structuredContent == {"reply": None}
            messages.append(message("outlook-reply", "Buyer <buyer@example.com>", 4, reference))
            received = await client.call_tool("gmail_find_reply", arguments)
            assert not received.isError
            assert received.structuredContent == {"reply": {
                "message_id": "outlook-reply", "thread_id": "thread", "from": "buyer@example.com",
                "subject": "Re: Demo purchase request", "received_ms": 4,
            }}
            messages.extend([
                message("outbound-follow-up", "sender@example.com", 5,
                        "<outlook-reply@example.com>", ("SENT",)),
                message("second-reply", "buyer@example.com", 6, "<outbound-follow-up@example.com>"),
                message("trashed-reply", "buyer@example.com", 7, reference, ("TRASH",)),
                message("unrelated-chain", "buyer@example.com", 8, "<other-sender@example.com>"),
            ])
            timeline = await client.call_tool("gmail_list_replies", arguments)
            assert not timeline.isError
            assert [reply["message_id"] for reply in timeline.structuredContent["replies"]] == [
                "outlook-reply", "second-reply",
            ]

    asyncio.run(check_replies())
    assert [url.rsplit("/", 2)[-2:] for url, _ in calls] == [
        ["messages", "sent"], ["threads", "thread"],
    ] * 3
    assert all(("format", "metadata") in params for _, params in calls[1::2])


def test_gmail_consent_uses_static_callback_and_hides_refresh_token(tmp_path, monkeypatch):
    url = os.getenv("AUSTRA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("AUSTRA_TEST_DATABASE_URL is required")
    schema = f"oauth_test_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            database_url = psycopg.conninfo.make_conninfo(url, options=f"-c search_path={schema}")
            monkeypatch.setenv("AUSTRA_DATABASE_URL", database_url)
            monkeypatch.setenv("AUSTRA_SUPER_ADMIN_USERNAME", "superadmin")
            monkeypatch.setenv("AUSTRA_SUPER_ADMIN_PASSWORD", "fixture platform password")
            monkeypatch.setenv("AUSTRA_PUBLIC_URL", "http://127.0.0.1:8000")
            monkeypatch.delenv("AUSTRA_GMAIL_REDIRECT_URI", raising=False)
            monkeypatch.setenv("AUSTRA_GMAIL_CLIENT_ID", "platform-client")
            monkeypatch.setenv("AUSTRA_GMAIL_CLIENT_SECRET", "platform-secret")
            monkeypatch.setenv("AUSTRA_CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode())
            app = create_app(tmp_path)
            with psycopg.connect(database_url) as db:
                organization = db.execute(
                    "INSERT INTO organizations (slug, name) "
                    "VALUES ('fixture-org', 'Fixture') RETURNING id"
                ).fetchone()[0]
                db.execute(
                    "INSERT INTO users (organization_id, username_normalized, role, "
                    "password_hash, must_change_password) VALUES "
                    "(%s, 'admin', 'org_admin', %s, false)",
                    (organization, password_hash("fixture admin password")),
                )
            client = TestClient(app, base_url="http://fixture-org.localhost:8000")
            login = client.post(
                "/api/login",
                json={
                    "username": "admin",
                    "password": "fixture admin password",
                    "organization_slug": "fixture-org",
                },
            )
            assert login.status_code == 200
            assert client.get("/organization/connections").status_code == 200
            started = client.post(
                "/api/org/connections/gmail/start", headers={"X-Austra-Request": "1"}
            )
            assert started.status_code == 200
            authorization = urlsplit(started.json()["authorization_url"])
            query = parse_qs(authorization.query)
            redirect = query["redirect_uri"][0]
            assert redirect == "http://localhost:8000/api/org/connections/gmail/callback"
            assert query["scope"] == [GMAIL_SCOPE]

            def exchange(name, arguments, credentials):
                assert name == "gmail_exchange_code"
                assert arguments == {"code": "one-time-code", "redirect_uri": redirect}
                assert credentials["GMAIL_CLIENT_ID"] == "platform-client"
                return {
                    "refresh_token": "private-refresh-token",
                    "email": "mailbox@example.com",
                    "scopes": GMAIL_SCOPE,
                }

            monkeypatch.setattr("austra.integrations.routes.call_external", exchange)
            monkeypatch.setattr(
                "austra.integrations.connections.gmail_identity",
                lambda _values: "mailbox@example.com",
            )
            callback = TestClient(app, base_url="http://localhost:8000")
            assert callback.get("/api/org/connections").status_code == 404
            assert callback.get("/api/org/connections/gmail/callback").status_code == 400
            completed = callback.get(
                "/api/org/connections/gmail/callback",
                params={"state": query["state"][0], "code": "one-time-code"},
                follow_redirects=False,
            )
            assert completed.status_code == 303
            assert completed.headers["location"].startswith(
                "http://fixture-org.localhost:8000/organization/connections"
            )
            listed = client.get("/api/org/connections").json()
            assert listed[0]["account"] == "mailbox@example.com"
            assert "private-refresh-token" not in str(listed)
            assert (
                callback.get(
                    "/api/org/connections/gmail/callback",
                    params={"state": query["state"][0], "code": "one-time-code"},
                ).status_code
                == 400
            )
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_workspace_consent_resumes_draft_and_stores_only_tenant_connections(tmp_path, monkeypatch):
    url = os.getenv("AUSTRA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("AUSTRA_TEST_DATABASE_URL is required")
    schema = f"workspace_oauth_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            database_url = psycopg.conninfo.make_conninfo(url, options=f"-c search_path={schema}")
            for name, value in {
                "AUSTRA_DATABASE_URL": database_url,
                "AUSTRA_SUPER_ADMIN_USERNAME": "superadmin",
                "AUSTRA_SUPER_ADMIN_PASSWORD": "fixture platform password",
                "AUSTRA_LEGACY_ORGANIZATION_SLUG": "fixture-workspace",
                "AUSTRA_PUBLIC_URL": "http://127.0.0.1:8000",
                "AUSTRA_GOOGLE_WORKSPACE_CLIENT_ID": "workspace-client",
                "AUSTRA_GOOGLE_WORKSPACE_CLIENT_SECRET": "fixture-client-secret",
                "AUSTRA_CONNECTION_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            }.items():
                monkeypatch.setenv(name, value)
            def planner(files, _access, chat_id):
                if files.chat(chat_id)["messages"][-1]["body"] == "Read the stock sheet":
                    return AgentReply(
                        reply="I need access to the stock sheet.",
                        google_workspace_required="Google Sheets",
                    )
                return AgentReply(reply="This workflow draft is ready for review.")

            app = create_app(tmp_path, planner=planner)
            organizations, workflows = {}, {}
            with psycopg.connect(database_url) as db:
                for tenant in ("fixture-workspace", "fixture-other"):
                    organizations[tenant] = db.execute(
                        "INSERT INTO organizations (slug, name) VALUES (%s, %s) RETURNING id",
                        (tenant, tenant),
                    ).fetchone()[0]
                    db.execute(
                        "INSERT INTO users (organization_id, username_normalized, role, "
                        "password_hash, must_change_password) VALUES (%s, 'admin', 'org_admin', %s, false)",
                        (organizations[tenant], password_hash("fixture admin password")),
                    )
                    workflows[tenant] = db.execute(
                        "INSERT INTO onboarding_cases (organization_id, workflow_name, created_event_id) "
                        "VALUES (%s, 'Fixture workflow', %s) RETURNING id",
                        (organizations[tenant], uuid4()),
                    ).fetchone()[0]
            clients = {}
            for tenant in organizations:
                clients[tenant] = TestClient(app, base_url=f"http://{tenant}.localhost:8000")
                login = clients[tenant].post("/api/login", json={
                    "username": "admin", "password": "fixture admin password", "organization_slug": tenant,
                })
                assert login.status_code == 200
            client = clients["fixture-workspace"]
            workflow = str(workflows["fixture-workspace"])
            start_path = "/api/org/connections/google-workspace/start"
            status_path = "/api/org/connections/google-workspace/status"
            callback_path = "/api/connections/gmail/callback"
            request_headers = {"X-Austra-Request": "1"}
            assert client.post(start_path, json={"workflow_id": str(workflows["fixture-other"])},
                               headers=request_headers).status_code == 404
            assert client.get(status_path).json() == {"apps": []}

            drafted = client.post("/api/chats", json={"body": "Draft a stock review workflow"},
                                  headers=request_headers)
            assert drafted.status_code == 201, drafted.text
            chat_id = drafted.json()["chat"]["id"]
            surfaces_path = f"/api/ui/chats/{chat_id}"
            assert client.get(surfaces_path).json()["surfaces"] == []
            requested = client.post(f"/api/chats/{chat_id}/messages",
                                    json={"body": "Read the stock sheet"}, headers=request_headers)
            assert requested.status_code == 200, requested.text
            [consent] = client.get(surfaces_path).json()["surfaces"]
            assert consent["kind"] == "workspace_consent"
            assert consent["message_id"] == requested.json()["chat"]["messages"][-1]["id"]
            actions = [
                item["action"]["event"]["name"]
                for item in consent["messages"][-1]["updateComponents"]["components"]
                if item["component"] == "Button"
            ]
            assert actions == ["connect_google_workspace", "check_google_workspace"]

            def start():
                started = client.post(start_path, json={"workflow_id": workflow}, headers=request_headers)
                assert started.status_code == 200
                return parse_qs(urlsplit(started.json()["authorization_url"]).query)

            callback = TestClient(create_app(tmp_path), base_url="http://localhost:8000")
            denied = callback.get(callback_path, params={"state": start()["state"][0],
                                  "error": "access_denied"}, follow_redirects=False)
            assert denied.status_code == 303
            assert parse_qs(urlsplit(denied.headers["location"]).query) == {
                "workspace_oauth": ["denied"], "workflow": [workflow],
            }

            query = start()
            assert query["scope"] == [f"openid email {GOOGLE_WORKSPACE_SCOPE}"]
            assert query["redirect_uri"] == [f"http://localhost:8000{callback_path}"]
            assert query["code_challenge_method"] == ["S256"]
            assert "client_secret" not in query

            def token(url, *, data, timeout):
                if data["grant_type"] == "authorization_code":
                    challenge = urlsafe_b64encode(hashlib.sha256(data["code_verifier"].encode()).digest())
                    assert challenge.rstrip(b"=").decode() == query["code_challenge"][0]
                    assert data["redirect_uri"] == query["redirect_uri"][0]
                else:
                    assert data["refresh_token"] == "fixture-refresh"
                return httpx.Response(200, request=httpx.Request("POST", url), json={
                    "access_token": "fixture-access", "refresh_token": "fixture-refresh",
                    "scope": GOOGLE_WORKSPACE_SCOPE,
                })

            def userinfo(url, *, headers, timeout):
                assert headers["Authorization"] == "Bearer fixture-access"
                return httpx.Response(200, request=httpx.Request("GET", url), json={
                    "email": "workspace@example.com", "email_verified": True,
                })

            def discover(endpoint, access_token):
                assert endpoint in GOOGLE_WORKSPACE_ENDPOINTS.values()
                assert access_token == "fixture-access"
                return [{"name": "read_fixture", "input_schema": {"type": "object"}}]

            monkeypatch.setattr("austra.integrations.google_workspace.httpx.post", token)
            monkeypatch.setattr("austra.integrations.google_workspace.httpx.get", userinfo)
            monkeypatch.setattr("austra.integrations.workspace_oauth.discover", discover)
            monkeypatch.setattr("austra.integrations.connections.discover", discover)
            params = {"state": query["state"][0], "code": "fixture-code"}
            finished = callback.get(callback_path, params=params, follow_redirects=False)
            assert finished.status_code == 303
            assert urlsplit(finished.headers["location"]).hostname == "fixture-workspace.localhost"
            assert parse_qs(urlsplit(finished.headers["location"]).query) == {
                "workspace_oauth": ["connected"], "workflow": [workflow],
            }
            assert callback.get(callback_path, params=params).status_code == 400
            assert len(client.get(status_path).json()["apps"]) == 4
            assert client.get(surfaces_path).json()["surfaces"] == []
            assert clients["fixture-other"].get(status_path).json() == {"apps": []}
            rows = client.get("/api/org/connections").json()
            assert len(rows) == 4
            assert all(row["actions"][0]["effect"] == "unreviewed" for row in rows)
            assert "fixture-refresh" not in str(rows)
            secrets = ConnectionSecrets(tmp_path).read(organizations["fixture-workspace"], UUID(rows[0]["id"]))
            assert secrets["google_workspace_refresh_token"] == "fixture-refresh"
            checked = client.post(f"/api/org/connections/{rows[0]['id']}/health", headers=request_headers)
            assert checked.json()["health"] == "healthy"

            def interrupted_discovery(endpoint, access_token):
                if endpoint == GOOGLE_WORKSPACE_ENDPOINTS["Google Slides"]:
                    raise ExternalToolError("Fixture endpoint unavailable")
                return discover(endpoint, access_token)

            monkeypatch.setattr("austra.integrations.workspace_oauth.discover", interrupted_discovery)
            query = start()
            partial = callback.get(callback_path, params={"state": query["state"][0], "code": "fixture-code"},
                                   follow_redirects=False)
            assert parse_qs(urlsplit(partial.headers["location"]).query)["workspace_oauth"] == ["partial"]
            status = {item["name"]: item["health"] for item in client.get(status_path).json()["apps"]}
            assert status == {name: "repair" if name == "Google Slides" else "healthy"
                              for name in GOOGLE_WORKSPACE_ENDPOINTS}
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_workspace_consent_reports_a_missing_file_grant(monkeypatch):
    def token(url, *, data, timeout):
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "access_token": "fixture-access", "scope": "openid email",
        })

    monkeypatch.setattr("austra.integrations.google_workspace.httpx.post", token)
    with pytest.raises(WorkspaceConsentError) as caught:
        exchange_code("fixture-code", "fixture-client", "fixture-secret", "http://localhost", "verifier")
    assert caught.value.reason == "file_scope_missing"
