"""Isolated database regressions for the company introduction."""

import os
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from austra.integrations.model_credentials import ModelCredentials
from austra.onboarding import routes
from austra.onboarding.conversation import InterviewTurn, WebsiteResult
from austra.platform.auth import password_hash
from austra.web import create_app


@pytest.fixture
def tour_client(tmp_path, monkeypatch):
    url = os.environ.get("AUSTRA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("AUSTRA_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    schema = f"tour_regression_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            database_url = psycopg.conninfo.make_conninfo(url, options=f"-c search_path={schema}")
            monkeypatch.setenv("AUSTRA_DATABASE_URL", database_url)
            monkeypatch.setenv("AUSTRA_SUPER_ADMIN_USERNAME", "superadmin")
            monkeypatch.setenv("AUSTRA_SUPER_ADMIN_PASSWORD", "test platform password 123")
            monkeypatch.setenv("AUSTRA_LEGACY_ORGANIZATION_SLUG", "legacy")
            monkeypatch.setattr(ModelCredentials, "get", lambda self, org_id: "fixture-key")
            app = create_app(tmp_path)
            with psycopg.connect(database_url) as database:
                org_id = database.execute(
                    "INSERT INTO organizations (slug, name) VALUES ('acme', 'Acme') RETURNING id"
                ).fetchone()[0]
                database.execute(
                    "INSERT INTO users (organization_id, username_normalized, role, "
                    "password_hash, must_change_password) VALUES "
                    "(%s, 'admin', 'org_admin', %s, false)",
                    (org_id, password_hash("fixture admin password")),
                )
                database.execute(
                    "INSERT INTO organization_tours (organization_id, about_admin, "
                    "about_organization, website_url, website_status, summary, review_ready, "
                    "revision) VALUES (%s, 'Ava is COO', 'Acme builds bikes', "
                    "'https://example.com/', 'Read 12 pages', 'Ava is COO. Acme builds bikes.', "
                    "true, 1)",
                    (org_id,),
                )
                database.execute(
                    "INSERT INTO onboarding_messages (organization_id, position, role, body) "
                    "VALUES (%s, 1, 'assistant', 'Ava is COO. Acme builds bikes. "
                    "Does this look right?')",
                    (org_id,),
                )
            client = TestClient(app, base_url="http://acme.localhost:8000")
            response = client.post(
                "/api/login",
                json={"username": "admin", "password": "fixture admin password"},
            )
            assert response.status_code == 200
            yield client
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def interview_turn(reply: str, *, confirmed: bool = False) -> InterviewTurn:
    return InterviewTurn(
        reply=reply,
        about_admin="Ava is COO",
        about_organization="Acme builds bikes",
        website_url="https://example.com/",
        social_url=r"www\.linkedin.com/company/acme",
        desired_outcome="",
        summary="Ava is COO. Acme builds bikes.",
        ready_for_confirmation=True,
        confirmed=confirmed,
    )


@pytest.mark.parametrize("confirmation", ["no that's it", "everything is ok"])
def test_natural_confirmation_finishes_onboarding(tour_client, monkeypatch, confirmation):
    monkeypatch.setattr(
        routes, "generate_turn",
        lambda *args: interview_turn("I’m still checking the company details."),
    )

    response = tour_client.post(
        "/api/org/tour/message", headers={"X-Austra-Request": "1"},
        json={"body": confirmation, "revision": 1},
    )

    assert response.status_code == 200
    state = response.json()
    assert state["completed"] is True
    assert state["messages"][-1]["body"].startswith("Thanks, your company introduction")
    assert state["social_url"] is None


def test_escaped_link_is_checked_and_failed_read_is_explicit(tour_client, monkeypatch):
    called = []
    monkeypatch.setattr(
        routes, "generate_turn",
        lambda *args: interview_turn(
            "Thanks for the LinkedIn link. It may be blocked. "
            "Ava is COO at Acme, which builds bikes. Does this look right?"
        ),
    )

    def blocked_read(url, identity, context):
        called.append(url)
        return WebsiteResult(f"Could not read the public social page at {url}: HTTP 999.", "", 0)

    monkeypatch.setattr(routes, "ingest_social_page", blocked_read)

    response = tour_client.post(
        "/api/org/tour/message", headers={"X-Austra-Request": "1"},
        json={"body": r"www\.linkedin.com/company/acme", "revision": 1},
    )

    assert response.status_code == 200
    state = response.json()
    assert called == ["https://www.linkedin.com/company/acme"]
    assert state["social_url"] == called[0]
    assert state["social_status"].startswith("Could not read")
    reply = state["messages"][-1]["body"]
    assert reply.startswith("Could not read the public social page")
    assert reply.count("Ava is COO at Acme") == 1
