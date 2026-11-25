"""Synchronize the reply timeline of a confirmed Gmail delivery."""

from datetime import UTC, datetime, timedelta
from functools import partial
from typing import NamedTuple
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from austra.connectors import GMAIL_CONTENT_SCOPE
from austra.integrations.reply_records import apply_record_replies
from austra.integrations.reply_recovery import READ_BATCH_SIZE, read_pending_replies
from austra.integrations.secrets import ConnectionSecrets
from austra.integrations.workflow_notifications import enqueue_notification
from austra.mcp_gateway import ExternalToolError, call_external
from austra.tenant.context import system_department_transaction, tenant_transaction

REPLY_CHECK_INTERVAL = timedelta(minutes=1)
REPLY_CHECK_BACKOFF = timedelta(minutes=5)
FIRST_REPLY_CHECK = timedelta(seconds=15)
REPLY_TRACKING_PERIOD = timedelta(days=30)


class SentJob(NamedTuple):
    connection_id: UUID
    receipt: str
    recipient: str
    record_id: UUID
    state: str
    data: dict
    health: str
    revision: int
    tracking_until: datetime | None
    owner: UUID
    content_allowed: bool
    email_alerts: bool
    recoveries: dict

    @property
    def delivery(self):
        return self.connection_id, self.receipt, self.recipient, self.record_id


def _sent_job(db, org, department, job, *, lock=False):
    row = db.execute(
        "SELECT x.connection_id, r.provider_receipt, j.payload->>'recipient', e.id, "
        "e.state, e.data, c.health, e.revision, j.reply_tracking_until, "
        "coalesce((j.payload->>'workflow_owner_id')::uuid,x.actor_user_id), "
        "%s=ANY(c.granted_scopes), j.owner_email_alerts, j.reply_recovery FROM outbox_jobs j "
        "JOIN capability_action_receipts r ON (r.organization_id, r.department_id, r.job_id) "
        "= (j.organization_id, j.department_id, j.id) "
        "JOIN integration_executions x ON (x.organization_id, x.department_id) "
        "= (j.organization_id, j.department_id) AND x.idempotency_key = 'email-job:' || j.id::text "
        "AND x.state = 'succeeded' AND x.receipt = r.provider_receipt "
        "JOIN integration_connections c ON (c.organization_id, c.id) = (x.organization_id, x.connection_id) "
        "JOIN capability_records e ON (e.organization_id, e.department_id, e.id) "
        "= (j.organization_id, j.department_id, (j.payload->>'email_id')::uuid) "
        "WHERE j.organization_id = %s AND j.department_id = %s AND j.id = %s "
        "AND j.topic = 'capability.email' AND j.state = 'delivered' AND r.provider_key = 'gmail' "
        "AND c.template_key = 'gmail' AND c.revoked_at IS NULL AND e.kind = 'email_proposal'"
        + (" FOR UPDATE OF e,j" if lock else ""),
        (GMAIL_CONTENT_SCOPE, org, department, job),
    ).fetchone()
    if row is None:
        raise LookupError("A delivered Gmail job with an active connection is required")
    result = SentJob(*row)
    if result.health != "healthy" or result.state not in {"queued", "reply_received"}:
        raise ValueError("Check Gmail connection health and the request status")
    return result


def reply_snapshot(data: dict, tracking_until: datetime | None, failed: bool) -> dict:
    first = data.get("gmail_reply")
    replies = data.get("gmail_replies", [first] if first else [])
    if tracking_until is None:
        status = "manual"
    elif tracking_until <= datetime.now(UTC):
        status = "ended"
    else:
        status = "retrying" if failed else "tracking"
    return {"state": "reply_received" if replies else "delivered", "reply": first,
            "replies": replies, "tracking": status, "tracking_until": tracking_until,
            **({"record_updates": data["record_updates"]} if "record_updates" in data else {})}


def reply_status(db, context, department, job):
    context.require("workflows.read", department)
    context.require("integrations.use", department)
    context.require_active_department(db, department)
    with tenant_transaction(db, context):
        row = db.execute(
            "SELECT e.data,j.reply_tracking_until,j.reply_check_failed,j.reply_recovery,"
            "j.automatic_reply_approved_by IS NOT NULL, r.external_id,r.state,r.revision FROM outbox_jobs j "
            "JOIN capability_records e ON (e.organization_id, e.department_id, e.id) "
            "= (j.organization_id, j.department_id, (j.payload->>'email_id')::uuid) "
            "LEFT JOIN runtime_events origin ON (origin.organization_id,origin.department_id,origin.id) "
            "= (j.organization_id,j.department_id,j.event_id) "
            "LEFT JOIN capability_records r ON (r.organization_id,r.department_id,r.id::text) "
            "= (j.organization_id,j.department_id,origin.payload->>'record_id') "
            "WHERE j.organization_id = %s AND j.department_id = %s AND j.id = %s "
            "AND j.topic = 'capability.email' AND j.state = 'delivered' AND e.kind = 'email_proposal'",
            (context.organization_id, department, job),
        ).fetchone()
        evidence = db.execute(
            "SELECT message_id, content, applied_at FROM workflow_reply_evidence WHERE organization_id=%s "
            "AND department_id=%s AND job_id=%s",
            (context.organization_id, department, job),
        ).fetchall()
        automatic = db.execute(
            "SELECT message_id,state,outcome->'findings',receipt IS NOT NULL FROM workflow_automatic_replies "
            "WHERE organization_id=%s AND department_id=%s AND job_id=%s", (context.organization_id, department, job),
        ).fetchall()
    if row is None:
        raise LookupError("A delivered Gmail job is required")
    recovery = {**row[3], **{message: {"state": "applying"} for message, _, applied in evidence if applied is None}}
    record = {"reference": row[5], "state": row[6], "revision": row[7]} if row[5] is not None else None
    return {
        **reply_snapshot(*row[:3]), "recovery": recovery, "automatic_enabled": row[4],
        "tracked_record": record,
        "evidence": {message: content for message, content, _ in evidence},
        "automatic_actions": {message: {"state": state, "findings": findings, "confirmed": confirmed}
                              for message, state, findings, confirmed in automatic},
    }


def capture_email_reply(db, context, root, department, job):
    context.require("workflows.run", department)
    context.require("workflows.approve", department)
    context.require("integrations.use", department)
    context.require_active_department(db, department)
    return _synchronize(
        db, root, context.organization_id, department, job,
        partial(tenant_transaction, db, context), str(context.actor.user_id),
    )


def capture_tracked_replies(db, root, org, department, job):
    return _synchronize(
        db, root, org, department, job,
        partial(system_department_transaction, db, org, department), None,
    )


def _synchronize(db, root, org, department, job, transaction, actor_id):
    with transaction():
        delivery = _sent_job(db, org, department, job)
    credentials = ConnectionSecrets(root).read(org, delivery.connection_id)
    try:
        result = call_external("gmail_list_replies", {
            "message_id": delivery.receipt, "recipient": delivery.recipient,
        }, credentials)
    except ExternalToolError:
        result = None
    with transaction():
        current = _sent_job(db, org, department, job, lock=True)
        _same_delivery(delivery, current)
        found = result["replies"] if result is not None else []
        data = _merge_replies(db, org, department, job, current, found, actor_id, [])
        if result is not None:
            _update_follow_up(db, org, department, job, found, result)
        processed = _saved_messages(db, org, department, job) if current.content_allowed else set()
        pending = reply_snapshot(data, current.tracking_until, False)["replies"] if current.content_allowed else []
        recoveries = current.recoveries
    evidence, updates = read_pending_replies(
        pending, processed, recoveries, credentials, call_external, datetime.now(UTC),
        manual=actor_id is not None,
    )
    with transaction():
        current = _sent_job(db, org, department, job, lock=True)
        _same_delivery(delivery, current)
        outcomes = current.recoveries
        if current.content_allowed and (evidence or updates):
            already = _saved_messages(db, org, department, job)
            saved = _save_reply_evidence(db, org, department, job, evidence)
            outcomes = _save_recoveries(db, org, department, job, current, recoveries, updates, already, saved)
        retrying = [value["retry_at"] for value in outcomes.values() if value["state"] == "retrying"]
        failed = result is None or bool(retrying)
        next_check = min(map(datetime.fromisoformat, retrying), default=None)
        # A successful check also enrolls prior deliveries, which already have Gmail consent.
        tracking_until = db.execute(
            "UPDATE outbox_jobs SET reply_check_failed=%s, reply_recovery=%s, "
            "reply_check_at=LEAST(%s::timestamptz, CASE WHEN %s "
            "THEN GREATEST(reply_check_at, now() + %s) WHEN %s OR reply_check_at IS NULL "
            "THEN now() + %s ELSE reply_check_at END), "
            "reply_tracking_until = COALESCE(reply_tracking_until, now() + %s) "
            "WHERE organization_id = %s AND department_id = %s AND id = %s "
            "RETURNING reply_tracking_until",
            (failed, Jsonb(outcomes), next_check if current.content_allowed else None,
             result is None, REPLY_CHECK_BACKOFF, actor_id is not None, REPLY_CHECK_INTERVAL, REPLY_TRACKING_PERIOD,
             org, department, job),
        ).fetchone()[0]
    with transaction():
        current = _sent_job(db, org, department, job, lock=True)
        _same_delivery(delivery, current)
        data = _apply_pending_evidence(db, org, department, job, current, actor_id)
    return {**reply_snapshot(data, tracking_until, failed), "recovery": current.recoveries}


def _same_delivery(original, current):
    if current.delivery != original.delivery:
        raise ValueError("Gmail delivery changed during the reply check")


def _saved_messages(db, org, department, job):
    return {row[0] for row in db.execute(
        "SELECT message_id FROM workflow_reply_evidence WHERE organization_id=%s "
        "AND department_id=%s AND job_id=%s", (org, department, job),
    )}


def _save_recoveries(db, org, department, job, current, baseline, updates, already, saved):
    outcomes = dict(current.recoveries)
    for message in saved:
        previous = outcomes.get(message)
        if previous is not None and (message not in updates or previous != baseline.get(message)):
            updates[message] = {"state": "recovered", "attempts": previous["attempts"] + 1,
                                "reason": previous["reason"]}
            baseline = {**baseline, message: previous}
    for message, outcome in updates.items():
        if message in already or outcomes.get(message) != baseline.get(message):
            continue
        outcomes[message] = outcome
        db.execute(
            "INSERT INTO runtime_events (organization_id,department_id,event_type,idempotency_key,"
            "correlation_id,payload) VALUES (%s,%s,'capability.reply_recovery',%s,%s,%s) "
            "ON CONFLICT (organization_id,event_type,idempotency_key) DO NOTHING",
            (org, department, f"{job}:{message}:{outcome['attempts']}", uuid4(),
             Jsonb({"job_id": str(job), "message_id": message, **outcome})),
        )
        if outcome["state"] == "needs_review":
            enqueue_notification(
                db, org, department, job, current.owner, f"reply-repair:{message}",
                "Workflow reply needs review",
                "Austra saved the reply but could not process its content. Review the email timeline.",
                False,
            )
    return outcomes


def _save_reply_evidence(db, org, department, job, evidence):
    saved = []
    for message, content in evidence.items():
        if db.execute(
            "INSERT INTO workflow_reply_evidence (organization_id,department_id,job_id,message_id,content) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING message_id",
            (org, department, job, message, Jsonb(content)),
        ).fetchone():
            saved.append(message)
    return saved


def _apply_pending_evidence(db, org, department, job, current, actor_id):
    if not current.content_allowed:
        return current.data
    pending = db.execute(
        "SELECT message_id,content FROM workflow_reply_evidence WHERE organization_id=%s "
        "AND department_id=%s AND job_id=%s AND applied_at IS NULL ORDER BY captured_at,message_id LIMIT %s",
        (org, department, job, READ_BATCH_SIZE),
    ).fetchall()
    evidence = [
        {"message_id": message, "attachments": [
            {"name": item["name"], "status": item["status"]} for item in content["attachments"]
        ], "quoted_terms": content.get("quoted_terms", {})} for message, content in pending
    ]
    data = _merge_replies(db, org, department, job, current, [], actor_id, evidence)
    if pending:
        db.execute(
            "UPDATE workflow_reply_evidence SET applied_at=now() WHERE organization_id=%s "
            "AND department_id=%s AND job_id=%s AND message_id=ANY(%s)",
            (org, department, job, [message for message, _ in pending]),
        )
    return data


def _update_follow_up(db, org, department, job, replies, result):
    latest_outbound = result.get("latest_outbound_ms", 0)
    latest_received = max((reply["received_ms"] for reply in replies), default=0)
    if latest_outbound >= latest_received and latest_outbound > result.get("original_sent_ms", 0):
        db.execute(
            "UPDATE outbox_jobs SET follow_up_due_at=NULL "
            "WHERE organization_id=%s AND department_id=%s AND id=%s "
            "AND follow_up_due_at IS NOT NULL",
            (org, department, job),
        )


def _merge_replies(db, org, department, job, current, found, actor_id, evidence):
    existing = reply_snapshot(current.data, current.tracking_until, False)["replies"]
    known = {reply["message_id"]: reply for reply in existing}
    added = []
    for reply in found:
        if reply["message_id"] not in known:
            known[reply["message_id"]] = reply
            added.append(reply)
    if not added and not evidence:
        return current.data
    replies = sorted(known.values(), key=lambda reply: (reply["received_ms"], reply["message_id"]))
    data = {**current.data, "gmail_reply": current.data.get("gmail_reply") or replies[0],
            "gmail_replies": replies}
    summary = {item["message_id"]: item["attachments"] for item in current.data.get("reply_documents", [])}
    summary.update((item["message_id"], item["attachments"]) for item in evidence)
    data["reply_documents"] = [{"message_id": key, "attachments": value} for key, value in summary.items()]
    event_type = "capability.email_reply_captured" if added else "capability.email_reply_processed"
    event = db.execute(
        "INSERT INTO runtime_events (organization_id, department_id, event_type, "
        "idempotency_key, correlation_id, payload) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (org, department, event_type, f"{job}:{current.revision + 1}", uuid4(),
         Jsonb({"job_id": str(job), "actor_id": actor_id, "reply": added[0] if added else None,
                "replies": added, "documents": evidence,
                "trigger": "manual" if actor_id else "scheduled"})),
    ).fetchone()[0]
    mapped = apply_record_replies(db, org, department, job, replies, added, evidence, event)
    if mapped is not None:
        outcomes = dict(current.data.get("record_updates", {}))
        for message, outcome in mapped.items():
            if "updated" in outcome:
                previous = outcomes.get(message, {})
                outcome["updated"] = sorted(set(previous.get("updated", []) + outcome["updated"]))
            outcomes[message] = outcome
        data["record_updates"] = outcomes
    revision = db.execute(
        "UPDATE capability_records SET state = 'reply_received', data = %s, revision = revision + 1, "
        "updated_at = now() WHERE organization_id = %s AND department_id = %s AND id = %s RETURNING revision",
        (Jsonb(data), org, department, current.record_id),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO capability_record_revisions (organization_id, department_id, record_id, revision, "
        "state, data, event_id) VALUES (%s, %s, %s, %s, 'reply_received', %s, %s)",
        (org, department, current.record_id, revision, Jsonb(data), event),
    )
    if mapped is None:
        _update_purchase_order(db, org, department, current, replies, summary, evidence, event)
    for reply in added:
        enqueue_notification(
            db, org, department, job, current.owner, f"reply:{reply['message_id']}",
            "Supplier replied" if mapped is None else "Workflow email reply received",
            f"{reply['from']} replied to “{reply['subject']}”.",
            current.email_alerts,
        )
    if added:
        db.execute(
            "UPDATE outbox_jobs SET follow_up_due_at=now() + make_interval(days => follow_up_days) "
            "WHERE organization_id=%s AND department_id=%s AND id=%s",
            (org, department, job),
        )
    return data


def _update_purchase_order(db, org, department, delivery, replies, summary, evidence, event):
    po_id = delivery.data.get("po_id")
    if not po_id or not replies:
        return
    info = {"status": "reply_received", "count": len(replies), "latest": replies[-1],
            "documents_to_review": sum(len(value) for value in summary.values())}
    quotes = {item["message_id"]: item["quoted_terms"] for item in evidence if item["quoted_terms"]}
    if quotes:
        earlier = db.execute(
            "SELECT data #>> '{supplier_response,supplier_quote,source_message_id}' "
            "FROM capability_records WHERE organization_id=%s AND department_id=%s AND id=%s FOR UPDATE",
            (org, department, UUID(po_id)),
        ).fetchone()
        previous_quote = earlier[0] if earlier else None
        quote = next(reply["message_id"] for reply in reversed(replies)
                     if reply["message_id"] in quotes or reply["message_id"] == previous_quote)
        if quote in quotes:
            info["supplier_quote"] = {"source_message_id": quote, "terms": quotes[quote], "status": "needs_review"}
    updated = db.execute(
        "UPDATE capability_records SET data=jsonb_set(data, '{supplier_response}', "
        "coalesce(data->'supplier_response','{}'::jsonb) || %s), "
        "revision=revision+1, updated_at=now() "
        "WHERE organization_id=%s AND department_id=%s AND id=%s AND kind='purchase_order' "
        "RETURNING revision,state,data",
        (Jsonb(info), org, department, UUID(po_id)),
    ).fetchone()
    if updated:
        revision, state, data = updated
        db.execute(
            "INSERT INTO capability_record_revisions (organization_id,department_id,record_id,revision,"
            "state,data,event_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (org, department, UUID(po_id), revision, state, Jsonb(data), event),
        )
