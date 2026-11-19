"""Continue business discovery after the company welcome conversation."""

import json
import logging
import re
from collections.abc import Callable
from time import monotonic
from typing import Any

from anthropic import Anthropic, APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, Field, ValidationError

from austra.memory import MEMORY_COMPACTION_MESSAGES
from austra.observability import observed_message
from austra.onboarding.conversation import MODEL, OUTPUT_TOKEN_LIMITS
from austra.reply_stream import ReplyStream

logger = logging.getLogger(__name__)
MAX_REPLY_CHARS = 1200
MAX_QUESTIONS = 30
WORKING_CONTEXT_CHARS = 80000
MODEL_TIMEOUT_SECONDS = 240


class DiscoveryTurn(BaseModel):
    reply: str = Field(min_length=1, max_length=MAX_REPLY_CHARS)
    working_summary: str
    unresolved_questions: list[str] = Field(max_length=MAX_QUESTIONS)
    resolved_questions: list[str] = Field(default_factory=list, max_length=MAX_QUESTIONS)


DISCOVERY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": list(DiscoveryTurn.model_fields),
    "properties": {
        "reply": {"type": "string"},
        "working_summary": {"type": "string"},
        "unresolved_questions": {"type": "array", "items": {"type": "string"}},
        "resolved_questions": {"type": "array", "items": {"type": "string"}},
    },
}

DISCOVERY_SYSTEM = (
    "You are Austra, helping an organization administrator describe a business process. "
    "Speak in ordinary business language and ask at most one focused question per reply. "
    "Do not repeat a full recap every turn; recap only when asked or when ready for review. "
    "Preserve uncertainty, corrections, conflicting accounts, and who supplied each fact. "
    "Treat all unapproved stages, attribution methods, and matching rules as proposals. "
    "Do not turn a suggestion into an approved rule. Distinguish commercial approval, "
    "permission to start work, contract acceptance, and permission to invoice. Do not claim "
    "that an app supports an action without verified account evidence. Never claim that a "
    "workflow was approved, built, tested, activated, or handed to another team. You have no "
    "connection, Docker, or workflow execution tools in this conversation. If asked to run or "
    "inspect one, say this conversation cannot do that and point to the relevant setup or "
    "preview page. The application saves this conversation, but no workflow is created from "
    "your reply. Keep reported facts and unresolved questions available. Carry "
    "forward earlier conflicts from the supplied state. List a previous question in "
    "resolved_questions only when the administrator explicitly answered it, using its exact "
    "previous text. A question about what this chat "
    "actually saved is answered by the application, not by inference. Provider accounts and "
    "paid sandboxes are not prerequisites for describing or previewing a workflow with sample "
    "data. Say that a real provider connection remains untested when that is the case. "
    "Rewrite working_summary only when compaction_needed is true. Otherwise copy the "
    "previous working_summary unchanged. During compaction retain reported facts, "
    "corrections, conflicts, and open decisions. Do not write a final requirements "
    "summary until requirements are complete. "
    f"Keep reply to at most {MAX_REPLY_CHARS} characters and each question list to at most "
    f"{MAX_QUESTIONS} items."
)

UNVERIFIED_PROGRESS = re.compile(
    r"\b(?:locked in|handed off|handoff (?:created|completed)|"
    r"i(?:'|’)ll (?:check|get back)|i will (?:check|get back)|"
    r"workflow (?:is|has been) (?:approved|activated|built|tested))\b",
    re.IGNORECASE,
)


def grounded_discovery_turn(turn: DiscoveryTurn) -> DiscoveryTurn:
    if not UNVERIFIED_PROGRESS.search(turn.reply):
        return turn
    question = (
        turn.unresolved_questions[0] if turn.unresolved_questions else "What remains unclear?"
    )
    return turn.model_copy(
        update={
            "reply": (
                "These are working notes. No workflow has been approved, built, or run from "
                f"this conversation. {question}"
            )
        }
    )


def merge_unresolved(previous: list[str], turn: DiscoveryTurn) -> list[str]:
    resolved = set(turn.resolved_questions)
    remaining = [question for question in previous if question not in resolved]
    for question in turn.unresolved_questions:
        if question not in remaining:
            remaining.append(question)
    return remaining


def generate_discovery_turn(
    key: str,
    organization_name: str,
    company_description: str,
    history: list[dict[str, str]],
    state: dict[str, Any],
    on_text: Callable[[str], None] | None = None,
) -> DiscoveryTurn:
    compacted_through = max(0, int(state.get("compacted_through", 0)))
    pending_history = history[compacted_through:]
    context = {
        "organization_name": organization_name,
        "company_description": company_description,
        "previous_discovery_state": state,
        "compaction_needed": (
            sum(len(item["body"]) for item in pending_history) >= WORKING_CONTEXT_CHARS
            or len(pending_history) >= MEMORY_COMPACTION_MESSAGES
        ),
    }
    messages = [{"role": item["role"], "content": item["body"]} for item in pending_history]
    correction = ""
    started_at = monotonic()
    try:
        client = Anthropic(api_key=key, timeout=MODEL_TIMEOUT_SECONDS, max_retries=0)
        for max_tokens in OUTPUT_TOKEN_LIMITS:
            streamed_reply = ReplyStream(on_text) if on_text else None
            response = observed_message(
                client,
                "discovery_reply",
                on_text=streamed_reply.feed if streamed_reply else None,
                model=MODEL,
                max_tokens=max_tokens,
                system=DISCOVERY_SYSTEM
                + correction
                + "\n\nApplication context: "
                + json.dumps(context, ensure_ascii=False),
                messages=messages,
                output_config={"format": {"type": "json_schema", "schema": DISCOVERY_SCHEMA}},
            )
            if response.stop_reason == "max_tokens":
                continue
            if response.stop_reason != "end_turn":
                raise RuntimeError("Austra did not finish its discovery reply. Please try again.")
            blocks = [block.text for block in response.content if block.type == "text"]
            if len(blocks) != 1:
                raise RuntimeError("Austra returned an unexpected discovery reply.")
            try:
                turn = grounded_discovery_turn(DiscoveryTurn.model_validate_json(blocks[0]))
                if streamed_reply and turn.reply.startswith(streamed_reply.sent):
                    remainder = turn.reply[len(streamed_reply.sent) :]
                    if remainder:
                        on_text(remainder)
                return turn
            except ValidationError as error:
                invalid_fields = [
                    ".".join(map(str, item["loc"])) for item in error.errors(include_input=False)
                ]
                logger.warning("Discovery reply validation failed: fields=%s", invalid_fields)
                correction = (
                    "\n\nThe previous response failed local validation for: "
                    + ", ".join(invalid_fields)
                    + ". Generate a new, shorter structured response within the stated limits."
                )
    except (APIConnectionError, APIStatusError, APITimeoutError) as error:
        logger.warning(
            "Discovery reply failed: error=%s status=%s elapsed_seconds=%.1f",
            type(error).__name__,
            getattr(error, "status_code", None),
            monotonic() - started_at,
        )
        raise RuntimeError(
            "Austra could not continue the conversation. Please try again."
        ) from error
    if correction:
        raise RuntimeError("Austra could not continue the conversation. Please try again.")
    raise RuntimeError("Austra did not finish its discovery reply. Please try again.")
