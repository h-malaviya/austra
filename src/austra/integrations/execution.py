"""Idempotent provider execution with explicit uncertain outcomes."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
import psycopg

from austra.integrations.catalog import TEMPLATES
from austra.integrations.connections import check_health
from austra.integrations.gateway import _load_action, _validate, digest
from austra.integrations.providers import discover, perform, safe_mcp_url
from austra.integrations.secrets import ConnectionSecrets
from austra.mcp_gateway import ExternalToolError
from austra.tenant.context import TenantContext, tenant_transaction


def _sanitize(value: object, credentials: dict[str, str]) -> object:
    sensitive = {"token", "secret", "password", "authorization", "api_key", "refresh_token"}
    if isinstance(value, dict):
        return {key: _sanitize(item, credentials) for key, item in value.items()
                if not any(part in key.lower() for part in sensitive)}
    if isinstance(value, list):
        return [_sanitize(item, credentials) for item in value]
    if isinstance(value, str):
        for secret in credentials.values():
            if secret:
                value = value.replace(secret, "[redacted]")
    return value


def _provider_result(action: dict, response: object,
                     credentials: dict[str, str]) -> tuple[str, dict]:
    if not isinstance(response, dict) or len(json.dumps(response, default=str)) > 100_000:
        raise ValueError("Provider returned an invalid or oversized result")
    clean = _sanitize(response, credentials)
    if action["effect"] == "write":
        field = action.get("receipt_field", "")
        receipt = response.get(field)
        if not isinstance(receipt, (str, int)) or not str(receipt):
            raise ValueError("Provider did not confirm the write with a receipt")
        if any(secret and secret in str(receipt) for secret in credentials.values()):
            raise ValueError("Provider receipt contains private credential material")
        if response.get("sent") is False or response.get("ok") is False:
            raise ValueError("Provider rejected the write")
        return str(receipt), {"receipt": str(receipt)}
    return digest(clean), {"result": clean}


def execute_proposal(db: psycopg.Connection, context: TenantContext, root: Path,
                     department_id: UUID, proposal_id: UUID, idempotency_key: str) -> dict:
    context.require("integrations.use", department_id)
    context.require("workflows.run", department_id)
    if not idempotency_key or len(idempotency_key) > 200:
        raise ValueError("Idempotency key is required")
    with tenant_transaction(db, context):
        proposal = db.execute(
            "SELECT approval_id, actor_user_id, argument_hash, arguments, preview, expires_at "
            "FROM integration_action_proposals WHERE organization_id = %s "
            "AND department_id = %s AND id = %s",
            (context.organization_id, department_id, proposal_id)).fetchone()
        if proposal is None:
            raise LookupError("Action preview not found")
        if proposal[1] != context.actor.user_id or proposal[5] <= datetime.now(UTC):
            raise PermissionError("Action preview expired or belongs to another actor")
    health = check_health(db, context, root, UUID(proposal[4]["account_id"]))
    if health["health"] != "healthy":
        raise ValueError(health["repair_reason"] or "Reconnect this app")
    with tenant_transaction(db, context):
        _load_action(db, context, department_id, proposal[0])
        prior = db.execute(
            "SELECT argument_hash, actor_user_id, state, receipt, approval_id, proposal_id "
            "FROM integration_executions "
            "WHERE organization_id = %s AND department_id = %s AND idempotency_key = %s",
            (context.organization_id, department_id, idempotency_key)).fetchone()
        if prior is not None:
            if (prior[0] != proposal[2] or prior[1] != context.actor.user_id
                    or prior[4] != proposal[0] or prior[5] != proposal_id):
                raise ValueError("Idempotency key was used for another action")
            return {"state": prior[2], "receipt": prior[3]}
    with tenant_transaction(db, context):
        row, action = _load_action(db, context, department_id, proposal[0])
        _validate(action, proposal[3])
        if digest(proposal[3]) != proposal[2]:
            raise ValueError("Preview arguments changed")
        if action["effect"] == "write":
            context.require("workflows.approve", department_id)
        for source in proposal[4].get("source_records", []):
            current = db.execute("SELECT revision FROM capability_records WHERE "
                                 "organization_id = %s AND department_id = %s AND id = %s",
                                 (context.organization_id, department_id,
                                  UUID(source["id"]))).fetchone()
            if current is None or current[0] != source["revision"]:
                raise ValueError("Source record changed after preview")
        if row[7] in TEMPLATES:
            expected = next((item for item in TEMPLATES[row[7]].actions
                             if item.name == row[3]), None)
            if expected and expected.schema_hash != action["schema_hash"]:
                raise ValueError("Reviewed provider template changed; review the action")
        db.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                   (f"{context.organization_id}:{row[6]}",))
        recent = db.execute(
            "SELECT count(*) FROM integration_executions WHERE organization_id = %s "
            "AND connection_id = %s AND created_at > now() - interval '1 minute'",
            (context.organization_id, row[6])).fetchone()[0]
        if recent >= TEMPLATES[row[7]].rate_per_minute:
            raise ValueError("App rate limit reached; retry later")
        inserted = db.execute(
            "INSERT INTO integration_executions (organization_id, department_id, "
            "connection_id, approval_id, proposal_id, actor_user_id, idempotency_key, "
            "argument_hash, state) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'reserved') "
            "ON CONFLICT DO NOTHING RETURNING id",
            (context.organization_id, department_id, row[6], proposal[0], proposal_id,
             context.actor.user_id, idempotency_key, proposal[2])).fetchone()
        if inserted is None:
            raise ValueError("Action is already reserved; inspect its receipt before retrying")
    state, receipt, result = "uncertain", None, {"state": "uncertain"}
    try:
        credentials = ConnectionSecrets(root).read(context.organization_id, row[6])
        if row[7] == "remote_mcp":
            safe_mcp_url(row[8])
            tools = discover(row[8], credentials.get("token", ""))
            provider_name = action["provider_name"]
            matching = next((tool for tool in tools if tool["name"] == provider_name), None)
            if matching is None or digest({"name": matching["name"],
                                           "input_schema": matching["input_schema"]}) != action["schema_hash"]:
                raise ValueError("MCP schema changed; review the connection")
        response = perform(row[7], row[3], proposal[3], credentials, row[8])
        receipt, result = _provider_result(action, response, credentials)
        state = "succeeded"
    except (ExternalToolError, httpx.HTTPError, OSError, RuntimeError, ValueError,
            KeyError, TypeError):
        result = {"state": "uncertain", "repair": "Check the provider receipt before retrying"}
    with tenant_transaction(db, context):
        db.execute("UPDATE integration_executions SET state = %s, receipt = %s, "
                   "reason = %s, updated_at = now() WHERE organization_id = %s "
                   "AND department_id = %s AND idempotency_key = %s",
                   (state, receipt, None if state == "succeeded" else "Provider outcome uncertain",
                    context.organization_id, department_id, idempotency_key))
    return {"state": state, **result}
