"""Connect the first released email outbox to reviewed Gmail actions."""

from pathlib import Path
from uuid import UUID

import psycopg

from austra.integrations.execution import execute_proposal
from austra.integrations.gateway import preview_action
from austra.tenant.context import TenantContext, tenant_transaction


def preview_email_job(
    db: psycopg.Connection, context: TenantContext, department_id: UUID, job_id: UUID
) -> dict:
    context.require("workflows.approve", department_id)
    with tenant_transaction(db, context):
        job = db.execute(
            "SELECT state, payload FROM outbox_jobs WHERE organization_id = %s "
            "AND department_id = %s AND id = %s AND topic = 'capability.email'",
            (context.organization_id, department_id, job_id),
        ).fetchone()
        if job is None:
            raise LookupError("Email job not found")
        if job[0] not in {"pending", "repair"}:
            raise ValueError("Email job is not awaiting delivery")
        email = db.execute(
            "SELECT revision, data FROM capability_records WHERE organization_id = %s "
            "AND department_id = %s AND id = %s AND kind = 'email_proposal' "
            "AND state = 'queued'",
            (context.organization_id, department_id, UUID(job[1]["email_id"])),
        ).fetchone()
        if email is None:
            raise ValueError("Approved email proposal is unavailable")
        approval = db.execute(
            "SELECT id FROM integration_approvals WHERE organization_id = %s "
            "AND department_id = %s AND action_name = 'gmail.send_email' "
            "AND revoked_at IS NULL ORDER BY approved_at DESC LIMIT 1",
            (context.organization_id, department_id),
        ).fetchone()
        if approval is None:
            raise ValueError("Gmail workflow action needs approved connection tests")
        arguments = {
            "to": job[1]["recipient"],
            "subject": job[1]["subject"],
            "body": job[1]["body"],
        }
        source = [{"id": job[1]["email_id"], "revision": email[0]}]
    return preview_action(
        db,
        context,
        department_id,
        approval[0],
        arguments,
        source,
        "Deliver the approved purchase order email",
    )


def deliver_email_job(
    db: psycopg.Connection,
    context: TenantContext,
    root: Path,
    department_id: UUID,
    job_id: UUID,
    proposal_id: UUID,
) -> dict:
    context.require("workflows.approve", department_id)
    with tenant_transaction(db, context):
        job = db.execute(
            "SELECT state, payload FROM outbox_jobs WHERE organization_id = %s "
            "AND department_id = %s AND id = %s AND topic = 'capability.email'",
            (context.organization_id, department_id, job_id),
        ).fetchone()
        if job is None:
            raise LookupError("Email job not found")
        receipt = db.execute(
            "SELECT provider_receipt FROM capability_action_receipts WHERE "
            "organization_id = %s AND job_id = %s",
            (context.organization_id, job_id),
        ).fetchone()
        if receipt:
            return {"state": "delivered", "receipt": receipt[0]}
        proposal = db.execute(
            "SELECT preview FROM integration_action_proposals "
            "WHERE organization_id = %s AND department_id = %s AND id = %s",
            (context.organization_id, department_id, proposal_id),
        ).fetchone()
        expected = {"to": job[1]["recipient"], "subject": job[1]["subject"], "body": job[1]["body"]}
        if (
            proposal is None
            or proposal[0]["action"] != "gmail.send_email"
            or proposal[0]["arguments"] != expected
            or not any(
                source["id"] == job[1]["email_id"] for source in proposal[0]["source_records"]
            )
        ):
            raise ValueError("Preview does not belong to this email job")
    result = execute_proposal(db, context, root, department_id, proposal_id, f"email-job:{job_id}")
    if result["state"] != "succeeded":
        with tenant_transaction(db, context):
            db.execute(
                "UPDATE outbox_jobs SET state = 'repair' WHERE organization_id = %s "
                "AND department_id = %s AND id = %s",
                (context.organization_id, department_id, job_id),
            )
        return {"state": "repair", "reason": "Check provider receipt before retrying"}
    with tenant_transaction(db, context):
        db.execute(
            "INSERT INTO capability_action_receipts (organization_id, department_id, "
            "job_id, provider_key, provider_receipt) VALUES (%s, %s, %s, 'gmail', %s) "
            "ON CONFLICT (organization_id, job_id) DO NOTHING",
            (context.organization_id, department_id, job_id, result["receipt"]),
        )
        db.execute(
            "UPDATE outbox_jobs SET state = 'delivered' WHERE organization_id = %s "
            "AND department_id = %s AND id = %s",
            (context.organization_id, department_id, job_id),
        )
    return {"state": "delivered", "receipt": result["receipt"]}
