from fastapi.testclient import TestClient

from austra.agent import AgentReply
from austra.files import Files
from austra.memory import MemoryNote
from austra.web import create_app


def test_chat_deletion_erases_memory_but_preserves_approved_files_and_audit(tmp_path):
    client = TestClient(
        create_app(
            tmp_path,
            planner=lambda _files, _access, _chat: AgentReply(reply="Remembered"),
        )
    )
    client.post("/api/setup", json={"password": "long private passphrase"})
    created = client.post("/api/chats", json={"body": "Our factory is in Pune"}).json()
    chat_id = created["chat"]["id"]
    files = Files(tmp_path)
    draft = files.add_drafts(
        chat_id,
        [
            {
                "kind": "policy",
                "name": "factory_location",
                "summary": "Factory location",
                "content": "The factory is in Pune.",
            }
        ],
    )[0]
    files.review(draft["id"], "approved")
    assert client.delete(f"/api/chats/{chat_id}").json() == {
        "deleted": True,
        "audit_preserved": True,
    }
    assert client.get("/api/chats").json() == []
    assert client.get(f"/api/chats/{chat_id}").status_code == 404
    assert not (tmp_path / "memory" / "short_term" / f"{chat_id}.json").exists()
    assert files.audit_chat(chat_id)["deleted_at"]
    assert client.get(f"/api/audit/chats/{chat_id}").json()["messages"][0]["body"] == (
        "Our factory is in Pune"
    )
    assert files.approved()[0]["name"] == "factory_location"
    assert any(item["action"] == "chat.deleted" for item in client.get("/api/audit").json())


def test_audit_requires_admin_and_omits_credential_value(tmp_path):
    client = TestClient(create_app(tmp_path, anthro_verifier=lambda _key: None))
    assert client.get("/api/audit").status_code == 401
    client.post("/api/setup", json={"password": "long private passphrase"})
    client.post(
        "/api/credentials/anthropic",
        json={
            "values": {"ANTHROPIC_API_KEY": "private-test-value"},
        },
    )
    assert "private-test-value" not in client.get("/api/audit").text


def test_delete_legacy_chat_does_not_recreate_memory_snapshot(tmp_path):
    files = Files(tmp_path)
    chat = files.create_chat("Legacy fact", "Admin")
    memory_path = tmp_path / "memory" / "short_term" / f"{chat['id']}.json"
    memory_path.unlink()
    assert "Legacy fact" in files.chat(chat["id"])["messages"][0]["body"]
    files.delete_chat(chat["id"])
    assert not memory_path.exists()
    assert files.audit_chat(chat["id"])["messages"][0]["body"] == "Legacy fact"


def test_user_memory_survives_only_with_another_live_source(tmp_path):
    files = Files(tmp_path)
    first = files.create_chat("I prefer concise reports", "Admin")
    second = files.create_chat("I prefer concise reports", "Admin")
    note = {
        "scope": "user",
        "text": "Prefers concise reports",
        "source_quote": "I prefer concise reports",
    }
    for chat in (first, second):
        files.remember(
            chat["id"], chat["messages"][0]["id"], "local_admin",
            "I prefer concise reports", [note],
        )
    assert len(files.user_long_term_memory("local_admin")) == 1
    files.delete_chat(first["id"])
    assert len(files.user_long_term_memory("local_admin")[0]["sources"]) == 1
    files.delete_chat(second["id"])
    assert files.user_long_term_memory("local_admin") == []


def test_chat_memory_rejects_ungrounded_notes_and_erases_summary(tmp_path):
    files = Files(tmp_path)
    chat = files.create_chat("The order requires approval", "Admin")
    files.remember(
        chat["id"], chat["messages"][0]["id"], "local_admin",
        "The order requires approval",
        [
            {"scope": "chat", "text": "Approval is required", "source_quote": "requires approval"},
            {"scope": "chat", "text": "Invented", "source_quote": "not in the message"},
        ],
        "The approval requirement is final.",
    )
    assert files.chat_long_term_memory(chat["id"])["facts"][0]["text"] == "Approval is required"
    assert len(files.chat_long_term_memory(chat["id"])["facts"]) == 1
    files.delete_chat(chat["id"])
    assert not (tmp_path / "memory" / "long_term" / "chats" / f"{chat['id']}.json").exists()


def test_agent_updates_memory_only_on_conclusion(tmp_path):
    complete = False

    def planner(_files, _access, _chat):
        return AgentReply(
            reply="I noted your preference.",
            memory_notes=[MemoryNote(
                scope="user", text="Prefers concise reports",
                source_quote="I prefer concise reports",
            )],
            requirements_complete=complete,
            final_summary="Confirmed concise reporting requirement." if complete else "",
        )

    client = TestClient(create_app(tmp_path, planner=planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    first = client.post("/api/chats", json={"body": "I prefer concise reports"}).json()
    files = Files(tmp_path)
    chat_id = first["chat"]["id"]
    assert files.user_long_term_memory("local_admin")[0]["text"] == "Prefers concise reports"
    assert files.chat_long_term_memory(chat_id)["final_summary"] == ""
    complete = True
    client.post(f"/api/chats/{chat_id}/messages", json={"body": "I prefer concise reports"})
    assert files.chat_long_term_memory(chat_id)["final_summary"] == (
        "Confirmed concise reporting requirement."
    )


def test_message_count_does_not_trigger_final_summary(tmp_path):
    files = Files(tmp_path)
    chat = files.create_chat("First requirement", "Admin")
    for number in range(20):
        files.add_message(chat["id"], "user", f"Detail {number}", "Admin")
    assert len(files.chat(chat["id"])["messages"]) == 21
    assert files.chat_long_term_memory(chat["id"])["final_summary"] == ""


def test_message_deletion_endpoint_is_unavailable(tmp_path):
    client = TestClient(
        create_app(tmp_path, planner=lambda _files, _access, _chat: AgentReply(reply="Saved"))
    )
    client.post("/api/setup", json={"password": "long private passphrase"})
    result = client.post("/api/chats", json={"body": "Business note"}).json()
    chat_id = result["chat"]["id"]
    message_id = result["chat"]["messages"][0]["id"]

    assert client.delete(f"/api/chats/{chat_id}/messages/{message_id}").status_code == 404
    assert [
        item["body"] for item in client.get(f"/api/chats/{chat_id}").json()["chat"]["messages"]
    ] == ["Business note", "Saved"]
