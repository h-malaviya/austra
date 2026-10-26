from fastapi.testclient import TestClient

from austra.agent import AgentReply
from austra.files import Files
from austra.web import create_app


def test_chat_deletion_preserves_memory_approved_files_and_audit(tmp_path):
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
    memory = (tmp_path / "memory" / "short_term" / f"{chat_id}.json").read_text()
    assert "Our factory is in Pune" in memory
    assert files.recent_memory() == []
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


def test_delete_legacy_chat_creates_memory_snapshot(tmp_path):
    files = Files(tmp_path)
    chat = files.create_chat("Legacy fact", "Admin")
    memory_path = tmp_path / "memory" / "short_term" / f"{chat['id']}.json"
    memory_path.unlink()
    assert files.recent_memory()[-1]["body"] == "Legacy fact"
    files.delete_chat(chat["id"])
    assert "Legacy fact" in memory_path.read_text(encoding="utf-8")
    assert files.recent_memory() == []
    (tmp_path / "chats" / f"{chat['id']}.json").unlink()
    assert files.audit_chat(chat["id"])["messages"][0]["body"] == "Legacy fact"


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
