"""Manager-reviewed policy drafts from operational cases."""

import sqlite3
from typing import Any

from austra.policy_files import write_approved_policy
from austra.store import Store, _id, _now


def save_policy_drafts(connection: sqlite3.Connection, case_id: str, plan: dict[str, Any]) -> None:
    connection.execute(
        "UPDATE policy_proposals SET status = 'superseded' "
        "WHERE case_id = ? AND status = 'proposed'",
        (case_id,),
    )
    for draft in plan.get("policy_drafts", []):
        connection.execute(
            "INSERT INTO policy_proposals VALUES (?, ?, ?, ?, ?, 'proposed', ?, NULL, NULL)",
            (_id(), case_id, draft["title"], draft["body"], draft["reason"], _now()),
        )


class PolicyManager:
    def __init__(self, store: Store) -> None:
        self.store = store

    def for_case(self, case_id: str) -> list[dict[str, Any]]:
        with self.store._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM policy_proposals WHERE case_id = ? ORDER BY created_at",
                    (case_id,),
                )
            ]

    def review(self, proposal_id: str, decision: str, manager: str) -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ValueError("Unknown decision")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM policy_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise KeyError(proposal_id)
            if row["status"] != "proposed":
                raise ValueError("Policy proposal has already been reviewed")
            if decision == "approved":
                version = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM policies WHERE title = ?",
                    (row["title"],),
                ).fetchone()[0]
                connection.execute(
                    "UPDATE policies SET status = 'superseded' "
                    "WHERE title = ? AND status = 'active'",
                    (row["title"],),
                )
                policy_id = _id()
                connection.execute(
                    "INSERT INTO policies VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
                    (
                        policy_id,
                        row["title"],
                        row["body"],
                        version,
                        _now(),
                        _now(),
                        manager,
                    ),
                )
                write_approved_policy(
                    self.store.path, policy_id, row["title"], version, row["body"]
                )
            connection.execute(
                "UPDATE policy_proposals SET status = ?, reviewed_at = ?, reviewed_by = ? "
                "WHERE id = ?",
                (decision, _now(), manager, proposal_id),
            )
            self.store._event(
                connection,
                row["case_id"],
                "policy_proposal_reviewed",
                {"proposal_id": proposal_id, "decision": decision, "manager": manager},
            )
            return dict(
                connection.execute(
                    "SELECT * FROM policy_proposals WHERE id = ?", (proposal_id,)
                ).fetchone()
            )
