"""Atomic event and outbox writes with tenant-scoped idempotency."""

from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from austra.build_release.monitoring import open_review
from austra.tenant.context import TenantContext, tenant_transaction


def emit_event(
    connection: psycopg.Connection,
    context: TenantContext,
    event_type: str,
    idempotency_key: str,
    correlation_id: UUID,
    payload: dict,
    *,
    department_id: UUID | None = None,
    outbox_topic: str | None = None,
) -> UUID:
    if department_id is None:
        context.require("workflows.approve")
    else:
        context.require("workflows.run", department_id)
    if not event_type.strip() or not idempotency_key.strip():
        raise ValueError("Event type and idempotency key are required")
    with tenant_transaction(connection, context):
        row = connection.execute(
            "INSERT INTO runtime_events (organization_id, department_id, event_type, "
            "idempotency_key, correlation_id, payload) VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (organization_id, event_type, idempotency_key) DO NOTHING RETURNING id",
            (
                context.organization_id,
                department_id,
                event_type,
                idempotency_key,
                correlation_id,
                Jsonb(payload),
            ),
        ).fetchone()
        if row is None:
            existing = connection.execute(
                "SELECT id, department_id, correlation_id, payload FROM runtime_events "
                "WHERE organization_id = %s AND event_type = %s AND idempotency_key = %s",
                (context.organization_id, event_type, idempotency_key),
            ).fetchone()
            if existing[1:] != (department_id, correlation_id, payload):
                raise ValueError("Idempotency key reused with different event data")
            event_id = existing[0]
        else:
            event_id = row[0]
        if outbox_topic:
            connection.execute(
                "INSERT INTO outbox_jobs (organization_id, department_id, event_id, topic, payload) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (organization_id, event_id, topic) DO NOTHING",
                (context.organization_id, department_id, event_id, outbox_topic, Jsonb(payload)),
            )
    return event_id


def lease_job(
    connection: psycopg.Connection, context: TenantContext, topic: str
) -> dict | None:
    context.require("workflows.approve")
    with tenant_transaction(connection, context):
        row = connection.execute(
            "SELECT id, event_id, payload, attempts FROM outbox_jobs "
            "WHERE organization_id = %s AND topic = %s "
            "AND ((state IN ('pending', 'repair') AND available_at <= now()) "
            "OR (state = 'leased' AND leased_until <= now())) "
            "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1",
            (context.organization_id, topic),
        ).fetchone()
        if row is None:
            return None
        connection.execute(
            "UPDATE outbox_jobs SET state = 'leased', attempts = attempts + 1, "
            "leased_until = now() + interval '5 minutes' WHERE organization_id = %s AND id = %s",
            (context.organization_id, row[0]),
        )
    return {"id": row[0], "event_id": row[1], "payload": row[2], "attempts": row[3] + 1}


def finish_job(
    connection: psycopg.Connection, context: TenantContext, job_id: UUID, success: bool
) -> None:
    context.require("workflows.approve")
    with tenant_transaction(connection, context):
        row = connection.execute(
            "UPDATE outbox_jobs SET state = %s, leased_until = NULL, "
            "available_at = CASE WHEN %s THEN available_at ELSE now() + interval '1 minute' END "
            "WHERE organization_id = %s AND id = %s AND state = 'leased' "
            "RETURNING id, event_id, topic",
            ("delivered" if success else "repair", success, context.organization_id, job_id),
        ).fetchone()
        if row is None:
            raise ValueError("Job is not leased in this organization")
        if not success:
            release = connection.execute(
                "SELECT blueprint_id, approved_by FROM releases WHERE organization_id = %s "
                "AND state = 'active'", (context.organization_id,),
            ).fetchone()
            open_review(
                connection, context, trigger="failed_run", subject_id=job_id,
                owner_user_id=release[1] if release else context.actor.user_id,
                blueprint_id=release[0] if release else None,
                citations=[{"job_id": str(job_id), "runtime_event_id": str(row[1])}],
                impact=f"Outbox run for {row[2]} failed; downstream work remains pending.",
                next_step="Inspect the run and provider receipt, repair the cause, then retry safely.",
            )
