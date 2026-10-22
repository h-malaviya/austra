import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from austra.agent import AgentReply, ConnectionCheckError, Draft
from austra.web import create_app


def planner(_files, _access, _chat_id):
    return AgentReply(
        reply="I drafted the requested policy for review.",
        drafts=[
            Draft(
                kind="policy",
                name="first_rule",
                summary="Rule from the chat",
                content="# First rule\nAsk the manager before committing.",
            )
        ],
    )


def test_first_run_chat_review_and_authentication(tmp_path):
    client = TestClient(create_app(tmp_path, planner=planner))
    assert client.get("/api/config").json()["admin_ready"] is False
    assert client.get("/api/chats").status_code == 401

    setup = client.post("/api/setup", json={"password": "long private passphrase"})
    assert setup.status_code == 200
    created = client.post("/api/chats", json={"body": "Create our first rule"})
    assert created.status_code == 201
    chat_id = created.json()["chat"]["id"]
    draft = created.json()["drafts"][0]
    assert draft["status"] == "proposed"
    assert client.get("/api/artifacts").json() == []
    approved = client.post(f"/api/drafts/{draft['id']}/review", json={"decision": "approved"})
    assert approved.status_code == 200
    assert (tmp_path / "approved/policy/first_rule/v1.md").exists()
    assert client.get(f"/api/chats/{chat_id}").json()["chat"]["messages"][0]["role"] == "user"


def test_private_credential_endpoint_and_secret_free_responses(tmp_path):
    client = TestClient(create_app(tmp_path, anthro_verifier=lambda _key: None))
    assert (
        client.post(
            "/api/credentials/anthropic", json={"values": {"ANTHROPIC_API_KEY": "private"}}
        ).status_code
        == 401
    )
    client.post("/api/setup", json={"password": "long private passphrase"})
    response = client.post(
        "/api/credentials/anthropic", json={"values": {"ANTHROPIC_API_KEY": "private"}}
    )
    assert response.json() == {"connector": "anthropic", "configured": True, "missing": []}
    assert client.post("/api/credentials/anthropic/test").json() == {"connected": True}
    assert "private" not in json.dumps(client.get("/api/config").json())
    assert client.post("/api/chats", json={"body": "sk-ant-secret"}).status_code == 400


def test_invalid_anthropic_key_is_not_saved_and_can_be_replaced(tmp_path):
    def verify(key):
        if key != "valid-example-key":
            raise ConnectionCheckError("Anthropic rejected this API key.", 400)

    client = TestClient(create_app(tmp_path, anthro_verifier=verify))
    client.post("/api/setup", json={"password": "long private passphrase"})
    bad = client.post(
        "/api/credentials/anthropic", json={"values": {"ANTHROPIC_API_KEY": "wrong-key"}}
    )
    assert bad.status_code == 400
    assert bad.json()["detail"] == "Anthropic rejected this API key."
    assert not (tmp_path / ".env").exists()

    good = client.post(
        "/api/credentials/anthropic",
        json={"values": {"ANTHROPIC_API_KEY": "valid-example-key"}},
    )
    assert good.status_code == 200
    assert client.post("/api/credentials/anthropic/test").json() == {"connected": True}
    assert "valid-example-key" not in json.dumps(client.get("/api/config").json())


def test_saved_bad_key_reports_failure_without_returning_key(tmp_path):
    def verify(_key):
        raise ConnectionCheckError("Anthropic rejected this API key.", 400)

    client = TestClient(create_app(tmp_path, anthro_verifier=verify))
    client.post("/api/setup", json={"password": "long private passphrase"})
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=old-invalid-key\n", encoding="utf-8")
    response = client.post("/api/credentials/anthropic/test")
    assert response.status_code == 400
    assert response.json()["detail"] == "Anthropic rejected this API key."
    assert "old-invalid-key" not in response.text


def test_signed_slack_event_is_added_once(tmp_path):
    client = TestClient(create_app(tmp_path, planner=planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    client.post(
        "/api/credentials/slack", json={"values": {"SLACK_SIGNING_SECRET": "local-signing-key"}}
    )
    payload = {
        "type": "event_callback",
        "event_id": "Ev123",
        "event": {
            "type": "message",
            "channel_type": "im",
            "channel": "D123",
            "ts": "123.456",
            "user": "U123",
            "text": "Please draft a purchasing rule",
        },
    }
    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    base = b"v0:" + timestamp.encode() + b":" + body
    signature = "v0=" + hmac.new(b"local-signing-key", base, hashlib.sha256).hexdigest()
    headers = {"X-Slack-Request-Timestamp": timestamp, "X-Slack-Signature": signature}
    assert client.post("/integrations/slack/events", content=body).status_code == 403
    assert (
        client.post("/integrations/slack/events", content=body, headers=headers).status_code == 200
    )
    assert (
        client.post("/integrations/slack/events", content=body, headers=headers).status_code == 200
    )
    chats = client.get("/api/chats").json()
    assert len(chats) == 1
    assert len(client.get(f"/api/chats/{chats[0]['id']}").json()["chat"]["messages"]) == 2
