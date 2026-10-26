"""Convert uploaded CSV evidence into schema-validated business records."""

import csv
import io
import json
import re
from datetime import UTC, datetime
from typing import Any

from openpyxl import load_workbook

from austra.files import Files

MAX_TABLE_ROWS = 5000
NUMERIC_FIELD = re.compile(
    r"(^minimum_purity_percent$|^package_count$|(_kg|_usd|_pct|_days|_percent)$)"
)


def _schema_for_headers(files: Files, headers: list[str]) -> dict[str, Any] | None:
    candidates = []
    for item in files.approved():
        if item["kind"] != "schema":
            continue
        schema = json.loads(item["content"])
        properties = set(schema.get("properties", {}))
        required = set(schema.get("required", [])) - {"provenance"}
        if set(headers).issubset(properties) and required.issubset(headers):
            candidates.append((len(properties) - len(headers), item))
    return min(candidates, key=lambda entry: entry[0])[1] if candidates else None


def _schema_name(filename: str, section: str) -> str:
    stem = section or filename.rsplit(".", 1)[0]
    name = re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_")
    if name.endswith("ies"):
        name = f"{name[:-3]}y"
    elif name.endswith("s"):
        name = name[:-1]
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", name):
        raise ValueError(f"{filename}: cannot derive a safe schema name")
    return name


def _inferred_schema(
    files: Files,
    filename: str,
    section: str,
    headers: list[str],
    rows: list[tuple[int, dict[str, str]]],
) -> tuple[str, dict[str, Any]]:
    name = _schema_name(filename, section)
    current = next(
        (item for item in files.approved() if item["kind"] == "schema" and item["name"] == name),
        None,
    )
    schema = (
        json.loads(current["content"])
        if current
        else {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "required": [],
        }
    )
    properties = schema["properties"]
    for header in headers:
        if header in properties:
            continue
        values = [row[header].strip() for _, row in rows if row.get(header) is not None]
        nonempty = [value for value in values if value]
        numeric = bool(nonempty) and bool(NUMERIC_FIELD.search(header))
        if numeric:
            try:
                for value in nonempty:
                    float(value)
            except ValueError:
                numeric = False
        field_type = (
            "integer"
            if numeric and all(re.fullmatch(r"[+-]?\d+", value) for value in nonempty)
            else "number"
            if numeric
            else "string"
        )
        properties[header] = {
            "type": [field_type, "null"] if numeric and "" in values else field_type
        }
    if "provenance" not in properties:
        properties["provenance"] = {
            "type": "object",
            "properties": {
                "source_file": {"type": "string"},
                "source_hash": {"type": "string"},
                "source_attachment": {"type": "string"},
                "source_row": {"type": "integer"},
                "ingested_at": {"type": "string"},
            },
            "required": ["source_file", "source_hash", "source_attachment", "source_row"],
        }
    if current:
        schema["required"] = [
            field
            for field in schema.get("required", [])
            if field in headers or field == "provenance"
        ]
    else:
        schema["required"] = [*headers, "provenance"]
    return name, schema


def _value(raw: str, definition: dict[str, Any], field: str, row_number: int) -> Any:
    types = definition.get("type", "string")
    allowed = [types] if isinstance(types, str) else types
    if raw == "":
        return None if "null" in allowed else ""
    try:
        if "integer" in allowed:
            return int(raw)
        if "number" in allowed:
            return float(raw)
        if "boolean" in allowed:
            if raw.casefold() in {"true", "1", "yes"}:
                return True
            if raw.casefold() in {"false", "0", "no"}:
                return False
            raise ValueError("invalid boolean")
    except ValueError as error:
        raise ValueError(f"CSV row {row_number}: invalid {field}") from error
    return raw


def _row_values(
    row: dict[str, str], schema: dict[str, Any], filename: str, digest: str, row_number: int
) -> dict[str, Any]:
    properties = schema["properties"]
    values = {
        field: _value(raw.strip(), properties[field], field, row_number)
        for field, raw in row.items()
        if raw is not None
    }
    provenance = properties.get("provenance")
    if provenance and provenance.get("type") == "object":
        source = {
            "source_file": filename,
            "source_hash": digest,
            "source_attachment": digest,
            "source_row": row_number,
            "ingested_at": datetime.now(UTC).date().isoformat(),
        }
        values["provenance"] = {
            field: source[field] for field in provenance.get("properties", {}) if field in source
        }
    return values


def _tables(
    path: Any, filename: str
) -> list[tuple[str, list[str], list[tuple[int, dict[str, str]]]]]:
    if filename.lower().endswith(".csv"):
        reader = csv.DictReader(io.StringIO(path.read_bytes().decode("utf-8-sig")))
        return [("", reader.fieldnames or [], list(enumerate(reader, start=2)))]
    workbook = load_workbook(io.BytesIO(path.read_bytes()), read_only=True, data_only=True)
    try:
        tables = []
        for sheet in workbook.worksheets:
            values = iter(sheet.values)
            first = next(values, None)
            if first is None:
                continue
            headers = ["" if cell is None else str(cell).strip() for cell in first]
            rows = []
            for row_number, cells in enumerate(values, start=2):
                if not any(cell is not None and str(cell).strip() for cell in cells):
                    continue
                if any(cell is not None for cell in cells[len(headers) :]):
                    raise ValueError(f"{sheet.title}: more values than headers")
                rows.append(
                    (
                        row_number,
                        {
                            header: "" if value is None else str(value)
                            for header, value in zip(headers, cells)
                        },
                    )
                )
            tables.append((sheet.title, headers, rows))
        return tables
    finally:
        workbook.close()


def inspect_chat_table(files: Files, chat_id: str, attachment_id: str) -> dict[str, Any]:
    """Inspect a complete table without returning its row contents to the model."""
    path, filename = files.attachment(chat_id, attachment_id)
    if not filename.lower().endswith((".csv", ".xlsx")):
        raise ValueError("Attachment is not a CSV or XLSX table")
    sections = []
    for section, headers, rows in _tables(path, filename):
        if not headers or not all(headers) or len(headers) != len(set(headers)):
            raise ValueError(f"{section or filename}: invalid column names")
        if len(rows) > MAX_TABLE_ROWS:
            raise ValueError(f"{section or filename}: too many rows")
        if any(None in row for _, row in rows):
            raise ValueError(f"{section or filename}: a row has more values than columns")
        name, inferred = _inferred_schema(files, filename, section, headers, rows)
        matching = _schema_for_headers(files, headers)
        existing = next(
            (
                item
                for item in files.approved()
                if item["kind"] == "schema" and item["name"] == name
            ),
            None,
        )
        old_schema = json.loads(existing["content"]) if existing else {}
        columns = {
            header: {
                "type": inferred["properties"][header]["type"],
                "blank_rows": sum(not row.get(header, "").strip() for _, row in rows),
            }
            for header in headers
        }
        sections.append(
            {
                "section": section,
                "row_count": len(rows),
                "columns": columns,
                "matching_schema": matching["name"] if matching else None,
                "candidate_schema": name,
                "new_columns": [
                    header for header in headers if header not in old_schema.get("properties", {})
                ],
                "missing_required_columns": [
                    field
                    for field in old_schema.get("required", [])
                    if field != "provenance" and field not in headers
                ],
            }
        )
    return {"filename": filename, "attachment_id": attachment_id, "sections": sections}


def import_chat_tables(
    files: Files,
    chat_id: str,
    *,
    auto_schema: bool = False,
    progress: Any = None,
) -> tuple[int, list[str]]:
    """Import attached CSV and XLSX tables once, reporting validation issues."""
    imported = 0
    issues = []
    attachments = {
        attachment["id"]: attachment["name"]
        for message in files.chat(chat_id)["messages"]
        for attachment in message.get("attachments", [])
        if message["role"] == "user" and attachment["name"].lower().endswith((".csv", ".xlsx"))
    }
    for digest, filename in attachments.items():
        if progress:
            progress(f"Inspecting {filename}")
        path, _ = files.attachment(chat_id, digest)
        try:
            for section, headers, source_rows in _tables(path, filename):
                if not headers or len(source_rows) > MAX_TABLE_ROWS:
                    raise ValueError(f"{section or filename}: empty or too many rows")
                if not all(headers) or len(set(headers)) != len(headers):
                    raise ValueError(f"{section or filename}: invalid column names")
                schema_item = _schema_for_headers(files, headers)
                if schema_item is None:
                    if not auto_schema:
                        issues.append(f"{filename}: no schema matches {section or 'the columns'}")
                        continue
                    name, inferred = _inferred_schema(
                        files, filename, section, headers, source_rows
                    )
                    files.apply_agent_drafts(
                        chat_id,
                        [
                            {
                                "kind": "schema",
                                "name": name,
                                "summary": f"Columns imported from {filename}",
                                "content": json.dumps(inferred),
                            }
                        ],
                        ["foundation"],
                    )
                    schema_item = _schema_for_headers(files, headers)
                    if schema_item is None:
                        raise ValueError(f"{filename}: inferred schema does not match columns")
                schema = json.loads(schema_item["content"])
                if progress:
                    progress(f"Importing {filename}: {len(source_rows)} rows")
                rows = []
                for row_number, row in source_rows:
                    if None in row:
                        raise ValueError(f"row {row_number}: too many columns")
                    rows.append(
                        (row_number, _row_values(row, schema, filename, digest, row_number))
                    )
                imported += files.save_imported_records(
                    chat_id, schema_item["name"], digest, filename, rows, section
                )
        except (UnicodeDecodeError, csv.Error, ValueError, OSError) as error:
            issues.append(f"{filename}: {error}")
    return imported, issues
