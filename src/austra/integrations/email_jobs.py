"""Connect released email outbox jobs to reviewed Gmail actions."""

from pathlib import Path
from uuid import UUID

import psycopg

from austra.capabilities.reply_mapping import TRACKED_EMAIL_EVENT
from austra.connectors import GMAIL_CONTENT_SCOPE
from austra.integrations.email_delivery_recovery import finish_email_delivery
from austra.integrations.execution import execute_proposal
from austra.integrations.gateway import approve_workflow, preview_action
from austra.tenant.context import TenantContext, tenant_transaction


def _email_job(db, context, department_id, job_id):
    return db.execute(
        "SELECT j.state,j.payload,e.event_type,e.payload FROM outbox_jobs j "
        "JOIN runtime_events e ON (e.organization_id,e.department_id,e.id)="
        "(j.organization_id,j.department_id,j.event_id) WHERE j.organization_id=%s "
        "AND j.department_id=%s AND j.id=%s AND j.topic='capability.email'",
        (context.organization_id, department_id, job_id),
    ).fetchone()


def _workflow_email(job, job_id):
    if job[2] != TRACKED_EMAIL_EVENT:
        return None
    return {
        "id": str(job_id), "build_id": job[3]["build_id"],
        "content_required": job[1]["content_required"],
        "approval_id": job[1].get("workflow_connection_approval_id"),
        "attachment": job[1].get("attachment"),
        **({"automatic_email": job[1]["automatic_email"]} if job[1].get("automatic_email") else {}),
    }


def approve_email_connection(db, context, root, department_id, job_id, connection_id):
    context.require("workflows.approve", department_id)
    context.require_active_department(db, department_id)
    with tenant_transaction(db, context):
        job = db.execute(
            "SELECT j.payload,event.payload->>'build_id',event.event_type FROM outbox_jobs j "
            "JOIN runtime_events event ON (event.organization_id,event.id) = (j.organization_id,j.event_id) "
            "JOIN releases r ON r.organization_id = j.organization_id "
            "AND r.build_id = (event.payload->>'build_id')::uuid AND r.state = 'active' "
            "WHERE j.organization_id = %s AND j.department_id = %s AND j.id = %s "
            "AND j.topic = 'capability.email' AND j.state IN ('pending', 'repair') FOR UPDATE OF j",
            (context.organization_id, department_id, job_id),
        ).fetchone()
        if not job:
            raise LookupError("An approved email in a released workflow is required")
        fixture = {key: job[0][field] for key, field in (
            ("to", "recipient"), ("subject", "subject"), ("body", "body"),
        )}
        approved = None
        if job[2] == TRACKED_EMAIL_EVENT:
            if context.actor.role != "org_admin":
                raise PermissionError("Organization admin required to approve Gmail for this workflow")
            org = context.organization_id
            scopes = db.execute(
                "SELECT granted_scopes FROM integration_connections WHERE organization_id=%s AND id=%s "
                "AND template_key='gmail' AND health='healthy' AND revoked_at IS NULL",
                (org, connection_id),
            ).fetchone()
            if not scopes:
                raise ValueError("Choose a healthy Gmail connection")
            if job[0]["content_required"] and GMAIL_CONTENT_SCOPE not in scopes[0]:
                raise PermissionError("Authorize Gmail reply text and attachments before sending")
            db.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                       (f"record-email-approval:{org}:{department_id}:{connection_id}:{job[1]}",))
            existing = db.execute(
                "SELECT id FROM integration_approvals WHERE organization_id=%s AND department_id=%s "
                "AND connection_id=%s AND build_id=%s AND action_name='gmail.send_email' AND revoked_at IS NULL",
                (org, department_id, connection_id, job[1]),
            ).fetchone()
            if existing:
                approved = {"approved": True, "approval_id": str(existing[0]), "effect": "write"}
        if approved is None:
            approved = approve_workflow(
                db, context, root, department_id, connection_id, UUID(job[1]), "gmail.send_email",
                fixture, {"event_id": str(job_id), "payload": fixture},
            )
        if job[2] == TRACKED_EMAIL_EVENT:
            db.execute(
                "UPDATE outbox_jobs SET payload=jsonb_set(payload,'{workflow_connection_approval_id}',to_jsonb(%s::text)) "
                "WHERE organization_id=%s AND department_id=%s AND id=%s",
                (approved["approval_id"], context.organization_id, department_id, job_id),
            )
        return approved


def preview_email_job(
    db: psycopg.Connection, context: TenantContext, department_id: UUID, job_id: UUID
) -> dict:
    context.require("workflows.approve", department_id)
    context.require_active_department(db, department_id)
    with tenant_transaction(db, context):
        job = _email_job(db, context, department_id, job_id)
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
        workflow_email = _workflow_email(job, job_id)
        approval = db.execute(
            "SELECT id FROM integration_approvals WHERE organization_id = %s "
            "AND department_id = %s AND action_name = 'gmail.send_email' "
            "AND build_id = %s AND revoked_at IS NULL "
            "AND (%s OR id = %s::uuid) ORDER BY approved_at DESC LIMIT 1",
            (context.organization_id, department_id, job[3].get("build_id"), workflow_email is None,
             workflow_email["approval_id"] if workflow_email else None),
        ).fetchone()
        if approval is None:
            raise ValueError("Gmail workflow action needs approved connection tests")
        arguments = {
            "to": job[1]["recipient"],
            "subject": job[1]["subject"],
            "body": job[1]["body"],
        }
        source = [{"id": job[1]["email_id"], "revision": email[0]}]
        if workflow_email:
            source.append({"id": job[3]["record_id"], "revision": job[3]["expected_revision"]})
    return preview_action(
        db,
        context,
        department_id,
        approval[0],
        arguments,
        source,
        "Deliver the reviewed workflow email" if workflow_email else "Deliver the approved purchase order email",
        workflow_email=workflow_email,
    )


def deliver_email_job(
    db: psycopg.Connection,
    context: TenantContext,
    root: Path,
    department_id: UUID,
    job_id: UUID,
    proposal_id: UUID,
    *,
    follow_up_days: int | None = None,
    owner_email_alerts: bool = False,
    automatic_replies: bool = False,
) -> dict:
    context.require("workflows.approve", department_id)
    context.require_active_department(db, department_id)
    with tenant_transaction(db, context):
        job = _email_job(db, context, department_id, job_id)
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
            or proposal[0].get("workflow_email") != _workflow_email(job, job_id)
            or not any(
                source["id"] == job[1]["email_id"] for source in proposal[0]["source_records"]
            )
        ):
            raise ValueError("Preview does not belong to this email job")
        if automatic_replies and not job[1].get("automatic_email"):
            raise ValueError("The current workflow has no approved automatic reply rules")
    result = execute_proposal(
        db, context, root, department_id, proposal_id, f"email-job:{job_id}", workflow_job_id=job_id,
        follow_up_days=follow_up_days, owner_email_alerts=owner_email_alerts, automatic_replies=automatic_replies,
    )
    if result["state"] != "succeeded":
        with tenant_transaction(db, context):
            db.execute(
                "UPDATE outbox_jobs SET state = 'repair' WHERE organization_id = %s "
                "AND department_id = %s AND id = %s AND state IN ('pending','repair')",
                (context.organization_id, department_id, job_id),
            )
        return {"state": "repair", "reason": "Check provider receipt before retrying"}
    with tenant_transaction(db, context):
        finish_email_delivery(db, context.organization_id, department_id, job_id, result["receipt"])
    return {"state": "delivered", "receipt": result["receipt"]}
