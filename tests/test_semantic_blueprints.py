"""Phase 2 graph, citation, compiler, and confirmation gates in isolated tenants."""

import os
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from io import BytesIO
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet
from docx import Document
from psycopg import sql
from pypdf import PdfReader

from austra.build_release.monitoring import list_reviews, triage_feedback
from austra.build_release.service import create_build, preview_build, publish, transition_build
from austra.capabilities.builder import build_capability
from austra.capabilities.contracts import CapabilityCommand
from austra.capabilities.projections import projection_data
from austra.capabilities.runtime import capability_view, execute_command
from austra.capabilities.verifier import verify_build
from austra.connectors import GMAIL_CONTENT_SCOPE, GMAIL_SCOPE
from austra.external_mcp_server import (
    gmail_find_automatic_reply,
    gmail_list_replies,
    gmail_reply_in_thread,
    gmail_send_email,
)
from austra.integrations import gmail_replies
from austra.integrations.automatic_replies import dispatch_automatic_reply, plan_automatic_reply
from austra.integrations.connections import connect_gmail
from austra.integrations.email_delivery_recovery import reconcile_email_delivery
from austra.integrations.email_jobs import (
    approve_email_connection,
    deliver_email_job,
    preview_email_job,
)
from austra.integrations.gmail_content import reply_content
from austra.integrations.gmail_replies import reply_status
from austra.integrations.gmail_reply_worker import check_due_gmail_reply
from austra.integrations.tracked_email import RecordEmail, prepare_record_email
from austra.integrations.workflow_notifications import list_notifications
from austra.mcp_gateway import ExternalToolError
from austra.onboarding import workflow_plans
from austra.onboarding.discovery import DiscoveryTurn, WorkflowDraft
from austra.onboarding.evidence import exclude_source, process_job, save_file, save_website_page
from austra.onboarding.service import draft_case, open_case, workflow_cases
from austra.semantics.service import (
    add_edge,
    add_revision,
    answer_question,
    confirm_candidate,
    decide_claim,
    extract_claims,
    propose_candidate,
    record_conflict,
    review_candidate,
)
from austra.semantics.workflow_definition import Definition, define_workflow
from austra.skills import PLATFORM_SKILLS, load_skill, load_skills, semantic_skill_cards
from austra.storage.migrate import apply_migrations
from austra.tenant.context import TenantContext, tenant_transaction
from austra.tenant.permissions.evaluator import AuthContext, DepartmentGrant


@pytest.fixture
def database():
    url = os.environ.get("AUSTRA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("AUSTRA_TEST_DATABASE_URL is required")
    schema = f"semantics_test_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            with psycopg.connect(url, options=f"-c search_path={schema}") as connection:
                apply_migrations(connection)
                yield connection
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def organization(connection, slug, name=None):
    org_id = connection.execute(
        "INSERT INTO organizations (slug, name) VALUES (%s, %s) RETURNING id",
        (slug, name or slug),
    ).fetchone()[0]
    user_id = connection.execute(
        "INSERT INTO users (organization_id, username_normalized, role, password_hash, "
        "must_change_password) VALUES (%s, 'admin', 'org_admin', 'test', false) RETURNING id",
        (org_id,),
    ).fetchone()[0]
    event_id = connection.execute(
        "INSERT INTO audit_events (organization_id, actor_user_id, channel, action, "
        "outcome, correlation_id) VALUES (%s, %s, 'test', 'organization.created', "
        "'success', %s) RETURNING id",
        (org_id, user_id, uuid4()),
    ).fetchone()[0]
    actor = TenantContext.from_auth(AuthContext(user_id, org_id, "org_admin", True, True))
    return actor, open_case(connection, org_id, event_id)


def source_claim(connection, actor, root, filename="operating.txt", body=None):
    source = save_file(
        connection, actor, root, filename,
        body or b"A purchasing manager may approve orders up to 100 USD.",
    )
    process_job(connection, actor, root, source["revision_id"])
    exclude_source(connection, actor, source["source_id"], False)
    with tenant_transaction(connection, actor):
        chunk_id = connection.execute(
            "SELECT id FROM evidence_chunks WHERE organization_id = %s AND revision_id = %s",
            (actor.organization_id, source["revision_id"]),
        ).fetchone()[0]
    claim_id = extract_claims(connection, actor, chunk_id)[0]["id"]
    decide_claim(connection, actor, claim_id, "confirmed", "Administrator verified operating rule")
    return claim_id


def proposal(revision_ids, edge_ids=()):
    return {
        "revision_ids": list(revision_ids),
        "edge_ids": list(edge_ids),
        "behavior": {
            "reads": [],
            "decisions": [],
            "actions": [],
            "approvers": [],
            "manual_steps": ["review"],
            "outputs": ["decision"],
        },
        "acceptance_examples": [
            {"category": category, "input": f"sample {category}", "expected": "review"}
            for category in ("normal", "boundary", "adverse")
        ],
    }


def test_parallel_workflows_from_draft_to_department_approval(database, tmp_path):
    tenants = []
    scenarios = (
        ("ikea", "IKEA", "Stock discrepancy", "counted_quantity", "pieces", "store"),
        ("jswsteel", "JSW Steel", "Batch quality hold", "batch_weight", "kg", "line"),
        ("sunpharma", "Sun Pharma", "Deviation review", "affected_quantity", "units", "site"),
        ("polysilicon-manufacturing", "Polysilicon Manufacturing", "Material shortage",
         "shortage_quantity", "kg", "material"),
    )
    for slug, label, primary, measure, unit, grouping in scenarios:
        admin, _ = organization(database, slug, label)
        department = database.execute(
            "INSERT INTO departments (organization_id, slug, name) VALUES (%s, 'ops', 'Ops') RETURNING id",
            (admin.organization_id,),
        ).fetchone()[0]
        user = database.execute(
            "INSERT INTO users (organization_id, username_normalized, role, password_hash, must_change_password) "
            "VALUES (%s, 'operator', 'employee', 'fixture', false) RETURNING id",
            (admin.organization_id,),
        ).fetchone()[0]
        operator = TenantContext.from_auth(AuthContext(user, admin.organization_id, "employee", True, True,
            grants=(DepartmentGrant(department, frozenset({"workflows.run", "workflows.read"})),)))
        workflows = []
        for name in (primary, "Supply exception"):
            case = draft_case(database, admin, name, uuid4())["id"]
            draft = propose_candidate(database, admin, case, {**proposal([]), "name": name,
                                      "open_decisions": ["Who approves?"]})
            claim = source_claim(
                database, admin, tmp_path, filename=name + ".txt",
                body=f"An approved reviewer may resolve {name} up to 100 {unit}.".encode(),
            )
            definition = Definition.model_validate({
                "content_hash": draft["content_hash"], "claim_id": claim, "department_ids": [department],
                "resolutions": {"Who approves?": "The designated workflow approver."},
                "rationale": "Manager confirms the approval rule from the included policy.",
                "states": ["pending", "approved"], "failure_path": "hold for review",
                "fields": [{"key": measure, "type": "decimal", "unit": unit},
                           {"key": grouping, "type": "text"}],
                "projections": [{"key": "pending_by_group", "title": name + " by group",
                                 "view": "chart", "group_by": grouping, "states": ["pending"],
                                 "fields": ["external_id", measure],
                                 "measure": measure if slug == "polysilicon-manufacturing" else None}],
                "transitions": [
                    {"key": "submit", "from": None, "to": "pending",
                     "fields": ["external_id", grouping, measure],
                     "permission": "workflows.run"},
                    {"key": "approve", "from": "pending", "to": "approved", "fields": [],
                     "permission": "workflows.approve", "allowed_user_ids": [admin.actor.user_id],
                     "guard": {"field": measure, "unit": unit, "op": "lte", "value": "100"}},
                ],
            })
            ready = define_workflow(database, admin, case, definition)
            assert ready["issues"] == []
            with pytest.raises(ValueError, match="changed"):
                define_workflow(database, admin, case, definition)
            with pytest.raises(ValueError, match="hash"):
                confirm_candidate(database, admin, case, draft["content_hash"])
            blueprint = confirm_candidate(database, admin, case, ready["content_hash"])["blueprint_id"]
            build = create_build(database, admin, blueprint)["id"]
            artifact = build_capability(database, admin, build, "Restore prior version or hold intake")
            revision_no = transition_build(database, admin, build, "testing", 1, uuid4())
            assert verify_build(database, admin, build)["passed"]
            revision_no = transition_build(database, admin, build, "admin_preview", revision_no, uuid4())
            preview_build(database, admin, build, revision_no, True)
            for stage in ("accepted", "ready_for_release"):
                revision_no = transition_build(database, admin, build, stage, revision_no, uuid4())
            publish(database, admin, build, revision_no, uuid4())
            workflows.append((case, artifact["manifest"], measure, grouping))
        extra_department = database.execute(
            "INSERT INTO departments (organization_id, slug, name) VALUES (%s, 'other', 'Other') RETURNING id",
            (admin.organization_id,),
        ).fetchone()[0]
        with pytest.raises(ValueError, match="No released capability"):
            capability_view(database, admin, extra_department)
        assert len(workflow_cases(database, admin)) == 2
        assert len(capability_view(database, operator, department)["workflows"]) == 2
        tenants.append((admin, operator, department, workflows))

    for index, (admin, operator, department, workflows) in enumerate(tenants):
        other = tenants[(index + 1) % len(tenants)]
        assert review_candidate(database, other[0], workflows[0][0]) is None
        record_ids = []
        for _, manifest, measure, grouping in workflows:
            key = manifest["capabilities"][0]["key"]
            chart = next(p for p in manifest["projections"] if p["view"] == "chart")
            submit = CapabilityCommand.model_validate({
                "department_id": department, "action": "transition_record", "idempotency_key": str(uuid4()),
                "correlation_id": uuid4(), "payload": {"capability_key": key, "transition_key": "submit",
                                                       "external_id": "REQ-1",
                                                       "fields": {measure: "100", grouping: "Group A"}},
            })
            result = execute_command(database, operator, submit)
            assert execute_command(database, operator, submit) == result
            record = result["record"]["id"]
            with pytest.raises(PermissionError):
                projection_data(database, operator, department, chart["key"], manifest)
            view = projection_data(database, admin, department, chart["key"], manifest)
            rows = view["surface"]["messages"][1]["updateComponents"]["components"][0]["rows"]
            assert [(row["label"], row["first"]) for row in rows] == [
                ("Group A", "100" if chart["measure"] else "1"),
            ]
            selected = projection_data(
                database, admin, department, chart["key"], manifest, group_value="Group A"
            )
            assert [item["values"] for item in selected["records"]] == [
                {"external_id": "REQ-1", measure: "100"},
            ]
            empty = projection_data(
                database, admin, department, chart["key"], manifest, group_value="Other group"
            )
            assert empty["record_count"] == 0
            approve = CapabilityCommand.model_validate({
                "department_id": department, "action": "transition_record", "idempotency_key": str(uuid4()),
                "correlation_id": uuid4(), "expected_revisions": {record: 1},
                "payload": {"capability_key": key, "transition_key": "approve", "record_id": record},
            })
            with pytest.raises(PermissionError):
                execute_command(database, operator, approve)
            if record_ids:
                wrong = approve.model_dump(mode="json")
                wrong["payload"]["record_id"] = record_ids[0]
                with pytest.raises(LookupError):
                    execute_command(database, admin, CapabilityCommand.model_validate(wrong))
            decision = execute_command(database, admin, approve)
            assert decision["record"]["state"] == "approved"
            assert projection_data(database, admin, department, chart["key"], manifest)["record_count"] == 0
            assert execute_command(database, admin, approve) == decision
            foreign = submit.model_copy(update={"department_id": other[2]})
            with pytest.raises(ValueError, match="released build"):
                execute_command(database, other[0], foreign)
            record_ids.append(record)


def _logistics_definition(db, admin, department, root):
    name = "Carrier Dispatch"
    question = "Which fields and labels from the carrier's reply update the shipment?"
    case = draft_case(db, admin, name, uuid4())["id"]
    draft = propose_candidate(db, admin, case, {**proposal([]), "name": name, "open_decisions": [question],
                                                "email_requirements": ["reply_mapping", "email_document"]})
    claim = source_claim(db, admin, root, body=(
        b"Send the approved Dispatch request PDF with destination. While awaiting the carrier, "
        b"read Tracking reference and Gross weight from the carrier's attachment."
    ))
    definition = Definition.model_validate({
        "content_hash": draft["content_hash"], "claim_id": claim, "department_ids": [department],
        "resolutions": {question: "Tracking reference and Gross weight in kg from the attachment."},
        "rationale": "Operations approved the carrier dispatch instructions.",
        "states": ["awaiting_carrier", "closed"], "failure_path": "Ask operations to review.",
        "fields": [{"key": key, "type": "text"} for key in
                   ("destination", "tracking_reference", "carrier_contact", "closure_notes")] +
                  [{"key": "gross_weight", "type": "decimal", "unit": "kg"}],
        "transitions": [
            {"key": "create_dispatch", "from": None, "to": "awaiting_carrier",
             "fields": ["external_id", "destination"], "permission": "workflows.run"},
            {"key": "close_dispatch", "from": "awaiting_carrier", "to": "closed",
             "fields": ["closure_notes"], "permission": "workflows.approve"},
        ],
        "projections": [{
            "key": "dispatches", "title": "Dispatches", "view": "table",
            "fields": ["external_id", "destination", "tracking_reference", "gross_weight", "carrier_contact"],
        }],
        "reply_mapping": {
            "states": ["awaiting_carrier"],
            "fields": [
                {"target": "tracking_reference", "source": "attachment_label", "label": "Tracking reference"},
                {"target": "gross_weight", "source": "attachment_label", "label": "Gross weight"},
                {"target": "carrier_contact", "source": "sender"},
            ],
        },
        "email_document": {"title": "Dispatch request", "fields": ["destination"]},
    })
    return case, draft, definition


def _logistics_release(db, admin, department, root, configure=None):
    case, draft, definition = _logistics_definition(db, admin, department, root)
    assert draft["issues"]
    with pytest.raises(ValueError):
        confirm_candidate(db, admin, case, draft["content_hash"])
    person = TenantContext.from_auth(AuthContext(uuid4(), admin.organization_id, "employee", True, True))
    with pytest.raises(PermissionError):
        define_workflow(db, person, case, definition)
    with pytest.raises(ValueError, match="Complete the reply mapping"):
        define_workflow(db, admin, case, definition.model_copy(update={"reply_mapping": None}))
    invalid = definition.model_copy(deep=True)
    invalid.reply_mapping.fields[0].target = "closure_notes"
    with pytest.raises(ValueError, match="approval controls"):
        define_workflow(db, admin, case, invalid)
    if configure:
        definition = Definition.model_validate(configure(definition.model_dump(by_alias=True)))
    ready = define_workflow(db, admin, case, definition)
    assert ready["issues"] == []
    review = review_candidate(db, admin, case)
    reviewed = next(row["content"] for row in review["revisions"] if row["kind"] == "capability")
    assert reviewed["reply_mapping"]["fields"][0]["target"] == "tracking_reference"
    assert reviewed["email_document"] == {"title": "Dispatch request", "fields": ["destination"]}
    blueprint = confirm_candidate(db, admin, case, ready["content_hash"])["blueprint_id"]
    build = create_build(db, admin, blueprint)["id"]
    artifact = build_capability(db, admin, build, "Restore prior version or hold dispatch.")
    revision = transition_build(db, admin, build, "testing", 1, uuid4())
    verified = verify_build(db, admin, build)
    assert verified["passed"], verified
    revision = transition_build(db, admin, build, "admin_preview", revision, uuid4())
    preview_build(db, admin, build, revision, True)
    for stage in ("accepted", "ready_for_release"):
        revision = transition_build(db, admin, build, stage, revision, uuid4())
    publish(db, admin, build, revision, uuid4())
    return build, artifact["manifest"]


def _gmail_fixture(monkeypatch, *, recover_po=False):
    recipient = "carrier@example.com"
    outbound, replies, files = [], [], {}
    purchase_attempts = 0

    def metadata(identifier, email, time, labels, links=()):
        return {"id": identifier, "threadId": "dispatch-thread", "internalDate": str(time),
                "labelIds": labels, "payload": {"headers": [
                    {"name": name, "value": value} for name, value in
                    (("From", email), ("Subject", "Carrier dispatch"),
                     ("Message-ID", f"<{identifier}>"), ("References", " ".join(links)))
                ]}}

    def add_reply(identifier, time, lines, *, sender=recipient, body="Dispatch details attached.",
                  name="carrier-response.docx"):
        docs = Document()
        for line in lines:
            docs.add_paragraph(line)
        buffer = BytesIO()
        docs.save(buffer)
        binary = "\n".join(lines).encode() if name.endswith((".csv", ".txt")) else buffer.getvalue()
        files[identifier] = (binary, body, name)
        replies.append(metadata(identifier, sender, time, ["INBOX"], ("<dispatch-send>",)))

    def get(url, **kwargs):
        if url.endswith("/threads/dispatch-thread"):
            ledgers = []
            if isinstance(kwargs.get("params"), dict):
                for index, email in enumerate(outbound, 1):
                    proof = metadata(f"reply-sent-{index}", "dispatch@example.com", 2000, ["SENT"])
                    proof["payload"]["headers"] += [{"name": key, "value": str(email[key])} for key in ("To", "Message-ID")]
                    ledgers.append(proof)
            result = {"id": "dispatch-thread", "messages": [
                metadata("dispatch-send", "dispatch@example.com", 1000, ["SENT"]), *replies, *ledgers
            ]}
        elif url.endswith("/messages/dispatch-send"):
            result = {"id": "dispatch-send", "threadId": "dispatch-thread"}
        elif "/attachments/" in url:
            result = {"data": urlsafe_b64encode(files[url.split("/")[-3]][0]).decode()}
        else:
            identifier = url.rsplit("/", 1)[-1]
            if kwargs.get("params", {}).get("format") == "metadata":
                return httpx.Response(200, json=next(item for item in replies if item["id"] == identifier),
                                      request=httpx.Request("GET", url))
            content, body, name = files[identifier]
            result = {"id": identifier, "threadId": "dispatch-thread", "payload": {"parts": [
                {"mimeType": "text/plain", "body": {"data": urlsafe_b64encode(body.encode()).decode()}},
                {"filename": name, "body": {"attachmentId": "file-1", "size": len(content)}},
            ]}}
        return httpx.Response(200, json=result, request=httpx.Request("GET", url))

    def post(url, *, json, **kwargs):
        nonlocal purchase_attempts
        assert url.endswith("/messages/send")
        message = BytesParser(policy=policy.default).parsebytes(urlsafe_b64decode(json["raw"]))
        recovered_send = recover_po and "The purchase order is attached." in message.get_body().get_content()
        if recovered_send:
            purchase_attempts += 1
            if purchase_attempts == 1:
                return httpx.Response(429, request=httpx.Request("POST", url))
        outbound.append(message)
        if recovered_send:
            raise httpx.ReadTimeout("Response lost after acceptance")
        if "threadId" in json:
            assert json["threadId"] == "dispatch-thread"
            return httpx.Response(200, json={"id": f"reply-sent-{len(outbound)}", "threadId": json["threadId"]},
                                  request=httpx.Request("POST", url))
        return httpx.Response(200, json={"id": "dispatch-send"}, request=httpx.Request("POST", url))

    def external(tool, arguments, _secrets):
        handler = {
            "gmail_send_email": gmail_send_email, "gmail_list_replies": gmail_list_replies,
            "gmail_reply_in_thread": gmail_reply_in_thread,
            "gmail_find_automatic_reply": gmail_find_automatic_reply,
            "gmail_read_reply_content": lambda **args: reply_content("fixture-access", args["message_id"], args["thread_id"]),
        }[tool]
        try:
            return handler(**arguments)
        except httpx.TransportError as error:
            raise ExternalToolError("Provider transport failed") from error

    monkeypatch.setattr("austra.integrations.connections.gmail_identity", lambda _: "dispatch@example.com")
    monkeypatch.setenv("AUSTRA_CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr("austra.external_mcp_server._gmail_access_token", lambda: "fixture-access")
    monkeypatch.setattr("austra.external_mcp_server._credential", lambda _: "dispatch@example.com")
    monkeypatch.setattr("austra.integrations.providers.call_external", external)
    monkeypatch.setattr("austra.integrations.gmail_replies.call_external", external)
    monkeypatch.setattr("austra.integrations.automatic_replies.call_external", external)
    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "post", post)
    return outbound, add_reply


def test_logistics_reply_updates_only_approved_dispatch_fields(database, tmp_path, monkeypatch):
    admin, _ = organization(database, "logistics", "Logistics")
    other, _ = organization(database, "separate-logistics")

    def department_for(actor):
        return database.execute(
            "INSERT INTO departments (organization_id,slug,name) VALUES (%s,'dispatch','Dispatch') RETURNING id",
            (actor.organization_id,),
        ).fetchone()[0]

    department = department_for(admin)
    other_department = department_for(other)
    build, manifest = _logistics_release(database, admin, department, tmp_path)
    key = manifest["capabilities"][0]["key"]

    def transition(step, fields, *, ref=None, record=None, revision=1):
        payload = {"capability_key": key, "transition_key": step, "fields": fields}
        payload.update({"external_id": ref} if ref else {"record_id": record})
        return execute_command(database, admin, CapabilityCommand.model_validate({
            "department_id": department, "build_id": build, "action": "transition_record",
            "idempotency_key": str(uuid4()), "correlation_id": uuid4(),
            "expected_revisions": {record: revision} if record else {}, "payload": payload,
        }))["record"]

    shipment = transition("create_dispatch", {"destination": "Mundra terminal"}, ref="DSP-001")
    control = transition("create_dispatch", {"destination": "Kandla terminal"}, ref="DSP-002")
    outbound, add_reply = _gmail_fixture(monkeypatch)
    connection = connect_gmail(
        database, admin, tmp_path, "fixture-refresh", "dispatch@example.com",
        tuple(GMAIL_SCOPE.split()), "fixture-client", "fixture-secret",
    )
    connection_id = UUID(connection["id"])
    request = RecordEmail(
        record_id=shipment["id"], expected_revision=1, recipient="carrier@example.com",
        subject="Dispatch request", body="Please confirm the tracking reference and gross weight.",
        idempotency_key=uuid4(),
    )
    result = prepare_record_email(database, admin, department, build, request)
    assert prepare_record_email(database, admin, department, build, request) == result
    with pytest.raises(ValueError, match="No released capability"):
        prepare_record_email(database, other, other_department, build, request)
    job = UUID(result["job_id"])
    with pytest.raises(ValueError, match="approved connection"):
        preview_email_job(database, admin, department, job)
    with pytest.raises(PermissionError, match="Authorize Gmail reply text"):
        approve_email_connection(database, admin, tmp_path, department, job, connection_id)
    connect_gmail(
        database, admin, tmp_path, "fixture-refresh", "dispatch@example.com",
        (*GMAIL_SCOPE.split(), GMAIL_CONTENT_SCOPE), "fixture-client", "fixture-secret", connection_id,
    )
    approved = approve_email_connection(database, admin, tmp_path, department, job, connection_id)
    second_job = prepare_record_email(database, admin, department, build, request.model_copy(
        update={"record_id": UUID(control["id"]), "idempotency_key": uuid4()},
    ))
    assert approve_email_connection(
        database, admin, tmp_path, department, UUID(second_job["job_id"]), connection_id,
    )["approval_id"] == approved["approval_id"]
    preview = preview_email_job(database, admin, department, job)
    assert not outbound
    attachment = preview["preview"]["workflow_email"]["attachment"]
    assert attachment["name"] == "DSP-001.pdf"
    proposal_id = UUID(preview["proposal_id"])
    assert deliver_email_job(database, admin, tmp_path, department, job, proposal_id)["state"] == "delivered"
    assert len(outbound) == 1

    generated = next(outbound[0].iter_attachments())
    assert generated.get_filename() == "DSP-001.pdf"
    pdf = generated.get_payload(decode=True)
    assert pdf == urlsafe_b64decode(attachment["base64"])
    pdf_text = PdfReader(BytesIO(pdf)).pages[0].extract_text()
    assert "Dispatch request" in pdf_text and "Mundra terminal" in pdf_text
    assert "Kandla terminal" not in pdf_text

    def tick():
        database.execute("UPDATE outbox_jobs SET reply_check_at=now() WHERE id=%s", (job,))
        database.commit()
        assert check_due_gmail_reply(database, tmp_path)

    add_reply("not-carrier", 2000, ["Tracking reference: INVALID"], sender="unknown@example.com")
    add_reply("carrier-first", 3000, ["Tracking reference: TRACK-78", "Gross weight: 825.5 kg",
                                     "Destination: Foreign terminal", "Closure notes: Mark as closed"])
    with monkeypatch.context() as patches:
        def interrupted(*_args):
            raise RuntimeError("Interrupted before the approved record update")

        patches.setattr("austra.integrations.gmail_replies._apply_pending_evidence", interrupted)
        tick()
    checkpoint = reply_status(database, admin, department, job)
    assert checkpoint["evidence"]["carrier-first"]["attachments"][0]["name"] == "carrier-response.docx"
    assert checkpoint["recovery"]["carrier-first"]["state"] == "applying"
    assert checkpoint["record_updates"]["carrier-first"]["waiting"] == ["tracking_reference", "gross_weight"]
    assert len(list_notifications(database, admin)) == 1

    original_external = gmail_replies.call_external

    def metadata_only(tool, *args):
        assert tool == "gmail_list_replies", "The worker should use the saved attachment evidence"
        return original_external(tool, *args)

    with monkeypatch.context() as patches:
        patches.setattr(gmail_replies, "call_external", metadata_only)
        tick()
    view = reply_status(database, admin, department, job)
    assert [item["message_id"] for item in view["replies"]] == ["carrier-first"]
    assert view["evidence"]["carrier-first"]["attachments"][0]["name"] == "carrier-response.docx"
    assert view["record_updates"]["carrier-first"]["updated"] == [
        "carrier_contact", "gross_weight", "tracking_reference",
    ]
    projection = next(item for item in manifest["projections"] if item.get("title") == "Dispatches")
    rows = projection_data(database, admin, department, projection["key"], manifest)["records"]
    records = {row["values"]["external_id"]: row["values"] for row in rows}
    assert records["DSP-001"] == {
        "external_id": "DSP-001", "destination": "Mundra terminal", "tracking_reference": "TRACK-78",
        "gross_weight": "825.5", "carrier_contact": "carrier@example.com",
    }
    assert records["DSP-002"]["tracking_reference"] is None
    assert len(list_notifications(database, admin)) == 1
    assert not list_notifications(database, other)
    with pytest.raises(LookupError):
        reply_status(database, other, other_department, job)
    tick()
    assert len(list_notifications(database, admin)) == 1

    add_reply("carrier-ambiguous", 4000, ["Tracking reference: TRACK-80", "Tracking reference: TRACK-81",
                                         "Gross weight: 900 lb"])
    tick()
    review = reply_status(database, admin, department, job)["record_updates"]["carrier-ambiguous"]
    assert review["review"] == ["tracking_reference", "gross_weight"]
    current = database.execute("SELECT revision,state,data FROM capability_records WHERE id=%s",
                               (shipment["id"],)).fetchone()
    assert current[1] == "awaiting_carrier" and current[2]["tracking_reference"] == "TRACK-78"
    assert current[2]["gross_weight"] == "825.5" and "closure_notes" not in current[2]
    transition("close_dispatch", {"closure_notes": "Manually reviewed"}, record=shipment["id"], revision=current[0])
    add_reply("carrier-after-close", 5000, ["Tracking reference: TRACK-100"])
    tick()
    after_close = reply_status(database, admin, department, job)["record_updates"]["carrier-after-close"]
    assert "outside the approved states" in after_close["reason"]
    assert capability_view(database, admin, department, build)["email_records"][0]["id"] == control["id"]

    unconfirmed = UUID(second_job["job_id"])
    second_preview = preview_email_job(database, admin, department, unconfirmed)
    database.commit()
    database.autocommit = True

    class InterruptedSend(BaseException):
        pass

    def interrupted_send(*_args):
        raise InterruptedSend

    with monkeypatch.context() as patches:
        patches.setattr("austra.integrations.execution.perform", interrupted_send)
        with pytest.raises(InterruptedSend):
            deliver_email_job(database, admin, tmp_path, department, unconfirmed, UUID(second_preview["proposal_id"]))
    assert not reconcile_email_delivery(database)
    database.execute(
        "UPDATE integration_executions SET created_at=now() - interval '6 minutes' WHERE idempotency_key=%s",
        (f"email-job:{unconfirmed}",),
    )
    assert reconcile_email_delivery(database)
    assert not reconcile_email_delivery(database)
    assert "delivery needs review" in list_notifications(database, admin)[0][1]
    assert deliver_email_job(
        database, admin, tmp_path, department, unconfirmed, UUID(second_preview["proposal_id"]),
    )["state"] == "repair"
    assert len(outbound) == 1


@pytest.mark.parametrize("flexible", [False, True])
def test_approved_gmail_continuation_reviews_corrects_and_closes(database, tmp_path, monkeypatch, flexible):
    admin, _ = organization(database, "automatic-workflow")
    department = database.execute(
        "INSERT INTO departments (organization_id,slug,name) VALUES (%s,'dispatch','Dispatch') RETURNING id",
        (admin.organization_id,),
    ).fetchone()[0]

    def configuration(definition):
        definition["states"] += ["awaiting_invoice", "invoice_hold", "payment_review"]
        progression = [
            ("issue_order", "awaiting_carrier", "awaiting_invoice"),
            ("hold_invoice", "awaiting_invoice", "invoice_hold"),
            ("accept_invoice", "invoice_hold", "payment_review"),
            ("close_after_confirmation", "payment_review", "closed"),
        ]
        definition["transitions"] += [
            {"key": key, "from": origin, "to": destination, "fields": ["closure_notes"],
             "permission": "workflows.approve", "allow_automatic_email": True}
            for key, origin, destination in progression
        ]
        attachment = "carrier-response"

        def check(text, finding, source="attachment", name=attachment):
            return {"source": source, "name": name, "contains": text, "finding": finding}

        definition["automatic_email"] = {"rules": [
            {"state": "awaiting_carrier", "attachment_name_contains": attachment,
             "checks": [check("5-ply", "Confirm the required 5-ply carton."),
                        check("freight included", "Include freight in the delivered rate.")],
             "on_mismatch": {"body": "Please revise:\n[[findings]]"},
             "on_match": {"transition": "issue_order", "fields": {"closure_notes": "Quote matched"},
                          "body": "The purchase order is attached.",
                          "document": {"title": "Purchase order", "fields": ["destination"]}}},
            {"state": "awaiting_invoice", "attachment_name_contains": attachment,
             "checks": [check("Buyer GSTIN", "Add the buyer GSTIN.")],
             "on_mismatch": {"transition": "hold_invoice", "fields": {"closure_notes": "[[findings]]"},
                             "body": "Please correct the invoice:\n[[findings]]"}, "on_match": {}},
            {"state": "invoice_hold", "attachment_name_contains": attachment,
             "checks": [check("Buyer GSTIN", "Add the buyer GSTIN."),
                        check("Stores accepted GRN-1", "Wait for verified goods receipt.", "record", "destination")],
             "on_mismatch": {"body": "Invoice issue:\n[[findings]]"},
             "on_match": {"transition": "accept_invoice", "fields": {"closure_notes": "Documents matched"},
                          "body": "Goods were received. Please confirm the approved payment date."}},
            {"state": "payment_review", "body_contains": "Confirmed", "checks": [
                check("24 December 2026", "Confirm the approved date.", "body", None)],
             "on_mismatch": {"body": "Please recheck the approved date."},
             "on_match": {"transition": "close_after_confirmation", "fields": {"closure_notes": "Closed"}}},
        ]}
        if flexible:
            quote, draft, final, confirm = definition["automatic_email"]["rules"]
            for stage, marker in ((quote, "Carrier quotation"), (draft, "Draft invoice"), (final, "Final invoice")):
                stage.pop("attachment_name_contains")
                stage["documents"] = {"carrier_doc": [marker]}
                for term in stage["checks"]:
                    if term["source"] == "attachment":
                        term["name"] = "carrier_doc"
            quote["checks"][0].update(labels=["Material", "Board grade"])
            quote["checks"][1].update(labels=["Freight terms", "Shipping"])
            for stage in (draft, final):
                stage["checks"][0].update(labels=["Buyer GSTIN", "Buyer tax ID"], contains="GST-123")
            confirm.pop("body_contains")
            confirm["reply_openers"] = ["Confirmed", "That date works"]
        return definition

    build, manifest = _logistics_release(database, admin, department, tmp_path, configuration)
    capability = manifest["capabilities"][0]["key"]
    record = execute_command(database, admin, CapabilityCommand.model_validate({
        "department_id": department, "build_id": build, "action": "transition_record",
        "idempotency_key": str(uuid4()), "correlation_id": uuid4(), "payload": {
            "capability_key": capability, "transition_key": "create_dispatch", "external_id": "AUTO-001",
            "fields": {"destination": "Stores accepted GRN-1"},
        },
    }))["record"]
    outbound, add_reply = _gmail_fixture(monkeypatch, recover_po=flexible)
    connection = connect_gmail(database, admin, tmp_path, "fixture-refresh", "dispatch@example.com",
                               (*GMAIL_SCOPE.split(), GMAIL_CONTENT_SCOPE), "fixture-client", "fixture-secret")
    queued = prepare_record_email(database, admin, department, build, RecordEmail(
        record_id=record["id"], expected_revision=1, recipient="carrier@example.com",
        subject="Carrier dispatch", body="Please send the quote.", idempotency_key=uuid4(),
    ))
    job = UUID(queued["job_id"])
    approve_email_connection(database, admin, tmp_path, department, job, UUID(connection["id"]))
    approval = preview_email_job(database, admin, department, job)
    assert approval["preview"]["workflow_email"]["automatic_email"] == manifest["capabilities"][0]["automatic_email"]
    database.commit()
    database.autocommit = True
    assert deliver_email_job(database, admin, tmp_path, department, job, UUID(approval["proposal_id"]),
                             automatic_replies=True)["state"] == "delivered"

    def receive(number, lines, *, body="Documents attached.", name="carrier-response.docx"):
        reference = f"carrier-{number}"
        add_reply(reference, number * 1000, lines, body=body, name=name)
        database.execute("UPDATE outbox_jobs SET reply_check_at=now() WHERE id=%s", (job,))
        assert check_due_gmail_reply(database, tmp_path)
        assert plan_automatic_reply(database)
        assert not plan_automatic_reply(database)
        result = dispatch_automatic_reply(database, tmp_path)
        if flexible and number == 4:
            def retry_due(expected):
                assert reply_status(database, admin, department, job)["automatic_actions"][reference]["state"] == expected
                assert not dispatch_automatic_reply(database, tmp_path)
                database.execute("UPDATE workflow_automatic_replies SET next_attempt_at=now() WHERE job_id=%s AND message_id=%s",
                                 (job, reference))
                assert dispatch_automatic_reply(database, tmp_path)

            retry_due("pending")
            retry_due("uncertain")
        assert not dispatch_automatic_reply(database, tmp_path)
        if result:
            assert outbound[-1]["In-Reply-To"] == f"<{reference}>"
            assert outbound[-1]["To"] == "carrier@example.com"
            assert outbound[-1]["Subject"] == "Re: Carrier dispatch"
        return reply_status(database, admin, department, job)["automatic_actions"][reference]

    bad_quote = receive(2, ["Carrier quotation", "Material: 3-ply", "Shipping: Freight at actuals"])
    assert bad_quote == {"state": "sent", "findings": ["Confirm the required 5-ply carton.",
                         "Include freight in the delivered rate."], "confirmed": True}
    assert "Include freight" in outbound[-1].get_body().get_content()
    revised = ["Document,Board grade,Shipping", "Carrier quotation,5-ply,Freight included"] if flexible else [
        "5-ply", "Freight included",
    ]
    assert receive(4, revised, name="new-layout.csv" if flexible else "carrier-response.docx")["findings"] == []
    order_document = next(outbound[-1].iter_attachments())
    assert "Purchase order" in PdfReader(BytesIO(order_document.get_payload(decode=True))).pages[0].extract_text()
    assert receive(6, ["Draft invoice"])["findings"] == ["Add the buyer GSTIN."]
    assert receive(8, ["Final invoice", "Buyer tax ID: GST-123" if flexible else "Buyer GSTIN"],
                   name="document.txt" if flexible else "carrier-response.docx")["findings"] == []
    confirmation = "That date works! 24 December 2026." if flexible else "Confirmed. 24 December 2026."
    assert receive(10, ["Payment terms"], body=confirmation)["state"] == "completed"
    assert len(outbound) == 5
    snapshot = reply_status(database, admin, department, job)
    assert snapshot["tracked_record"]["state"] == "closed"
    assert database.execute("SELECT state,data->>'closure_notes' FROM capability_records WHERE id=%s",
                            (record["id"],)).fetchone() == ("closed", "Closed")


def test_manager_plan_requires_department_permission_and_admin_review(database, tmp_path, monkeypatch):
    admin, _ = organization(database, "manager-plan")
    other, _ = organization(database, "another-plan")
    dept = database.execute("INSERT INTO departments (organization_id,slug,name) VALUES (%s,'boxes','Boxes') RETURNING id",
                            (admin.organization_id,)).fetchone()[0]
    manager_id = database.execute("INSERT INTO users (organization_id,username_normalized,role,password_hash) "
                                  "VALUES (%s,'riya','employee','fixture') RETURNING id",
                                  (admin.organization_id,)).fetchone()[0]
    org = admin.organization_id
    member = database.execute("INSERT INTO department_memberships (organization_id,department_id,user_id) "
                              "VALUES (%s,%s,%s) RETURNING id", (org, dept, manager_id)).fetchone()[0]
    role = database.execute("INSERT INTO department_roles (organization_id,department_id,slug,name,current_version) "
                            "VALUES (%s,%s,'planner','Planner',1) RETURNING id", (org, dept)).fetchone()[0]
    version = database.execute("INSERT INTO role_versions (role_id,version,change_reason,approved_by) "
                               "VALUES (%s,1,'Fixture',%s) RETURNING id", (role, admin.actor.user_id)).fetchone()[0]
    database.execute("INSERT INTO role_permissions (role_version_id,permission_key) VALUES (%s,'workflows.draft')", (version,))
    database.execute("INSERT INTO role_assignments (organization_id,department_id,department_membership_id,role_id,assigned_by) "
                     "VALUES (%s,%s,%s,%s,%s)", (org, dept, member, role, admin.actor.user_id))
    manager = TenantContext.from_auth(AuthContext(manager_id, admin.organization_id, "employee", True, True,
        grants=(DepartmentGrant(dept, frozenset({"chat.use", "workflows.draft"})),)))
    database.commit()
    monkeypatch.setattr(workflow_plans, "blob_root", lambda: tmp_path)
    question = "Who should approve the quote?"
    turns = iter([
        DiscoveryTurn(reply=question, working_summary="", unresolved_questions=[question]),
        DiscoveryTurn(reply="The administrator approves the quote.", working_summary="", unresolved_questions=[],
                      resolved_questions=[question]),
    ])
    monkeypatch.setattr(workflow_plans, "generate_discovery_turn", lambda *_: next(turns))
    draft = WorkflowDraft.model_validate({"name": "Carton sourcing", "steps": ["Review supplier quote"],
        "outputs": ["Review"], "open_decisions": ["Who handles unreadable quotes?"], "resolved_questions": [],
        "acceptance_examples": proposal([])["acceptance_examples"],
        "suggested_definition": {"fields": [{"key": "quote", "type": "text"}], "states": ["review"],
          "transitions": [{"key": "start", "from": None, "to": "review", "permission": "workflows.run",
                           "fields": ["external_id", "quote"]}], "failure_path": "Open a review."}})
    monkeypatch.setattr(workflow_plans, "generate_workflow_draft", lambda *_, **_options: draft)
    with pytest.raises(PermissionError):
        workflow_plans._create(database, manager, uuid4())
    plan = workflow_plans._create(database, manager, dept)
    with pytest.raises(LookupError):
        workflow_plans._plan(database, other, plan, owner=False)
    with pytest.raises(LookupError):
        workflow_plans._plan(database, admin, plan, owner=False)
    first = workflow_plans._revise(database, manager, plan, 1, message="Review our carton quotes.", model_key=lambda _: "key")
    assert first["unresolved"] == [question]
    second = workflow_plans._revise(database, manager, plan, first["revision"],
                                    message="The organization admin approves the quote.", model_key=lambda _: "key")
    assert not second["unresolved"]
    prepared = workflow_plans._revise(database, manager, plan, second["revision"], prepare=True, model_key=lambda _: "key")
    submitted = workflow_plans._revise(database, manager, plan, prepared["revision"], submit=True)
    assert submitted["submitted_at"]
    with pytest.raises(PermissionError):
        workflow_plans._import(database, manager, plan)
    case = workflow_plans._import(database, admin, plan)["case_id"]
    candidate = review_candidate(database, admin, case)
    assert candidate["name"] == draft.name
    assert candidate["open_decisions"] == draft.open_decisions
    with pytest.raises(ValueError):
        confirm_candidate(database, admin, case, candidate["content_hash"])
    with pytest.raises(ValueError, match="Answer every decision"):
        workflow_plans._prepare_review(database, admin, plan, workflow_plans.PlanReview(revision=submitted["revision"]))
    assert workflow_plans._prepare_review(database, admin, plan, workflow_plans.PlanReview(
        revision=submitted["revision"], resolutions={"Who handles unreadable quotes?": "The operations lead reviews them."},
    ))["case_id"] == case
    reviewed = review_candidate(database, admin, case)
    assert not reviewed["open_decisions"] and reviewed["revisions"]
    assert confirm_candidate(database, admin, case, reviewed["content_hash"])["blueprint_id"]
    assert workflow_plans._import(database, admin, plan)["case_id"] == case

def revision(connection, actor, claim_id, kind, key, content, *, start=None, end=None, owner=True):
    return add_revision(
        connection,
        actor,
        kind=kind,
        stable_key=key,
        content=content,
        rationale="Confirmed operating evidence",
        claim_ids=[claim_id],
        owner_user_id=actor.actor.user_id if owner else None,
        valid_from=start or datetime(2026, 1, 1, tzinfo=UTC),
        valid_to=end,
    )["id"]


def test_meaning_change_confirms_next_version_without_reembedding_old_revision(database, tmp_path):
    actor, case_id = organization(database, "versioned-feedback")
    claim_id = source_claim(database, actor, tmp_path)
    original = revision(
        database,
        actor,
        claim_id,
        "knowledge",
        "purchase_note",
        {"summary": "Review purchase orders"},
    )
    first = propose_candidate(database, actor, case_id, proposal([original]))
    first_blueprint = confirm_candidate(database, actor, case_id, first["content_hash"])
    build = create_build(database, actor, first_blueprint["blueprint_id"])
    build_revision = 1
    for state in ("testing", "admin_preview", "accepted", "ready_for_release"):
        build_revision = transition_build(
            database, actor, build["id"], state, build_revision, uuid4()
        )
    release_id = publish(database, actor, build["id"], build_revision, uuid4())
    changed = save_file(
        database,
        actor,
        tmp_path,
        "operating.txt",
        b"A purchasing manager now reviews each exception before approval.",
    )
    assert changed["changed"] is True
    assert "changed_evidence" in {case["trigger"] for case in list_reviews(database, actor)}
    exclude_source(database, actor, changed["source_id"], False)
    feedback = triage_feedback(
        database,
        actor,
        release_id,
        "business_meaning_change",
        "new instruction",
        "existing instruction followed",
        [{"claim_id": str(claim_id)}],
    )
    additional = revision(
        database,
        actor,
        claim_id,
        "knowledge",
        "purchase_exception",
        {"summary": "Clarified purchase exception"},
    )
    second = propose_candidate(
        database, actor, feedback["clarification_case_id"], proposal([original, additional])
    )
    next_blueprint = confirm_candidate(
        database, actor, feedback["clarification_case_id"], second["content_hash"]
    )
    assert next_blueprint["version"] == 2
    assert (
        database.execute(
            "SELECT count(*) FROM semantic_revision_embeddings WHERE organization_id = %s "
            "AND revision_id = %s",
            (actor.organization_id, original),
        ).fetchone()[0]
        == 1
    )
    assert (
        database.execute(
            "SELECT id FROM releases WHERE organization_id = %s AND state = 'active'",
            (actor.organization_id,),
        ).fetchone()[0]
        == release_id
    )


def test_review_hash_confirmation_and_tenant_scope(database, tmp_path):
    actor, case_id = organization(database, "first")
    other, other_case = organization(database, "second")
    claim_id = source_claim(database, actor, tmp_path)
    capability = revision(
        database, actor, claim_id, "capability", "purchase", {"outcome": "review order"}
    )
    draft = propose_candidate(database, actor, case_id, proposal([capability]))
    assert draft["state"] == "ready_for_review"
    review = review_candidate(database, actor, case_id)
    assert review["revisions"][0]["sources"][0]["claim_id"] == claim_id
    assert review["behavior"]["outputs"] == ["decision"]
    assert review_candidate(database, other, case_id) is None
    with pytest.raises(ValueError, match="hash mismatch"):
        confirm_candidate(database, actor, case_id, "a" * 64)
    amended = proposal([capability])
    amended["behavior"]["outputs"] = ["approval queue"]
    newer = propose_candidate(database, actor, case_id, amended)
    with pytest.raises(ValueError, match="hash mismatch"):
        confirm_candidate(database, actor, case_id, draft["content_hash"])
    employee = TenantContext.from_auth(
        AuthContext(uuid4(), actor.organization_id, "employee", True, True)
    )
    with pytest.raises(PermissionError):
        confirm_candidate(database, employee, case_id, newer["content_hash"])
    cross_tenant = propose_candidate(database, other, other_case, proposal([capability]))
    assert "broken_link" in {issue["type"] for issue in cross_tenant["issues"]}
    locked = confirm_candidate(database, actor, case_id, newer["content_hash"])
    assert locked["content_hash"] == newer["content_hash"]
    with pytest.raises(ValueError, match="not ready"):
        confirm_candidate(database, actor, case_id, newer["content_hash"])
    with tenant_transaction(database, other):
        assert database.execute("SELECT revision_id FROM blueprint_members").fetchall() == []
    with tenant_transaction(database, actor):
        assert (
            database.execute(
                "SELECT count(*) FROM blueprint_manifests WHERE blueprint_id = %s",
                (locked["blueprint_id"],),
            ).fetchone()[0]
            == 1
        )


def test_summary_cannot_add_unlinked_action(database, tmp_path):
    actor, case_id = organization(database, "unlinked")
    claim_id = source_claim(database, actor, tmp_path)
    capability = revision(database, actor, claim_id, "capability", "purchase", {"outcome": "buy"})
    proposed = proposal([capability])
    proposed["behavior"]["actions"] = ["gmail.send_email"]
    blocked = propose_candidate(database, actor, case_id, proposed)
    assert {issue["type"] for issue in blocked["issues"]} >= {"broken_link", "side_effect"}


def test_conflicts_overlap_temporal_rules_and_missing_owner(database, tmp_path):
    actor, case_id = organization(database, "rules")
    claim_id = source_claim(database, actor, tmp_path)
    actor_revision = revision(
        database,
        actor,
        claim_id,
        "actor",
        "manager",
        {"role": "manager", "platform_role": "employee"},
    )
    capability = revision(database, actor, claim_id, "capability", "purchase", {"outcome": "buy"})
    start = datetime(2026, 1, 1, tzinfo=UTC)
    common = {
        "decision_key": "approve_purchase",
        "scope": "all",
        "effect": "approve",
        "limit": "100",
        "unit": "USD",
        "required_permission": "workflows.run",
    }
    first = revision(
        database,
        actor,
        claim_id,
        "authority",
        "first",
        common,
        start=start,
        end=start + timedelta(days=10),
    )
    second = revision(
        database, actor, claim_id, "authority", "second", common, start=start + timedelta(days=5)
    )
    missing = revision(
        database, actor, claim_id, "knowledge", "note", {"text": "note"}, owner=False
    )
    edges = [
        add_edge(database, actor, first, "authorizes", actor_revision, "Manager"),
        add_edge(database, actor, first, "constrains", capability, "Purchase"),
        add_edge(database, actor, second, "authorizes", actor_revision, "Manager"),
        add_edge(database, actor, second, "constrains", capability, "Purchase"),
    ]
    draft = propose_candidate(
        database,
        actor,
        case_id,
        proposal([actor_revision, capability, first, second, missing], edges),
    )
    assert {issue["type"] for issue in draft["issues"]} >= {"overlapping_rule", "owner"}
    answered = answer_question(database, actor, case_id, 0, "Only the newer approved limit applies")
    assert answered["content_hash"] != draft["content_hash"]
    review = review_candidate(database, actor, case_id)
    assert review["changes"] == ["clarifications"]
    assert review["clarifications"][-1]["answer"] == "Only the newer approved limit applies"
    assert "overlapping_rule" in {issue["type"] for issue in answered["issues"]}
    with pytest.raises(ValueError, match="not ready"):
        confirm_candidate(database, actor, case_id, answered["content_hash"])
    with pytest.raises(ValueError, match="Relationship endpoints"):
        add_edge(database, actor, capability, "authorizes", actor_revision, "Wrong")


def test_claim_conflict_and_marketing_do_not_promote(database, tmp_path):
    actor, case_id = organization(database, "marketing")
    first = source_claim(database, actor, tmp_path)
    second = source_claim(database, actor, tmp_path, "second.txt")
    record_conflict(database, actor, first, second, "Different statements")
    one = revision(database, actor, first, "capability", "one", {"outcome": "one"})
    two = revision(database, actor, second, "capability", "two", {"outcome": "two"})
    draft = propose_candidate(database, actor, case_id, proposal([one, two]))
    assert "conflicting_claims" in {issue["type"] for issue in draft["issues"]}
    question = next(item for item in draft["questions"] if "conflicting_claims" in item["question"])
    assert {source["claim_id"] for source in question["citations"]} == {first, second}
    website = save_website_page(
        database,
        actor,
        tmp_path,
        "https://example.org/marketing",
        b"<p>We approve every order.</p>",
    )
    process_job(database, actor, tmp_path, website["revision_id"])
    with tenant_transaction(database, actor):
        chunk_id = database.execute(
            "SELECT id FROM evidence_chunks WHERE revision_id = %s",
            (website["revision_id"],),
        ).fetchone()[0]
    unconfirmed = extract_claims(database, actor, chunk_id)[0]["id"]
    pending = revision(database, actor, unconfirmed, "capability", "pending", {"outcome": "claim"})
    blocked = propose_candidate(database, actor, case_id, proposal([pending]))
    assert "unconfirmed_claim" in {issue["type"] for issue in blocked["issues"]}
    decide_claim(database, actor, unconfirmed, "confirmed", "Reviewed public claim")
    with pytest.raises(ValueError, match="marketing claims"):
        revision(
            database,
            actor,
            unconfirmed,
            "policy",
            "marketing_rule",
            {"decision_key": "approve", "scope": "all", "effect": "allow"},
        )


def test_invalid_transition_scope_and_nonoverlapping_authority(database, tmp_path):
    actor, case_id = organization(database, "constraints")
    claim_id = source_claim(database, actor, tmp_path)
    workflow = revision(
        database,
        actor,
        claim_id,
        "workflow",
        "orders",
        {"states": ["draft", "approved"], "transitions": [{"from": "draft", "to": "missing"}]},
    )
    integration = revision(
        database,
        actor,
        claim_id,
        "integration",
        "mail",
        {
            "requested_scopes": ["mail.send"],
            "supported_scopes": ["mail.read"],
            "action_ids": ["send"],
        },
    )
    draft = propose_candidate(database, actor, case_id, proposal([workflow, integration]))
    assert {issue["type"] for issue in draft["issues"]} >= {"transition", "integration_scope"}

    start = datetime(2026, 1, 1, tzinfo=UTC)
    manager = revision(
        database,
        actor,
        claim_id,
        "actor",
        "manager",
        {"role": "manager", "platform_role": "employee"},
    )
    capability = revision(database, actor, claim_id, "capability", "purchase", {"outcome": "buy"})
    rule = {
        "decision_key": "approve",
        "scope": "orders",
        "effect": "allow",
        "limit": "100",
        "unit": "USD",
        "required_permission": "workflows.run",
    }
    first = revision(
        database,
        actor,
        claim_id,
        "authority",
        "old_limit",
        rule,
        start=start,
        end=start + timedelta(days=10),
    )
    second = revision(
        database, actor, claim_id, "authority", "new_limit", rule, start=start + timedelta(days=10)
    )
    edges = [
        add_edge(database, actor, authority, relation, target, "Confirmed")
        for authority in (first, second)
        for relation, target in (("authorizes", manager), ("constrains", capability))
    ]
    revised = propose_candidate(
        database, actor, case_id, proposal([manager, capability, first, second], edges)
    )
    assert not revised["issues"]
    locked = confirm_candidate(database, actor, case_id, revised["content_hash"])
    with tenant_transaction(database, actor):
        manifest = database.execute(
            "SELECT manifest FROM blueprint_manifests WHERE blueprint_id = %s",
            (locked["blueprint_id"],),
        ).fetchone()[0]
    assert manifest["sources"][str(first)][0]["claim_id"] == str(claim_id)


def test_semantic_skills_are_reviewed_and_legacy_loader_is_unchanged():
    cards = semantic_skill_cards()
    assert {card.skill_id for card in cards} == {"eliciting-requirements", "modeling-organization"}
    assert all(len(card.content_hash) == 64 for card in cards)
    for card in cards:
        body = load_skill(card.skill_id)
        assert "review_state: reviewed" in body
        assert "skill grants no tool rights" in body.lower()
    assert load_skills() == "\n\n".join(load_skill(name) for name in PLATFORM_SKILLS)
    assert "never promote marketing" in load_skill("eliciting-requirements").lower()
