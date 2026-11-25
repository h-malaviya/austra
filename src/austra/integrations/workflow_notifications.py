"""Durable owner alerts for replies captured against delivered workflow email."""

import logging
import re

from austra.integrations.providers import perform
from austra.integrations.secrets import ConnectionSecrets
from austra.tenant.context import tenant_transaction

logger = logging.getLogger(__name__)


def enqueue_notification(db, org, department, job, owner, event_key, title, message, email):
    db.execute(
        "INSERT INTO workflow_notifications (organization_id, department_id, job_id, owner_user_id, "
        "event_key, title, message, email_state) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT DO NOTHING",
        (org, department, job, owner, event_key, title, message, "pending" if email else "off"),
    )


def list_notifications(db, context):
    with tenant_transaction(db, context):
        return db.execute(
            "SELECT id, title, message, created_at, read_at, email_state FROM workflow_notifications "
            "WHERE organization_id=%s AND owner_user_id=%s ORDER BY created_at DESC LIMIT 50",
            (context.organization_id, context.actor.user_id),
        ).fetchall()


def mark_notification_read(db, context, identifier):
    with tenant_transaction(db, context):
        updated = db.execute(
            "UPDATE workflow_notifications SET read_at=COALESCE(read_at, now()) "
            "WHERE organization_id=%s AND owner_user_id=%s AND id=%s RETURNING id",
            (context.organization_id, context.actor.user_id, identifier),
        ).fetchone()
    if not updated:
        raise LookupError("That notification is no longer available")
    return {"read": True}


def enqueue_due_follow_up(db):
    """Create one reminder for the last pending conversation update."""
    with db.transaction():
        job = db.execute(
            "SELECT j.organization_id,j.department_id,j.id,"
            "coalesce((j.payload->>'workflow_owner_id')::uuid,x.actor_user_id), "
            "j.follow_up_due_at, j.owner_email_alerts, j.payload->>'subject' "
            "FROM outbox_jobs j "
            "JOIN integration_executions x ON (x.organization_id,x.department_id) = "
            "(j.organization_id,j.department_id) AND x.idempotency_key='email-job:' || j.id::text "
            "AND x.state='succeeded' "
            "JOIN organizations o ON o.id=j.organization_id AND o.status='active' "
            "JOIN departments d ON (d.organization_id,d.id)=(j.organization_id,j.department_id) "
            "AND d.archived_at IS NULL "
            "WHERE j.topic='capability.email' AND j.state='delivered' AND j.follow_up_due_at <= now() "
            "ORDER BY j.follow_up_due_at LIMIT 1 FOR UPDATE OF j SKIP LOCKED",
        ).fetchone()
        if job is None:
            return False
        org, department, identifier, owner, due, email, subject = job
        enqueue_notification(
            db, org, department, identifier, owner, f"follow-up:{due.isoformat()}",
            "Supplier follow-up due", f"Review the tracked purchase email “{subject}”.", email,
        )
        db.execute(
            "UPDATE outbox_jobs SET follow_up_due_at=NULL "
            "WHERE organization_id=%s AND department_id=%s AND id=%s",
            (org, department, identifier),
        )
    return True


def dispatch_owner_notification(db, root):
    """Deliver an opted-in, fixed notification using the original workflow mailbox."""
    with db.transaction():
        db.execute(
            "UPDATE workflow_notifications SET email_state='attention' "
            "WHERE email_state='dispatching' AND email_attempted_at < now() - interval '5 minutes'",
        )
        row = db.execute(
            "WITH due AS (SELECT n.id, x.connection_id, "
            "coalesce(nullif(u.email,''), CASE WHEN c.owner_user_id=u.id THEN c.account_label END) recipient "
            "FROM workflow_notifications n "
            "JOIN outbox_jobs j ON (j.organization_id,j.department_id,j.id)="
            "(n.organization_id,n.department_id,n.job_id) AND j.owner_email_alerts "
            "JOIN organizations o ON o.id=n.organization_id AND o.status='active' "
            "JOIN departments d ON (d.organization_id,d.id)=(n.organization_id,n.department_id) "
            "AND d.archived_at IS NULL "
            "JOIN users u ON (u.organization_id,u.id)=(n.organization_id,n.owner_user_id) "
            "AND u.status='active' AND u.deleted_at IS NULL "
            "JOIN integration_executions x ON (x.organization_id,x.department_id,x.actor_user_id)="
            "(n.organization_id,n.department_id,n.owner_user_id) "
            "AND x.idempotency_key='email-job:' || n.job_id::text AND x.state='succeeded' "
            "JOIN integration_connections c ON (c.organization_id,c.id)=(x.organization_id,x.connection_id) "
            "AND c.template_key='gmail' AND c.health='healthy' AND c.revoked_at IS NULL "
            "WHERE n.email_state='pending' ORDER BY n.created_at LIMIT 1 FOR UPDATE OF n SKIP LOCKED) "
            "UPDATE workflow_notifications n SET email_state='dispatching',email_attempted_at=now() "
            "FROM due WHERE n.id=due.id RETURNING n.id,n.organization_id,due.connection_id,due.recipient",
        ).fetchone()
    if row is None:
        return False
    identifier, org, connection, to = row
    state = "attention"
    if to and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to):
        try:
            credentials = ConnectionSecrets(root).read(org, connection)
            receipt = perform("gmail", "gmail.send_email", {
                "to": to, "subject": "Austra workflow notification",
                "body": "There is new supplier activity or a follow-up reminder. Open Austra Notifications to review.",
            }, credentials)
            if receipt.get("message_id"):
                state = "sent"
        except (RuntimeError, OSError, ValueError, LookupError) as error:
            logger.warning("Owner notification %s requires reconciliation (%s)", identifier, type(error).__name__)
    with db.transaction():
        db.execute(
            "UPDATE workflow_notifications SET email_state=%s WHERE organization_id=%s "
            "AND id=%s AND email_state='dispatching'",
            (state, org, identifier),
        )
    return True
