"""Deterministic, tenant scoped SO, stock, PO, and email proposal execution."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from austra.a2ui import capability_decision_surface
from austra.arithmetic import calculate_expression
from austra.capabilities.contracts import CapabilityCommand
from austra.capabilities.state_machine import execute_transition
from austra.material_plan import verified_available_kg
from austra.tenant.context import TenantContext, tenant_transaction

CAPABILITY_KEY = "so_inventory_po_email"
EMAIL_PROVIDER = "email"


def _text(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


def _active_build(connection: psycopg.Connection, context: TenantContext) -> dict:
    row = connection.execute(
        "SELECT r.blueprint_id, r.build_id, b.content_hash, a.blueprint_hash, a.manifest "
        "FROM releases r JOIN blueprints b ON b.id = r.blueprint_id "
        "JOIN capability_build_artifacts a ON a.build_id = r.build_id "
        "WHERE r.organization_id = %s AND r.state = 'active'",
        (context.organization_id,),
    ).fetchone()
    if row is None or row[2] != row[3]:
        raise ValueError("No released capability pinned to its confirmed blueprint")
    return {
        "blueprint_id": row[0],
        "build_id": row[1],
        "blueprint_hash": row[2],
        "manifest": row[4],
    }


def _authority(
    manifest: dict, command: CapabilityCommand, context: TenantContext, at: datetime
) -> dict:
    matches = []
    decision_key = (
        f"command.{command.payload.capability_key}.{command.payload.transition_key}"
        if command.action == "transition_record"
        else f"command.{command.action}"
    )
    for rule in manifest["decision_tables"]:
        content = rule["content"]
        if content.get("decision_key") != decision_key:
            continue
        valid_from = datetime.fromisoformat(rule["valid_from"])
        valid_to = datetime.fromisoformat(rule["valid_to"]) if rule.get("valid_to") else None
        if valid_from <= at and (valid_to is None or at < valid_to):
            matches.append(rule)
    if len(matches) != 1:
        raise ValueError("Exactly one dated authority rule is required")
    permission = matches[0]["content"].get("required_permission")
    if permission not in {"workflows.run", "workflows.approve"}:
        raise ValueError("Unsupported capability authority")
    context.require(permission, command.department_id)
    return matches[0]


def _record(
    connection: psycopg.Connection,
    context: TenantContext,
    command: CapabilityCommand,
    record_id: UUID,
    kind: str,
) -> dict:
    row = connection.execute(
        "SELECT external_id, revision, state, data FROM capability_records "
        "WHERE organization_id = %s AND department_id = %s AND id = %s AND kind = %s FOR UPDATE",
        (context.organization_id, command.department_id, record_id, kind),
    ).fetchone()
    if row is None:
        raise LookupError(f"{kind} not found in department")
    if command.expected_revisions.get(record_id) != row[1]:
        raise ValueError(f"Stale or missing record revision for {record_id}")
    return {
        "id": record_id,
        "external_id": row[0],
        "revision": row[1],
        "state": row[2],
        "data": row[3],
    }


def _save(
    connection: psycopg.Connection,
    context: TenantContext,
    command: CapabilityCommand,
    event_id: UUID,
    kind: str,
    external_id: str,
    state: str,
    data: dict,
    current: dict | None = None,
) -> dict:
    if current is None:
        record_id, revision = connection.execute(
            "INSERT INTO capability_records (organization_id, department_id, kind, external_id, "
            "state, data) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, revision",
            (context.organization_id, command.department_id, kind, external_id, state, Jsonb(data)),
        ).fetchone()
    else:
        record_id = current["id"]
        revision = connection.execute(
            "UPDATE capability_records SET revision = revision + 1, state = %s, data = %s, "
            "updated_at = now() WHERE organization_id = %s AND department_id = %s AND id = %s "
            "AND revision = %s RETURNING revision",
            (
                state,
                Jsonb(data),
                context.organization_id,
                command.department_id,
                record_id,
                current["revision"],
            ),
        ).fetchone()[0]
    connection.execute(
        "INSERT INTO capability_record_revisions (organization_id, department_id, record_id, "
        "revision, state, data, event_id) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            context.organization_id,
            command.department_id,
            record_id,
            revision,
            state,
            Jsonb(data),
            event_id,
        ),
    )
    return {
        "id": str(record_id),
        "revision": revision,
        "kind": kind,
        "state": state,
        "external_id": external_id,
    }


def _execute(
    connection: psycopg.Connection,
    context: TenantContext,
    command: CapabilityCommand,
    event_id: UUID,
    rule: dict,
    manifest: dict,
) -> dict:
    if command.action == "transition_record":
        return execute_transition(
            connection, context, command, event_id, manifest, rule, _save, _record
        )
    data = command.payload.model_dump(mode="json")
    action = command.action
    if action == "submit_order":
        return {
            "record": _save(
                connection,
                context,
                command,
                event_id,
                "sales_order",
                data["external_id"],
                "open",
                {**data, "reserved_kg": "0"},
            )
        }
    if action == "reserve_stock":
        order = _record(connection, context, command, UUID(data["order_id"]), "sales_order")
        lot = _record(connection, context, command, UUID(data["lot_id"]), "stock_lot")
        if order["state"] not in {"open", "partially_reserved", "reserved"}:
            raise ValueError("Order cannot be reserved in its current state")
        if lot["data"].get("product") != order["data"]["product"]:
            raise ValueError("Stock product does not match the order")
        quantity = Decimal(data["quantity_kg"])
        remaining = Decimal(order["data"]["quantity_kg"]) - Decimal(order["data"]["reserved_kg"])
        if quantity > remaining or quantity > verified_available_kg(lot["data"]):
            raise ValueError("Reservation exceeds verified available stock or open demand")
        order_data = {
            **order["data"],
            "reserved_kg": _text(Decimal(order["data"]["reserved_kg"]) + quantity),
        }
        lot_data = {
            **lot["data"],
            "reserved_kg": _text(Decimal(lot["data"].get("reserved_kg", "0")) + quantity),
            "available_kg": _text(Decimal(lot["data"]["available_kg"]) - quantity),
        }
        order_state = "reserved" if quantity == remaining else "partially_reserved"
        return {
            "order": _save(
                connection,
                context,
                command,
                event_id,
                "sales_order",
                order["external_id"],
                order_state,
                order_data,
                order,
            ),
            "lot": _save(
                connection,
                context,
                command,
                event_id,
                "stock_lot",
                lot["external_id"],
                lot["state"],
                lot_data,
                lot,
            ),
        }
    if action == "draft_po":
        order = _record(connection, context, command, UUID(data["order_id"]), "sales_order")
        if order["state"] not in {"open", "partially_reserved", "reserved"}:
            raise ValueError("Purchase order cannot be drafted for this order")
        committed = connection.execute(
            "SELECT coalesce(sum((data->>'quantity_kg')::numeric), 0) FROM capability_records "
            "WHERE organization_id = %s AND department_id = %s AND kind = 'purchase_order' "
            "AND data->>'order_id' = %s AND state IN ('draft', 'escalated', 'approved')",
            (context.organization_id, command.department_id, data["order_id"]),
        ).fetchone()[0]
        if Decimal(data["quantity_kg"]) > (
            Decimal(order["data"]["quantity_kg"])
            - Decimal(order["data"]["reserved_kg"])
            - committed
        ):
            raise ValueError("Purchase quantity exceeds unfilled demand")
        total = calculate_expression(
            "quantity * price",
            {"quantity": data["quantity_kg"], "price": data["unit_price_per_kg"]},
        )
        return {
            "record": _save(
                connection,
                context,
                command,
                event_id,
                "purchase_order",
                data["external_id"],
                "draft",
                {
                    **data,
                    "total": total,
                    "source_records": [{"id": str(order["id"]), "revision": order["revision"]}],
                },
            )
        }
    if action == "approve_po":
        po = _record(connection, context, command, UUID(data["po_id"]), "purchase_order")
        if po["state"] != "draft":
            raise ValueError("Only a draft purchase order can be decided")
        order = _record(connection, context, command, UUID(po["data"]["order_id"]), "sales_order")
        if order["revision"] != po["data"]["source_records"][0]["revision"]:
            raise ValueError("Source order changed since purchase draft")
        authority = rule["content"]
        if authority.get("unit") != po["data"]["currency"]:
            raise ValueError("Approval limit currency does not match purchase order")
        limit = Decimal(str(authority["limit"]))
        if not limit.is_finite() or limit < 0:
            raise ValueError("Invalid approval limit")
        state = "escalated" if Decimal(po["data"]["total"]) > limit else "approved"
        return {
            "record": _save(
                connection,
                context,
                command,
                event_id,
                "purchase_order",
                po["external_id"],
                state,
                po["data"],
                po,
            ),
            "approval_limit": _text(limit),
            "escalation_required": state == "escalated",
        }
    if action == "propose_email":
        po = _record(connection, context, command, UUID(data["po_id"]), "purchase_order")
        if po["state"] != "approved":
            raise ValueError("Email proposal requires an approved purchase order")
        duplicate = connection.execute(
            "SELECT 1 FROM capability_records WHERE organization_id = %s AND department_id = %s "
            "AND kind = 'email_proposal' AND data->>'po_id' = %s LIMIT 1",
            (context.organization_id, command.department_id, data["po_id"]),
        ).fetchone()
        if duplicate:
            raise ValueError("Purchase order already has an email proposal")
        proposal = {
            **data,
            "source_records": [{"id": str(po["id"]), "revision": po["revision"]}],
            "approval_required": True,
        }
        return {
            "record": _save(
                connection,
                context,
                command,
                event_id,
                "email_proposal",
                data["external_id"],
                "proposed",
                proposal,
            ),
            "preview": proposal,
        }
    email = _record(connection, context, command, UUID(data["email_id"]), "email_proposal")
    if email["state"] != "proposed":
        raise ValueError("Only a proposed email can be approved")
    po = _record(connection, context, command, UUID(email["data"]["po_id"]), "purchase_order")
    if (
        po["state"] != "approved"
        or po["revision"] != email["data"]["source_records"][0]["revision"]
    ):
        raise ValueError("Source purchase order changed since email proposal")
    health = connection.execute(
        "SELECT state FROM capability_connection_health WHERE organization_id = %s "
        "AND provider_key = %s",
        (context.organization_id, EMAIL_PROVIDER),
    ).fetchone()
    repair = health is None or health[0] != "healthy"
    record = _save(
        connection,
        context,
        command,
        event_id,
        "email_proposal",
        email["external_id"],
        "queued",
        email["data"],
        email,
    )
    connection.execute(
        "INSERT INTO outbox_jobs (organization_id, department_id, event_id, topic, payload, state) "
        "VALUES (%s, %s, %s, 'capability.email', %s, %s)",
        (
            context.organization_id,
            command.department_id,
            event_id,
            Jsonb(
                {
                    "email_id": record["id"],
                    "recipient": email["data"]["recipient"],
                    "subject": email["data"]["subject"],
                    "body": email["data"]["body"],
                }
            ),
            "repair" if repair else "pending",
        ),
    )
    return {"record": record, "effect": "repair_connection" if repair else "queued"}


def execute_command(
    connection: psycopg.Connection, context: TenantContext, command: CapabilityCommand
) -> dict:
    """All transports call this service; the model never supplies executable code."""
    context.require("workflows.run", command.department_id)
    at = datetime.now(UTC)
    from austra.build_release.monitoring import scan_command_expiry

    scan_command_expiry(
        connection, context, command.action, at, getattr(command.payload, "transition_key", None)
    )
    if command.occurred_at is not None and abs((at - command.occurred_at).total_seconds()) > 300:
        raise ValueError("Command time must be current; history uses pinned events")
    with tenant_transaction(connection, context):
        digest = command.digest()
        prior = connection.execute(
            "SELECT r.command_hash, r.result, e.payload FROM capability_command_results r "
            "JOIN runtime_events e ON e.id = r.event_id WHERE r.organization_id = %s "
            "AND r.department_id = %s AND r.idempotency_key = %s",
            (context.organization_id, command.department_id, command.idempotency_key),
        ).fetchone()
        if prior is not None:
            if prior[0] != digest or prior[2]["actor_id"] != str(context.actor.user_id):
                raise ValueError("Idempotency key reused for a different command or actor")
            return prior[1]
        build = _active_build(connection, context)
        if command.action not in build["manifest"]["commands"]:
            raise ValueError("Command is not in the released build")
        rule = _authority(build["manifest"], command, context, at)
        event = connection.execute(
            "INSERT INTO runtime_events (organization_id, department_id, event_type, "
            "idempotency_key, correlation_id, payload) VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (organization_id, event_type, idempotency_key) DO NOTHING RETURNING id",
            (
                context.organization_id,
                command.department_id,
                "capability.command",
                command.idempotency_key,
                command.correlation_id,
                Jsonb(
                    {
                        "command_hash": digest,
                        "actor_id": str(context.actor.user_id),
                        "action": command.action,
                        "blueprint_id": str(build["blueprint_id"]),
                        "build_id": str(build["build_id"]),
                        "authority_revision_id": rule["id"],
                    }
                ),
            ),
        ).fetchone()
        if event is None:
            prior = connection.execute(
                "SELECT r.command_hash, r.result, e.payload FROM capability_command_results r "
                "JOIN runtime_events e ON e.id = r.event_id WHERE r.organization_id = %s "
                "AND r.department_id = %s AND r.idempotency_key = %s",
                (context.organization_id, command.department_id, command.idempotency_key),
            ).fetchone()
            if (
                prior is None
                or prior[0] != digest
                or prior[2]["actor_id"] != str(context.actor.user_id)
            ):
                raise ValueError("Idempotency key reused for a different command or actor")
            return prior[1]
        result = _execute(connection, context, command, event[0], rule, build["manifest"])
        decision = {
            **result,
            "event_id": str(event[0]),
            "blueprint_id": str(build["blueprint_id"]),
            "blueprint_hash": build["blueprint_hash"],
            "build_id": str(build["build_id"]),
            "authority_revision_id": rule["id"],
            "occurred_at": at.isoformat(),
            "projection": build["manifest"].get("projections", []),
        }
        decision["a2ui"] = capability_decision_surface(decision)
        connection.execute(
            "INSERT INTO capability_command_results (organization_id, department_id, "
            "idempotency_key, command_hash, event_id, result) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                context.organization_id,
                command.department_id,
                command.idempotency_key,
                digest,
                event[0],
                Jsonb(decision),
            ),
        )
        return decision


def explain_event(
    connection: psycopg.Connection, context: TenantContext, department_id: UUID, event_id: UUID
) -> dict:
    context.require("workflows.read", department_id)
    with tenant_transaction(connection, context):
        row = connection.execute(
            "SELECT e.payload, e.occurred_at, r.result FROM runtime_events e "
            "JOIN capability_command_results r ON r.event_id = e.id "
            "WHERE e.organization_id = %s AND e.department_id = %s AND e.id = %s",
            (context.organization_id, department_id, event_id),
        ).fetchone()
        if row is None:
            raise LookupError("Capability event not found")
        artifact = connection.execute(
            "SELECT manifest FROM capability_build_artifacts WHERE organization_id = %s "
            "AND build_id = %s",
            (context.organization_id, UUID(row[0]["build_id"])),
        ).fetchone()
        if artifact is None:
            raise ValueError("Pinned build artifact is unavailable")
        authority = next(
            (
                rule
                for rule in artifact[0]["decision_tables"]
                if rule["id"] == row[0]["authority_revision_id"]
            ),
            None,
        )
        if authority is None:
            raise ValueError("Pinned authority revision is unavailable")
        return {
            "action": row[0]["action"],
            "occurred_at": row[1],
            "blueprint_id": row[0]["blueprint_id"],
            "build_id": row[0]["build_id"],
            "authority_revision_id": row[0]["authority_revision_id"],
            "authority": authority,
            "decision": row[2],
        }


def capability_view(
    connection: psycopg.Connection, context: TenantContext, department_id: UUID
) -> dict:
    context.require("workflows.read", department_id)
    with tenant_transaction(connection, context):
        build = _active_build(connection, context)
        if build["manifest"].get("engine") == "state_machine":
            return {
                "blueprint_id": str(build["blueprint_id"]),
                "build_id": str(build["build_id"]),
                "blueprint_hash": build["blueprint_hash"],
                "commands": build["manifest"]["commands"],
                "capabilities": build["manifest"]["capabilities"],
                "custom_module_drafts": (
                    build["manifest"].get("custom_module_drafts", [])
                    if context.actor.role == "org_admin"
                    else []
                ),
                "projections": [
                    item
                    for item in build["manifest"]["projections"]
                    if context.actor.role in item["roles"]
                ],
                "records": [],
                "record_count": 0,
                "complete": True,
            }
        rows = connection.execute(
            "SELECT id, kind, external_id, revision, state, data FROM capability_records "
            "WHERE organization_id = %s AND department_id = %s "
            "ORDER BY created_at, id LIMIT 200",
            (context.organization_id, department_id),
        ).fetchall()
        total = connection.execute(
            "SELECT count(*) FROM capability_records WHERE organization_id = %s "
            "AND department_id = %s",
            (context.organization_id, department_id),
        ).fetchone()[0]
        return {
            "blueprint_id": str(build["blueprint_id"]),
            "build_id": str(build["build_id"]),
            "blueprint_hash": build["blueprint_hash"],
            "commands": build["manifest"]["commands"],
            "projections": build["manifest"].get("projections", []),
            "record_count": total,
            "complete": len(rows) == total,
            "records": [
                {
                    "id": str(row[0]),
                    "kind": row[1],
                    "external_id": row[2],
                    "revision": row[3],
                    "state": row[4],
                    "data": row[5],
                }
                for row in rows
            ],
        }
