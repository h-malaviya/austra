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
    requests = []

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
    def create(**kwargs):
        requests.append(kwargs)
        return response

    model_client = SimpleNamespace(messages=SimpleNamespace(create=create))

    with observability.trace_turn(
        "chat_message",
        organization_id="org-1",
        organization_name="Acme",
        user_id="user-1",
        chat_id="chat-1",
        message_id="message-1",
        source="ui",
    ):
        assert (
            observability.observed_message(
                model_client,
                "structured_reply",
                model="claude-sonnet-5",
                max_tokens=100,
                messages=[{"role": "user", "content": "Private business data"}],
            )
            is response
        )

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
    assert requests[0]["cache_control"] == {"type": "ephemeral"}
    exported = [
        {"attributes": observation.attributes, "updates": observation.updates}
        for observation in langfuse.observations
    ]
    assert "Private business data" not in repr(exported)


def test_unconfigured_observability_still_enables_prompt_cache(monkeypatch):
    monkeypatch.setattr(observability, "_client", lambda: None)
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return "reply"

    model_client = SimpleNamespace(messages=SimpleNamespace(create=create))
    with observability.trace_turn("chat_message", message_id="message-1"):
        assert (
            observability.observed_message(
                model_client, "structured_reply", model="claude-sonnet-5"
            )
            == "reply"
        )
    assert calls == [{"model": "claude-sonnet-5", "cache_control": {"type": "ephemeral"}}]


def test_cache_control_applies_to_streamed_calls_and_prompt_changes(monkeypatch):
    monkeypatch.setattr(observability, "_client", lambda: None)
    requests = []
    chunks = []

    class Stream:
        def __init__(self):
            self.text_stream = iter(["Hello", " there"])

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get_final_message(self):
            return "complete"

    def stream(**kwargs):
        requests.append(kwargs)
        return Stream()

    model_client = SimpleNamespace(messages=SimpleNamespace(stream=stream))
    first = observability.observed_message(
        model_client, "discovery_reply", on_text=chunks.append,
        model="claude-sonnet-5", max_tokens=100, system="Prompt version 1",
    )
    second = observability.observed_message(
        model_client, "discovery_reply", on_text=chunks.append,
        model="claude-sonnet-5", max_tokens=100, system="Prompt version 2",
    )

    assert first == second == "complete"
    assert chunks == ["Hello", " there", "Hello", " there"]
    assert [request["system"] for request in requests] == ["Prompt version 1", "Prompt version 2"]
    assert all(request["cache_control"] == {"type": "ephemeral"} for request in requests)


def test_model_error_is_not_swallowed(monkeypatch):
    monkeypatch.setattr(observability, "_client", lambda: LangfuseStub())

    def fail(**_kwargs):
        raise RuntimeError("provider unavailable")

    model_client = SimpleNamespace(messages=SimpleNamespace(create=fail))
    with pytest.raises(RuntimeError, match="provider unavailable"):
        observability.observed_message(
            model_client, "structured_reply", model="claude-sonnet-5", max_tokens=100
        )
