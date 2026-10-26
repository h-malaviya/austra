import hashlib
import threading
import time

from fastapi.testclient import TestClient

from austra.agent import AgentReply
from austra.web import create_app


def test_multi_file_submission_analyzes_once_and_skips_reuploads(tmp_path):
    analyzed = []

    def planner(_files, _access, chat_id):
        analyzed.append(chat_id)
        return AgentReply(reply="Files received")

    client = TestClient(create_app(tmp_path, planner=planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    first = client.post(
        "/api/messages",
        data={"body": "Read these reports"},
        files=[
            ("uploads", ("one.csv", b"name,value\nalpha,1", "text/csv")),
            ("uploads", ("two.csv", b"name,value\nbeta,2", "text/csv")),
        ],
    )
    assert first.status_code == 200
    chat = first.json()["chat"]
    assert len(analyzed) == 1
    assert len(chat["messages"]) == 2
    assert chat["messages"][0]["display_body"] == "Read these reports"
    assert [item["name"] for item in chat["messages"][0]["attachments"]] == [
        "one.csv",
        "two.csv",
    ]
    assert "alpha,1" in chat["messages"][0]["body"]
    assert len(list((tmp_path / "evidence").iterdir())) == 2

    duplicate = client.post(
        "/api/messages",
        data={"chat_id": chat["id"]},
        files=[("uploads", ("renamed.csv", b"name,value\nalpha,1", "text/csv"))],
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["unchanged"] is True
    assert duplicate.json()["duplicate_files"] == ["renamed.csv"]
    assert len(duplicate.json()["chat"]["messages"]) == 2
    assert len(analyzed) == 1
    assert len(list((tmp_path / "evidence").iterdir())) == 2

    legacy_route = client.post(
        f"/api/chats/{chat['id']}/attachments",
        files={"upload": ("one.csv", b"name,value\nalpha,1", "text/csv")},
    )
    assert legacy_route.json()["unchanged"] is True
    assert len(analyzed) == 1


def test_file_only_submission_and_duplicate_in_same_batch(tmp_path):
    client = TestClient(
        create_app(
            tmp_path,
            planner=lambda _files, _access, _chat: AgentReply(reply="Reviewed"),
        )
    )
    client.post("/api/setup", json={"password": "long private passphrase"})
    content = b"item,quantity\nquartz,3"
    response = client.post(
        "/api/messages",
        files=[
            ("uploads", ("stock.csv", content, "text/csv")),
            ("uploads", ("copy.csv", content, "text/csv")),
        ],
    )
    assert response.status_code == 200
    result = response.json()
    assert result["duplicate_files"] == ["copy.csv"]
    assert result["chat"]["title"] == "Uploaded stock.csv"
    assert result["chat"]["messages"][0]["display_body"] == ""
    assert len(result["chat"]["messages"][0]["attachments"]) == 1
    assert (tmp_path / "evidence" / hashlib.sha256(content).hexdigest()).exists()


def test_invalid_attachment_batch_does_not_create_chat_or_evidence(tmp_path):
    client = TestClient(
        create_app(
            tmp_path,
            planner=lambda _files, _access, _chat: AgentReply(reply="Reviewed"),
        )
    )
    client.post("/api/setup", json={"password": "long private passphrase"})
    response = client.post(
        "/api/messages",
        data={"body": "Review files"},
        files=[
            ("uploads", ("good.csv", b"item\none", "text/csv")),
            ("uploads", ("bad.exe", b"unsupported", "application/octet-stream")),
        ],
    )
    assert response.status_code == 400
    assert client.get("/api/chats").json() == []
    assert not (tmp_path / "evidence").exists()


def test_attachment_stays_downloadable_in_chat_and_audit(tmp_path):
    client = TestClient(
        create_app(tmp_path, planner=lambda _files, _access, _chat: AgentReply(reply="Received"))
    )
    client.post("/api/setup", json={"password": "long private passphrase"})
    content = b"item,quantity\nquartz,3"
    result = client.post(
        "/api/messages", files={"uploads": ("stock.csv", content, "text/csv")}
    ).json()
    chat_id = result["chat"]["id"]
    user_message = result["chat"]["messages"][0]
    digest = user_message["attachments"][0]["id"]
    url = f"/api/chats/{chat_id}/attachments/{digest}"

    assert client.get(f"/api/chats/{chat_id}").json()["chat"]["messages"][0]["attachments"]
    assert client.get(url).content == content
    assert client.delete(f"/api/chats/{chat_id}").status_code == 200
    assert client.get(url).status_code == 404
    assert client.get(f"/api/audit/chats/{chat_id}/attachments/{digest}").content == content


def test_background_message_reports_running_time_and_completion(tmp_path):
    started = threading.Event()
    release = threading.Event()

    def planner(_files, _access, _chat):
        started.set()
        assert release.wait(5)
        return AgentReply(reply="Finished processing")

    client = TestClient(create_app(tmp_path, planner=planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    response = client.post("/api/messages/background", data={"body": "Review this"})
    assert response.status_code == 202
    job = response.json()["job"]
    assert started.wait(2)
    assert job["status"] == "running"
    assert client.get("/api/jobs").json()[0]["chat_id"] == response.json()["chat"]["id"]
    assert client.get(f"/api/jobs/{job['id']}").json()["elapsed_seconds"] >= 0
    release.set()
    for _ in range(100):
        result = client.get(f"/api/jobs/{job['id']}").json()
        if result["status"] == "completed":
            break
        time.sleep(0.02)
    assert result["status"] == "completed"
    assert client.get("/api/jobs").json() == []
    assert (
        client.get(f"/api/chats/{job['chat_id']}").json()["chat"]["messages"][-1]["body"]
        == "Finished processing"
    )
