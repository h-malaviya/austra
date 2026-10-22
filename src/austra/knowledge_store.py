"""Reviewed Markdown skills and sourced long-term memory."""

import hashlib
import json
import re
import sqlite3
from difflib import unified_diff
from typing import Any

from austra.skills import BUILTIN_SKILLS, load_builtin_skill
from austra.store import Store, _id, _now

SKILL_NAME = re.compile(r"^[a-z][a-z0-9_]{1,49}$")
MAX_SKILL_BYTES = 12000


def save_knowledge_drafts(
    connection: sqlite3.Connection, case_id: str, plan: dict[str, Any]
) -> None:
    for table in ("skill_proposals", "memory_proposals", "system_change_proposals"):
        connection.execute(
            f"UPDATE {table} SET status = 'superseded' WHERE case_id = ? AND status = 'proposed'",
            (case_id,),
        )
    for draft in plan.get("skill_drafts", []):
        connection.execute(
            "INSERT INTO skill_proposals VALUES (?, ?, ?, ?, ?, 'proposed', ?, NULL, NULL)",
            (_id(), case_id, draft["name"], draft["body"], draft["reason"], _now()),
        )
    for draft in plan.get("memory_drafts", []):
        document_ids = draft["source_document_ids"]
        if document_ids:
            found = connection.execute(
                f"SELECT id FROM documents WHERE id IN ({','.join('?' for _ in document_ids)})",
                document_ids,
            ).fetchall()
            if len(found) != len(set(document_ids)):
                raise ValueError("Memory draft references an unknown document")
        connection.execute(
            "INSERT INTO memory_proposals VALUES (?, ?, ?, ?, ?, 'proposed', ?, NULL, NULL)",
            (
                _id(),
                case_id,
                draft["content"],
                draft["reason"],
                json.dumps(document_ids),
                _now(),
            ),
        )
    for draft in plan.get("system_changes", []):
        connection.execute(
            "INSERT INTO system_change_proposals "
            "VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?, NULL, NULL)",
            (
                _id(),
                case_id,
                draft["area"],
                draft["title"],
                draft["specification"],
                draft["reason"],
                _now(),
            ),
        )


class KnowledgeStore:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.root = store.path.parent / "skills"

    def proposals_for_case(self, case_id: str) -> dict[str, list[dict[str, Any]]]:
        with self.store._connect() as connection:
            result = {}
            for key, table in (
                ("skill_drafts", "skill_proposals"),
                ("memory_drafts", "memory_proposals"),
                ("system_changes", "system_change_proposals"),
            ):
                rows = connection.execute(
                    f"SELECT * FROM {table} WHERE case_id = ? ORDER BY created_at",
                    (case_id,),
                )
                result[key] = [dict(row) for row in rows]
            for draft in result["memory_drafts"]:
                draft["source_document_ids"] = json.loads(draft.pop("source_document_ids_json"))
            for draft in result["skill_drafts"]:
                current = connection.execute(
                    "SELECT version, body FROM skill_versions WHERE name = ? AND status = 'active'",
                    (draft["name"],),
                ).fetchone()
                before = current["body"].splitlines(keepends=True) if current else []
                after = draft["body"].splitlines(keepends=True)
                draft["diff"] = "".join(
                    unified_diff(
                        before,
                        after,
                        fromfile=f"{draft['name']} v{current['version']}"
                        if current
                        else "/dev/null",
                        tofile=f"{draft['name']} proposed",
                    )
                )
            return result

    def active_skills(self) -> list[dict[str, Any]]:
        with self.store._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT name, version, sha256 FROM skill_versions "
                    "WHERE status = 'active' ORDER BY name"
                )
            ]

    def read_skill(self, name: str) -> dict[str, Any]:
        if name in BUILTIN_SKILLS:
            skill = load_builtin_skill(name)
            return {"name": name, "version": skill.version, "body": skill.body}
        if not SKILL_NAME.fullmatch(name):
            raise ValueError("Invalid skill name")
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT name, version, body, sha256 FROM skill_versions "
                "WHERE name = ? AND status = 'active'",
                (name,),
            ).fetchone()
            if row is None:
                raise KeyError(name)
            result = dict(row)
            source = self.root / name / f"v{result['version']}.md"
            try:
                body = source.read_text(encoding="utf-8")
            except OSError as error:
                raise ValueError("Skill source file is unavailable") from error
            if body != result["body"] or hashlib.sha256(body.encode()).hexdigest() != result.pop(
                "sha256"
            ):
                raise ValueError("Skill content integrity check failed")
            return result

    def review_skill(self, proposal_id: str, decision: str, manager: str) -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ValueError("Unknown decision")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM skill_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise KeyError(proposal_id)
            if row["status"] != "proposed":
                raise ValueError("Proposal has already been reviewed")
            name, body = row["name"], row["body"]
            if decision == "approved":
                if name in BUILTIN_SKILLS or not SKILL_NAME.fullmatch(name):
                    raise ValueError("Invalid or reserved skill name")
                if not body.strip() or len(body.encode()) > MAX_SKILL_BYTES:
                    raise ValueError("Skill content is empty or too large")
                version = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM skill_versions WHERE name = ?",
                    (name,),
                ).fetchone()[0]
                target = self.root / name / f"v{version}.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("x", encoding="utf-8", newline="\n") as source:
                    source.write(body)
                digest = hashlib.sha256(body.encode()).hexdigest()
                connection.execute(
                    "UPDATE skill_versions SET status = 'superseded' "
                    "WHERE name = ? AND status = 'active'",
                    (name,),
                )
                connection.execute(
                    "INSERT INTO skill_versions VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
                    (_id(), name, version, body, digest, proposal_id, _now()),
                )
            connection.execute(
                "UPDATE skill_proposals SET status = ?, reviewed_at = ?, reviewed_by = ? "
                "WHERE id = ?",
                (decision, _now(), manager, proposal_id),
            )
            self.store._event(
                connection,
                row["case_id"],
                "skill_reviewed",
                {"proposal_id": proposal_id, "decision": decision, "manager": manager},
            )
            return dict(
                connection.execute(
                    "SELECT * FROM skill_proposals WHERE id = ?", (proposal_id,)
                ).fetchone()
            )

    def review_memory(self, proposal_id: str, decision: str, manager: str) -> dict[str, Any]:
        return self._review_simple("memory_proposals", proposal_id, decision, manager)

    def review_system_change(self, proposal_id: str, decision: str, manager: str) -> dict[str, Any]:
        return self._review_simple("system_change_proposals", proposal_id, decision, manager)

    def _review_simple(
        self, table: str, proposal_id: str, decision: str, manager: str
    ) -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ValueError("Unknown decision")
        with self.store._connect() as connection:
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
            self.store._event(
                connection,
                row["case_id"],
                "memory_reviewed" if table == "memory_proposals" else "system_change_reviewed",
                {"proposal_id": proposal_id, "decision": decision, "manager": manager},
            )
            return dict(
                connection.execute(f"SELECT * FROM {table} WHERE id = ?", (proposal_id,)).fetchone()
            )

    def search_memory(self, query: str) -> list[dict[str, Any]]:
        words = re.findall(r"[a-z0-9]{3,}", query.lower())[:5]
        if not words:
            return []
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT id, case_id, content, source_document_ids_json, reviewed_at, "
                "reviewed_by FROM memory_proposals WHERE status = 'approved' "
                "ORDER BY reviewed_at DESC LIMIT 100"
            )
            matches = [
                dict(row) for row in rows if any(word in row["content"].lower() for word in words)
            ][:10]
        for match in matches:
            match["source_document_ids"] = json.loads(match.pop("source_document_ids_json"))
        return matches
