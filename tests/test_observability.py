"""Langfuse tracing keeps identity and usage while omitting business content."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from austra import observability


class Observation:
    def __init__(self, attributes):
        self.attributes = attributes
        self.updates = []

    def update(self, **attributes):
        self.updates.append(attributes)


class LangfuseStub:
    def __init__(self):
        self.observations = []

    @contextmanager
    def start_as_current_observation(self, **attributes):
        observation = Observation(attributes)
        self.observations.append(observation)
        yield observation


def test_message_trace_records_identity_and_provider_usage_without_content(monkeypatch):
    langfuse = LangfuseStub()
    propagated = []

    @contextmanager
    def capture_attributes(**attributes):
        propagated.append(attributes)
        yield

    monkeypatch.setattr(observability, "_client", lambda: langfuse)
    monkeypatch.setattr(observability, "propagate_attributes", capture_attributes)
    response = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=4,
            cache_creation_input_tokens=3,
            cache_read_input_tokens=2,
        ),
        stop_reason="end_turn",
    )
    model_client = SimpleNamespace(messages=SimpleNamespace(create=lambda **_kwargs: response))

    with observability.trace_turn(
        "chat_message",
        organization_id="org-1",
        organization_name="Acme",
        user_id="user-1",
        chat_id="chat-1",
        message_id="message-1",
        source="ui",
    ):
        assert observability.observed_message(
            model_client,
            "structured_reply",
            model="claude-sonnet-5",
            max_tokens=100,
            messages=[{"role": "user", "content": "Private business data"}],
        ) is response

    assert propagated[0]["user_id"] == "user-1"
    assert propagated[0]["session_id"] == "chat-1"
    assert propagated[0]["metadata"] == {
        "organization_id": "org-1",
        "organization_name": "Acme",
        "user_id": "user-1",
        "chat_id": "chat-1",
        "message_id": "message-1",
        "source": "ui",
    }
    assert langfuse.observations[1].updates[0]["usage_details"] == {
        "input": 10,
        "output": 4,
        "cache_creation_input_tokens": 3,
        "cache_read_input_tokens": 2,
    }
    exported = [
        {"attributes": observation.attributes, "updates": observation.updates}
        for observation in langfuse.observations
    ]
    assert "Private business data" not in repr(exported)


def test_unconfigured_observability_does_not_change_model_calls(monkeypatch):
    monkeypatch.setattr(observability, "_client", lambda: None)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return "reply"

    model_client = SimpleNamespace(messages=SimpleNamespace(create=create))
    with observability.trace_turn("chat_message", message_id="message-1"):
        assert observability.observed_message(
            model_client, "structured_reply", model="claude-sonnet-5"
        ) == "reply"
    assert calls == [{"model": "claude-sonnet-5"}]


def test_model_error_is_not_swallowed(monkeypatch):
    monkeypatch.setattr(observability, "_client", lambda: LangfuseStub())

    def fail(**_kwargs):
        raise RuntimeError("provider unavailable")

    model_client = SimpleNamespace(messages=SimpleNamespace(create=fail))
    with pytest.raises(RuntimeError, match="provider unavailable"):
        observability.observed_message(
            model_client, "structured_reply", model="claude-sonnet-5", max_tokens=100
        )
