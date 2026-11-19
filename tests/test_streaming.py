"""Streaming preserves the saved reply and hides unfinished JSON fields."""

from contextlib import contextmanager
from types import SimpleNamespace

from fastapi.testclient import TestClient

from austra.agent import AgentReply
from austra.observability import _create_message
from austra.reply_stream import ReplyStream
from austra.web import create_app


def test_reply_stream_decodes_split_escapes_without_exposing_other_fields():
    chunks = []
    decoder = ReplyStream(chunks.append)
    for piece in ('{"rep', 'ly":"First\\', 'nline \\"two\\', '"","title":"Private"}'):
        decoder.feed(piece)
    assert "".join(chunks) == 'First\nline "two"'
    assert "Private" not in "".join(chunks)


def test_observed_message_uses_provider_stream_when_available(monkeypatch):
    from austra import observability

    monkeypatch.setattr(observability, "_client", lambda: None)
    final = SimpleNamespace(content=[SimpleNamespace(text='{"reply":"Done"}')])

    class Stream:
        text_stream = iter(['{"reply":"D', 'one"}'])

        def get_final_message(self):
            return final

    @contextmanager
    def stream(**_kwargs):
        yield Stream()

    model = SimpleNamespace(messages=SimpleNamespace(stream=stream))
    chunks = []
    result = _create_message(model, chunks.append, {"model": "test-model"})
    assert result is final
    assert "".join(chunks) == '{"reply":"Done"}'


def test_background_job_replays_stage_and_completion_events(tmp_path):
    client = TestClient(
        create_app(tmp_path, planner=lambda _files, _access, _chat: AgentReply(reply="Finished"))
    )
    client.post("/api/setup", json={"password": "long private passphrase"})
    submitted = client.post("/api/messages/background", data={"body": "Hello"})
    job_id = submitted.json()["job"]["id"]
    response = client.get(f"/api/jobs/{job_id}/events")
    assert response.status_code == 200
    assert "event: stage" in response.text
    assert 'event: done\ndata: {"status": "completed", "error": null}' in response.text
    assert (
        client.get(f"/api/chats/{submitted.json()['chat']['id']}").json()["chat"]["messages"][-1][
            "body"
        ]
        == "Finished"
    )
