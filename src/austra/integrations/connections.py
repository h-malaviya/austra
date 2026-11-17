"""Tenant owned connection setup, review, health, and revocation."""

import hashlib
import json
import re
from pathlib import Path
from uuid import UUID

import psycopg
from jsonschema import Draft202012Validator, SchemaError
from psycopg.types.json import Jsonb

from austra.build_release.monitoring import open_review
from austra.integrations.catalog import TEMPLATES
from austra.integrations.providers import discover, github_identity, gmail_identity, slack_identity
from austra.integrations.secrets import ConnectionSecrets
from austra.mcp_gateway import ExternalToolError
from austra.tenant.context import TenantContext, tenant_transaction


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _remote_catalog(tools: list[dict]) -> list[dict]:
    if not tools or len(tools) > 100:
        raise ValueError("MCP server must expose between 1 and 100 tools")
    found = []
    names = set()
    for tool in tools:
        name = tool.get("name")
        schema = tool.get("input_schema")
        if not isinstance(name, str) or not name or name in names or not isinstance(schema, dict):
            raise ValueError("MCP tool names and schemas must be unique and valid")
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as error:
            raise ValueError("MCP tool input schema is invalid") from error
        names.add(name)
        found.append(
            {
                "name": f"remote_mcp.{name}",
                "provider_name": name,
                "input_schema": schema,
                "schema_hash": _digest({"name": name, "input_schema": schema}),
                "effect": "unreviewed",
                "scopes": [],
                "receipt_field": "",
            }
        )
    return found


def _public(row: tuple) -> dict:
    return {
        "id": str(row[0]),
        "template": row[1],
        "account": row[2],
        "scopes": row[3],
        "actions": row[4],
        "health": row[5],
        "repair_reason": row[6],
        "endpoint_url": row[7],
    }


def list_connections(db: psycopg.Connection, context: TenantContext) -> list[dict]:
    context.require("integrations.use")
    with tenant_transaction(db, context):
        rows = db.execute(
            "SELECT id, template_key, account_label, granted_scopes, action_catalog, health, "
            "repair_reason, endpoint_url FROM integration_connections WHERE organization_id = %s "
            "AND revoked_at IS NULL ORDER BY created_at",
            (context.organization_id,),
        ).fetchall()
    return [_public(row) for row in rows]


def _credentials(template: str, values: dict[str, str]) -> dict[str, str]:
    fields = set(TEMPLATES[template].private_fields)
    if set(values) != fields:
        raise ValueError("Provide exactly the private fields listed for this app")
    if template != "remote_mcp" and any(not value for value in values.values()):
        raise ValueError("Private credential fields are required for this app")
    return values


def connect_key(
    db: psycopg.Connection,
    context: TenantContext,
    root: Path,
    template: str,
    values: dict[str, str],
    endpoint_url: str | None = None,
    account_label: str | None = None,
    connection_id: UUID | None = None,
) -> dict:
    context.require("integrations.use")
    if context.actor.role != "org_admin" or template not in {"github", "slack", "remote_mcp"}:
        raise PermissionError("Organization admin and a private key template required")
    credentials = _credentials(template, values)
    if template in {"github", "slack"}:
        if endpoint_url:
            raise ValueError("Curated app endpoint is fixed by its reviewed template")
        if template == "github":
            account, scopes = github_identity(credentials["token"])
        else:
            account, scopes = slack_identity(credentials)
            if account != credentials["team_id"]:
                raise ValueError("Slack bot belongs to another workspace")
        if not set(TEMPLATES[template].scopes) <= set(scopes):
            raise ValueError("App token lacks required scope; reconnect")
        actions = [
            action.public() | {"receipt_field": action.receipt_field}
            for action in TEMPLATES[template].actions
        ]
    else:
        if not endpoint_url or not account_label or not account_label.strip():
            raise ValueError("Remote MCP needs its HTTPS URL and an account label")
        actions = _remote_catalog(discover(endpoint_url, credentials["token"]))
        account, scopes = account_label.strip(), ()
    vault = ConnectionSecrets(root)
    with tenant_transaction(db, context):
        if connection_id is None:
            row = db.execute(
                "INSERT INTO integration_connections (organization_id, owner_user_id, template_key, "
                "endpoint_url, account_label, granted_scopes, action_catalog, catalog_hash, "
                "health, checked_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'healthy', now()) "
                "RETURNING id",
                (
                    context.organization_id,
                    context.actor.user_id,
                    template,
                    endpoint_url,
                    account,
                    list(scopes),
                    Jsonb(actions),
                    _digest(actions),
                ),
            ).fetchone()
        else:
            existing = db.execute(
                "SELECT template_key, account_label, endpoint_url, "
                "action_catalog FROM integration_connections WHERE "
                "organization_id = %s AND id = %s AND revoked_at IS NULL "
                "FOR UPDATE",
                (context.organization_id, connection_id),
            ).fetchone()
            if existing is None or existing[:3] != (template, account, endpoint_url):
                raise ValueError("Reconnect must use the same app, account, and endpoint")
            old_schemas = {item["name"]: item["schema_hash"] for item in existing[3]}
            new_schemas = {item["name"]: item["schema_hash"] for item in actions}
            changed = old_schemas != new_schemas
            if changed:
                db.execute(
                    "UPDATE integration_approvals SET revoked_at = now() WHERE "
                    "organization_id = %s AND connection_id = %s AND revoked_at IS NULL",
                    (context.organization_id, connection_id),
                )
                active = db.execute(
                    "SELECT blueprint_id FROM releases WHERE organization_id = %s "
                    "AND state = 'active'",
                    (context.organization_id,),
                ).fetchone()
                open_review(
                    db,
                    context,
                    trigger="schema_drift",
                    subject_id=connection_id,
                    owner_user_id=context.actor.user_id,
                    blueprint_id=active[0] if active else None,
                    citations=[
                        {
                            "connection_id": str(connection_id),
                            "old_schema_hashes": old_schemas,
                            "new_schema_hashes": new_schemas,
                        }
                    ],
                    impact="Connector actions changed; prior approvals are revoked.",
                    next_step="Review action schemas, scopes, mappings, and contract tests.",
                )
            else:
                actions = existing[3]
            review_required = changed and template == "remote_mcp"
            db.execute(
                "UPDATE integration_connections SET granted_scopes = %s, "
                "action_catalog = %s, catalog_hash = %s, health = %s, "
                "repair_reason = %s, checked_at = now() "
                "WHERE organization_id = %s AND id = %s",
                (
                    list(scopes),
                    Jsonb(actions),
                    _digest(actions),
                    "schema_review" if review_required else "healthy",
                    "Tool schema changed; review actions and tests" if review_required else None,
                    context.organization_id,
                    connection_id,
                ),
            )
            row = (connection_id,)
        vault.save(context.organization_id, row[0], credentials)
        return {
            "id": str(row[0]),
            "template": template,
            "account": account,
            "scopes": scopes,
            "actions": actions,
            "health": "schema_review"
            if connection_id is not None and review_required
            else "healthy",
        }


def connect_gmail(
    db: psycopg.Connection,
    context: TenantContext,
    root: Path,
    refresh_token: str,
    account: str,
    scopes: tuple[str, ...],
    client_id: str,
    client_secret: str,
    connection_id: UUID | None = None,
) -> dict:
    context.require("integrations.use")
    if context.actor.role != "org_admin":
        raise PermissionError("Organization admin required")
    required = set(TEMPLATES["gmail"].scopes)
    if not required <= set(scopes):
        raise ValueError("Gmail consent is missing metadata or send permission; reconnect")
    credentials = {
        "GMAIL_CLIENT_ID": client_id,
        "GMAIL_CLIENT_SECRET": client_secret,
        "GMAIL_REFRESH_TOKEN": refresh_token,
        "GMAIL_SENDER_EMAIL": account,
    }
    if gmail_identity(credentials).casefold() != account.casefold():
        raise ValueError("Gmail account verification did not match the consented mailbox")
    actions = [
        action.public() | {"receipt_field": action.receipt_field}
        for action in TEMPLATES["gmail"].actions
    ]
    vault = ConnectionSecrets(root)
    with tenant_transaction(db, context):
        if connection_id is None:
            row = db.execute(
                "INSERT INTO integration_connections (organization_id, owner_user_id, template_key, "
                "account_label, granted_scopes, action_catalog, catalog_hash, health, checked_at) "
                "VALUES (%s, %s, 'gmail', %s, %s, %s, %s, 'healthy', now()) RETURNING id",
                (
                    context.organization_id,
                    context.actor.user_id,
                    account,
                    list(scopes),
                    Jsonb(actions),
                    _digest(actions),
                ),
            ).fetchone()
        else:
            existing = db.execute(
                "SELECT action_catalog FROM integration_connections WHERE "
                "organization_id = %s AND id = %s AND template_key = 'gmail' "
                "AND lower(account_label) = lower(%s) AND revoked_at IS NULL "
                "FOR UPDATE",
                (context.organization_id, connection_id, account),
            ).fetchone()
            if existing is None:
                raise ValueError("Reconnect must use the same Gmail account")
            if _digest(existing[0]) != _digest(actions):
                db.execute(
                    "UPDATE integration_approvals SET revoked_at = now() WHERE "
                    "organization_id = %s AND connection_id = %s AND revoked_at IS NULL",
                    (context.organization_id, connection_id),
                )
                active = db.execute(
                    "SELECT blueprint_id FROM releases WHERE organization_id = %s "
                    "AND state = 'active'",
                    (context.organization_id,),
                ).fetchone()
                open_review(
                    db,
                    context,
                    trigger="schema_drift",
                    subject_id=connection_id,
                    owner_user_id=context.actor.user_id,
                    blueprint_id=active[0] if active else None,
                    citations=[
                        {
                            "connection_id": str(connection_id),
                            "old_catalog_hash": _digest(existing[0]),
                            "new_catalog_hash": _digest(actions),
                        }
                    ],
                    impact="Gmail action schema changed; prior approvals are revoked.",
                    next_step="Review the changed actions and rerun connection tests.",
                )
            row = db.execute(
                "UPDATE integration_connections SET granted_scopes = %s, "
                "action_catalog = %s, catalog_hash = %s, "
                "health = 'healthy', repair_reason = NULL, checked_at = now() "
                "WHERE organization_id = %s AND id = %s RETURNING id",
                (
                    list(scopes),
                    Jsonb(actions),
                    _digest(actions),
                    context.organization_id,
                    connection_id,
                ),
            ).fetchone()
        vault.save(context.organization_id, row[0], credentials)
        db.execute(
            "INSERT INTO capability_connection_health (organization_id, provider_key, state) "
            "VALUES (%s, 'email', 'healthy') ON CONFLICT (organization_id, provider_key) "
            "DO UPDATE SET state = 'healthy', checked_at = now()",
            (context.organization_id,),
        )
    return {
        "id": str(row[0]),
        "template": "gmail",
        "account": account,
        "scopes": scopes,
        "actions": actions,
        "health": "healthy",
    }


def review_remote(
    db: psycopg.Connection,
    context: TenantContext,
    connection_id: UUID,
    action_name: str,
    effect: str,
    scopes: list[str],
    receipt_field: str,
) -> dict:
    context.require("integrations.use")
    if context.actor.role != "org_admin" or effect not in {"read", "write"}:
        raise PermissionError("Organization admin action review required")
    if effect == "write" and not receipt_field:
        raise ValueError("A write needs a provider receipt field")
    if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9:/._-]{0,199}", scope) for scope in scopes) or (
        receipt_field and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", receipt_field)
    ):
        raise ValueError("Scope or receipt field is invalid")
    with tenant_transaction(db, context):
        row = db.execute(
            "SELECT action_catalog FROM integration_connections WHERE organization_id = %s "
            "AND id = %s AND template_key = 'remote_mcp' "
            "AND health IN ('healthy', 'schema_review') "
            "AND revoked_at IS NULL FOR UPDATE",
            (context.organization_id, connection_id),
        ).fetchone()
        if row is None:
            raise LookupError("Healthy remote MCP connection not found")
        actions = row[0]
        action = next((item for item in actions if item["name"] == action_name), None)
        if action is None:
            raise LookupError("MCP action not discovered")
        action.update(effect=effect, scopes=sorted(set(scopes)), receipt_field=receipt_field)
        ready = all(item["effect"] in {"read", "write"} for item in actions)
        db.execute(
            "UPDATE integration_connections SET action_catalog = %s, catalog_hash = %s, "
            "health = %s, repair_reason = %s "
            "WHERE organization_id = %s AND id = %s",
            (
                Jsonb(actions),
                _digest(actions),
                "healthy" if ready else "schema_review",
                None if ready else "Review every discovered action",
                context.organization_id,
                connection_id,
            ),
        )
        db.execute(
            "UPDATE integration_approvals SET revoked_at = now() WHERE "
            "organization_id = %s AND connection_id = %s AND action_name = %s "
            "AND revoked_at IS NULL",
            (context.organization_id, connection_id, action_name),
        )
        return action


def check_health(
    db: psycopg.Connection, context: TenantContext, root: Path, connection_id: UUID
) -> dict:
    context.require("integrations.use")
    with tenant_transaction(db, context):
        row = db.execute(
            "SELECT template_key, endpoint_url, account_label, action_catalog, health, "
            "owner_user_id "
            "FROM integration_connections WHERE organization_id = %s AND id = %s "
            "AND revoked_at IS NULL",
            (context.organization_id, connection_id),
        ).fetchone()
        if row is None:
            raise LookupError("Connection not found")
    state, reason = "healthy", None
    discovered = None
    schema_changed = False
    try:
        secrets = ConnectionSecrets(root).read(context.organization_id, connection_id)
        if row[0] == "gmail":
            if gmail_identity(secrets).casefold() != row[2].casefold():
                raise ValueError("Mailbox changed; reconnect")
        elif row[0] == "github":
            account, scopes = github_identity(secrets["token"])
            if account != row[2] or "repo" not in scopes:
                raise ValueError("GitHub account or scope changed; reconnect")
        elif row[0] == "slack":
            account, scopes = slack_identity(secrets)
            if account != row[2] or "chat:write" not in scopes:
                raise ValueError("Slack account or scope changed; reconnect")
        else:
            discovered = _remote_catalog(discover(row[1], secrets["token"]))
            old = {item["name"]: item["schema_hash"] for item in row[3]}
            new = {item["name"]: item["schema_hash"] for item in discovered}
            if old != new:
                schema_changed = True
                state, reason = "schema_review", "Tool schema changed; review actions and tests"
            elif row[4] == "schema_review" and any(
                item["effect"] == "unreviewed" for item in row[3]
            ):
                state, reason = "schema_review", "Review every discovered action"
    except (RuntimeError, ValueError, ExternalToolError):
        if state != "schema_review":
            state, reason = "repair", "Provider check failed; reconnect or repair"
    if state == "healthy" and row[0] != "remote_mcp":
        expected = {action.name: action.schema_hash for action in TEMPLATES[row[0]].actions}
        stored = {action["name"]: action["schema_hash"] for action in row[3]}
        if expected != stored:
            state, reason = "schema_review", "App action template changed; reconnect and retest"
    with tenant_transaction(db, context):
        if state == "schema_review" and (schema_changed or row[4] != "schema_review"):
            active = db.execute(
                "SELECT blueprint_id FROM releases WHERE organization_id = %s AND state = 'active'",
                (context.organization_id,),
            ).fetchone()
            open_review(
                db,
                context,
                trigger="schema_drift",
                subject_id=connection_id,
                owner_user_id=row[5],
                blueprint_id=active[0] if active else None,
                citations=[
                    {
                        "connection_id": str(connection_id),
                        "catalog_hash": _digest(row[3]),
                        "observed_catalog_hash": _digest(discovered) if discovered else None,
                    }
                ],
                impact="Connector schema changed; affected actions cannot run.",
                next_step="Review the changed schema and rerun connection contract tests.",
            )
        if state == "repair" and row[4] != "repair":
            db.execute(
                "INSERT INTO operational_metrics (organization_id, metric, value) "
                "VALUES (%s, 'connection_failure', 1)",
                (context.organization_id,),
            )
        if state == "schema_review":
            db.execute(
                "UPDATE integration_approvals SET revoked_at = now() WHERE "
                "organization_id = %s AND connection_id = %s AND revoked_at IS NULL",
                (context.organization_id, connection_id),
            )
        if discovered is not None and schema_changed:
            db.execute(
                "UPDATE integration_connections SET action_catalog = %s, "
                "catalog_hash = %s WHERE organization_id = %s AND id = %s",
                (Jsonb(discovered), _digest(discovered), context.organization_id, connection_id),
            )
        db.execute(
            "UPDATE integration_connections SET health = %s, repair_reason = %s, "
            "checked_at = now() WHERE organization_id = %s AND id = %s",
            (state, reason, context.organization_id, connection_id),
        )
        if row[0] == "gmail":
            db.execute(
                "INSERT INTO capability_connection_health (organization_id, "
                "provider_key, state) VALUES (%s, 'email', %s) "
                "ON CONFLICT (organization_id, provider_key) DO UPDATE SET "
                "state = EXCLUDED.state, checked_at = now()",
                (context.organization_id, "healthy" if state == "healthy" else "disconnected"),
            )
    return {"health": state, "repair_reason": reason, "account": row[2]}


def revoke(db: psycopg.Connection, context: TenantContext, root: Path, connection_id: UUID) -> None:
    context.require("integrations.use")
    if context.actor.role != "org_admin":
        raise PermissionError("Organization admin required")
    with tenant_transaction(db, context):
        row = db.execute(
            "UPDATE integration_connections SET health = 'revoked', "
            "revoked_at = now(), repair_reason = 'Disconnected by administrator' "
            "WHERE organization_id = %s AND id = %s AND revoked_at IS NULL "
            "RETURNING id, template_key",
            (context.organization_id, connection_id),
        ).fetchone()
        if row is None:
            raise LookupError("Connection not found")
        db.execute(
            "UPDATE integration_approvals SET revoked_at = now() "
            "WHERE organization_id = %s AND connection_id = %s AND revoked_at IS NULL",
            (context.organization_id, connection_id),
        )
        if row[1] == "gmail":
            db.execute(
                "UPDATE capability_connection_health SET state = 'disconnected', "
                "checked_at = now() WHERE organization_id = %s AND provider_key = 'email'",
                (context.organization_id,),
            )
    ConnectionSecrets(root).revoke(context.organization_id, connection_id)
