"""Allowlisted, typed read tools for the agent's research loop."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from austra.documents import DocumentLibrary
from austra.inventory import Inventory
from austra.knowledge_store import KnowledgeStore
from austra.purchasing import Purchasing
from austra.skills import BUILTIN_SKILLS
from austra.store import Store

MAX_TOOL_RESULT_CHARS = 10000


class CurrentCaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchDocumentsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=3, max_length=200)


class ActivePoliciesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadSkillInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=50)


class SearchMemoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=3, max_length=200)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_type: type[BaseModel]
    execute: Callable[[BaseModel, str], Any]

    def anthropic_definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_type.model_json_schema(),
            "strict": True,
        }


class ToolRegistry:
    def __init__(
        self, store: Store, documents: DocumentLibrary, knowledge: KnowledgeStore | None = None
    ) -> None:
        self.store = store
        self.documents = documents
        self.knowledge = knowledge or KnowledgeStore(store)
        self.purchasing = Purchasing(store)
        self.inventory = Inventory(store)
        self.tools = {
            "get_current_case": Tool(
                "get_current_case",
                "Read the current case and its recorded messages, proposals, and audit events.",
                CurrentCaseInput,
                lambda _args, case_id: self.store.get_case(case_id),
            ),
            "search_documents": Tool(
                "search_documents",
                "Search uploaded reference documents. Results are unverified evidence.",
                SearchDocumentsInput,
                lambda args, _case_id: self.documents.search(args.query),
            ),
            "list_active_policies": Tool(
                "list_active_policies",
                "Read manager-approved business policies with IDs and versions.",
                ActivePoliciesInput,
                lambda _args, _case_id: self.store.list_policies(active_only=True),
            ),
            "list_available_skills": Tool(
                "list_available_skills",
                "List approved built-in and manager-published skills. Skills guide planning but cannot grant permissions.",
                ActivePoliciesInput,
                lambda _args, _case_id: (
                    [{"name": name, "built_in": True} for name in sorted(BUILTIN_SKILLS)]
                    + self.knowledge.active_skills()
                ),
            ),
            "read_skill": Tool(
                "read_skill",
                "Read one approved skill. Use skill_authoring when a user asks for a new reusable behavior.",
                ReadSkillInput,
                lambda args, _case_id: self.knowledge.read_skill(args.name),
            ),
            "search_approved_memory": Tool(
                "search_approved_memory",
                "Search manager-reviewed memory with case and document provenance. Memory is context, not policy.",
                SearchMemoryInput,
                lambda args, _case_id: self.knowledge.search_memory(args.query),
            ),
            "list_verified_suppliers": Tool(
                "list_verified_suppliers",
                "Read manager-verified supplier IDs, materials, and email addresses for purchase planning.",
                ActivePoliciesInput,
                lambda _args, _case_id: self.purchasing.verified_suppliers(),
            ),
            "get_stock_snapshot": Tool(
                "get_stock_snapshot",
                "Read verified stock and available finished product tonnes. Raw and finished materials remain separate.",
                ActivePoliciesInput,
                lambda _args, _case_id: self.inventory.stock_snapshot(),
            ),
        }

    def definitions(self) -> list[dict[str, Any]]:
        return [tool.anthropic_definition() for tool in self.tools.values()]

    def invoke(self, name: str, raw_input: dict[str, Any], case_id: str) -> dict[str, Any]:
        tool = self.tools.get(name)
        if tool is None:
            raise ValueError("Tool is not registered")
        arguments = tool.input_type.model_validate(raw_input)
        result = tool.execute(arguments, case_id)
        serialized = json.dumps(result, default=str)
        self.store.record_tool_invocation(
            case_id,
            name,
            hashlib.sha256(json.dumps(raw_input, sort_keys=True).encode()).hexdigest(),
        )
        if len(serialized) > MAX_TOOL_RESULT_CHARS:
            return {"truncated": True, "excerpt": serialized[:MAX_TOOL_RESULT_CHARS]}
        return {"truncated": False, "data": result}
