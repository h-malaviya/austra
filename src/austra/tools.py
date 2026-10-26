"""Deterministic, typed read tools available to the planning agent."""

import json
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from austra.files import Files
from austra.imports import inspect_chat_table
from austra.skills import PLATFORM_SKILLS, SKILL_ROOT

MAX_RESULT_CHARS = 12000
ATTACHMENT_CHUNK_CHARS = 8000
Kind = Literal["policy", "skill", "workflow", "schema", "backend", "frontend"]


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=2, max_length=120)


class ReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")


class SkillInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")


class ListRecordsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offset: int = 0
    limit: int = 10
    schema_name: str | None = Field(default=None, alias="schema")


class AttachmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attachment_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    offset: int = 0


class DelegateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=10, max_length=1000)


class ListRulesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(default="", max_length=120)


class ReadTools:
    def __init__(
        self, files: Files, chat_id: str, delegate: Callable[[str], str] | None = None
    ) -> None:
        self.files = files
        self.chat_id = chat_id
        self.delegate = delegate
        self.input_types = {
            "search_workspace": SearchInput,
            "read_artifact": ReadInput,
            "read_platform_skill": SkillInput,
            "list_records": ListRecordsInput,
            "read_attachment": AttachmentInput,
            "inspect_attachment_table": AttachmentInput,
            "list_rules": ListRulesInput,
        }
        if delegate is not None:
            self.input_types["delegate_research"] = DelegateInput
        self.descriptions = {
            "search_workspace": "Search approved files and recorded chat evidence by text.",
            "read_artifact": "Read one latest approved business or source file by kind and name.",
            "read_platform_skill": "Read one built-in platform authoring skill.",
            "list_records": "List complete saved business records, optionally by schema, with total count and pagination.",
            "read_attachment": "Read more extracted text from an attachment in this chat by ID and offset.",
            "inspect_attachment_table": "Inspect the complete structure of an attached CSV or XLSX table, including inferred types, blank values, and schema mismatches, without returning every row.",
            "delegate_research": "Assign a bounded, read-only research task to a worker agent. Use for a distinct part of a larger business request; the supervisor makes the final decision.",
            "list_rules": "List active administrator-approved policies and workflows, optionally filtered by use case. Empty results mean no matching rule is active.",
        }

    def definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "description": self.descriptions[name],
                "input_schema": input_type.model_json_schema(),
                "strict": True,
            }
            for name, input_type in self.input_types.items()
        ]

    def invoke(self, name: str, raw_input: dict[str, Any]) -> dict[str, Any]:
        input_type = self.input_types.get(name)
        if input_type is None:
            raise ValueError("Unknown tool")
        arguments = input_type.model_validate(raw_input)
        self.files.record_tool_use(self.chat_id, name, raw_input)
        if name == "delegate_research":
            if self.delegate is None:
                raise ValueError("Research delegation is unavailable")
            result: Any = {"findings": self.delegate(arguments.task)}
        elif name == "list_rules":
            query = arguments.query.casefold()
            result = [
                item
                for item in self.files.approved()
                if item["kind"] in {"policy", "workflow"}
                and (
                    not query
                    or query in item["name"].casefold()
                    or query in item["content"].casefold()
                )
            ]
        elif name == "search_workspace":
            query = arguments.query.casefold()
            results = [
                {
                    "kind": item["kind"],
                    "name": item["name"],
                    "version": item["version"],
                    "excerpt": item["content"][:800],
                }
                for item in self.files.approved()
                if query in item["content"].casefold() or query in item["name"].casefold()
            ][:20]
            chat = self.files.chat(self.chat_id)
            results.extend(
                {
                    "kind": "chat_evidence",
                    "message_id": message["id"],
                    "excerpt": message["body"][:800],
                }
                for message in chat["messages"]
                if message["role"] == "user" and query in message["body"].casefold()
            )
            result: Any = results[:30]
        elif name == "read_artifact":
            result = next(
                (
                    item
                    for item in self.files.approved()
                    if item["kind"] == arguments.kind and item["name"] == arguments.name
                ),
                {"error": "Approved artifact not found"},
            )
        elif name == "read_platform_skill":
            result = (
                (SKILL_ROOT / arguments.name / "SKILL.md").read_text(encoding="utf-8")
                if arguments.name in PLATFORM_SKILLS
                else {"error": "Platform skill not found"}
            )
        elif name == "read_attachment":
            if arguments.offset < 0:
                raise ValueError("Attachment offset must be nonnegative")
            self.files.attachment(self.chat_id, arguments.attachment_id)
            path = self.files.root / "evidence_text" / f"{arguments.attachment_id}.txt"
            if not path.is_file():
                raise ValueError("Extracted attachment text is unavailable")
            attachment_text = path.read_text(encoding="utf-8")
            start = arguments.offset
            result = {
                "text": attachment_text[start : start + ATTACHMENT_CHUNK_CHARS],
                "next_offset": min(start + ATTACHMENT_CHUNK_CHARS, len(attachment_text)),
                "total_length": len(attachment_text),
            }
        elif name == "inspect_attachment_table":
            result = inspect_chat_table(self.files, self.chat_id, arguments.attachment_id)
        else:
            if arguments.offset < 0 or not 1 <= arguments.limit <= 20:
                raise ValueError("Record offset or limit is invalid")
            records = [
                record
                for record in self.files.records()
                if arguments.schema_name is None or record["schema"] == arguments.schema_name
            ]
            page = records[arguments.offset : arguments.offset + arguments.limit]
            visible = [
                {
                    "id": record["id"],
                    "schema": record["schema"],
                    "values": record["values"],
                    "source_attachment": record.get("source_attachment", ""),
                    "source_file": record.get("source_file", ""),
                }
                for record in page
            ]
            while (
                visible
                and len(json.dumps(visible, ensure_ascii=False, default=str))
                > MAX_RESULT_CHARS - 500
            ):
                visible.pop()
            result = {
                "total": len(records),
                "next_offset": arguments.offset + len(visible)
                if arguments.offset + len(visible) < len(records)
                else None,
                "records": visible,
            }
            if not visible and page:
                result["error"] = "Record is too large for this tool result"
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        if len(serialized) > MAX_RESULT_CHARS:
            return {"truncated": True, "excerpt": serialized[:MAX_RESULT_CHARS]}
        return {"truncated": False, "data": result}
