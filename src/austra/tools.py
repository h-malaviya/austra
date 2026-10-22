"""Deterministic, typed read tools available to the planning agent."""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from austra.files import Files
from austra.skills import PLATFORM_SKILLS, SKILL_ROOT

MAX_RESULT_CHARS = 12000
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


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadTools:
    def __init__(self, files: Files, chat_id: str) -> None:
        self.files = files
        self.chat_id = chat_id
        self.input_types = {
            "search_workspace": SearchInput,
            "read_artifact": ReadInput,
            "read_platform_skill": SkillInput,
            "list_records": EmptyInput,
        }
        self.descriptions = {
            "search_workspace": "Search approved files and recorded chat evidence by text.",
            "read_artifact": "Read one latest approved business or source file by kind and name.",
            "read_platform_skill": "Read one built-in platform authoring skill.",
            "list_records": "List recent approved business records and their source IDs.",
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
        if name == "search_workspace":
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
        else:
            result = self.files.records()[-30:]
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        if len(serialized) > MAX_RESULT_CHARS:
            return {"truncated": True, "excerpt": serialized[:MAX_RESULT_CHARS]}
        return {"truncated": False, "data": result}
