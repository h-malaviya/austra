"""Metadata-only Langfuse tracing for chat turns and Anthropic generations."""

import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from langfuse import get_client, propagate_attributes

logger = logging.getLogger(__name__)


def _client() -> Any | None:
    if not os.getenv("LANGFUSE_PUBLIC_KEY") or not os.getenv("LANGFUSE_SECRET_KEY"):
        return None
    try:
        return get_client()
    except Exception:
        logger.exception("Langfuse initialization failed")
        return None


@contextmanager
def trace_turn(
    name: str,
    *,
    organization_id: str | None = None,
    organization_name: str | None = None,
    user_id: str | None = None,
    chat_id: str | None = None,
    message_id: str | None = None,
    session_id: str | None = None,
    request_id: str | None = None,
    source: str | None = None,
) -> Iterator[None]:
    client = _client()
    if client is None:
        yield
        return
    metadata = {
        key: value
        for key, value in {
            "organization_id": organization_id,
            "organization_name": organization_name,
            "user_id": user_id,
            "chat_id": chat_id,
            "message_id": message_id,
            "session_id": session_id,
            "request_id": request_id,
            "source": source,
        }.items()
        if value is not None
    }
    with (
        client.start_as_current_observation(name=name, as_type="span"),
        propagate_attributes(
            user_id=user_id,
            session_id=chat_id or session_id,
            metadata=metadata,
            trace_name=name,
        ),
    ):
        yield


def observed_message(
    client: Any, stage: str, *, on_text: Callable[[str], None] | None = None, **kwargs: Any
) -> Any:
    """Call Anthropic and record provider usage without exporting message content."""
    # Anthropic keys cache entries to the exact prompt, so a prompt edit creates a new entry.
    request = {"cache_control": {"type": "ephemeral"}, **kwargs}
    langfuse = _client()
    if langfuse is None:
        return _create_message(client, on_text, request)
    with langfuse.start_as_current_observation(
        name=stage,
        as_type="generation",
        model=request["model"],
        model_parameters={"max_tokens": request["max_tokens"]},
    ) as generation:
        response = _create_message(client, on_text, request)
        usage = getattr(response, "usage", None)
        if usage is not None:
            details = {
                "input": getattr(usage, "input_tokens", 0) or 0,
                "output": getattr(usage, "output_tokens", 0) or 0,
            }
            for provider_key in ("cache_creation_input_tokens", "cache_read_input_tokens"):
                count = getattr(usage, provider_key, 0) or 0
                if count:
                    details[provider_key] = count
            generation.update(
                usage_details=details,
                metadata={"stop_reason": getattr(response, "stop_reason", None)},
            )
        return response


def _create_message(
    client: Any, on_text: Callable[[str], None] | None, kwargs: dict[str, Any]
) -> Any:
    if on_text is None or not callable(getattr(client.messages, "stream", None)):
        return client.messages.create(**kwargs)
    with client.messages.stream(**kwargs) as stream:
        for chunk in stream.text_stream:
            on_text(chunk)
        return stream.get_final_message()
