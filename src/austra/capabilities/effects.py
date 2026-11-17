"""Receipt based email outbox delivery through an injected sandbox/provider adapter."""

from collections.abc import Callable
from uuid import UUID

import psycopg

from austra.tenant.context import TenantContext, tenant_transaction

ReceiptLookup = Callable[[str], str | None]
EmailSender = Callable[[dict, str], str | None]


def deliver_email(
    connection: psycopg.Connection, context: TenantContext, job_id: UUID,
    lookup_receipt: ReceiptLookup, send: EmailSender,
) -> dict:
    context.require("workflows.approve")
    with tenant_transaction(connection, context):
        row = connection.execute(
            "SELECT j.state, j.payload, h.state, j.department_id FROM outbox_jobs j "
            "LEFT JOIN capability_connection_health h ON h.organization_id = j.organization_id "
            "AND h.provider_key = 'email' WHERE j.organization_id = %s AND j.id = %s "
            "AND j.topic = 'capability.email' FOR UPDATE OF j",
            (context.organization_id, job_id),
        ).fetchone()
        if row is None:
            raise LookupError("Email job not found")
        receipt = connection.execute(
            "SELECT provider_receipt FROM capability_action_receipts "
            "WHERE organization_id = %s AND job_id = %s",
            (context.organization_id, job_id),
        ).fetchone()
        if receipt:
            return {"state": "delivered", "receipt": receipt[0]}
        if row[2] != "healthy":
            connection.execute(
                "UPDATE outbox_jobs SET state = 'repair' WHERE organization_id = %s AND id = %s",
                (context.organization_id, job_id),
            )
            return {"state": "repair", "reason": "Reconnect email provider"}
        if row[0] not in {"pending", "repair"}:
            raise ValueError("Email job is already being delivered")
        connection.execute(
            "UPDATE outbox_jobs SET state = 'leased', attempts = attempts + 1 "
            "WHERE organization_id = %s AND id = %s",
            (context.organization_id, job_id),
        )
        payload = row[1]
    try:
        provider_receipt = lookup_receipt(str(job_id))
        if provider_receipt is None:
            provider_receipt = send(payload, str(job_id))
        if not provider_receipt:
            raise ValueError("Provider did not return a receipt")
    except (ConnectionError, OSError, TimeoutError, ValueError):
        with tenant_transaction(connection, context):
            connection.execute(
                "UPDATE outbox_jobs SET state = 'repair', leased_until = NULL "
                "WHERE organization_id = %s AND id = %s AND state = 'leased'",
                (context.organization_id, job_id),
            )
        return {"state": "repair", "reason": "Reconcile provider receipt before retry"}
    with tenant_transaction(connection, context):
        connection.execute(
            "INSERT INTO capability_action_receipts (organization_id, department_id, job_id, "
            "provider_key, provider_receipt) VALUES (%s, %s, %s, 'email', %s) "
            "ON CONFLICT (organization_id, job_id) DO NOTHING",
            (context.organization_id, row[3], job_id, provider_receipt),
        )
        connection.execute(
            "UPDATE outbox_jobs SET state = 'delivered', leased_until = NULL "
            "WHERE organization_id = %s AND id = %s AND state = 'leased'",
            (context.organization_id, job_id),
        )
    return {"state": "delivered", "receipt": provider_receipt}
