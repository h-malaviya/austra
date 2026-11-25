"""Execution gate for approved tenant actions and provider receipts."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import psycopg
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from psycopg.types.json import Jsonb

from austra.connectors import GMAIL_CONTENT_SCOPE
from austra.integrations.google_workspace import GOOGLE_WORKSPACE_SCOPE
from austra.integrations.providers import perform
from austra.integrations.secrets import ConnectionSecrets
from austra.tenant.context import TenantContext, tenant_transaction


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _validate(action: dict, arguments: dict) -> None:
    if not isinstance(arguments, dict):
        raise TypeError("Action arguments must be an object")
    if any(
        key.lower() in {"token", "api_key", "authorization", "password", "secret"}
        for key in arguments
    ):
        raise ValueError("Credentials are not action arguments")
    try:
        Draft202012Validator(action["input_schema"], format_checker=FormatChecker()).validate(
            arguments
        )
    except ValidationError as error:
        raise ValueError("Arguments do not match the approved action schema") from error


def _active_build(db: psycopg.Connection, context: TenantContext, build_id: UUID) -> tuple:
    row = db.execute(
        "SELECT r.blueprint_id, r.build_id, b.content_hash, a.manifest FROM releases r "
        "JOIN blueprints b ON b.organization_id = r.organization_id AND b.id = r.blueprint_id "
        "JOIN capability_build_artifacts a ON a.organization_id = r.organization_id "
        "AND a.build_id = r.build_id WHERE r.organization_id = %s AND r.state = 'active' AND r.build_id = %s",
        (context.organization_id, build_id),
    ).fetchone()
    if row is None or row[2] != row[3].get("blueprint_hash"):
        raise PermissionError("Approval is not pinned to the active blueprint and build")
    return row


def _authorized_action(manifest: dict, action_name: str, scopes: set[str]) -> bool:
    return any(
        action_name in item.get("content", {}).get("action_ids", [])
        and set(item["content"].get("requested_scopes", [])) <= scopes
        for item in manifest.get("allowed_actions", [])
    )


def _authority(
    manifest: dict, action_name: str, context: TenantContext, department_id: UUID
) -> None:
    key = (
        "command.approve_email"
        if action_name == "gmail.send_email"
        else f"integration.{action_name}"
    )
    now = datetime.now(UTC)
    matches = []
    for rule in manifest.get("decision_tables", []):
        if rule.get("content", {}).get("decision_key") != key:
            continue
        start = datetime.fromisoformat(str(rule["valid_from"]))
        end = datetime.fromisoformat(str(rule["valid_to"])) if rule.get("valid_to") else None
        if start <= now and (end is None or now < end):
            matches.append(rule)
    if len(matches) != 1:
        raise PermissionError("One current business authority rule is required")
    permission = matches[0]["content"].get("required_permission")
    if permission not in {"workflows.approve", "integrations.use"}:
        raise PermissionError("Business authority permission is invalid")
    context.require(permission, department_id)


def _load_action(
    db: psycopg.Connection, context: TenantContext, department_id: UUID, approval_id: UUID
) -> tuple:
    row = db.execute(
        "SELECT p.id, p.blueprint_id, p.build_id, p.action_name, p.action_schema_hash, "
        "p.effect, c.id, c.template_key, c.endpoint_url, c.granted_scopes, "
        "c.action_catalog, c.health FROM integration_approvals p "
        "JOIN integration_connections c ON c.organization_id = p.organization_id "
        "AND c.id = p.connection_id WHERE p.organization_id = %s AND p.department_id = %s "
        "AND p.id = %s AND p.revoked_at IS NULL AND c.revoked_at IS NULL",
        (context.organization_id, department_id, approval_id),
    ).fetchone()
    if row is None:
        raise LookupError("Approved action not found")
    action = next((item for item in row[10] if item["name"] == row[3]), None)
    if action is None or action["effect"] != row[5] or action["schema_hash"] != row[4]:
        raise ValueError("Action catalog changed; review the schema and approval")
    if row[11] != "healthy":
        raise ValueError("Connection needs repair or review; reconnect before retrying")
    if (row[7] != "remote_mcp" or GOOGLE_WORKSPACE_SCOPE in row[9]) and not set(action["scopes"]) <= set(row[9]):
        raise PermissionError("Connection lacks the action scope; reconnect")
    active = _active_build(db, context, row[2])
    if row[1] != active[0] or row[2] != active[1]:
        raise PermissionError("Approval is not pinned to the active blueprint and build")
    if not _authorized_action(active[3], row[3], set(row[9]) | set(action["scopes"])):
        raise PermissionError("Active blueprint does not permit this integration action")
    _authority(active[3], row[3], context, department_id)
    return row, action


def approve_workflow(
    db: psycopg.Connection,
    context: TenantContext,
    root: Path,
    department_id: UUID,
    connection_id: UUID,
    build_id: UUID,
    action_name: str,
    fixture: dict,
    trigger_fixture: dict,
    mapping: dict[str, str] | None = None,
) -> dict:
    context.require("workflows.approve", department_id)
    if (
        context.actor.role != "org_admin"
        or not isinstance(trigger_fixture.get("event_id"), str)
        or not trigger_fixture["event_id"].strip()
    ):
        raise PermissionError("Admin approval and a replayable trigger sample are required")
    mapping = mapping or {}
    if mapping:
        payload = trigger_fixture.get("payload")
        if (
            not isinstance(payload, dict)
            or any(source not in payload for source in mapping.values())
            or any(field not in fixture for field in mapping)
        ):
            raise ValueError("Trigger sample does not contain mapped fields")
        mapped = {field: payload[source] for field, source in mapping.items()}
        if any(fixture.get(field) != value for field, value in mapped.items()):
            raise ValueError("Mapped trigger sample does not match the action fixture")
    with tenant_transaction(db, context):
        build = db.execute(
            "SELECT b.blueprint_id, a.manifest, a.preview_state FROM builds b "
            "JOIN capability_build_artifacts a ON a.organization_id = b.organization_id "
            "AND a.build_id = b.id WHERE b.organization_id = %s AND b.id = %s",
            (context.organization_id, build_id),
        ).fetchone()
        conn = db.execute(
            "SELECT template_key, endpoint_url, granted_scopes, action_catalog, health "
            "FROM integration_connections WHERE organization_id = %s AND id = %s "
            "AND revoked_at IS NULL",
            (context.organization_id, connection_id),
        ).fetchone()
        if build is None or build[2] != "accepted" or conn is None or conn[4] != "healthy":
            raise ValueError("Accepted build and healthy connection required")
        action = next((item for item in conn[3] if item["name"] == action_name), None)
        if action is None or action["effect"] not in {"read", "write"}:
            raise ValueError("Action effect must be reviewed")
        if (conn[0] != "remote_mcp" or GOOGLE_WORKSPACE_SCOPE in conn[2]) and not set(action["scopes"]) <= set(conn[2]):
            raise PermissionError("Connection lacks required scope")
        if not _authorized_action(build[1], action_name, set(conn[2]) | set(action["scopes"])):
            raise PermissionError("Blueprint does not permit this action")
        _authority(build[1], action_name, context, department_id)
        _validate(action, fixture)
        if action["effect"] == "read":
            secrets = ConnectionSecrets(root).read(context.organization_id, connection_id)
            result = perform(conn[0], action_name, fixture, secrets, conn[1])
            if not isinstance(result, dict):
                raise ValueError("Read test did not return a provider result")
        preview = (
            {
                "action": action_name,
                "effect": "write",
                "arguments": fixture,
                "trigger": trigger_fixture,
            }
            if action["effect"] == "write"
            else None
        )
        approved = db.execute(
            "INSERT INTO integration_approvals (organization_id, department_id, connection_id, "
            "blueprint_id, build_id, action_name, action_schema_hash, effect, fixture, "
            "trigger_fixture, mapping, write_preview, approved_by) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                context.organization_id,
                department_id,
                connection_id,
                build[0],
                build_id,
                action_name,
                action["schema_hash"],
                action["effect"],
                Jsonb(fixture),
                Jsonb(trigger_fixture),
                Jsonb(mapping),
                Jsonb(preview) if preview else None,
                context.actor.user_id,
            ),
        ).fetchone()
    return {
        "approved": True,
        "approval_id": str(approved[0]),
        "effect": action["effect"],
        "preview": preview,
    }


def preview_action(
    db: psycopg.Connection,
    context: TenantContext,
    department_id: UUID,
    approval_id: UUID,
    arguments: dict,
    source_records: list[dict],
    reason: str = "",
    *,
    workflow_email: dict | None = None,
) -> dict:
    context.require("integrations.use", department_id)
    context.require("workflows.run", department_id)
    with tenant_transaction(db, context):
        row, action = _load_action(db, context, department_id, approval_id)
        if row[3] == "gmail.send_email":
            manifest = _active_build(db, context, row[2])[3]
            if manifest.get("engine") == "state_machine" and (
                not workflow_email or workflow_email["build_id"] != str(row[2])
            ):
                raise PermissionError("Prepare a record email in the released workflow")
            if workflow_email and workflow_email["content_required"] and GMAIL_CONTENT_SCOPE not in row[9]:
                raise PermissionError("Authorize Gmail reply text and attachments before sending")
        _validate(action, arguments)
        if action["effect"] == "write":
            context.require("workflows.approve", department_id)
            if not reason.strip():
                raise ValueError("A write preview needs its business reason")
        for source in source_records:
            record = db.execute(
                "SELECT revision FROM capability_records WHERE "
                "organization_id = %s AND department_id = %s AND id = %s",
                (context.organization_id, department_id, UUID(source["id"])),
            ).fetchone()
            if record is None or record[0] != source["revision"]:
                raise ValueError("Source record changed; prepare a fresh preview")
        account = db.execute(
            "SELECT account_label FROM integration_connections WHERE "
            "organization_id = %s AND id = %s",
            (context.organization_id, row[6]),
        ).fetchone()[0]
        preview = {
            "action": row[3],
            "effect": action["effect"],
            "account": account,
            "account_id": str(row[6]),
            "arguments": arguments,
            "source_records": source_records,
            "reason": reason.strip(),
            "blueprint_id": str(row[1]),
            "build_id": str(row[2]),
        }
        if workflow_email:
            preview["workflow_email"] = workflow_email
        proposal = db.execute(
            "INSERT INTO integration_action_proposals (organization_id, department_id, "
            "approval_id, actor_user_id, argument_hash, arguments, preview) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                context.organization_id,
                department_id,
                approval_id,
                context.actor.user_id,
                digest(arguments),
                Jsonb(arguments),
                Jsonb(preview),
            ),
        ).fetchone()
    return {"proposal_id": str(proposal[0]), "preview": preview}


def receive_webhook(
    db: psycopg.Connection,
    context: TenantContext,
    connection_id: UUID,
    event_id: str,
    payload: dict,
) -> bool:
    context.require("integrations.use")
    if not event_id or len(event_id) > 200:
        raise ValueError("Provider event ID is required")
    with tenant_transaction(db, context):
        row = db.execute(
            "SELECT 1 FROM integration_connections WHERE organization_id = %s "
            "AND id = %s AND health = 'healthy' AND revoked_at IS NULL",
            (context.organization_id, connection_id),
        ).fetchone()
        if row is None:
            raise LookupError("Healthy connection not found")
        inserted = db.execute(
            "INSERT INTO integration_webhook_deliveries (organization_id, connection_id, "
            "provider_event_id, payload_hash) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING "
            "RETURNING provider_event_id",
            (context.organization_id, connection_id, event_id, digest(payload)),
        ).fetchone()
        if inserted is None:
            prior = db.execute(
                "SELECT payload_hash FROM integration_webhook_deliveries WHERE "
                "organization_id = %s AND connection_id = %s "
                "AND provider_event_id = %s",
                (context.organization_id, connection_id, event_id),
            ).fetchone()
            if prior is None or prior[0] != digest(payload):
                raise ValueError("Provider event ID was reused with another payload")
    return inserted is not None
