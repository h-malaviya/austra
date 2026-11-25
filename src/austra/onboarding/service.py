"""Small, transactional onboarding state machine."""

from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from austra.tenant.context import TenantContext, tenant_transaction

TRANSITIONS = {
    "created": {"collecting"},
    "collecting": {"clarifying", "ready_for_review"},
    "clarifying": {"collecting", "ready_for_review"},
    "ready_for_review": {"clarifying", "locked_candidate"},
    "locked_candidate": set(),
}


def open_case(
    connection: psycopg.Connection, organization_id: UUID, created_event_id: UUID
) -> UUID:
    """Consume only a committed organization.created audit event, once."""
    event = connection.execute(
        "SELECT correlation_id, action FROM audit_events WHERE id = %s AND organization_id = %s "
        "AND action IN ('organization.created', 'onboarding.case_initialized') "
        "AND outcome = 'success'",
        (created_event_id, organization_id),
    ).fetchone()
    if event is None:
        raise ValueError("A matching onboarding initialization event is required")
    with connection.transaction():
        connection.execute("SET LOCAL ROLE austra_tenant_app")
        connection.execute(
            "SELECT set_config('austra.organization_id', %s, true), "
            "set_config('austra.can_write', 'on', true), "
            "set_config('austra.all_departments', 'on', true)",
            (str(organization_id),),
        )
        row = connection.execute(
            "SELECT id FROM onboarding_cases WHERE organization_id = %s AND created_event_id = %s",
            (organization_id, created_event_id),
        ).fetchone()
        if row is None:
            row = connection.execute(
                "INSERT INTO onboarding_cases (organization_id, created_event_id) "
                "VALUES (%s, %s) ON CONFLICT (organization_id, created_event_id) "
                "DO UPDATE SET created_event_id = EXCLUDED.created_event_id RETURNING id",
                (organization_id, created_event_id),
            ).fetchone()
        runtime_event = connection.execute(
            "INSERT INTO runtime_events (organization_id, event_type, idempotency_key, "
            "correlation_id, payload) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (organization_id, event_type, idempotency_key) DO NOTHING RETURNING id",
            (organization_id, event[1], str(created_event_id), event[0], Jsonb({"case_id": str(row[0])})),
        ).fetchone()
        if runtime_event is None:
            runtime_event = connection.execute(
                "SELECT id FROM runtime_events WHERE organization_id = %s "
                "AND event_type = %s AND idempotency_key = %s",
                (organization_id, event[1], str(created_event_id)),
            ).fetchone()
        connection.execute(
            "INSERT INTO outbox_jobs (organization_id, event_id, topic, payload) "
            "VALUES (%s, %s, 'onboarding.case_opened', %s) "
            "ON CONFLICT (organization_id, event_id, topic) DO NOTHING",
            (organization_id, runtime_event[0], Jsonb({"case_id": str(row[0])})),
        )
        connection.execute("RESET ROLE")
        connection.execute("RESET austra.organization_id")
        connection.execute("RESET austra.can_write")
        connection.execute("RESET austra.all_departments")
        return row[0]


def ensure_case(connection: psycopg.Connection, context: TenantContext) -> UUID:
    """Initialize a legacy organization's case on its admin's first evidence action."""
    context.require("policies.draft")
    if context.actor.role != "org_admin":
        raise PermissionError("Organization admin required")
    with connection.transaction():
        organization = connection.execute(
            "SELECT id FROM organizations WHERE id = %s FOR UPDATE",
            (context.organization_id,),
        ).fetchone()
        if organization is None:
            raise LookupError("Organization not found")
        with tenant_transaction(connection, context):
            case = connection.execute(
                "SELECT id FROM onboarding_cases WHERE organization_id = %s "
                "AND parent_case_id IS NULL ORDER BY (state <> 'locked_candidate') DESC, created_at DESC LIMIT 1",
                (context.organization_id,),
            ).fetchone()
        if case is not None:
            return case[0]
        event_id = connection.execute(
            "INSERT INTO audit_events (organization_id, actor_user_id, channel, action, "
            "resource_id, outcome, reason, correlation_id) "
            "VALUES (%s, %s, 'api', 'onboarding.case_initialized', %s, 'success', "
            "'First evidence intake for a legacy organization', %s) RETURNING id",
            (context.organization_id, context.actor.user_id, context.organization_id, uuid4()),
        ).fetchone()[0]
        return open_case(connection, context.organization_id, event_id)


def current_case(connection: psycopg.Connection, context: TenantContext) -> dict | None:
    context.require("policies.read")
    with tenant_transaction(connection, context):
        row = connection.execute(
            "SELECT id, state, revision, blueprint_id, business_description, desired_outcome "
            "FROM onboarding_cases "
            "WHERE organization_id = %s AND parent_case_id IS NULL ORDER BY (state <> 'locked_candidate') DESC, "
            "created_at DESC LIMIT 1",
            (context.organization_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "state": row[1],
        "revision": row[2],
        "blueprint_id": row[3],
        "business_description": row[4],
        "desired_outcome": row[5],
    }


def workflow_cases(connection: psycopg.Connection, context: TenantContext) -> list[dict]:
    context.require("policies.read")
    with tenant_transaction(connection, context):
        rows = connection.execute(
            "SELECT c.id, c.state, c.workflow_name, c.blueprint_id, build.id, build.state, build.revision, "
            "build.verification, build.preview_state FROM onboarding_cases c LEFT JOIN LATERAL ("
            "SELECT b.id, b.state, b.revision, a.verification, a.preview_state FROM builds b "
            "LEFT JOIN capability_build_artifacts a ON (a.organization_id, a.build_id) = (b.organization_id, b.id) "
            "WHERE b.organization_id = c.organization_id AND b.blueprint_id = c.blueprint_id "
            "ORDER BY b.build_revision DESC LIMIT 1) build ON true "
            "WHERE c.organization_id = %s AND EXISTS (SELECT 1 FROM blueprint_candidates b "
            "WHERE b.organization_id = c.organization_id AND b.case_id = c.id) "
            "ORDER BY c.created_at, c.id", (context.organization_id,),
        ).fetchall()
    fields = ("id", "state", "name", "blueprint_id", "build_id", "build_state", "build_revision", "verification", "preview_state")
    return [dict(zip(fields, row, strict=True)) for row in rows]


def draft_case(connection, context, name: str, origin: UUID, *, create_only: bool = False) -> dict | None:
    """Resolve a named draft while the caller holds the organization's tour lock."""
    case = current_case(connection, context)
    if case is None:
        return None
    with tenant_transaction(connection, context):
        matched = connection.execute(
            "SELECT id, state FROM onboarding_cases WHERE organization_id = %s "
            "AND lower(workflow_name) = lower(%s) ORDER BY created_at DESC LIMIT 1 FOR UPDATE",
            (context.organization_id, name),
        ).fetchone()
        if matched:
            if create_only:
                raise ValueError("A workflow with this name already exists. Choose it to revise, or give the new workflow a different name.")
            if matched[1] == "locked_candidate":
                raise ValueError("This workflow is confirmed. Open a change review to revise it.")
            return {"id": matched[0]}
        first = connection.execute(
            "UPDATE onboarding_cases SET workflow_name = %s WHERE organization_id = %s "
            "AND id = %s AND workflow_name IS NULL AND state <> 'locked_candidate' "
            "AND NOT EXISTS (SELECT 1 FROM blueprint_candidates WHERE organization_id = %s "
            "AND case_id = %s) RETURNING id",
            (name, context.organization_id, case["id"], context.organization_id, case["id"]),
        ).fetchone()
        if first:
            return case
        created = connection.execute(
            "INSERT INTO onboarding_cases (organization_id, parent_case_id, created_event_id, "
            "workflow_name) VALUES (%s, %s, %s, %s) RETURNING id",
            (context.organization_id, case["id"], origin, name),
        ).fetchone()
        return {"id": created[0]}


def transition_case(
    connection: psycopg.Connection,
    context: TenantContext,
    case_id: UUID,
    target: str,
    expected_revision: int,
    correlation_id: UUID,
    *,
    content_hash: str | None = None,
) -> dict:
    context.require("policies.approve")
    with tenant_transaction(connection, context):
        row = connection.execute(
            "SELECT state, revision FROM onboarding_cases WHERE organization_id = %s "
            "AND id = %s FOR UPDATE",
            (context.organization_id, case_id),
        ).fetchone()
        if row is None:
            raise LookupError("Onboarding case not found")
        state, revision = row
        if revision != expected_revision:
            raise ValueError("Stale case revision")
        if target not in TRANSITIONS[state]:
            raise ValueError(f"Illegal onboarding transition: {state} -> {target}")
        blueprint_id = None
        if target == "locked_candidate":
            raise ValueError("Use reviewed candidate confirmation to lock a blueprint")
        connection.execute(
            "UPDATE onboarding_cases SET state = %s, revision = revision + 1, "
            "blueprint_id = coalesce(%s, blueprint_id), updated_at = now() "
            "WHERE organization_id = %s AND id = %s",
            (target, blueprint_id, context.organization_id, case_id),
        )
        connection.execute(
            "INSERT INTO lifecycle_events (organization_id, actor_user_id, resource_type, "
            "resource_id, from_state, to_state, correlation_id) "
            "VALUES (%s, %s, 'onboarding', %s, %s, %s, %s)",
            (
                context.organization_id,
                context.actor.user_id,
                case_id,
                state,
                target,
                correlation_id,
            ),
        )
    return {"id": case_id, "state": target, "revision": revision + 1, "blueprint_id": blueprint_id}
