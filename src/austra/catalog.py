"""Approved record definitions and validated case records."""

import json
import math
from datetime import date
from typing import Any

from austra.store import Store, _id, _now


def _valid_value(data_type: str, value: Any) -> bool:
    if data_type == "text":
        return isinstance(value, str) and 0 < len(value) <= 2000
    if data_type == "number":
        return (
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        )
    if data_type == "boolean":
        return isinstance(value, bool)
    if data_type == "date" and isinstance(value, str):
        try:
            date.fromisoformat(value)
            return True
        except ValueError:
            return False
    return False


class Catalog:
    def __init__(self, store: Store) -> None:
        self.store = store

    def review_definition(self, proposal_id: str, decision: str, manager: str) -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ValueError("Unknown decision")
        with self.store._connect() as connection:
            proposal = connection.execute(
                "SELECT * FROM record_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if proposal is None:
                raise KeyError(proposal_id)
            if proposal["status"] != "proposed":
                raise ValueError("Proposal has already been reviewed")
            connection.execute(
                "UPDATE record_proposals SET status = ?, reviewed_at = ?, reviewed_by = ? "
                "WHERE id = ?",
                (decision, _now(), manager, proposal_id),
            )
            definition_id = None
            if decision == "approved":
                version = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM record_definitions WHERE name = ?",
                    (proposal["name"],),
                ).fetchone()[0]
                connection.execute(
                    "UPDATE record_definitions SET status = 'superseded' "
                    "WHERE name = ? AND status = 'active'",
                    (proposal["name"],),
                )
                definition_id = _id()
                connection.execute(
                    "INSERT INTO record_definitions VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
                    (
                        definition_id,
                        proposal["name"],
                        version,
                        proposal["description"],
                        proposal["fields_json"],
                        proposal_id,
                        _now(),
                    ),
                )
            self.store._event(
                connection,
                proposal["case_id"],
                "record_definition_reviewed",
                {
                    "proposal_id": proposal_id,
                    "definition_id": definition_id,
                    "decision": decision,
                    "manager": manager,
                },
            )
            return {
                "proposal_id": proposal_id,
                "decision": decision,
                "definition_id": definition_id,
            }

    def list_definitions(self) -> list[dict[str, Any]]:
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM record_definitions ORDER BY name, version DESC"
            )
            result = [dict(row) for row in rows]
            for definition in result:
                definition["fields"] = json.loads(definition.pop("fields_json"))
            return result

    def create_record(
        self, case_id: str, definition_id: str, values: dict[str, Any], actor: str
    ) -> dict[str, Any]:
        with self.store._connect() as connection:
            self.store._case_row(connection, case_id)
            definition = connection.execute(
                "SELECT * FROM record_definitions WHERE id = ?", (definition_id,)
            ).fetchone()
            if definition is None or definition["status"] != "active":
                raise ValueError("Record definition is not active")
            fields = {field["name"]: field for field in json.loads(definition["fields_json"])}
            if not values or not set(values).issubset(fields):
                raise ValueError("Record contains unknown fields or no values")
            for name, value in values.items():
                if not _valid_value(fields[name]["data_type"], value):
                    raise ValueError(f"Invalid value for {name}")
            record_id, timestamp = _id(), _now()
            connection.execute(
                "INSERT INTO business_records VALUES (?, ?, ?, ?, 'unverified', ?, ?)",
                (record_id, case_id, definition_id, json.dumps(values), actor, timestamp),
            )
            self.store._event(
                connection,
                case_id,
                "business_record_created",
                {
                    "record_id": record_id,
                    "definition_id": definition_id,
                    "actor": actor,
                },
            )
            return {
                "id": record_id,
                "case_id": case_id,
                "definition_id": definition_id,
                "values": values,
                "status": "unverified",
                "entered_by": actor,
                "created_at": timestamp,
            }

    def records_for_case(self, case_id: str) -> list[dict[str, Any]]:
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM business_records WHERE case_id = ? ORDER BY created_at", (case_id,)
            )
            result = [dict(row) for row in rows]
            for record in result:
                record["values"] = json.loads(record.pop("values_json"))
            return result
