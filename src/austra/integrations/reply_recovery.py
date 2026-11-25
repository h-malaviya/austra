"""Repair a matched Gmail reply read without replaying its outbound email."""

from collections.abc import Callable
from datetime import datetime, timedelta

from pydantic import TypeAdapter, ValidationError

from austra.integrations.gmail_content import GmailReplyContent
from austra.mcp_gateway import ExternalContractError, ExternalToolError

CONTRACT = TypeAdapter(GmailReplyContent)
MAX_CONTENT_BYTES = 256_000
MAX_ATTEMPTS = 5
READ_BATCH_SIZE = 4
RETRY_DELAY = timedelta(minutes=1)


def _read_reply(call_tool: Callable, reply: dict, credentials: dict):
    arguments = {"message_id": reply["message_id"], "thread_id": reply["thread_id"]}
    try:
        result = call_tool("gmail_read_reply_content", arguments, credentials)
    except ExternalContractError as error:
        if not error.legacy_text or len(error.legacy_text) > MAX_CONTENT_BYTES:
            raise
        return CONTRACT.validate_json(error.legacy_text, strict=True), "legacy_mcp_envelope"
    return CONTRACT.validate_python(result, strict=True), None


def read_pending_replies(
    replies: list[dict], processed: set[str], recoveries: dict, credentials: dict,
    call_tool: Callable, now: datetime, *, manual: bool,
) -> tuple[dict, dict]:
    evidence, changes = {}, {}
    for reply in replies:
        if len(evidence.keys() | changes.keys()) >= READ_BATCH_SIZE:
            break
        message = reply["message_id"]
        previous = recoveries.get(message, {})
        retry_at = previous.get("retry_at")
        if message in processed or (not manual and (
            previous.get("state") == "needs_review"
            or (retry_at and datetime.fromisoformat(retry_at) > now)
        )):
            continue
        attempts = previous.get("attempts", 0) + 1
        try:
            content, repair = _read_reply(call_tool, reply, credentials)
        except (ExternalContractError, ValidationError):
            issue = "invalid_response"
        except ExternalToolError:
            issue = "provider_unavailable"
        else:
            evidence[message] = content
            if previous or repair:
                changes[message] = {
                    "state": "recovered", "attempts": attempts,
                    "reason": repair or previous["reason"],
                }
            continue
        changes[message] = {
            "state": "needs_review" if attempts >= MAX_ATTEMPTS else "retrying",
            "attempts": attempts, "reason": issue,
            "retry_at": ((now + RETRY_DELAY * 2 ** (attempts - 1)).isoformat()
                         if attempts < MAX_ATTEMPTS else None),
        }
    return evidence, changes
