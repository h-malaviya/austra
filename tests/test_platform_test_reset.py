"""Test-only organization reset keeps access while removing tenant work."""

import os
from uuid import uuid4

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from psycopg import sql

from austra.integrations.model_credentials import ModelCredentials
from austra.integrations.secrets import ConnectionSecrets
from austra.platform.auth import password_hash
from austra.web import create_app


def test_platform_reset_restarts_only_enabled_organization(tmp_path, monkeypatch):
    url = os.environ.get("AUSTRA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("AUSTRA_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    schema = f"test_reset_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as root:
        root.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            database_url = psycopg.conninfo.make_conninfo(url, options=f"-c search_path={schema}")
            monkeypatch.setenv("AUSTRA_DATABASE_URL", database_url)
            monkeypatch.setenv("AUSTRA_SUPER_ADMIN_USERNAME", "superadmin")
            monkeypatch.setenv("AUSTRA_SUPER_ADMIN_PASSWORD", "fixture platform password")
            monkeypatch.setenv("AUSTRA_LEGACY_ORGANIZATION_SLUG", "legacy")
            monkeypatch.setenv("AUSTRA_TEST_RESET_SLUG", "bt")
            monkeypatch.setenv("AUSTRA_WORKSPACE", str(tmp_path))
            monkeypatch.setenv("AUSTRA_CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode())
            app = create_app(tmp_path)
            with psycopg.connect(database_url) as db:
                ids = {}
                for slug in ("bt", "other"):
                    ids[slug] = db.execute(
                        "INSERT INTO organizations (slug, name, email) "
                        "VALUES (%s, %s, 'contact@example.com') RETURNING id",
                        (slug, slug),
                    ).fetchone()[0]
                    admin_id = db.execute(
                        "INSERT INTO users (organization_id, username_normalized, role, "
                        "password_hash, must_change_password, email) "
                        "VALUES (%s, 'admin', 'org_admin', %s, false, 'admin@example.com') "
                        "RETURNING id",
                        (ids[slug], password_hash("fixture admin password")),
                    ).fetchone()[0]
                    db.execute(
                        "INSERT INTO organization_tours (organization_id, about_organization, "
                        "completed_at) VALUES (%s, 'old company profile', now())", (ids[slug],)
                    )
                    db.execute(
                        "INSERT INTO organization_profile_revisions (organization_id, "
                        "organization_name, about_organization, confirmed_by, confirmed_at, "
                        "tour_revision, source) VALUES (%s, %s, 'old company profile', "
                        "%s, now(), 0, 'welcome_confirmation')",
                        (ids[slug], slug, admin_id),
                    )
                    db.execute(
                        "INSERT INTO onboarding_messages (organization_id, position, role, body) "
                        "VALUES (%s, 1, 'user', 'old conversation')", (ids[slug],)
                    )
                    db.execute(
                        "INSERT INTO audit_events (organization_id, actor_user_id, channel, "
                        "action, outcome, correlation_id) VALUES (%s, %s, 'api', "
                        "'fixture.created', 'success', %s)",
                        (ids[slug], admin_id, uuid4()),
                    )
            ModelCredentials(tmp_path).save(ids["bt"], "fixture-anthropic-key")
            other_secret_id = uuid4()
            ConnectionSecrets(tmp_path).save(ids["bt"], other_secret_id, {"token": "fixture"})
            blob_dir = tmp_path / "evidence_blobs" / str(ids["bt"])
            blob_dir.mkdir(parents=True)
            (blob_dir / "old-document").write_text("test fixture", encoding="utf-8")
            client = TestClient(app, base_url="http://admin.localhost:8000")
            assert client.post(
                "/api/login", json={"username": "superadmin", "password": "fixture platform password"}
            ).status_code == 200
            headers = {"X-Austra-Request": "1"}
            path = f"/api/platform/organizations/{ids['bt']}/test-reset"
            assert client.post(path, headers=headers, json={"confirm_slug": "wrong"}).status_code == 403
            assert client.post(path, headers=headers, json={"confirm_slug": "bt"}).status_code == 200
            with psycopg.connect(database_url) as db:
                assert db.execute(
                    "SELECT count(*) FROM organization_tours WHERE organization_id = %s",
                    (ids["bt"],),
                ).fetchone()[0] == 0
                assert db.execute(
                    "SELECT count(*) FROM organization_profile_revisions WHERE organization_id = %s",
                    (ids["bt"],),
                ).fetchone()[0] == 0
                assert db.execute(
                    "SELECT count(*) FROM organization_profile_revisions WHERE organization_id = %s",
                    (ids["other"],),
                ).fetchone()[0] == 1
                assert db.execute(
                    "SELECT count(*) FROM onboarding_messages WHERE organization_id = %s",
                    (ids["bt"],),
                ).fetchone()[0] == 0
                assert db.execute(
                    "SELECT count(*) FROM onboarding_cases WHERE organization_id = %s",
                    (ids["bt"],),
                ).fetchone()[0] == 1
                assert db.execute(
                    "SELECT count(*) FROM onboarding_messages WHERE organization_id = %s",
                    (ids["other"],),
                ).fetchone()[0] == 1
                assert db.execute(
                    "SELECT email FROM organizations WHERE id = %s", (ids["bt"],)
                ).fetchone()[0] is None
                assert db.execute(
                    "SELECT count(*) FROM users WHERE organization_id = %s AND role = 'org_admin'",
                    (ids["bt"],),
                ).fetchone()[0] == 1
            assert ModelCredentials(tmp_path).get(ids["bt"]) == "fixture-anthropic-key"
            assert not (blob_dir / "old-document").exists()
            assert not (tmp_path / "integration_secrets" / str(ids["bt"]) / str(other_secret_id)).exists()
        finally:
            root.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
