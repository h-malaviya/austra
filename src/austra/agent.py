"""Austra planning over user supplied facts and approved workspace files."""

import json
import logging
from typing import Any, Literal

from anthropic import Anthropic, APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from austra.access import Access
from austra.files import Files
from austra.skills import load_skills
from austra.tools import ReadTools

ConnectorName = Literal["anthropic", "gmail", "slack"]
ArtifactName = Literal[
    "policy", "skill", "workflow", "schema", "record", "backend", "frontend", "infrastructure"
]
logger = logging.getLogger(__name__)
FINAL_RESPONSE_TOKENS = 24000
MODEL_TIMEOUT_SECONDS = 240
RESEARCH_RESPONSE_TOKENS = 4096
TOKEN_COUNT_THRESHOLD_CHARS = 100000


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ArtifactName
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    summary: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1)


class RecordChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["update", "delete"]
    scope: Literal["id", "schema", "all"]
    record_id: str = ""
    schema_name: str = ""
    source_attachment: str = ""
    values_json: str = ""
    reason: str = Field(min_length=1, max_length=300)


class RuleChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["activate", "retire"]
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    content: str = ""
    reason: str = Field(min_length=1, max_length=300)


class AgentReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply: str = Field(min_length=1, max_length=10000)
    title: str = Field(default="", max_length=60)
    drafts: list[Draft] = Field(default_factory=list, max_length=50)
    credential_requests: list[ConnectorName] = Field(default_factory=list, max_length=3)
    decision: Literal["allow", "deny"] = "allow"
    save_records: bool = False
    record_changes: list[RecordChange] = Field(default_factory=list, max_length=50)
    rule_changes: list[RuleChange] = Field(default_factory=list, max_length=20)


OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "reply",
        "title",
        "drafts",
        "credential_requests",
        "decision",
        "save_records",
        "record_changes",
        "rule_changes",
    ],
    "properties": {
        "reply": {"type": "string"},
        "title": {"type": "string"},
        "drafts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "name", "summary", "content"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [
                            "policy",
                            "skill",
                            "workflow",
                            "schema",
                            "record",
                            "backend",
                            "frontend",
                            "infrastructure",
                        ],
                    },
                    "name": {"type": "string"},
                    "summary": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
        },
        "credential_requests": {
            "type": "array",
            "items": {"type": "string", "enum": ["anthropic", "gmail", "slack"]},
        },
        "decision": {"type": "string", "enum": ["allow", "deny"]},
        "save_records": {"type": "boolean"},
        "record_changes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "action",
                    "scope",
                    "record_id",
                    "schema_name",
                    "source_attachment",
                    "values_json",
                    "reason",
                ],
                "properties": {
                    "action": {"type": "string", "enum": ["update", "delete"]},
                    "scope": {"type": "string", "enum": ["id", "schema", "all"]},
                    "record_id": {"type": "string"},
                    "schema_name": {"type": "string"},
                    "source_attachment": {"type": "string"},
                    "values_json": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
        "rule_changes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["action", "name", "content", "reason"],
                "properties": {
                    "action": {"type": "string", "enum": ["activate", "retire"]},
                    "name": {"type": "string"},
                    "content": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}


class AnalysisUnavailable(RuntimeError):
    """The configured model could not return a usable answer."""


class ConnectionCheckError(RuntimeError):
    """A connector could not be verified without exposing its credential."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _record_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for record in records:
        schema = record["schema"]
        entry = summary.setdefault(schema, {"count": 0, "field_coverage": {}})
        entry["count"] += 1
        for name, value in record["values"].items():
            if value not in (None, "", []):
                coverage = entry["field_coverage"]
                coverage[name] = coverage.get(name, 0) + 1
    return summary


def verify_anthropic_key(key: str) -> None:
    """Check the key and access to the model used by this workspace."""
    try:
        Anthropic(api_key=key, timeout=10.0, max_retries=0).models.retrieve("claude-sonnet-5")
    except APIStatusError as error:
        messages = {
            401: "Anthropic rejected this API key. Check it in Anthropic Console and try again.",
            402: "Anthropic billing needs attention. Check your Anthropic Console account.",
            403: "This API key cannot access Austra's model. Check workspace permissions.",
            404: "Austra's model is unavailable for this API key.",
            429: "Anthropic has limited this account. Check its usage or try again later.",
        }
        message = messages.get(
            error.status_code, "Anthropic could not verify the connection. Try again."
        )
        status_code = 400 if error.status_code < 500 else 503
        raise ConnectionCheckError(message, status_code) from error
    except APIConnectionError as error:
        raise ConnectionCheckError(
            "Could not reach Anthropic. Check your internet connection and try again.", 503
        ) from error


def _delegate_research(client: Any, files: Files, chat_id: str, task: str) -> str:
    """Run a read-only worker for one bounded part of the supervisor's request."""
    worker_tools = ReadTools(files, chat_id)
    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    for _ in range(3):
        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=RESEARCH_RESPONSE_TOKENS,
            system=(
                load_skills(files)
                + "\nYou are a research worker. Inspect only the evidence needed for the assigned "
                "task using read tools. Report findings, record IDs, source references, and "
                "uncertainty to the supervisor. You cannot write records or make the final decision."
            ),
            messages=messages,
            tools=worker_tools.definitions(),
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
        )
        if response.stop_reason == "end_turn":
            return "\n".join(block.text for block in response.content if block.type == "text")[
                :8000
            ]
        if response.stop_reason != "tool_use":
            raise ValueError("Research worker could not finish")
        calls = [block for block in response.content if block.type == "tool_use"]
        if not calls:
            raise ValueError("Research worker requested no readable tool")
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for call in calls:
            try:
                result = worker_tools.invoke(call.name, call.input)
                results.append(
                    {"type": "tool_result", "tool_use_id": call.id, "content": json.dumps(result)}
                )
            except (KeyError, ValueError) as error:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": str(error),
                        "is_error": True,
                    }
                )
        messages.append({"role": "user", "content": results})
    raise ValueError("Research worker reached its tool limit")


def analyze(files: Files, access: Access, chat_id: str) -> AgentReply:
    key = access.get("ANTHROPIC_API_KEY")
    if not key:
        return AgentReply(
            reply=(
                "I can save this conversation, but I need an Anthropic API key before I can "
                "build skills and business structures from it. Open Connections."
            ),
            credential_requests=["anthropic"],
        )
    chat = files.chat(chat_id)
    client = Anthropic(api_key=key, timeout=MODEL_TIMEOUT_SECONDS, max_retries=0)
    tools = ReadTools(
        files, chat_id, delegate=lambda task: _delegate_research(client, files, chat_id, task)
    )
    records = files.records()
    context = {
        "conversation": [
            {
                "role": item["role"],
                "body": item.get("display_body", item["body"])
                if index == len(chat["messages"][-16:]) - 1
                else item.get("display_body", item["body"])[:900],
                "attachments": [
                    {"id": attachment["id"], "name": attachment["name"]}
                    for attachment in item.get("attachments", [])
                ],
            }
            for index, item in enumerate(chat["messages"][-16:])
        ],
        "short_term_memory": [
            {"role": item["role"], "body": item["body"][:400]}
            for item in files.recent_memory(exclude_chat=chat_id)
        ],
        "approved_workspace_index": [
            {"kind": item["kind"], "name": item["name"], "version": item["version"]}
            for item in files.approved()
        ],
        "record_count": len(records),
        "saved_record_summary": _record_summary(records),
        "connector_status": access.configured(),
    }
    serialized = json.dumps(context, ensure_ascii=False, default=str)
    instructions = load_skills(files)
    try:
        if len(serialized) > TOKEN_COUNT_THRESHOLD_CHARS:
            model = client.models.retrieve("claude-sonnet-5")
            if model.max_input_tokens:
                token_budget = model.max_input_tokens - FINAL_RESPONSE_TOKENS - 2048
                for _ in range(20):
                    serialized = json.dumps(context, ensure_ascii=False, default=str)
                    count = client.messages.count_tokens(
                        model="claude-sonnet-5",
                        system=instructions,
                        messages=[{"role": "user", "content": serialized}],
                    ).input_tokens
                    if count <= token_budget:
                        break
                    if context["short_term_memory"]:
                        context["short_term_memory"].pop(0)
                    elif len(context["conversation"]) > 1:
                        context["conversation"].pop(0)
                    elif context["saved_record_summary"]:
                        context["saved_record_summary"] = {}
                    else:
                        body = context["conversation"][0]["body"]
                        context["conversation"][0]["body"] = body[
                            : max(0, int(len(body) * token_budget / count * 0.9))
                        ]
                else:
                    raise AnalysisUnavailable("The message exceeds the model context window")
        messages: list[dict[str, Any]] = [{"role": "user", "content": serialized}]
        research_incomplete = False
        has_document_attachment = any(
            not attachment["name"].lower().endswith((".csv", ".xlsx"))
            for item in chat["messages"]
            for attachment in item.get("attachments", [])
        )
        latest = chat["messages"][-1]
        latest_attachments = latest.get("attachments", [])
        latest_attachment_fully_visible = bool(latest_attachments) and all(
            (path := files.root / "evidence_text" / f"{attachment['id']}.txt").is_file()
            and path.read_text(encoding="utf-8") in latest["body"]
            for attachment in latest_attachments
        )
        if not latest_attachment_fully_visible and (
            context["approved_workspace_index"]
            or context["record_count"]
            or has_document_attachment
        ):
            for _ in range(3):
                research = client.messages.create(
                    model="claude-sonnet-5",
                    max_tokens=RESEARCH_RESPONSE_TOKENS,
                    system=(
                        instructions
                        + "\nResearch phase: use a read tool to inspect only the evidence needed "
                        "for the latest request. Do not draft records or write the final reply here."
                    ),
                    messages=messages,
                    tools=tools.definitions(),
                    tool_choice={"type": "any", "disable_parallel_tool_use": True},
                )
                if research.stop_reason == "max_tokens":
                    logger.warning("Research response reached token limit")
                    research_incomplete = True
                    break
                messages.append({"role": "assistant", "content": research.content})
                if research.stop_reason == "end_turn":
                    messages.append(
                        {"role": "user", "content": "Return the final structured answer."}
                    )
                    break
                if research.stop_reason != "tool_use":
                    raise AnalysisUnavailable("Austra research did not finish")
                calls = [block for block in research.content if block.type == "tool_use"]
                if not calls:
                    raise AnalysisUnavailable("Austra requested a tool without a call")
                results = []
                for call in calls:
                    try:
                        result = tools.invoke(call.name, call.input)
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": call.id,
                                "content": json.dumps(result),
                            }
                        )
                    except (KeyError, ValueError) as error:
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": call.id,
                                "content": str(error),
                                "is_error": True,
                            }
                        )
                messages.append({"role": "user", "content": results})
        for attempt in range(2):
            response = client.messages.create(
                model="claude-sonnet-5",
                max_tokens=FINAL_RESPONSE_TOKENS,
                system=(
                    instructions
                    + "\nGive the chat a short, specific title based on the user's request "
                    "in the title field. Use at most five words and 60 characters."
                    + (
                        "\nResearch was incomplete. Use only available evidence, state any "
                        "uncertainty, and do not claim complete extraction or save partial records."
                        if research_incomplete
                        else ""
                    )
                ),
                messages=messages,
                output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            )
            if response.stop_reason != "end_turn":
                logger.warning(
                    "Structured response stopped: reason=%s output_tokens=%s",
                    response.stop_reason,
                    getattr(getattr(response, "usage", None), "output_tokens", None),
                )
                raise AnalysisUnavailable(
                    f"Austra did not finish its response ({response.stop_reason})"
                )
            blocks = [block.text for block in response.content if block.type == "text"]
            if len(blocks) != 1:
                raise AnalysisUnavailable("Austra returned an unexpected response")
            try:
                return AgentReply.model_validate_json(blocks[0])
            except ValidationError as error:
                issues = [
                    {"field": ".".join(map(str, item["loc"])), "type": item["type"]}
                    for item in error.errors(include_input=False)
                ]
                logger.warning("Structured response validation failed: %s", issues)
                if attempt:
                    raise AnalysisUnavailable(
                        "Austra could not validate its response. Please retry the request."
                    ) from error
                messages.append({"role": "assistant", "content": response.content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your response failed application validation at these fields: "
                            f"{json.dumps(issues)}. Return a corrected, complete structured "
                            "answer. Keep all record changes requested by the administrator."
                        ),
                    }
                )
    except AnalysisUnavailable:
        raise
    except APIStatusError as error:
        if error.status_code == 401:
            raise AnalysisUnavailable(
                "Anthropic rejected the saved API key. Open Connections to replace it."
            ) from error
        if error.status_code == 402:
            raise AnalysisUnavailable(
                "Anthropic billing needs attention. Check your account."
            ) from error
        if error.status_code == 403:
            raise AnalysisUnavailable(
                "The saved API key cannot access Austra's model. Check workspace permissions."
            ) from error
        raise AnalysisUnavailable(
            f"Austra request failed (HTTP {error.status_code}). Try again."
        ) from error
    except APITimeoutError as error:
        raise AnalysisUnavailable("Austra's model response timed out. Try again.") from error
    except APIConnectionError as error:
        raise AnalysisUnavailable(
            "Could not reach Austra's model provider. Check your internet connection."
        ) from error
    except Exception as error:
        logger.warning("Analysis failed with %s", type(error).__name__)
        raise AnalysisUnavailable("Austra could not analyze this chat") from error
