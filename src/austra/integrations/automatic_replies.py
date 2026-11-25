"""Evaluate and deliver the explicitly approved replies of a tracked Gmail case."""

from datetime import timedelta
from typing import NamedTuple
from uuid import uuid4

from psycopg.types.json import Jsonb

from austra.capabilities.email_automation import FINDINGS, evaluate_reply
from austra.connectors import GMAIL_CONTENT_SCOPE
from austra.integrations.secrets import ConnectionSecrets
from austra.integrations.tracked_email import _document
from austra.integrations.workflow_notifications import enqueue_notification
from austra.mcp_gateway import ExternalToolError, call_external
from austra.tenant.context import system_department_transaction

MAX_OUTBOUND = 20
SEND_DEADLINE = timedelta(minutes=5)
RETRY_DELAYS = (timedelta(seconds=15), timedelta(minutes=1), timedelta(minutes=5))
MAX_ATTEMPTS = len(RETRY_DELAYS)

BINDINGS = (
    " FROM outbox_jobs j "
    "JOIN runtime_events e ON (e.organization_id,e.department_id,e.id)=(j.organization_id,j.department_id,j.event_id) "
    "JOIN releases r ON r.organization_id=j.organization_id AND r.build_id::text=e.payload->>'build_id' "
    "AND r.state='active' "
    "JOIN capability_build_artifacts a ON (a.organization_id,a.build_id)=(r.organization_id,r.build_id) "
    "JOIN organizations o ON o.id=j.organization_id AND o.status='active' "
    "JOIN departments d ON (d.organization_id,d.id)=(j.organization_id,j.department_id) AND d.archived_at IS NULL "
    "JOIN users approver ON (approver.organization_id,approver.id)=(j.organization_id,j.automatic_reply_approved_by) "
    "AND approver.status='active' AND approver.deleted_at IS NULL AND approver.role='org_admin' "
    "JOIN integration_executions x ON (x.organization_id,x.department_id)=(j.organization_id,j.department_id) "
    "AND x.idempotency_key='email-job:'||j.id::text AND x.state='succeeded' AND x.actor_user_id=approver.id "
    "JOIN capability_action_receipts receipt ON (receipt.organization_id,receipt.job_id)=(j.organization_id,j.id) "
    "AND receipt.provider_key='gmail' AND receipt.provider_receipt=x.receipt "
    "JOIN integration_approvals approval ON (approval.organization_id,approval.department_id,approval.id)="
    "(x.organization_id,x.department_id,x.approval_id) AND approval.revoked_at IS NULL "
    "AND approval.connection_id=x.connection_id AND approval.build_id=r.build_id "
    "AND approval.action_name='gmail.send_email' "
    "JOIN integration_connections c ON (c.organization_id,c.id)=(x.organization_id,x.connection_id) "
    "AND c.health='healthy' AND c.revoked_at IS NULL AND c.template_key='gmail' AND %s=ANY(c.granted_scopes) "
    "JOIN capability_records record ON (record.organization_id,record.department_id)=(j.organization_id,j.department_id) "
    "AND record.id::text=e.payload->>'record_id' "
    "JOIN LATERAL jsonb_array_elements(a.manifest->'capabilities') definition ON "
    "definition->>'key'=record.data->>'capability_key' AND definition->>'record_kind'=record.kind "
    "AND definition->'automatic_email'=j.payload->'automatic_email' "
    "AND (definition->'department_ids'='[]' OR definition->'department_ids' ? j.department_id::text) "
    "JOIN capability_records email ON (email.organization_id,email.department_id)=(j.organization_id,j.department_id) "
    "AND email.id::text=j.payload->>'email_id' "
)


class PendingReply(NamedTuple):
    org: object
    department: object
    job: object
    record: object
    state: str
    reference: str
    data: dict
    config: dict
    transitions: list[dict]
    authorities: list[dict]
    message: dict
    evidence: dict
    recipient: str
    subject: str
    owner: object


def plan_automatic_reply(db):
    with db.transaction():
        row = db.execute(
            "SELECT j.organization_id,j.department_id,j.id,record.id,record.state,record.external_id,record.data,"
            "definition->'automatic_email',definition->'transitions',a.manifest->'decision_tables',message,ev.content,"
            "j.payload->>'recipient',j.payload->>'subject',(j.payload->>'workflow_owner_id')::uuid"
            + BINDINGS +
            "JOIN workflow_reply_evidence ev ON (ev.organization_id,ev.department_id,ev.job_id)="
            "(j.organization_id,j.department_id,j.id) AND ev.applied_at IS NOT NULL "
            "JOIN LATERAL jsonb_array_elements(email.data->'gmail_replies') message "
            "ON message->>'message_id'=ev.message_id "
            "WHERE j.state='delivered' AND j.topic='capability.email' "
            "AND NOT EXISTS (SELECT 1 FROM workflow_automatic_replies f WHERE f.organization_id=j.organization_id "
            "AND f.job_id=j.id AND (f.message_id=ev.message_id OR f.state IN ('pending','sending','uncertain'))) "
            "ORDER BY (message->>'received_ms')::bigint,ev.message_id LIMIT 1 FOR UPDATE OF j,record SKIP LOCKED",
            (GMAIL_CONTENT_SCOPE,),
        ).fetchone()
        if not row:
            return False
        reply = PendingReply(*row)
        decision, findings = evaluate_reply(reply.config, reply.state, reply.data, reply.evidence)
        transition = decision.get("transition") if decision else None
        if transition and not _current_authority(reply, transition):
            decision, findings = None, ["The automatic transition no longer has current approval."]
        if reply.message.get("from", "").casefold() != reply.recipient.casefold():
            decision, findings = None, ["The reply sender differs from the approved recipient."]
        if decision and decision.get("body") and db.execute(
            "SELECT count(*) FROM workflow_automatic_replies WHERE organization_id=%s AND job_id=%s AND state='sent'",
            (reply.org, reply.job),
        ).fetchone()[0] >= MAX_OUTBOUND:
            decision, findings = None, ["Automatic reply limit reached for this conversation."]
        with system_department_transaction(db, reply.org, reply.department):
            _save_plan(db, reply, decision, findings)
    return True


def _current_authority(reply, transition):
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    target = f"command.{reply.data['capability_key']}.{transition}"
    return any(rule["content"].get("decision_key") == target and rule["content"].get("allow_automatic_email")
               and datetime.fromisoformat(rule["valid_from"]) <= now and not rule.get("valid_to")
               for rule in reply.authorities)


def _save_plan(db, reply, decision, findings):
    message = reply.message["message_id"]
    outcome = {"origin": reply.state, "record": str(reply.record), "owner": str(reply.owner), "findings": findings}
    state = "needs_review" if findings and not (decision or {}).get("body") and not (
        decision or {}
    ).get("transition") else "completed"
    if decision:
        formatted = "\n".join(f"- {finding}" for finding in findings)
        fields = {key: value.replace(FINDINGS, formatted) for key, value in decision.get("fields", {}).items()}
        if transition := decision.get("transition"):
            step = next(item for item in reply.transitions if item["key"] == transition)
            outcome.update({"next_state": step["to"], "fields": fields})
        if body := decision.get("body"):
            outcome["message"] = {
                "to": reply.recipient, "subject": reply.subject, "body": body.replace(FINDINGS, formatted),
                "thread_id": reply.message["thread_id"], "parent_id": message,
            }
            state = "pending"
        if document := decision.get("document"):
            try:
                output = _document(document, reply.reference, {**reply.data, **fields}, name_prefix=document["title"])
            except ValueError:
                state = "needs_review"
                outcome["findings"] = [*findings, "An approved field for the outgoing document is missing."]
            else:
                outcome["message"].update({"attachment_name": output["name"], "attachment_base64": output["base64"]})
    db.execute(
        "INSERT INTO workflow_automatic_replies (organization_id,department_id,job_id,message_id,state,outcome) "
        "VALUES (%s,%s,%s,%s,%s,%s)", (reply.org, reply.department, reply.job, message, state, Jsonb(outcome)),
    )
    if state == "completed":
        _advance(db, reply.org, reply.department, reply.job, message, outcome)
    if state == "needs_review":
        _notify_review(db, reply.org, reply.department, reply.job, message, outcome)


def _advance(db, org, department, job, message, outcome):
    if not outcome.get("next_state"):
        return True
    updated = db.execute(
        "UPDATE capability_records SET state=%s,data=data||%s,revision=revision+1,updated_at=now() "
        "WHERE organization_id=%s AND department_id=%s AND id=%s AND state=%s RETURNING revision,data",
        (outcome["next_state"], Jsonb(outcome["fields"]), org, department, outcome["record"], outcome["origin"]),
    ).fetchone()
    if not updated:
        return False
    event = db.execute(
        "INSERT INTO runtime_events (organization_id,department_id,event_type,idempotency_key,correlation_id,payload) "
        "VALUES (%s,%s,'capability.automatic_email_transition',%s,%s,%s) RETURNING id",
        (org, department, f"{job}:{message}", uuid4(), Jsonb({"job_id": str(job), "message_id": message,
         "record_id": outcome["record"], "state": outcome["next_state"], "findings": outcome["findings"]})),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO capability_record_revisions (organization_id,department_id,record_id,revision,state,data,event_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (org, department, outcome["record"], updated[0], outcome["next_state"], Jsonb(updated[1]), event),
    )
    return True


def _notify_review(db, org, department, job, message, outcome):
    enqueue_notification(db, org, department, job, outcome["owner"], f"automatic-reply-review:{message}",
                         "Supplier reply needs review", "Open the tracked email and review the saved findings.", False)


def _claim(db):
    with db.transaction():
        row = db.execute(
            "SELECT f.id,j.organization_id,j.department_id,j.id,f.message_id,f.state,f.outcome,x.connection_id,"
            "record.state,f.send_attempts,f.receipt_checks" + BINDINGS +
            "JOIN workflow_automatic_replies f ON (f.organization_id,f.department_id,f.job_id)="
            "(j.organization_id,j.department_id,j.id) "
            "WHERE j.state='delivered' AND ((f.state='pending' AND f.next_attempt_at<=now()) "
            "OR (f.state='uncertain' AND f.next_attempt_at<=now() AND f.receipt_checks<%s) "
            "OR (f.state='sending' AND f.updated_at < now()-%s)) "
            "ORDER BY f.next_attempt_at,f.created_at LIMIT 1 FOR UPDATE OF f,record SKIP LOCKED",
            (GMAIL_CONTENT_SCOPE, MAX_ATTEMPTS, SEND_DEADLINE),
        ).fetchone()
        if not row:
            return None, False
        identifier, org, department, job, message, state, outcome, _connection, record_state, _, _ = row
        checking = state in {"sending", "uncertain"}
        if not checking and record_state != outcome["origin"]:
            with system_department_transaction(db, org, department):
                _notify_review(db, org, department, job, message, outcome)
            db.execute("UPDATE workflow_automatic_replies SET state='needs_review',updated_at=now() "
                       "WHERE organization_id=%s AND department_id=%s AND id=%s", (org, department, identifier))
            return None, True
        db.execute("UPDATE workflow_automatic_replies SET state=%s,updated_at=now(),next_attempt_at=now()+%s, "
                   "send_attempts=send_attempts+%s,receipt_checks=receipt_checks+%s "
                   "WHERE organization_id=%s AND department_id=%s AND id=%s",
                   ("uncertain" if checking else "sending", SEND_DEADLINE, int(not checking), int(checking),
                    org, department, identifier))
        return row, True


def dispatch_automatic_reply(db, root):
    row, worked = _claim(db)
    if not row:
        return worked
    identifier, org, department, job, message, prior, outcome, connection, _, sends, checks = row
    checking = prior != "pending"
    payload = outcome["message"]
    receipt = None
    result = {}
    try:
        credentials = ConnectionSecrets(root).read(org, connection)
        if checking:
            result = call_external("gmail_find_automatic_reply", {"thread_id": payload["thread_id"],
                                   "dispatch_id": identifier.hex, "recipient": payload["to"]}, credentials)
        else:
            result = call_external("gmail_reply_in_thread", {**payload, "dispatch_id": identifier.hex}, credentials)
        if (checking or result.get("sent") is True) and isinstance(result.get("message_id"), str):
            receipt = result["message_id"] or None
    except (ExternalToolError, OSError, ValueError, LookupError):
        receipt = None
    with system_department_transaction(db, org, department):
        current = db.execute("SELECT state FROM workflow_automatic_replies WHERE organization_id=%s AND "
                             "department_id=%s AND id=%s FOR UPDATE", (org, department, identifier)).fetchone()
        if not current or current[0] not in {"sending", "uncertain"}:
            return True
        delay = RETRY_DELAYS[min(checks if checking else sends, MAX_ATTEMPTS - 1)]
        if receipt:
            final = "sent"
        elif checking:
            final = "uncertain"
        elif result.get("safe_retry") is True:
            final = "pending" if sends + 1 < MAX_ATTEMPTS else "needs_review"
        elif result.get("rejected") is True or result.get("sent") is False:
            final = "needs_review"
        else:
            final = "uncertain"
        if receipt and not _advance(db, org, department, job, message, outcome):
            final = "needs_review"
        db.execute("UPDATE workflow_automatic_replies SET state=%s,receipt=%s,updated_at=now(),next_attempt_at=now()+%s "
                   "WHERE organization_id=%s AND department_id=%s AND id=%s AND state IN ('sending','uncertain')",
                   (final, receipt, delay, org, department, identifier))
        if final == "needs_review" or (final == "uncertain" and checking and checks + 1 >= MAX_ATTEMPTS):
            _notify_review(db, org, department, job, message, outcome)
    return True
