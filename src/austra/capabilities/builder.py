"""Build a reviewed manifest from a locked semantic blueprint."""

import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from austra.capabilities.contracts import PAYLOADS
from austra.capabilities.state_machine import compile_state_machine
from austra.tenant.context import TenantContext, tenant_transaction

CAPABILITY_KEY = "so_inventory_po_email"


def compile_manifest(source: dict, blueprint_hash: str) -> dict:
    """Reject incomplete business meaning instead of filling it with defaults."""
    revisions = source.get("revisions", [])
    if any(
        item.get("kind") == "capability"
        and item.get("content", {}).get("engine") == "state_machine"
        for item in revisions
    ):
        return compile_state_machine(source, blueprint_hash)
    if not any(
        item.get("kind") == "capability" and item.get("key") == CAPABILITY_KEY for item in revisions
    ):
        raise ValueError("Locked blueprint does not include the first capability")
    rules = {}
    for item in source.get("decision_tables", []):
        key = item.get("content", {}).get("decision_key")
        if key not in {f"command.{action}" for action in PAYLOADS if action != "transition_record"}:
            continue
        if key in rules:
            raise ValueError(f"Overlapping capability authority: {key}")
        if not item.get("id") or not item.get("valid_from"):
            raise ValueError("Authority revision identity and valid time are required")
        start = datetime.fromisoformat(str(item["valid_from"]))
        end = datetime.fromisoformat(str(item["valid_to"])) if item.get("valid_to") else None
        if start.tzinfo is None or (end is not None and (end.tzinfo is None or end <= start)):
            raise ValueError("Authority must have a valid dated interval")
        permission = item["content"].get("required_permission")
        expected = (
            "workflows.approve"
            if key in {"command.approve_po", "command.approve_email"}
            else "workflows.run"
        )
        if permission != expected:
            raise ValueError(f"Authority permission mismatch: {key}")
        rules[key] = {
            "id": str(item["id"]),
            "content": item["content"],
            "valid_from": start.isoformat(),
            "valid_to": end.isoformat() if end else None,
        }
    if set(rules) != {f"command.{action}" for action in PAYLOADS if action != "transition_record"}:
        raise ValueError("Every first-slice command needs a confirmed authority rule")
    approval = rules["command.approve_po"]["content"]
    try:
        limit = Decimal(str(approval["limit"]))
    except (KeyError, InvalidOperation, TypeError) as error:
        raise ValueError("PO approval needs a decimal limit") from error
    if (
        not limit.is_finite()
        or limit < 0
        or not isinstance(approval.get("unit"), str)
        or len(approval["unit"]) != 3
    ):
        raise ValueError("PO approval needs a nonnegative limit and currency unit")
    routes = source.get("event_routes", [])
    if not any(
        item.get("kind") == "workflow"
        and item.get("content", {}).get("capability") == CAPABILITY_KEY
        for item in routes
    ):
        raise ValueError("Blueprint needs the first-slice workflow")
    integration_rules = [
        item
        for item in source.get("decision_tables", [])
        if str(item.get("content", {}).get("decision_key", "")).startswith("integration.")
    ]
    return {
        "capability": CAPABILITY_KEY,
        "blueprint_hash": blueprint_hash,
        "engine": "reviewed_handler",
        "capabilities": [
            {
                "key": CAPABILITY_KEY,
                "engine": "reviewed_handler",
                "owner": "approved_blueprint",
                "record_kinds": ["sales_order", "stock_lot", "purchase_order", "email_proposal"],
                "transitions": [
                    {"key": "submit_order", "from": None, "to": "open"},
                    {"key": "reserve_stock", "from": "open", "to": "reserved"},
                    {"key": "draft_po", "from": None, "to": "draft"},
                    {"key": "approve_po", "from": "draft", "to": "approved_or_escalated"},
                    {"key": "propose_email", "from": None, "to": "proposed"},
                    {"key": "approve_email", "from": "proposed", "to": "queued"},
                ],
                "failure_path": "repair_or_escalate",
                "idempotency": "organization_department_key",
                "audit": "runtime_event_and_record_revision",
            }
        ],
        "commands": sorted(action for action in PAYLOADS if action != "transition_record"),
        "decision_tables": list(rules.values()) + integration_rules,
        "projections": source.get("projections", []),
        "mappings": source.get("typed_mappings", []),
        "event_routes": routes,
        "allowed_actions": source.get("allowed_actions", []),
    }


def build_capability(
    connection: psycopg.Connection, context: TenantContext, build_id: UUID, rollback_plan: str
) -> dict:
    context.require("workflows.approve")
    if not rollback_plan.strip():
        raise ValueError("Rollback plan is required")
    with tenant_transaction(connection, context):
        row = connection.execute(
            "SELECT b.state, p.content_hash, m.manifest FROM builds b "
            "JOIN blueprints p ON p.id = b.blueprint_id "
            "JOIN blueprint_manifests m ON m.blueprint_id = p.id "
            "JOIN onboarding_cases c ON c.blueprint_id = p.id "
            "WHERE b.organization_id = %s AND b.id = %s AND c.state = 'locked_candidate'",
            (context.organization_id, build_id),
        ).fetchone()
        if row is None or row[0] not in {"building", "fixing"}:
            raise ValueError("Locked candidate in building or fixing state required")
        manifest = compile_manifest(row[2], row[1])
        if manifest.get("engine") == "state_machine":
            fixtures = {
                "acceptance_examples": {
                    item["key"]: item["acceptance_examples"] for item in manifest["capabilities"]
                },
                "rollback_plan": rollback_plan.strip(),
            }
        else:
            approval = next(
                item
                for item in manifest["decision_tables"]
                if item["content"]["decision_key"] == "command.approve_po"
            )
            limit = Decimal(str(approval["content"]["limit"]))
            fixtures = {
                "normal": {"quantity_kg": "10", "available_kg": "10", "expected": "reserved"},
                "boundary": {"approval_total": str(limit), "expected": "approved"},
                "adverse": {
                    "provider": "disconnected",
                    "stock": "insufficient",
                    "expected": "repair_connection",
                },
                "rollback_plan": rollback_plan.strip(),
            }
        artifact_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        connection.execute(
            "INSERT INTO capability_build_artifacts (organization_id, build_id, blueprint_hash, "
            "manifest, fixtures, rollback_plan) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                context.organization_id,
                build_id,
                row[1],
                Jsonb(manifest),
                Jsonb(fixtures),
                rollback_plan.strip(),
            ),
        )
        return {
            "build_id": build_id,
            "blueprint_hash": row[1],
            "artifact_hash": artifact_hash,
            "manifest": manifest,
            "fixtures": fixtures,
        }
