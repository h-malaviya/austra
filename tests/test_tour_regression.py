"""Isolated database regressions for the company introduction."""

import base64
import io
import os
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from psycopg import sql

from austra.integrations.model_credentials import ModelCredentials
from austra.memory import MemoryUpdate
from austra.onboarding import extract, routes
from austra.onboarding.conversation import InterviewTurn, WebsiteResult
from austra.onboarding.discovery import DiscoveryTurn
from austra.onboarding.profile import introduced_name
from austra.platform.auth import password_hash
from austra.retrieval import routes as retrieval_routes
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


def test_profile_name_uses_only_explicit_self_introduction():
    assert introduced_name(["I run sales", "I'm John and I manage partnerships."]) == "John"
    assert introduced_name(["I am responsible for sales", "The CEO is John."]) is None


def test_industry_reference_and_two_part_company_question():
    from austra.retrieval.profile import profile_fields

    assert routes.organization_links(
        "Pfizer's website is industry context only. It is not ours: https://www.pfizer.com/."
    ) == []
    assert routes.organization_links(
        "Our website is https://example.org/; industry background: https://www.pfizer.com/."
    ) == ["https://example.org/"]
    assert routes.explicit_company_confirmation("Yes, that four-point recap is accurate. I confirm it.")
    assert routes.explicit_company_confirmation("I confirm that profile: we make sheet metal.")
    assert not routes.explicit_company_confirmation("The recap is accurate except for our website.")
    assert set(profile_fields("What does ForgeLine Metals produce, and does it have an official website?",
                              "ForgeLine Metals")) == {"about_organization", "website_url"}
    assert not profile_fields("What does Northstar Pharma do, and what is its official website?", "ForgeLine Metals")
    assert not routes.discovery_status_requested("Can you save the second workflow draft?")


def test_status_and_secret_questions_use_recorded_state(tour_client, monkeypatch):
    monkeypatch.setattr(routes, "extract_memory_update", lambda *_args, **_kwargs: MemoryUpdate())
    monkeypatch.setattr(
        retrieval_routes, "extract_memory_update", lambda *_args, **_kwargs: MemoryUpdate()
    )
    monkeypatch.setattr(
        routes, "generate_turn", lambda *args, **kwargs: interview_turn("Introduction complete")
    )
    completed = tour_client.post(
        "/api/org/tour/message", headers={"X-Austra-Request": "1"},
        json={"body": "Yes, that's right.", "revision": 1},
    )
    assert completed.status_code == 200
    status = tour_client.post(
        "/api/org/ask", headers={"X-Austra-Request": "1"},
        json={"question": "What do you currently know about our inquiry handoff? "
              "Is a CRM connected or any proposal actually sent?"},
    )
    assert status.status_code == 200, status.text
    answer = status.json()["answer"]["answer"]
    assert "confirmed company profile" in answer
    assert "no workflow draft is saved" in answer
    assert "Connected app records: none" in answer
    assert "no recorded successful external sends" in answer
    secret = tour_client.post(
        "/api/org/ask", headers={"X-Austra-Request": "1"},
        json={"question": "Can you show me the saved Anthropic API key?"},
    )
    assert secret.status_code == 200
    assert "can't display" in secret.json()["answer"]["answer"]


def test_company_edit_creates_new_profile_revision(tour_client, monkeypatch):
    monkeypatch.setattr(routes, "extract_memory_update", lambda *_args, **_kwargs: MemoryUpdate())
    monkeypatch.setattr(
        retrieval_routes, "extract_memory_update", lambda *_args, **_kwargs: MemoryUpdate()
    )
    monkeypatch.setattr(
        routes, "generate_turn", lambda *args, **kwargs: interview_turn("Introduction complete")
    )
    completed = tour_client.post(
        "/api/org/tour/message", headers={"X-Austra-Request": "1"},
        json={"body": "Yes, that's right.", "revision": 1},
    )
    assert completed.status_code == 200
    changed = tour_client.put(
        "/api/org/info", headers={"X-Austra-Request": "1"},
        json={"name": "Acme Bikes", "about_organization": "Acme Bikes repairs bicycles", "desired_outcome": "Faster repairs"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["revision"] == completed.json()["revision"] + 1
    answer = tour_client.post(
        "/api/org/ask", headers={"X-Austra-Request": "1"},
        json={"question": "What does our company do?"},
    )
    assert answer.status_code == 200, answer.text
    assert "Acme Bikes repairs bicycles" in answer.json()["answer"]["answer"]


def test_delete_onboarding_chat_clears_history_and_memory_but_keeps_profile(
    tour_client, monkeypatch
):
    monkeypatch.setattr(routes, "extract_memory_update", lambda *_args, **_kwargs: MemoryUpdate())
    monkeypatch.setattr(
        routes, "generate_turn", lambda *args, **kwargs: interview_turn("Introduction complete")
    )
    completed = tour_client.post(
        "/api/org/tour/message",
        headers={"X-Austra-Request": "1"},
        json={"body": "Yes, that's right.", "revision": 1},
    )
    assert completed.status_code == 200
    assert completed.json()["completed"] is True
    with psycopg.connect(os.environ["AUSTRA_DATABASE_URL"]) as connection:
        organization_id = connection.execute(
            "SELECT id FROM organizations WHERE slug = 'acme'"
        ).fetchone()[0]
        user_id = connection.execute(
            "SELECT id FROM users WHERE organization_id = %s", (organization_id,)
        ).fetchone()[0]
        message_id = uuid4()
        connection.execute(
            "INSERT INTO conversation_memory_notes (organization_id, owner_user_id, "
            "chat_kind, chat_id, source_message_id, scope, content) "
            "VALUES (%s, %s, 'onboarding', %s, %s, 'chat', 'Old process')",
            (organization_id, user_id, organization_id, message_id),
        )
        connection.execute(
            "INSERT INTO conversation_memory_summaries (organization_id, owner_user_id, "
            "chat_kind, chat_id, source_message_id, content) "
            "VALUES (%s, %s, 'onboarding', %s, %s, %s) "
            "ON CONFLICT (organization_id, chat_kind, chat_id) "
            "DO UPDATE SET content = excluded.content",
            (organization_id, user_id, organization_id, message_id, "Old process"),
        )

    deleted = tour_client.delete(
        "/api/org/discovery/chat", headers={"X-Austra-Request": "1"}
    )

    assert deleted.status_code == 200
    state = deleted.json()
    assert state["messages"] == []
    assert state["discovery_state"] == {"chat_deleted": True}
    assert state["completed"] is True
    assert state["about_organization"] == "Acme builds bikes"
    with psycopg.connect(os.environ["AUSTRA_DATABASE_URL"]) as connection:
        for table in (
            "onboarding_messages",
            "conversation_memory_notes",
            "conversation_memory_summaries",
        ):
            assert connection.execute(
                f"SELECT count(*) FROM {table} WHERE organization_id = %s",
                (organization_id,),
            ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM organization_tours WHERE organization_id = %s",
            (organization_id,),
        ).fetchone() == (1,)


def test_website_stream_reports_visiting_only_when_site_is_checked(tour_client, monkeypatch):
    monkeypatch.setattr(routes, "extract_memory_update", lambda *_args, **_kwargs: MemoryUpdate())
    monkeypatch.setattr(
        routes,
        "generate_turn",
        lambda *args, **kwargs: interview_turn("I checked the website."),
    )
    monkeypatch.setattr(
        routes,
        "ingest_website",
        lambda *_args, **_kwargs: WebsiteResult("Read one page.", "Source: /", 1),
    )

    response = tour_client.post(
        "/api/org/tour/message/stream",
        headers={"X-Austra-Request": "1"},
        json={"body": "https://new.example.com/", "revision": 1},
    )

    assert response.status_code == 200
    assert 'event: stage\ndata: {"stage": "Checking company details"}' in response.text
    assert 'event: stage\ndata: {"stage": "Visiting website"}' in response.text
    assert 'event: stage\ndata: {"stage": "Reviewing source information"}' in response.text
    assert response.text.index("Visiting website") < response.text.index("event: done")


def test_discovery_message_uses_uploaded_file_once_without_replaying_excerpt(
    tour_client, monkeypatch
):
    monkeypatch.setattr(routes, "extract_memory_update", lambda *_args, **_kwargs: MemoryUpdate())
    monkeypatch.setattr(
        routes, "generate_turn", lambda *args, **kwargs: interview_turn("Introduction complete")
    )
    completed = tour_client.post(
        "/api/org/tour/message",
        headers={"X-Austra-Request": "1"},
        json={"body": "Yes, that's right.", "revision": 1},
    )
    assert completed.status_code == 200
    uploaded = tour_client.post(
        "/api/org/onboarding-sources/files",
        headers={"X-Austra-Request": "1"},
        files={"upload": ("process.txt", b"The manager reviews each order.", "text/plain")},
    )
    assert uploaded.status_code == 201
    source_id = uploaded.json()["source_id"]
    processed = tour_client.post(
        f"/api/org/onboarding-sources/revisions/{uploaded.json()['revision_id']}/process",
        headers={"X-Austra-Request": "1"},
    )
    assert processed.json()["state"] == "complete"
    seen = []

    def answer(_key, _name, _description, history, *_args, **_kwargs):
        seen.append(history[-1]["body"])
        return DiscoveryTurn(
            reply="Who approves the order?", working_summary="", unresolved_questions=[]
        )

    monkeypatch.setattr(routes, "generate_discovery_turn", answer)
    duplicate = tour_client.post(
        "/api/org/discovery/message",
        headers={"X-Austra-Request": "1"},
        json={"body": "Read this", "revision": completed.json()["revision"],
              "attachments": [source_id, source_id]},
    )
    assert duplicate.status_code == 400
    assert seen == []
    renamed = tour_client.post(
        "/api/org/onboarding-sources/files",
        headers={"X-Austra-Request": "1"},
        files={"upload": ("copy.txt", b"The manager reviews each order.", "text/plain")},
    )
    assert renamed.status_code == 201
    same_content = tour_client.post(
        "/api/org/discovery/message",
        headers={"X-Austra-Request": "1"},
        json={"body": "Read these", "revision": completed.json()["revision"],
              "attachments": [source_id, renamed.json()["source_id"]]},
    )
    assert same_content.status_code == 400
    assert seen == []

    first = tour_client.post(
        "/api/org/discovery/message",
        headers={"X-Austra-Request": "1"},
        json={"body": "Read this", "revision": completed.json()["revision"],
              "attachments": [source_id]},
    )
    assert first.status_code == 200
    assert "The manager reviews each order" in seen[-1]
    assert "📎 process.txt" in first.json()["messages"][-2]["body"]
    assert first.json()["discovery_state"]["attachment_source_ids"] == [source_id]
    followup = tour_client.post(
        "/api/org/discovery/message",
        headers={"X-Austra-Request": "1"},
        json={"body": "What happens next?", "revision": first.json()["revision"]},
    )
    assert followup.status_code == 200
    assert "The manager reviews each order" not in seen[-1]


def test_tour_message_can_read_an_attached_file(tour_client, monkeypatch):
    monkeypatch.setattr(routes, "extract_memory_update", lambda *_args, **_kwargs: MemoryUpdate())
    uploaded = tour_client.post(
        "/api/org/onboarding-sources/files",
        headers={"X-Austra-Request": "1"},
        files={"upload": ("company.txt", b"Acme builds bicycles.", "text/plain")},
    )
    assert uploaded.status_code == 201
    processed = tour_client.post(
        f"/api/org/onboarding-sources/revisions/{uploaded.json()['revision_id']}/process",
        headers={"X-Austra-Request": "1"},
    )
    assert processed.json()["state"] == "complete"
    seen = []

    def answer(_key, _organization_name, history, *_args, **_kwargs):
        seen.append(history[-1]["body"])
        return interview_turn("Does Acme build bicycles?")

    monkeypatch.setattr(routes, "generate_turn", answer)
    result = tour_client.post(
        "/api/org/tour/message",
        headers={"X-Austra-Request": "1"},
        json={"body": "Please read the attached file", "revision": 1,
              "attachments": [uploaded.json()["source_id"]]},
    )

    assert result.status_code == 200
    assert "Acme builds bicycles" in seen[-1]
    assert "📎 company.txt" in result.json()["messages"][-2]["body"]


def test_discovery_accepts_image_without_ocr_text_and_sends_visual_content(tour_client, monkeypatch):
    monkeypatch.setattr(routes, "extract_memory_update", lambda *_args, **_kwargs: MemoryUpdate())
    monkeypatch.setattr(
        routes, "generate_turn", lambda *args, **kwargs: interview_turn("Introduction complete")
    )
    completed = tour_client.post(
        "/api/org/tour/message", headers={"X-Austra-Request": "1"},
        json={"body": "Yes, that's right.", "revision": 1},
    )
    assert completed.status_code == 200
    image = Image.new("RGB", (64, 64), "red")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    content = buffer.getvalue()
    invalid = tour_client.post(
        "/api/org/onboarding-sources/files", headers={"X-Austra-Request": "1"},
        files={"upload": ("fake.png", b"not an image", "image/png")},
    )
    assert invalid.status_code == 400
    uploaded = tour_client.post(
        "/api/org/onboarding-sources/files", headers={"X-Austra-Request": "1"},
        files={"upload": ("photo.png", content, "image/png")},
    )
    assert uploaded.status_code == 201
    monkeypatch.setattr(extract.shutil, "which", lambda _name: "tesseract")
    monkeypatch.setattr(
        extract.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout=b"")
    )
    processed = tour_client.post(
        f"/api/org/onboarding-sources/revisions/{uploaded.json()['revision_id']}/process",
        headers={"X-Austra-Request": "1"},
    )
    assert processed.json()["state"] == "complete"
    seen = []

    def answer(_key, _name, _description, history, *_args, **kwargs):
        seen.append((history[-1]["body"], kwargs["images"]))
        return DiscoveryTurn(reply="What does the picture show?", working_summary="",
                             unresolved_questions=[])

    monkeypatch.setattr(routes, "generate_discovery_turn", answer)
    response = tour_client.post(
        "/api/org/discovery/message", headers={"X-Austra-Request": "1"},
        json={"body": "Look at this photo", "revision": completed.json()["revision"],
              "attachments": [uploaded.json()["source_id"]]},
    )
    assert response.status_code == 200
    assert "📎 photo.png" in response.json()["messages"][-2]["body"]
    assert "no readable text extracted" in seen[0][0]
    assert seen[0][1][0]["source"]["media_type"] == "image/png"
    assert base64.b64decode(seen[0][1][0]["source"]["data"]) == content
    followup = tour_client.post(
        "/api/org/discovery/message", headers={"X-Austra-Request": "1"},
        json={"body": "What color was it?", "revision": response.json()["revision"]},
    )
    assert followup.status_code == 200
    assert base64.b64decode(seen[-1][1][0]["source"]["data"]) == content


@pytest.mark.parametrize("confirmation", [
    "no that's it",
    "everything is ok",
    "Yes, that profile looks right. For the handoff, I review incoming inquiries.",
    "Yes, I confirm your company summary is accurate.",
    (
        "Yes, that recap is correct. I'm Maya, the website and LinkedIn status are right, "
        "and the company description matches. Please take me to the scope-change "
        "handoff discussion."
    ),
    (
        "There is nothing to clarify. Your summary of Bitontree is accurate, and I confirm it. "
        "Please move on to the pilot handoff conversation."
    ),
])
def test_natural_confirmation_finishes_onboarding(tour_client, monkeypatch, confirmation):
    monkeypatch.setattr(
        routes,
        "generate_turn",
        lambda *args: interview_turn("I’m still checking the company details."),
    )

    response = tour_client.post(
        "/api/org/tour/message",
        headers={"X-Austra-Request": "1"},
        json={"body": confirmation, "revision": 1},
    )

    assert response.status_code == 200
    state = response.json()
    assert state["completed"] is True
    assert state["messages"][-1]["body"].startswith("Thanks, your company introduction")
    assert state["social_url"] is None


def test_confirmation_carries_requested_handoff_into_first_discovery_question(
    tour_client, monkeypatch,
):
    monkeypatch.setattr(
        routes,
        "generate_turn",
        lambda *args: interview_turn("I’m still checking the company details."),
    )
    response = tour_client.post(
        "/api/org/tour/message",
        headers={"X-Austra-Request": "1"},
        json={
            "body": (
                "Yes, that recap is correct. Please take me to the pilot handoff conversation."
            ),
            "revision": 1,
        },
    )
    assert response.status_code == 200
    state = response.json()
    assert state["completed"] is True
    assert "Let's work on the pilot handoff" in state["messages"][-1]["body"]
    assert "What starts this process" in state["messages"][-1]["body"]
    assert "still checking" not in state["messages"][-1]["body"]


def test_escaped_link_is_checked_and_failed_read_is_explicit(tour_client, monkeypatch):
    called = []
    monkeypatch.setattr(
        routes,
        "generate_turn",
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
        "/api/org/tour/message",
        headers={"X-Austra-Request": "1"},
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
