"""Reconcile approved workflow deliveries using the journaled provider outcome."""

from datetime import timedelta

from austra.integrations.gmail_replies import FIRST_REPLY_CHECK, REPLY_TRACKING_PERIOD
from austra.integrations.workflow_notifications import enqueue_notification

STALE_RESERVATION = timedelta(minutes=5)
DELIVERY_REVIEW_KEY = "delivery-needs-review"


def finish_email_delivery(db, org, department, job, receipt):
    locked = db.execute(
        "SELECT state FROM outbox_jobs WHERE organization_id=%s AND department_id=%s "
        "AND id=%s AND topic='capability.email' FOR UPDATE",
        (org, department, job),
    ).fetchone()
    if locked is None or locked[0] not in {"pending", "repair", "delivered"}:
        raise ValueError("Email is not eligible for delivery reconciliation")
    db.execute(
        "INSERT INTO capability_action_receipts (organization_id,department_id,job_id,"
        "provider_key,provider_receipt) VALUES (%s,%s,%s,'gmail',%s) "
        "ON CONFLICT (organization_id,job_id) DO NOTHING",
        (org, department, job, receipt),
    )
    recorded = db.execute(
        "SELECT provider_key,provider_receipt FROM capability_action_receipts "
        "WHERE organization_id=%s AND department_id=%s AND job_id=%s",
        (org, department, job),
    ).fetchone()
    if recorded != ("gmail", receipt):
        raise ValueError("Recorded receipt differs from the approved Gmail delivery")
    db.execute(
        "UPDATE outbox_jobs SET state='delivered',"
        "reply_check_at=COALESCE(reply_check_at,now()+%s),"
        "reply_tracking_until=COALESCE(reply_tracking_until,now()+%s),"
        "follow_up_due_at=now()+make_interval(days=>follow_up_days) "
        "WHERE organization_id=%s AND department_id=%s AND id=%s AND state<>'delivered'",
        (FIRST_REPLY_CHECK, REPLY_TRACKING_PERIOD, org, department, job),
    )


def reconcile_email_delivery(db):
    with db.transaction():
        delivery = db.execute(
            "SELECT j.organization_id,j.department_id,j.id,CASE WHEN x.state='succeeded' THEN x.receipt END,"
            "coalesce((j.payload->>'workflow_owner_id')::uuid,x.actor_user_id) FROM outbox_jobs j "
            "JOIN organizations o ON o.id=j.organization_id AND o.status='active' "
            "JOIN departments d ON (d.organization_id,d.id)=(j.organization_id,j.department_id) "
            "AND d.archived_at IS NULL "
            "JOIN runtime_events e ON (e.organization_id,e.department_id,e.id)="
            "(j.organization_id,j.department_id,j.event_id) "
            "JOIN integration_executions x ON (x.organization_id,x.department_id)="
            "(j.organization_id,j.department_id) AND x.idempotency_key='email-job:'||j.id::text "
            "JOIN integration_approvals a ON (a.organization_id,a.department_id,a.id)="
            "(x.organization_id,x.department_id,x.approval_id) AND a.action_name='gmail.send_email' "
            "AND a.build_id::text=e.payload->>'build_id' "
            "JOIN integration_action_proposals p ON (p.organization_id,p.department_id,p.id)="
            "(x.organization_id,x.department_id,x.proposal_id) AND p.approval_id=a.id "
            "AND p.arguments=jsonb_build_object('to',j.payload->>'recipient',"
            "'subject',j.payload->>'subject','body',j.payload->>'body') "
            "AND p.preview->'source_records' @> jsonb_build_array(jsonb_build_object('id',j.payload->>'email_id')) "
            "JOIN integration_connections c ON (c.organization_id,c.id)="
            "(x.organization_id,x.connection_id) AND c.template_key='gmail' "
            "WHERE j.topic='capability.email' AND j.state IN ('pending','repair') "
            "AND ((x.state='succeeded' AND nullif(x.receipt,'') IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM capability_action_receipts r "
            "WHERE r.organization_id=j.organization_id AND r.job_id=j.id "
            "AND (r.provider_key<>'gmail' OR r.provider_receipt<>x.receipt))) "
            "OR ((x.state='uncertain' OR (x.state='reserved' AND x.created_at < now() - %s)) "
            "AND NOT EXISTS (SELECT 1 FROM workflow_notifications n "
            "WHERE n.organization_id=j.organization_id AND n.job_id=j.id AND n.event_key=%s))) "
            "ORDER BY j.created_at,j.id LIMIT 1 FOR UPDATE OF j SKIP LOCKED",
            (STALE_RESERVATION, DELIVERY_REVIEW_KEY),
        ).fetchone()
        if delivery is None:
            return False
        org, department, job, receipt, owner = delivery
        if receipt:
            finish_email_delivery(db, org, department, job, receipt)
        else:
            db.execute(
                "UPDATE outbox_jobs SET state='repair' WHERE organization_id=%s AND department_id=%s AND id=%s",
                (org, department, job),
            )
            enqueue_notification(
                db, org, department, job, owner, DELIVERY_REVIEW_KEY,
                "Workflow email delivery needs review",
                "Gmail delivery could not be confirmed. Check the original send before retrying.", False,
            )
    return True
