import json
from subprocess import CalledProcessError, CompletedProcess

import pytest
from fastapi.testclient import TestClient

from austra.agent import AgentReply, Draft
from austra.files import Files
from austra.infrastructure import Infrastructure, InfrastructureError
from austra.web import create_app


class FakeInfrastructure:
    def __init__(self):
        self.changes = []
        self.fail = False

    def change(self, service, action):
        if self.fail:
            raise InfrastructureError("Compose unavailable")
        self.changes.append((service, action))
        return "running" if action == "start" else "exited"

    def status(self):
        return {"redis": "stopped", "mongo": "stopped", "qdrant": "stopped"}


def test_infrastructure_draft_requires_review_and_records_actor_skills(tmp_path, monkeypatch):
    controller = FakeInfrastructure()
    monkeypatch.setattr("austra.web.Infrastructure", lambda: controller)

    def planner(_files, _access, _chat_id):
        return AgentReply(
            reply="Redis is proposed for review.",
            drafts=[
                Draft(
                    kind="infrastructure",
                    name="redis",
                    summary="Cache for approved flow",
                    content=json.dumps({"service": "redis", "action": "start"}),
                )
            ],
        )

    client = TestClient(create_app(tmp_path, planner=planner))
    client.post("/api/setup", json={"password": "long private passphrase"})
    result = client.post("/api/chats", json={"body": "Request cache"}).json()
    draft = result["drafts"][0]
    assert controller.changes == []
    controller.fail = True
    assert (
        client.post(f"/api/drafts/{draft['id']}/review", json={"decision": "approved"}).status_code
        == 503
    )
    assert (
        client.get(f"/api/chats/{result['chat']['id']}").json()["drafts"][0]["status"] == "proposed"
    )
    controller.fail = False
    assert (
        client.post(f"/api/drafts/{draft['id']}/review", json={"decision": "approved"}).status_code
        == 200
    )
    assert controller.changes == [("redis", "start")]
    events = client.get("/api/audit").json()
    changed = next(item for item in events if item["action"] == "infrastructure.changed")
    assert changed["actor"] == "Admin"
    assert changed["details"]["action"] == "start"
    assert "structure_authoring" in changed["skills_used"]
    assert any(item["action"] == "infrastructure.failed" for item in events)


def test_infrastructure_rejects_arbitrary_service_and_command(tmp_path):
    files = Files(tmp_path)
    chat = files.create_chat("Add a service", "Admin")
    with pytest.raises(ValueError, match="allowed service"):
        files.add_drafts(
            chat["id"],
            [
                {
                    "kind": "infrastructure",
                    "name": "docker",
                    "summary": "Unsafe",
                    "content": json.dumps({"service": "docker", "action": "start"}),
                }
            ],
        )
    assert files.drafts() == []


def test_compose_commands_are_fixed_and_failure_is_sanitized(tmp_path, monkeypatch):
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text("services: {}", encoding="utf-8")
    controller = Infrastructure(compose_file)
    commands = []

    def run(command, **_options):
        commands.append(command)
        if "ps" in command:
            return CompletedProcess(command, 0, '[{"Service":"redis","State":"running"}]')
        return CompletedProcess(command, 0, "")

    monkeypatch.setattr("austra.infrastructure.subprocess.run", run)
    assert controller.change("redis", "start") == "running"
    assert commands[0][-4:] == ["up", "-d", "--no-deps", "redis"]
    with pytest.raises(ValueError, match="Unsupported"):
        controller.change("anything", "start")

    def fail(command, **_options):
        raise CalledProcessError(1, command, stderr="secret from daemon")

    monkeypatch.setattr("austra.infrastructure.subprocess.run", fail)
    with pytest.raises(InfrastructureError, match="Could not stop redis") as error:
        controller.change("redis", "stop")
    assert "secret from daemon" not in str(error.value)
