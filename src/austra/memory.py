"""Conversation memory scope and grounded model-supplied updates."""

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

from anthropic import Anthropic, APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, ConfigDict, Field

from austra.observability import observed_message

_owner: ContextVar[str] = ContextVar("memory_owner", default="local_admin")
logger = logging.getLogger(__name__)
MEMORY_MODEL = "claude-sonnet-5"
MEMORY_RESPONSE_TOKENS = 3000
MEMORY_COMPACTION_CHARS = 20000
MEMORY_COMPACTION_MESSAGES = 20


class MemoryNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["chat", "user"]
    text: str = Field(min_length=1, max_length=500)
    source_quote: str = Field(min_length=1, max_length=500)


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notes: list[MemoryNote] = Field(default_factory=list, max_length=20)
    requirements_complete: bool = False
    final_summary: str = Field(default="", max_length=3000)
    working_compaction: str = Field(default="", max_length=5000)


MEMORY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["notes", "requirements_complete", "final_summary", "working_compaction"],
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["scope", "text", "source_quote"],
                "properties": {
                    "scope": {"type": "string", "enum": ["chat", "user"]},
                    "text": {"type": "string"},
                    "source_quote": {"type": "string"},
                },
            },
        },
        "requirements_complete": {"type": "boolean"},
        "final_summary": {"type": "string"},
        "working_compaction": {"type": "string"},
    },
}


def extract_memory_update(
    key: str,
    latest_user_message: str,
    conversation: list[dict[str, str]],
    working_facts: list[str],
    unresolved_questions: list[str],
    *,
    prior_compaction: str = "",
    compaction_needed: bool = False,
) -> MemoryUpdate:
    """Extract only grounded notes; create a summary only at a clear conclusion."""
    if not key or not latest_user_message.strip():
        return MemoryUpdate()
    payload = {
        "latest_user_message": latest_user_message,
        "recent_conversation": conversation[-20:],
        "working_facts": working_facts,
        "prior_compaction": prior_compaction,
        "compaction_needed": compaction_needed,
        "unresolved_questions": unresolved_questions,
    }
    try:
        client = Anthropic(api_key=key, timeout=45.0, max_retries=0)
        response = observed_message(
            client,
            "memory_update",
            model=MEMORY_MODEL,
            max_tokens=MEMORY_RESPONSE_TOKENS,
            system=(
                "Extract durable memory from the latest user message. Return only facts the "
                "user explicitly supplied. Every note's source_quote must copy exact words "
                "from latest_user_message. Scope=user only for a stable personal preference "
                "or important user information; scope=chat for conversation requirements "
                "and business facts. Omit secrets, credentials, speculation and assistant claims. "
                "Treat later explicit corrections as superseding earlier conflicting facts. "
                "An empty notes list is normal. Do not summarize a routine turn. Set "
                "requirements_complete true and write final_summary only when the user "
                "confirms the requirements or the conversation clearly reaches a complete "
                "conclusion with no material unresolved questions. If anything material is "
                "open, return false and an empty summary. Never treat a status query as completion. "
                "Write working_compaction only when compaction_needed is true; combine the prior "
                "compaction with all working_facts, preserving uncertainties and corrections. "
                "Otherwise return an empty working_compaction."
            ),
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            output_config={"format": {"type": "json_schema", "schema": MEMORY_SCHEMA}},
        )
        if response.stop_reason != "end_turn":
            return MemoryUpdate()
        blocks = [block.text for block in response.content if block.type == "text"]
        if len(blocks) != 1:
            return MemoryUpdate()
        return MemoryUpdate.model_validate_json(blocks[0])
    except (APIConnectionError, APIStatusError, APITimeoutError, ValueError) as error:
        logger.warning("Memory extraction unavailable: %s", type(error).__name__)
        return MemoryUpdate()


@contextmanager
def memory_scope(owner_id: str) -> Iterator[None]:
    token = _owner.set(owner_id)
    try:
        yield
    finally:
        _owner.reset(token)


def current_memory_owner() -> str:
    return _owner.get()
