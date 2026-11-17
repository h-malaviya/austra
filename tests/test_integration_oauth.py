"""Platform owned Gmail consent uses a static callback and tenant bound state."""

import os
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from psycopg import sql

from austra.connectors import GMAIL_SCOPE
from austra.platform.auth import password_hash
from austra.web import create_app


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
            monkeypatch.setenv("AUSTRA_GMAIL_CLIENT_ID", "platform-client")
            monkeypatch.setenv("AUSTRA_GMAIL_CLIENT_SECRET", "platform-secret")
            monkeypatch.setenv("AUSTRA_CONNECTION_ENCRYPTION_KEY",
                               Fernet.generate_key().decode())
            app = create_app(tmp_path)
            with psycopg.connect(database_url) as db:
                organization = db.execute("INSERT INTO organizations (slug, name) "
                                          "VALUES ('fixture-org', 'Fixture') RETURNING id"
                                          ).fetchone()[0]
                db.execute("INSERT INTO users (organization_id, username_normalized, role, "
                           "password_hash, must_change_password) VALUES "
                           "(%s, 'admin', 'org_admin', %s, false)",
                           (organization, password_hash("fixture admin password")))
            client = TestClient(app, base_url="http://fixture-org.localhost:8000")
            login = client.post("/api/login", json={"username": "admin",
                "password": "fixture admin password", "organization_slug": "fixture-org"})
            assert login.status_code == 200
            assert client.get("/organization/connections").status_code == 200
            started = client.post("/api/org/connections/gmail/start",
                                  headers={"X-Austra-Request": "1"})
            assert started.status_code == 200
            authorization = urlsplit(started.json()["authorization_url"])
            query = parse_qs(authorization.query)
            redirect = query["redirect_uri"][0]
            assert redirect == "http://admin.localhost:8000/api/org/connections/gmail/callback"
            assert query["scope"] == [GMAIL_SCOPE]

            def exchange(name, arguments, credentials):
                assert name == "gmail_exchange_code"
                assert arguments == {"code": "one-time-code", "redirect_uri": redirect}
                assert credentials["GMAIL_CLIENT_ID"] == "platform-client"
                return {"refresh_token": "private-refresh-token",
                        "email": "mailbox@example.com", "scopes": GMAIL_SCOPE}

            monkeypatch.setattr("austra.integrations.routes.call_external", exchange)
            monkeypatch.setattr("austra.integrations.connections.gmail_identity",
                                lambda _values: "mailbox@example.com")
            callback = TestClient(app, base_url="http://admin.localhost:8000")
            completed = callback.get("/api/org/connections/gmail/callback",
                                     params={"state": query["state"][0],
                                             "code": "one-time-code"}, follow_redirects=False)
            assert completed.status_code == 303
            assert completed.headers["location"].startswith(
                "http://fixture-org.localhost:8000/organization/connections")
            listed = client.get("/api/org/connections").json()
            assert listed[0]["account"] == "mailbox@example.com"
            assert "private-refresh-token" not in str(listed)
            assert callback.get("/api/org/connections/gmail/callback",
                                params={"state": query["state"][0],
                                        "code": "one-time-code"}).status_code == 400
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
