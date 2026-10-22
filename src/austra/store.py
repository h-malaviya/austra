"""Persistent cases, policies, proposals, and audit events."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from austra.migrations import initialize_database
from austra.policy_files import write_approved_policy
from austra.workflow import load_workflow, transition_case


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _id() -> str:
    return uuid4().hex


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.workflow = load_workflow()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            initialize_database(connection)

    def _event(
        self,
        connection: sqlite3.Connection,
        case_id: str | None,
        kind: str,
        details: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
            (_id(), case_id, kind, json.dumps(details), _now()),
        )

    def create_case(
        self, request_text: str, channel: str, sender: str, external_id: str | None = None
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if external_id:
                existing = connection.execute(
                    "SELECT case_id FROM messages WHERE channel = ? AND external_id = ?",
                    (channel, external_id),
                ).fetchone()
                if existing:
                    return self._case_row(connection, existing["case_id"])
            case_id, timestamp = _id(), _now()
            connection.execute(
                "INSERT INTO cases VALUES (?, ?, NULL, NULL, ?, ?, ?)",
                (case_id, request_text, self.workflow.initial_state, timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_id(), case_id, channel, sender, external_id, request_text, timestamp),
            )
            self._event(connection, case_id, "case_created", {"channel": channel, "sender": sender})
            return self._case_row(connection, case_id)

    def _case_row(self, connection: sqlite3.Connection, case_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        if row is None:
            raise KeyError(case_id)
        return dict(row)

    def list_cases(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM cases ORDER BY updated_at DESC LIMIT 100"
                )
            ]

    def record_tool_invocation(self, case_id: str, name: str, input_sha256: str) -> None:
        with self._connect() as connection:
            self._case_row(connection, case_id)
            self._event(
                connection,
                case_id,
                "tool_invoked",
                {"tool": name, "input_sha256": input_sha256},
            )

    def add_message(
        self,
        case_id: str,
        body: str,
        sender: str,
        channel: str = "ui",
        external_id: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._case_row(connection, case_id)
            if external_id is not None:
                existing = connection.execute(
                    "SELECT * FROM messages WHERE channel = ? AND external_id = ?",
                    (channel, external_id),
                ).fetchone()
                if existing is not None:
                    if existing["case_id"] != case_id:
                        raise ValueError("External message belongs to another case")
                    return {**dict(existing), "duplicate": True}
            message_id, timestamp = _id(), _now()
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                (message_id, case_id, channel, sender, external_id, body, timestamp),
            )
            for table in (
                "action_proposals",
                "record_proposals",
                "skill_proposals",
                "memory_proposals",
                "system_change_proposals",
                "supplier_proposals",
                "purchase_orders",
                "stock_receipts",
                "sales_orders",
                "policy_proposals",
            ):
                connection.execute(
                    f"UPDATE {table} SET status = 'superseded' "
                    "WHERE case_id = ? AND status = 'proposed'",
                    (case_id,),
                )
            new_state = transition_case(connection, case_id, "message_added", self.workflow)
            connection.execute("UPDATE cases SET updated_at = ? WHERE id = ?", (timestamp, case_id))
            self._event(
                connection,
                case_id,
                "message_added",
                {
                    "message_id": message_id,
                    "sender": sender,
                    "channel": channel,
                    "workflow_version": self.workflow.version,
                    "state": new_state,
                },
            )
            return {
                "id": message_id,
                "case_id": case_id,
                "sender": sender,
                "body": body,
                "channel": channel,
                "external_id": external_id,
                "created_at": timestamp,
                "duplicate": False,
            }

    def get_case(self, case_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            result = self._case_row(connection, case_id)
            for key, table in (
                ("messages", "messages"),
                ("actions", "action_proposals"),
                ("records", "record_proposals"),
                ("events", "events"),
            ):
                rows = connection.execute(
                    f"SELECT * FROM {table} WHERE case_id = ? ORDER BY created_at", (case_id,)
                )
                result[key] = [dict(row) for row in rows]
            for record in result["records"]:
                record["fields"] = json.loads(record.pop("fields_json"))
            for event in result["events"]:
                event["details"] = json.loads(event.pop("details_json"))
            return result

    def add_policy(self, title: str, body: str) -> dict[str, Any]:
        with self._connect() as connection:
            version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM policies WHERE title = ?", (title,)
            ).fetchone()[0]
            policy_id, timestamp = _id(), _now()
            connection.execute(
                "INSERT INTO policies VALUES (?, ?, ?, ?, 'draft', ?, NULL, NULL)",
                (policy_id, title, body, version, timestamp),
            )
            self._event(connection, None, "policy_drafted", {"policy_id": policy_id})
            return dict(
                connection.execute("SELECT * FROM policies WHERE id = ?", (policy_id,)).fetchone()
            )

    def activate_policy(self, policy_id: str, manager: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM policies WHERE id = ?", (policy_id,)).fetchone()
            if row is None:
                raise KeyError(policy_id)
            if row["status"] != "draft":
                raise ValueError("Only draft policies can be activated")
            connection.execute(
                "UPDATE policies SET status = 'superseded' WHERE title = ? AND status = 'active'",
                (row["title"],),
            )
            connection.execute(
                "UPDATE policies SET status = 'active', approved_at = ?, approved_by = ? WHERE id = ?",
                (_now(), manager, policy_id),
            )
            write_approved_policy(self.path, policy_id, row["title"], row["version"], row["body"])
            self._event(
                connection,
                None,
                "policy_activated",
                {"policy_id": policy_id, "version": row["version"], "manager": manager},
            )
            return dict(
                connection.execute("SELECT * FROM policies WHERE id = ?", (policy_id,)).fetchone()
            )

    def list_policies(self, active_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM policies"
        if active_only:
            query += " WHERE status = 'active'"
        query += " ORDER BY title, version DESC"
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(query)]

    def save_plan(
        self,
        case_id: str,
        plan: dict[str, Any],
        policy_ids: list[str],
        document_ids: list[str] | None = None,
    ) -> None:
        with self._connect() as connection:
            self._case_row(connection, case_id)
            connection.execute(
                "UPDATE action_proposals SET status = 'superseded' "
                "WHERE case_id = ? AND status = 'proposed'",
                (case_id,),
            )
            connection.execute(
                "UPDATE record_proposals SET status = 'superseded' "
                "WHERE case_id = ? AND status = 'proposed'",
                (case_id,),
            )
            new_state = transition_case(connection, case_id, "plan_proposed", self.workflow)
            connection.execute(
                "UPDATE cases SET summary = ?, category = ?, updated_at = ? WHERE id = ?",
                (plan["summary"], plan["category"], _now(), case_id),
            )
            for action in plan["actions"]:
                connection.execute(
                    "INSERT INTO action_proposals VALUES (?, ?, ?, ?, ?, 'proposed', ?, NULL, NULL)",
                    (
                        _id(),
                        case_id,
                        action["kind"],
                        action["description"],
                        action["reason"],
                        _now(),
                    ),
                )
            for record in plan["record_types"]:
                connection.execute(
                    "INSERT INTO record_proposals VALUES (?, ?, ?, ?, ?, 'proposed', ?, NULL, NULL)",
                    (
                        _id(),
                        case_id,
                        record["name"],
                        record["description"],
                        json.dumps(record["fields"]),
                        _now(),
                    ),
                )
            from austra.inventory import save_inventory_drafts
            from austra.knowledge_store import save_knowledge_drafts
            from austra.policy_manager import save_policy_drafts
            from austra.purchasing import save_purchasing_drafts

            save_knowledge_drafts(connection, case_id, plan)
            save_purchasing_drafts(connection, case_id, plan)
            save_inventory_drafts(connection, case_id, plan)
            save_policy_drafts(connection, case_id, plan)
            self._event(
                connection,
                case_id,
                "plan_proposed",
                {
                    "policy_ids": policy_ids,
                    "document_ids": document_ids or [],
                    "missing_information": plan["missing_information"],
                    "workflow_version": self.workflow.version,
                    "state": new_state,
                },
            )

    def review_proposal(
        self, table: str, proposal_id: str, decision: str, manager: str
    ) -> dict[str, Any]:
        if table not in {"action_proposals", "record_proposals"}:
            raise ValueError("Unknown proposal type")
        if decision not in {"approved", "rejected"}:
            raise ValueError("Unknown decision")
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM {table} WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise KeyError(proposal_id)
            if row["status"] != "proposed":
                raise ValueError("Proposal has already been reviewed")
            connection.execute(
                f"UPDATE {table} SET status = ?, reviewed_at = ?, reviewed_by = ? WHERE id = ?",
                (decision, _now(), manager, proposal_id),
            )
            self._event(
                connection,
                row["case_id"],
                "proposal_reviewed",
                {
                    "proposal_id": proposal_id,
                    "type": table,
                    "decision": decision,
                    "manager": manager,
                },
            )
            return dict(
                connection.execute(f"SELECT * FROM {table} WHERE id = ?", (proposal_id,)).fetchone()
            )
