"""Queue an email or generated attachment for a record in an approved workflow."""

import re
from base64 import b64encode
from hashlib import sha256
from html import escape
from io import BytesIO
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate

from austra.capabilities.reply_mapping import CONTENT_SOURCES, TRACKED_EMAIL_EVENT
from austra.capabilities.runtime import active_builds, department_capabilities, select_build
from austra.integrations.catalog import GMAIL_SEND
from austra.integrations.gateway import _validate
from austra.tenant.context import tenant_transaction


class RecordEmail(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    record_id: UUID
    expected_revision: int = Field(ge=1)
    recipient: str = Field(min_length=3, max_length=254, pattern=r"^[^\s,;<>@]+@[^\s,;<>@]+$")
    subject: str = Field(min_length=1, max_length=200, pattern=r"^[^\r\n]+$")
    body: str = Field(min_length=1, max_length=10000)
    idempotency_key: UUID


def _document(config, external_id, data, *, name_prefix=""):
    buffer = BytesIO()
    styles = getSampleStyleSheet()
    story = [Paragraph(escape(config["title"]), styles["Title"]),
             Paragraph(f"Record reference: {escape(external_id)}", styles["Normal"])]
    for field in config["fields"]:
        value = data.get(field)
        if not value:
            raise ValueError(f"Fill in {field.replace('_', ' ')} before preparing this document")
        unit = data.get("units", {}).get(field)
        if unit:
            value = f"{value} {unit}"
        story.extend([Paragraph(escape(field.replace("_", " ").capitalize()), styles["Heading3"]),
                      Paragraph(escape(value).replace("\n", "<br/>"), styles["BodyText"])])
    SimpleDocTemplate(buffer, title=config["title"]).build(story)
    content = buffer.getvalue()
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{name_prefix} {external_id}".strip()).strip("-.")[:100] or "record"
    return {"name": f"{name}.pdf", "sha256": sha256(content).hexdigest(),
            "base64": b64encode(content).decode("ascii")}


def prepare_record_email(db, context, department, build_id, request: RecordEmail):
    context.require("workflows.approve", department)
    context.require("integrations.use", department)
    context.require_active_department(db, department)
    if context.actor.role != "org_admin":
        raise PermissionError("Organization admin required to prepare this email")
    _validate(GMAIL_SEND.public(), {"to": request.recipient, "subject": request.subject, "body": request.body})
    org = context.organization_id
    event_payload = {**request.model_dump(mode="json", exclude={"idempotency_key"}),
                     "build_id": str(build_id), "actor_id": str(context.actor.user_id)}
    params = (org, department, TRACKED_EMAIL_EVENT, str(request.idempotency_key))
    with tenant_transaction(db, context):
        event = db.execute(
            "INSERT INTO runtime_events (organization_id,department_id,event_type,idempotency_key,"
            "correlation_id,payload) VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (organization_id,event_type,idempotency_key) DO NOTHING RETURNING id",
            (*params, uuid4(), Jsonb(event_payload)),
        ).fetchone()
        if event is None:
            prior = db.execute(
                "SELECT e.payload,j.id,r.external_id FROM runtime_events e "
                "JOIN outbox_jobs j ON (j.organization_id,j.event_id)=(e.organization_id,e.id) "
                "JOIN capability_records r ON (r.organization_id,r.department_id,r.id)="
                "(j.organization_id,j.department_id,(j.payload->>'email_id')::uuid) "
                "WHERE e.organization_id=%s AND e.department_id=%s AND e.event_type=%s "
                "AND e.idempotency_key=%s", params,
            ).fetchone()
            if prior is None or prior[0] != event_payload:
                raise ValueError("Request key already used for a different email or admin")
            return {"job_id": str(prior[1]), "reference": prior[2]}
        build = select_build(active_builds(db, context), lambda candidate: candidate["build_id"] == build_id)
        record = db.execute(
            "SELECT kind,external_id,revision,state,data FROM capability_records "
            "WHERE organization_id=%s AND department_id=%s AND id=%s FOR SHARE",
            (org, department, request.record_id),
        ).fetchone()
        if not record:
            raise LookupError("Workflow record not found")
        kind, reference, revision, state, data = record
        definition = next((item for item in department_capabilities(build["manifest"], department)
                           if item["record_kind"] == kind and item["key"] == data.get("capability_key")
                           and (item.get("reply_mapping") or item.get("email_document")
                                or item.get("automatic_email"))), None)
        if not definition or revision != request.expected_revision:
            raise ValueError("Choose a current record from this released workflow")
        if (mapping := definition.get("reply_mapping")) and state not in mapping["states"]:
            raise ValueError("The record is outside the approved states for tracking replies")
        mail = {"recipient": request.recipient, "subject": request.subject, "body": request.body}
        mail["content_required"] = bool(definition.get("automatic_email")) or any(
            field["source"] in CONTENT_SOURCES for field in (mapping or {}).get("fields", [])
        )
        mail["workflow_owner_id"] = definition["owner"]
        if automation := definition.get("automatic_email"):
            mail["automatic_email"] = automation
        if config := definition.get("email_document"):
            mail["attachment"] = _document(config, reference, data)
        email_reference = f"{reference[:89]} · {event[0].hex[:8]}"
        email = db.execute(
            "INSERT INTO capability_records (organization_id,department_id,kind,external_id,state,data) "
            "VALUES (%s,%s,'email_proposal',%s,'queued',%s) RETURNING id",
            (org, department, email_reference, Jsonb({"record_reference": reference})),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO capability_record_revisions (organization_id,department_id,record_id,"
            "revision,state,data,event_id) VALUES (%s,%s,%s,1,'queued',%s,%s)",
            (org, department, email, Jsonb({"record_reference": reference}), event[0]),
        )
        job_id = db.execute(
            "INSERT INTO outbox_jobs (organization_id,department_id,event_id,topic,payload,state) "
            "VALUES (%s,%s,%s,'capability.email',%s,'pending') RETURNING id",
            (org, department, event[0], Jsonb({**mail, "email_id": str(email)})),
        ).fetchone()[0]
    return {"job_id": str(job_id), "reference": email_reference}
