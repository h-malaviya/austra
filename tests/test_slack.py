import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from austra.web import create_app


def _signed(body: bytes, secret: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    digest = hmac.new(
        secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    return {"X-Slack-Request-Timestamp": timestamp, "X-Slack-Signature": f"v0={digest}"}


def test_signed_slack_event_is_deduplicated(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "test-signing-secret")
    monkeypatch.setenv("SLACK_ALLOWED_TEAM_ID", "T123")
    client = TestClient(create_app(tmp_path / "business.db"))
    payload = {
        "type": "event_callback",
        "team_id": "T123",
        "event_id": "Ev123",
        "event": {"type": "app_mention", "user": "U123", "text": "Need quartz"},
    }
    body = json.dumps(payload).encode()
    headers = _signed(body, "test-signing-secret")

    first = client.post("/integrations/slack/events", content=body, headers=headers)
    repeated = client.post("/integrations/slack/events", content=body, headers=headers)
    rejected = client.post("/integrations/slack/events", content=body)

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert rejected.status_code == 403
    assert len(client.get("/api/cases").json()) == 1
