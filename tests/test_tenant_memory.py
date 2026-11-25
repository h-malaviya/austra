"""Conversation memory uses tenant and user scopes in disposable schemas."""

import os
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from austra.memory import MemoryNote, MemoryUpdate
from austra.platform.auth import password_hash
from austra.storage.migrate import apply_migrations
from austra.tenant.context import TenantContext
from austra.tenant.memory import (
    add_assistant_message,
    add_user_message,
    chat,
    delete_chat,
    list_chats,
    memory_context,
    save_memory_update,
)
from austra.tenant.permissions.evaluator import AuthContext
from austra.web import create_app


@pytest.fixture
def database():
    url = os.environ.get("AUSTRA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("AUSTRA_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    schema = f"memory_test_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            scoped_url = psycopg.conninfo.make_conninfo(url, options=f"-c search_path={schema}")
            with psycopg.connect(scoped_url) as connection:
                apply_migrations(connection)
                yield connection
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def users(connection):
    organization_id = connection.execute(
        "INSERT INTO organizations (slug, name) VALUES ('memory-test', 'Memory Test') RETURNING id"
    ).fetchone()[0]
    contexts = []
    for username in ("first", "second"):
        user_id = connection.execute(
            "INSERT INTO users (organization_id, username_normalized, role, password_hash, "
            "must_change_password) VALUES (%s, %s, 'org_admin', 'fixture', false) RETURNING id",
            (organization_id, username),
        ).fetchone()[0]
        contexts.append(
            TenantContext(
                AuthContext(user_id, organization_id, "org_admin", True, True),
                organization_id,
            )
        )
    connection.commit()
    return contexts


def test_chat_memory_isolated_and_hard_deleted_after_soft_delete(database):
    first, second = users(database)
    chat_id, message_id = add_user_message(database, first, "I prefer concise reports", None)
    add_assistant_message(database, first, chat_id, "Understood.")
    update = MemoryUpdate(
        notes=[
            MemoryNote(
                scope="user",
                text="Prefers concise reports",
                source_quote="I prefer concise reports",
            )
        ]
    )
    save_memory_update(
        database,
        first,
        "ask",
        chat_id,
        message_id,
        "I prefer concise reports",
        update,
    )
    assert len(chat(database, first, chat_id)["messages"]) == 2
    assert memory_context(database, first, "ask", chat_id)["user_preferences"] == [
        "Prefers concise reports"
    ]
    assert list_chats(database, first)[0]["id"] == str(chat_id)
    assert list_chats(database, second) == []
    with pytest.raises(LookupError):
        chat(database, second, chat_id)
    with pytest.raises(LookupError):
        add_user_message(database, second, "Read this", chat_id)
    delete_chat(database, first, chat_id)
    assert list_chats(database, first) == []
    assert memory_context(database, first, "ask", chat_id)["user_preferences"] == []
    assert (
        database.execute(
            "SELECT count(*) FROM organization_chat_messages WHERE chat_id = %s", (chat_id,)
        ).fetchone()[0]
        == 2
    )
    with pytest.raises(LookupError):
        chat(database, first, chat_id)


def test_user_preference_survives_with_second_chat_source(database):
    first, _second = users(database)
    update = MemoryUpdate(
        notes=[
            MemoryNote(
                scope="user",
                text="Prefers concise reports",
                source_quote="I prefer concise reports",
            )
        ]
    )
    chats = []
    for _ in range(2):
        chat_id, message_id = add_user_message(database, first, "I prefer concise reports", None)
        save_memory_update(
            database,
            first,
            "ask",
            chat_id,
            message_id,
            "I prefer concise reports",
            update,
        )
        chats.append(chat_id)
    delete_chat(database, first, chats[0])
    assert memory_context(database, first, "ask", chats[1])["user_preferences"] == [
        "Prefers concise reports"
    ]


def test_memory_rejects_note_without_quote_in_source(database):
    first, _second = users(database)
    chat_id, message_id = add_user_message(database, first, "What happened?", None)
    update = MemoryUpdate(
        notes=[MemoryNote(scope="chat", text="The order was approved", source_quote="approved")]
    )
    save_memory_update(database, first, "ask", chat_id, message_id, "What happened?", update)
    assert memory_context(database, first, "ask", chat_id)["facts"] == []


def test_working_memory_compacts_at_twenty_messages(database):
    first, _second = users(database)
    chat_id = None
    for index in range(10):
        chat_id, message_id = add_user_message(database, first, f"Requirement {index}", chat_id)
        add_assistant_message(database, first, chat_id, f"Question {index}")
    assert memory_context(database, first, "ask", chat_id)["pending_message_count"] == 20
    save_memory_update(
        database,
        first,
        "ask",
        chat_id,
        message_id,
        "Requirement 9",
        MemoryUpdate(working_compaction="Requirements remain under discussion."),
    )
    assert memory_context(database, first, "ask", chat_id)["pending_message_count"] == 0
    assert memory_context(database, first, "ask", chat_id)["final_summary"] == ""


def test_second_admin_can_update_shared_onboarding_summary(database):
    first, second = users(database)
    save_memory_update(
        database,
        first,
        "onboarding",
        first.organization_id,
        uuid4(),
        "",
        MemoryUpdate(),
        confirmed_summary="First confirmed requirements",
    )
    save_memory_update(
        database,
        second,
        "onboarding",
        first.organization_id,
        uuid4(),
        "",
        MemoryUpdate(),
        confirmed_summary="Revised confirmed requirements",
    )
    assert (
        memory_context(database, first, "onboarding", first.organization_id)["final_summary"]
        == "Revised confirmed requirements"
    )


def test_final_summary_waits_for_resolved_questions_and_compaction_preserves_notes(database):
    first, _second = users(database)
    chat_id, first_id = add_user_message(database, first, "The contract needs review", None)
    fact = MemoryNote(scope="chat", text="Contract needs review", source_quote="needs review")
    save_memory_update(
        database,
        first,
        "ask",
        chat_id,
        first_id,
        "The contract needs review",
        MemoryUpdate(notes=[fact], requirements_complete=True, final_summary="Final contract rule"),
        unresolved_questions=["Who reviews it?"],
    )
    assert memory_context(database, first, "ask", chat_id)["final_summary"] == ""
    second_id = add_user_message(database, first, "Legal reviews it", chat_id)[1]
    save_memory_update(
        database,
        first,
        "ask",
        chat_id,
        second_id,
        "Legal reviews it",
        MemoryUpdate(
            notes=[
                MemoryNote(scope="chat", text="Legal reviews it", source_quote="Legal reviews it")
            ],
            working_compaction="Contract review remains open.",
        ),
    )
    compacted = memory_context(database, first, "ask", chat_id)
    assert compacted["working_compaction"] == "Contract review remains open."
    assert compacted["facts"] == ["Legal reviews it"]
    third_id = add_user_message(database, first, "That is final", chat_id)[1]
    save_memory_update(
        database,
        first,
        "ask",
        chat_id,
        third_id,
        "That is final",
        MemoryUpdate(requirements_complete=True, final_summary="Legal reviews contracts."),
    )
    assert memory_context(database, first, "ask", chat_id)["final_summary"] == (
        "Legal reviews contracts."
    )


def test_organization_ask_persists_and_deletes_chat(database, monkeypatch, tmp_path):
    first, _second = users(database)
    database.execute(
        "UPDATE users SET password_hash = %s WHERE id = %s",
        (password_hash("fixture admin password"), first.actor.user_id),
    )
    database.commit()
    scoped_url = psycopg.conninfo.make_conninfo(
        os.environ["AUSTRA_TEST_DATABASE_URL"],
        options=database.info.get_parameters()["options"],
    )
    monkeypatch.setenv("AUSTRA_DATABASE_URL", scoped_url)
    monkeypatch.setenv("AUSTRA_SUPER_ADMIN_USERNAME", "superadmin")
    monkeypatch.setenv("AUSTRA_SUPER_ADMIN_PASSWORD", "fixture platform password")
    monkeypatch.setenv("AUSTRA_LEGACY_ORGANIZATION_SLUG", "legacy")
    client = TestClient(create_app(tmp_path), base_url="http://memory-test.localhost:8000")
    assert (
        client.post(
            "/api/login", json={"username": "first", "password": "fixture admin password"}
        ).status_code
        == 200
    )
    headers = {"X-Austra-Request": "1"}
    first_reply = client.post(
        "/api/org/ask", headers=headers, json={"question": "How many active records?"}
    )
    assert first_reply.status_code == 200
    chat_id = first_reply.json()["chat"]["id"]
    second_reply = client.post(
        "/api/org/ask",
        headers=headers,
        json={"question": "How many active records now?", "chat_id": chat_id},
    )
    assert second_reply.status_code == 200
    streamed = client.post(
        "/api/org/ask/stream",
        headers=headers,
        json={"question": "How many active records now?", "chat_id": chat_id},
    )
    assert streamed.status_code == 200
    assert "event: accepted" in streamed.text
    assert "event: done" in streamed.text
    assert len(client.get(f"/api/org/chats/{chat_id}").json()["chat"]["messages"]) == 6
    monkeypatch.setattr(
        "austra.retrieval.routes.ModelCredentials.get", lambda *_args: "fixture-key"
    )
    monkeypatch.setattr(
        "austra.retrieval.routes.extract_memory_update", lambda *_args, **_kwargs: MemoryUpdate()
    )

    def fail_memory_update(*_args, **_kwargs):
        raise RuntimeError("memory unavailable")

    monkeypatch.setattr("austra.retrieval.routes.save_memory_update", fail_memory_update)
    after_memory_failure = client.post(
        "/api/org/ask/stream",
        headers=headers,
        json={"question": "How many active records now?", "chat_id": chat_id},
    )
    assert "event: done" in after_memory_failure.text
    assert "event: error" not in after_memory_failure.text
    assert len(client.get(f"/api/org/chats/{chat_id}").json()["chat"]["messages"]) == 8
    assert client.delete(f"/api/org/chats/{chat_id}", headers=headers).status_code == 200
    assert client.get(f"/api/org/chats/{chat_id}").status_code == 404
