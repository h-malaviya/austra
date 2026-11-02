"""Deterministic, typed read tools available to the planning agent."""

import json
from collections.abc import Callable
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from austra.access import Access
from austra.arithmetic import calculate_expression
from austra.files import Files
from austra.formulas import run_formula
from austra.imports import inspect_chat_table
from austra.mcp_connections import McpConnections
from austra.skills import PLATFORM_SKILLS, SKILL_ROOT

MAX_RESULT_CHARS = 12000
ATTACHMENT_CHUNK_CHARS = 8000
MAX_COMPLETE_RECORD_CHARS = 500000
MAX_COMPLETE_RECORD_PAGES = 100
Kind = Literal["policy", "skill", "workflow", "schema", "backend", "frontend", "formula"]


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


class RecordLookupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schemas: list[str] = Field(min_length=1, max_length=8)
    save_as: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,63}$")


class SavedLookupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")


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


class McpConnectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,31}$")


class DocsSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=3, max_length=300)


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CalculateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["add", "subtract", "multiply", "divide"]
    operands: list[str] = Field(min_length=2, max_length=100)


class ExpressionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expression: str = Field(min_length=1, max_length=500)
    variables: dict[str, str] = Field(default_factory=dict, max_length=20)


class ApprovedFormulaInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    identity: str = Field(min_length=1, max_length=120)
    overrides: dict[str, str] = Field(default_factory=dict, max_length=20)


class ReadTools:
    def __init__(
        self, files: Files, chat_id: str, delegate: Callable[[str], str] | None = None
    ) -> None:
        self.files = files
        self.chat_id = chat_id
        self.delegate = delegate
        self.mcp_connections = McpConnections(files.root, Access(files.root))
        self.input_types = {
            "search_workspace": SearchInput,
            "read_artifact": ReadInput,
            "read_platform_skill": SkillInput,
            "list_records": ListRecordsInput,
            "compose_record_lookup": RecordLookupInput,
            "run_saved_record_lookup": SavedLookupInput,
            "read_attachment": AttachmentInput,
            "inspect_attachment_table": AttachmentInput,
            "list_rules": ListRulesInput,
            "list_recovery_history": EmptyInput,
            "list_mcp_connections": EmptyInput,
            "list_mcp_tools": McpConnectionInput,
            "search_microsoft_docs": DocsSearchInput,
            "calculate_decimal": CalculateInput,
            "calculate_expression": ExpressionInput,
            "run_approved_formula": ApprovedFormulaInput,
        }
        if delegate is not None:
            self.input_types["delegate_research"] = DelegateInput
        self.descriptions = {
            "search_workspace": "Search approved files and recorded chat evidence by text.",
            "read_artifact": "Read one latest approved business or source file by kind and name.",
            "read_platform_skill": "Read one built-in platform authoring skill.",
            "list_records": "List complete saved business records, optionally by schema, with total count and pagination.",
            "compose_record_lookup": "Build a read-only lookup across up to eight named record schemas and return every matching record with per-schema counts. Set save_as to retain this lookup for later chats when the plan is reusable.",
            "run_saved_record_lookup": "Run a read-only record lookup previously composed and saved by Austra.",
            "read_attachment": "Read more extracted text from an attachment in this chat by ID and offset.",
            "inspect_attachment_table": "Inspect the complete structure of an attached CSV or XLSX table, including inferred types, blank values, and schema mismatches, without returning every row.",
            "delegate_research": "Assign a bounded, read-only research task to a worker agent. Use for a distinct part of a larger business request; the supervisor makes the final decision.",
            "list_rules": "List active administrator-approved policies and workflows, optionally filtered by use case. Empty results mean no matching rule is active.",
            "list_recovery_history": "List recent read-tool failures, successful remedies, and unresolved failures without business record contents.",
            "list_mcp_connections": "List configured MCP integrations without exposing tokens.",
            "list_mcp_tools": "List verified tools and input schemas for one MCP integration.",
            "search_microsoft_docs": "Search current Microsoft Learn documentation through its public MCP server for Microsoft or Teams integration setup.",
            "calculate_decimal": "Calculate bounded decimal sums, differences, products, or quotients from numeric strings. Division uses 40 significant digits. Reconcile with source rows; never estimate arithmetic in prose.",
            "calculate_expression": "Evaluate a bounded decimal formula with named numeric inputs, parentheses, and + - * /. Use only reviewed formulas and sourced input values. Arbitrary code and functions are rejected.",
            "run_approved_formula": "Run an administrator-approved formula for one record identity. Mapped source fields are checked for missing or conflicting records. Optional decimal overrides are a read-only scenario and are disclosed in the result.",
        }

    def definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "description": self.descriptions[name],
                "input_schema": input_type.model_json_schema(),
            }
            for name, input_type in self.input_types.items()
        ]

    def invoke_for_research(self, name: str, raw_input: dict[str, Any]) -> dict[str, Any]:
        """Collect record pages in code so the model cannot repeat or omit a page."""
        result = self.invoke(name, raw_input)
        if name != "list_records" or result["truncated"]:
            return result
        first = result["data"]
        if first.get("error"):
            return result
        records = list(first["records"])
        next_offset = first["next_offset"]
        seen_offsets = {raw_input.get("offset", 0)}
        seen_ids = {record["id"] for record in records}
        pages = 1
        while next_offset is not None:
            if next_offset in seen_offsets or pages >= MAX_COMPLETE_RECORD_PAGES:
                return self._recover_stalled_records(raw_input)
            if (
                len(json.dumps(records, ensure_ascii=False, default=str))
                > MAX_COMPLETE_RECORD_CHARS
            ):
                raise ValueError("Record set is too large; read one schema at a time")
            seen_offsets.add(next_offset)
            page_input = {**raw_input, "offset": next_offset}
            page = self.invoke(name, page_input)
            if page["truncated"] or page["data"].get("error"):
                return self._recover_stalled_records(raw_input)
            if page["data"]["total"] != first["total"]:
                return self._recover_stalled_records(raw_input)
            page_ids = {record["id"] for record in page["data"]["records"]}
            if page_ids & seen_ids or not page_ids:
                return self._recover_stalled_records(raw_input)
            seen_ids.update(page_ids)
            records.extend(page["data"]["records"])
            next_offset = page["data"]["next_offset"]
            pages += 1
        if raw_input.get("offset", 0) == 0 and len(records) != first["total"]:
            return self._recover_stalled_records(raw_input)
        if len(json.dumps(records, ensure_ascii=False, default=str)) > MAX_COMPLETE_RECORD_CHARS:
            raise ValueError("Record set is too large; read one schema at a time")
        return {
            "truncated": False,
            "data": {
                "total": first["total"],
                "start_offset": raw_input.get("offset", 0),
                "next_offset": None,
                "complete": raw_input.get("offset", 0) == 0 and len(records) == first["total"],
                "records": records,
            },
        }

    def _recover_stalled_records(self, raw_input: dict[str, Any]) -> dict[str, Any]:
        recovery_id = self.files.record_recovery_failure(
            self.chat_id, "list_records", "pagination_stalled"
        )
        arguments = ListRecordsInput.model_validate(raw_input)
        records = [
            {
                "id": record["id"],
                "schema": record["schema"],
                "values": record["values"],
                "source_attachment": record.get("source_attachment", ""),
                "source_file": record.get("source_file", ""),
            }
            for record in self.files.records()
            if arguments.schema_name is None or record["schema"] == arguments.schema_name
        ]
        selected = records[arguments.offset :]
        if len(json.dumps(selected, ensure_ascii=False, default=str)) > MAX_COMPLETE_RECORD_CHARS:
            raise ValueError("Record set is too large; compose a narrower record lookup")
        self.files.resolve_recovery(recovery_id, "snapshot_record_lookup")
        return {
            "truncated": False,
            "data": {
                "total": len(records),
                "start_offset": arguments.offset,
                "next_offset": None,
                "complete": arguments.offset == 0,
                "recovered_from": "pagination_stalled",
                "records": selected,
            },
        }

    def _record_lookup(self, schemas: list[str]) -> dict[str, Any]:
        selected_schemas = set(schemas)
        selected_records = [
            record for record in self.files.records() if record["schema"] in selected_schemas
        ]
        return {
            "complete": True,
            "schemas": {
                schema: {
                    "total": sum(record["schema"] == schema for record in selected_records),
                    "records": [
                        {
                            "id": record["id"],
                            "values": record["values"],
                            "source_attachment": record.get("source_attachment", ""),
                            "source_file": record.get("source_file", ""),
                        }
                        for record in selected_records
                        if record["schema"] == schema
                    ],
                }
                for schema in schemas
            },
        }

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
        elif name == "calculate_decimal":
            if any(len(value) > 60 for value in arguments.operands):
                raise ValueError("Decimal input is too long")
            try:
                numbers = [Decimal(value) for value in arguments.operands]
            except InvalidOperation as error:
                raise ValueError("All operands must be decimal numbers") from error
            if any(not number.is_finite() for number in numbers):
                raise ValueError("Operands must be finite")
            if any(abs(number.adjusted()) > 100 for number in numbers):
                raise ValueError("Decimal input is outside the supported range")
            with localcontext() as context:
                context.prec = 40
                value = numbers[0]
                try:
                    for operand in numbers[1:]:
                        if arguments.operation == "add":
                            value += operand
                        elif arguments.operation == "subtract":
                            value -= operand
                        elif arguments.operation == "multiply":
                            value *= operand
                        else:
                            if operand == 0:
                                raise ValueError("Division by zero")
                            value /= operand
                except InvalidOperation as error:
                    raise ValueError("Calculation is outside the supported range") from error
            output = format(value, "f")
            if len(output) > 200:
                raise ValueError("Calculation result is too large")
            result = {"value": output, "operation": arguments.operation}
        elif name == "calculate_expression":
            result = {
                "value": calculate_expression(arguments.expression, arguments.variables),
                "expression": arguments.expression,
            }
        elif name == "run_approved_formula":
            result = run_formula(
                self.files.approved(),
                self.files.records(),
                arguments.name,
                arguments.identity,
                arguments.overrides,
            )
        elif name == "list_mcp_connections":
            result = {
                connection_name: {
                    "url": connection["url"],
                    "tools": [tool["name"] for tool in connection["tools"]],
                }
                for connection_name, connection in self.mcp_connections.list().items()
            }
        elif name == "list_mcp_tools":
            result = self.mcp_connections.test(arguments.name)["tools"]
        elif name == "search_microsoft_docs":
            result = self.mcp_connections.call(
                "microsoft_learn", "microsoft_docs_search", {"query": arguments.query}
            )
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
        elif name == "list_recovery_history":
            result = self.files.recovery_history()
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
        elif name == "compose_record_lookup":
            result = self._record_lookup(arguments.schemas)
        elif name == "run_saved_record_lookup":
            result = self._record_lookup(self.files.read_plan(arguments.name)["schemas"])
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
        if name in {"compose_record_lookup", "run_saved_record_lookup"}:
            if len(serialized) > MAX_COMPLETE_RECORD_CHARS:
                raise ValueError("Record lookup is too large; select fewer schemas")
            if name == "compose_record_lookup" and arguments.save_as:
                self.files.save_read_plan(self.chat_id, arguments.save_as, arguments.schemas)
                result["saved_as"] = arguments.save_as
            return {"truncated": False, "data": result}
        if len(serialized) > MAX_RESULT_CHARS:
            return {"truncated": True, "excerpt": serialized[:MAX_RESULT_CHARS]}
        return {"truncated": False, "data": result}
