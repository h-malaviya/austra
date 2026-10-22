"""Claude planning over user supplied facts and approved workspace files."""

import json
from typing import Any, Literal

from anthropic import Anthropic
from pydantic import BaseModel, ConfigDict, Field

from austra.access import Access
from austra.files import Files
from austra.skills import load_skills
from austra.tools import ReadTools

ConnectorName = Literal["anthropic", "gmail", "slack"]
ArtifactName = Literal["policy", "skill", "workflow", "schema", "record", "backend", "frontend"]


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ArtifactName
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    summary: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=30000)


class AgentReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply: str = Field(min_length=1, max_length=10000)
    drafts: list[Draft] = Field(default_factory=list, max_length=8)
    credential_requests: list[ConnectorName] = Field(default_factory=list, max_length=3)


OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reply", "drafts", "credential_requests"],
    "properties": {
        "reply": {"type": "string"},
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
    },
}


class AnalysisUnavailable(RuntimeError):
    """The configured model could not return a usable answer."""


def analyze(files: Files, access: Access, chat_id: str) -> AgentReply:
    key = access.get("ANTHROPIC_API_KEY")
    if not key:
        return AgentReply(
            reply=(
                "I can save this conversation, but I need a Claude API key before I can "
                "build skills and business structures from it. Open the private setup prompt."
            ),
            credential_requests=["anthropic"],
        )
    chat = files.chat(chat_id)
    tools = ReadTools(files, chat_id)
    context = {
        "conversation": [
            {"role": item["role"], "body": item["body"]} for item in chat["messages"][-16:]
        ],
        "approved_workspace_index": [
            {"kind": item["kind"], "name": item["name"], "version": item["version"]}
            for item in files.approved()
        ],
        "record_count": len(files.records()),
        "connector_status": access.configured(),
    }
    serialized = json.dumps(context, ensure_ascii=False, default=str)
    if len(serialized) > 50000:
        raise AnalysisUnavailable("Context is too large; start a new chat or shorten attachments")
    client = Anthropic(api_key=key, timeout=40.0, max_retries=1)
    try:
        messages: list[dict[str, Any]] = [{"role": "user", "content": serialized}]
        for _ in range(3):
            research = client.messages.create(
                model="claude-sonnet-5",
                max_tokens=1800,
                system=load_skills(),
                messages=messages,
                tools=tools.definitions(),
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            )
            messages.append({"role": "assistant", "content": research.content})
            if research.stop_reason == "end_turn":
                messages.append({"role": "user", "content": "Return the final structured answer."})
                break
            if research.stop_reason != "tool_use":
                raise AnalysisUnavailable("Claude research did not finish")
            calls = [block for block in research.content if block.type == "tool_use"]
            if not calls:
                raise AnalysisUnavailable("Claude requested a tool without a call")
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
        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=6000,
            system=load_skills(),
            messages=messages,
            output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        )
        if response.stop_reason != "end_turn":
            raise AnalysisUnavailable("Claude did not finish its response")
        blocks = [block.text for block in response.content if block.type == "text"]
        if len(blocks) != 1:
            raise AnalysisUnavailable("Claude returned an unexpected response")
        return AgentReply.model_validate_json(blocks[0])
    except AnalysisUnavailable:
        raise
    except Exception as error:
        raise AnalysisUnavailable("Claude could not analyze this chat") from error
