"""Phase 5 gates against disposable tenant schemas and fake providers."""

import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from cryptography.fernet import Fernet
from psycopg import sql
from psycopg.types.json import Jsonb

from austra.build_release.monitoring import list_reviews
from austra.connectors import GMAIL_CONTENT_SCOPE, GMAIL_SCOPE
from austra.integrations.catalog import TEMPLATES
from austra.integrations.connections import (
    _remote_catalog,
    check_health,
    connect_gmail,
    connect_key,
    list_connections,
    review_remote,
    revoke,
)
from austra.integrations.email_delivery_recovery import reconcile_email_delivery
from austra.integrations.email_jobs import deliver_email_job, preview_email_job
from austra.integrations.execution import execute_proposal
from austra.integrations.gateway import approve_workflow, preview_action, receive_webhook
from austra.integrations.gmail_replies import capture_email_reply, reply_status
from austra.integrations.gmail_reply_worker import check_due_gmail_reply
from austra.integrations.providers import safe_mcp_url
from austra.integrations.reply_recovery import MAX_ATTEMPTS, read_pending_replies
from austra.integrations.secrets import ConnectionSecrets
from austra.integrations.workflow_notifications import (
    dispatch_owner_notification,
    enqueue_due_follow_up,
    list_notifications,
)
from austra.mcp_gateway import ExternalContractError, ExternalToolError
from austra.storage.migrate import apply_migrations
from austra.tenant.context import TenantContext, tenant_transaction
from austra.tenant.permissions.evaluator import AuthContext


@pytest.fixture
def database(monkeypatch):
    url = os.getenv("AUSTRA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("AUSTRA_TEST_DATABASE_URL is required")
    monkeypatch.setenv("AUSTRA_CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    schema = f"connection_test_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            with psycopg.connect(url, options=f"-c search_path={schema}") as db:
                assert "016_discovery_state.sql" in apply_migrations(db)
                yield db
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def tenant(db, slug):
    organization = db.execute(
        "INSERT INTO organizations (slug, name) VALUES (%s, %s) RETURNING id", (slug, slug)
    ).fetchone()[0]
    actor = db.execute(
        "INSERT INTO users (organization_id, username_normalized, role, "
        "password_hash, must_change_password) VALUES "
        "(%s, 'admin', 'org_admin', 'fixture', false) RETURNING id",
        (organization,),
    ).fetchone()[0]
    department = db.execute(
        "INSERT INTO departments (organization_id, slug, name) "
        "VALUES (%s, 'operations', 'Operations') RETURNING id",
        (organization,),
    ).fetchone()[0]
    context = TenantContext.from_auth(AuthContext(actor, organization, "org_admin", True, True))
    return context, department


def release(db, context, action_name, scopes):
    blueprint = db.execute(
        "INSERT INTO blueprints (organization_id, blueprint_version, "
        "content_hash, status, confirmed_by) VALUES "
        "(%s, 1, %s, 'active', %s) RETURNING id",
        (context.organization_id, "a" * 64, context.actor.user_id),
    ).fetchone()[0]
    build = db.execute(
        "INSERT INTO builds (organization_id, blueprint_id, build_revision, "
        "state) VALUES (%s, %s, 1, 'released') RETURNING id",
        (context.organization_id, blueprint),
    ).fetchone()[0]
    gmail = action_name == "gmail.send_email"
    rule = {
        "id": str(uuid4()),
        "valid_from": "2026-01-01T00:00:00+00:00",
        "valid_to": None,
        "content": {
            "decision_key": "command.approve_email" if gmail else f"integration.{action_name}",
            "required_permission": "workflows.approve" if gmail else "integrations.use",
        },
    }
    manifest = {
        "blueprint_hash": "a" * 64,
        "decision_tables": [rule],
        "allowed_actions": [{"content": {"action_ids": [action_name], "requested_scopes": scopes}}],
    }
    db.execute(
        "INSERT INTO capability_build_artifacts (organization_id, build_id, "
        "blueprint_hash, manifest, fixtures, rollback_plan, preview_state) "
        "VALUES (%s, %s, %s, %s, '{}'::jsonb, 'fixture rollback', 'accepted')",
        (context.organization_id, build, "a" * 64, Jsonb(manifest)),
    )
    db.execute(
        "INSERT INTO releases (organization_id, blueprint_id, build_id, "
        "approved_by) VALUES (%s, %s, %s, %s)",
        (context.organization_id, blueprint, build, context.actor.user_id),
    )
    return build


def github_connection(db, context, root, monkeypatch):
    monkeypatch.setattr(
        "austra.integrations.connections.github_identity",
        lambda _token: ("fixture-account", ("repo",)),
    )
    monkeypatch.setattr(
        "austra.integrations.providers.github_identity",
        lambda _token: ("fixture-account", ("repo",)),
    )
    return connect_key(db, context, root, "github", {"token": "private-fixture-token"})


def test_schema_drift_opens_owned_review(database, tmp_path, monkeypatch):
    context, _department = tenant(database, "drift-fixture")
    original = [{"name": "find", "input_schema": {"type": "object", "properties": {}}}]
    changed = [
        {
            "name": "find",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
        }
    ]
    monkeypatch.setattr("austra.integrations.connections.discover", lambda *_args: original)
    connection = connect_key(
        database,
        context,
        tmp_path,
        "remote_mcp",
        {"token": "fixture-secret"},
        "https://example.com/mcp",
        "fixture",
    )
    monkeypatch.setattr("austra.integrations.connections.discover", lambda *_args: changed)
    assert (
        check_health(database, context, tmp_path, UUID(connection["id"]))["health"]
        == "schema_review"
    )
    cases = list_reviews(database, context)
    assert len(cases) == 1
    assert cases[0]["trigger"] == "schema_drift"
    assert cases[0]["owner_user_id"] == context.actor.user_id
    assert cases[0]["citations"][0]["connection_id"] == connection["id"]
    assert (
        check_health(database, context, tmp_path, UUID(connection["id"]))["health"]
        == "schema_review"
    )
    assert len(list_reviews(database, context)) == 1


def approved(db, context, root, department, connection, build, action_name, monkeypatch):
    monkeypatch.setattr("austra.integrations.gateway.perform", lambda *_args: {"items": []})
    fixture = (
        {"owner": "fixture", "repo": "repo", "title": "Issue"}
        if action_name == "github.create_issue"
        else {"per_page": 1}
    )
    result = approve_workflow(
        db,
        context,
        root,
        department,
        UUID(connection["id"]),
        build,
        action_name,
        fixture,
        {"event_id": "fixture-event"},
    )
    return UUID(result["approval_id"]), fixture


def test_catalog_and_vault_keep_private_fields_out_of_metadata(database, tmp_path, monkeypatch):
    context, department = tenant(database, "catalog-fixture")
    connection = github_connection(database, context, tmp_path, monkeypatch)
    listed = list_connections(database, context)
    assert listed[0]["account"] == "fixture-account"
    assert "private-fixture-token" not in str(listed)
    assert "private-fixture-token" not in str(TEMPLATES["github"].public())
    stored = (
        tmp_path / "integration_secrets" / str(context.organization_id) / connection["id"]
    ).read_bytes()
    assert b"private-fixture-token" not in stored
    assert (
        ConnectionSecrets(tmp_path).read(context.organization_id, UUID(connection["id"]))["token"]
        == "private-fixture-token"
    )
    assert department


def test_retry_uncertain_receipt_revocation_and_tenant_isolation(database, tmp_path, monkeypatch):
    context, department = tenant(database, "actions-fixture")
    other, other_department = tenant(database, "other-fixture")
    connection = github_connection(database, context, tmp_path, monkeypatch)
    assert list_connections(database, other) == []
    with pytest.raises(LookupError):
        check_health(database, other, tmp_path, UUID(connection["id"]))
    build = release(database, context, "github.create_issue", ["repo"])
    with pytest.raises(ValueError):
        approve_workflow(
            database,
            context,
            tmp_path,
            department,
            UUID(connection["id"]),
            build,
            "github.create_issue",
            {"owner": "fixture", "repo": "repo", "title": "Issue"},
            {"event_id": "fixture-event", "payload": {"title": "Other"}},
            {"title": "title"},
        )
    approval_id, arguments = approved(
        database,
        context,
        tmp_path,
        department,
        connection,
        build,
        "github.create_issue",
        monkeypatch,
    )
    preview = preview_action(
        database, context, department, approval_id, arguments, [], "Open the approved issue"
    )
    calls = []

    def send(*_args):
        calls.append(True)
        return {"id": 42}

    monkeypatch.setattr("austra.integrations.execution.perform", send)
    proposal_id = UUID(preview["proposal_id"])
    first = execute_proposal(database, context, tmp_path, department, proposal_id, "first")
    assert first == {"state": "succeeded", "receipt": "42"}
    assert (
        execute_proposal(database, context, tmp_path, department, proposal_id, "first")["receipt"]
        == "42"
    )
    assert len(calls) == 1
    second = preview_action(
        database, context, department, approval_id, arguments, [], "Open the approved issue"
    )
    monkeypatch.setattr("austra.integrations.execution.perform", lambda *_args: {})
    uncertain = execute_proposal(
        database, context, tmp_path, department, UUID(second["proposal_id"]), "second"
    )
    assert uncertain["state"] == "uncertain"
    assert (
        execute_proposal(
            database, context, tmp_path, department, UUID(second["proposal_id"]), "second"
        )["state"]
        == "uncertain"
    )
    monkeypatch.setitem(TEMPLATES, "github", replace(TEMPLATES["github"], rate_per_minute=2))
    third = preview_action(
        database, context, department, approval_id, arguments, [], "Open the approved issue"
    )
    with pytest.raises(ValueError, match="rate limit"):
        execute_proposal(
            database, context, tmp_path, department, UUID(third["proposal_id"]), "third"
        )
    monkeypatch.setitem(TEMPLATES, "github", replace(TEMPLATES["github"], rate_per_minute=60))
    timeout_preview = preview_action(
        database, context, department, approval_id, arguments, [], "Open the approved issue"
    )

    def timeout(*_args):
        raise TimeoutError("provider timeout")

    monkeypatch.setattr("austra.integrations.execution.perform", timeout)
    assert (
        execute_proposal(
            database, context, tmp_path, department, UUID(timeout_preview["proposal_id"]), "timeout"
        )["state"]
        == "uncertain"
    )
    assert receive_webhook(database, context, UUID(connection["id"]), "event-1", {"x": 1})
    assert not receive_webhook(database, context, UUID(connection["id"]), "event-1", {"x": 1})
    with pytest.raises(ValueError):
        receive_webhook(database, context, UUID(connection["id"]), "event-1", {"x": 2})
    revoke(database, context, tmp_path, UUID(connection["id"]))
    with pytest.raises(LookupError):
        preview_action(
            database, context, department, approval_id, arguments, [], "Open the approved issue"
        )
    assert list_connections(database, context) == []
    assert other_department


def test_expired_scope_and_schema_drift_stop_actions(database, tmp_path, monkeypatch):
    context, department = tenant(database, "repair-fixture")
    connection = github_connection(database, context, tmp_path, monkeypatch)
    build = release(database, context, "github.list_repositories", ["repo"])
    approval_id, arguments = approved(
        database,
        context,
        tmp_path,
        department,
        connection,
        build,
        "github.list_repositories",
        monkeypatch,
    )
    with tenant_transaction(database, context):
        database.execute(
            "UPDATE integration_connections SET granted_scopes = '{}' "
            "WHERE organization_id = %s AND id = %s",
            (context.organization_id, UUID(connection["id"])),
        )
    with pytest.raises(PermissionError):
        preview_action(database, context, department, approval_id, arguments, [])
    with tenant_transaction(database, context):
        database.execute(
            "UPDATE integration_connections SET granted_scopes = ARRAY['repo'] "
            "WHERE organization_id = %s AND id = %s",
            (context.organization_id, UUID(connection["id"])),
        )
    monkeypatch.setattr(
        "austra.integrations.connections.github_identity",
        lambda _token: (_ for _ in ()).throw(ExternalToolError("expired")),
    )
    assert check_health(database, context, tmp_path, UUID(connection["id"]))["health"] == "repair"
    with pytest.raises(ValueError):
        preview_action(database, context, department, approval_id, arguments, [])
    assert ConnectionSecrets(tmp_path).read(context.organization_id, UUID(connection["id"]))[
        "token"
    ]

    tools = [
        {
            "name": "create",
            "input_schema": {"type": "object"},
            "read_only": True,
            "description": "safe read",
        }
    ]
    assert _remote_catalog(tools)[0]["effect"] == "unreviewed"
    with pytest.raises(ValueError):
        safe_mcp_url("https://127.0.0.1/mcp")
    monkeypatch.setattr("austra.integrations.connections.discover", lambda *_: tools)
    remote = connect_key(
        database,
        context,
        tmp_path,
        "remote_mcp",
        {"token": "private"},
        "https://mcp.example/tools",
        "fixture workspace",
    )
    public = connect_key(
        database,
        context,
        tmp_path,
        "remote_mcp",
        {"token": ""},
        "https://mcp.example/tools",
        "public documentation",
    )
    assert public["health"] == "healthy"
    review_remote(database, context, UUID(remote["id"]), "remote_mcp.create", "write", [], "id")
    monkeypatch.setattr(
        "austra.integrations.connections.discover",
        lambda *_: [{"name": "create", "input_schema": {"type": "object", "required": ["x"]}}],
    )
    assert (
        check_health(database, context, tmp_path, UUID(remote["id"]))["health"] == "schema_review"
    )


def test_gmail_job_reconnect_preview_and_idempotent_delivery(database, tmp_path, monkeypatch):
    context, department = tenant(database, "mail-fixture")
    monkeypatch.setattr(
        "austra.integrations.connections.gmail_identity", lambda _credentials: "mailbox@example.com"
    )
    scopes = tuple(GMAIL_SCOPE.split())
    connected = connect_gmail(
        database,
        context,
        tmp_path,
        "refresh-token",
        "mailbox@example.com",
        scopes,
        "client",
        "secret",
    )
    build = release(database, context, "gmail.send_email", list(scopes))
    fixture = {"to": "buyer@example.com", "subject": "Purchase order", "body": "Approved"}
    approved_result = approve_workflow(
        database,
        context,
        tmp_path,
        department,
        UUID(connected["id"]),
        build,
        "gmail.send_email",
        fixture,
        {"event_id": "mail-trigger"},
    )
    assert approved_result["preview"]["arguments"] == fixture
    with tenant_transaction(database, context):
        po_id = database.execute(
            "INSERT INTO capability_records (organization_id, department_id, kind, external_id, state, data) "
            "VALUES (%s,%s,'purchase_order','PO-1','approved',%s) RETURNING id",
            (context.organization_id, department, Jsonb({"total": "0"})),
        ).fetchone()[0]
        email_id = database.execute(
            "INSERT INTO capability_records (organization_id, department_id, kind, "
            "external_id, state, data) VALUES (%s, %s, 'email_proposal', 'MAIL-1', "
            "'queued', %s) RETURNING id",
            (context.organization_id, department, Jsonb({"po_id": str(po_id)})),
        ).fetchone()[0]
        event_id = database.execute(
            "INSERT INTO runtime_events (organization_id, department_id, event_type, "
            "idempotency_key, correlation_id, payload) VALUES (%s, %s, 'capability.command', %s, %s, %s) "
            "RETURNING id",
            (context.organization_id, department, "mail-fixture", uuid4(), Jsonb({"build_id": str(build)})),
        ).fetchone()[0]
        job_id = database.execute(
            "INSERT INTO outbox_jobs (organization_id, department_id, event_id, topic, "
            "payload, state) VALUES (%s, %s, %s, 'capability.email', %s, 'repair') "
            "RETURNING id",
            (
                context.organization_id,
                department,
                event_id,
                Jsonb(
                    {
                        "email_id": str(email_id),
                        "recipient": fixture["to"],
                        "subject": fixture["subject"],
                        "body": fixture["body"],
                    }
                ),
            ),
        ).fetchone()[0]
    with tenant_transaction(database, context):
        database.execute(
            "UPDATE integration_connections SET health = 'repair' WHERE "
            "organization_id = %s AND id = %s",
            (context.organization_id, UUID(connected["id"])),
        )
    reconnected = connect_gmail(
        database,
        context,
        tmp_path,
        "new-refresh-token",
        "mailbox@example.com",
        scopes,
        "client",
        "secret",
        UUID(connected["id"]),
    )
    assert reconnected["id"] == connected["id"]
    preview = preview_email_job(database, context, department, job_id)
    assert preview["preview"]["arguments"] == fixture
    assert preview["preview"]["source_records"][0]["id"] == str(email_id)
    sent = []

    def send(*_args):
        sent.append(True)
        return {"sent": True, "message_id": "provider-message-1"}

    monkeypatch.setattr("austra.integrations.execution.perform", send)
    proposal_id = UUID(preview["proposal_id"])
    database.commit()
    database.autocommit = True
    with pytest.raises(PermissionError, match="reviewed workflow email"):
        execute_proposal(database, context, tmp_path, department, proposal_id, f"email-job:{job_id}")
    assert not reconcile_email_delivery(database)

    def interrupted(*_args):
        raise RuntimeError("Interruption after the receipt was saved")

    with monkeypatch.context() as patches:
        patches.setattr("austra.integrations.email_jobs.finish_email_delivery", interrupted)
        with pytest.raises(RuntimeError, match="after the receipt"):
            deliver_email_job(database, context, tmp_path, department, job_id, proposal_id,
                              follow_up_days=2, owner_email_alerts=True)
    assert database.execute("SELECT state FROM outbox_jobs WHERE id=%s", (job_id,)).fetchone() == ("repair",)
    assert reconcile_email_delivery(database)
    assert not reconcile_email_delivery(database)
    first = deliver_email_job(database, context, tmp_path, department, job_id, proposal_id,
                              follow_up_days=2, owner_email_alerts=True)
    assert first == {"state": "delivered", "receipt": "provider-message-1"}
    assert deliver_email_job(database, context, tmp_path, department, job_id, proposal_id) == first
    assert len(sent) == 1
    reply = {"message_id": "reply-message-1", "thread_id": "thread-1",
             "from": fixture["to"], "subject": "Re: Purchase order", "received_ms": 1730000000000}
    follow_up = {**reply, "message_id": "reply-message-2", "received_ms": 1730000010000}
    replies = []

    def list_replies(tool, arguments, _credentials):
        assert tool == "gmail_list_replies"
        assert arguments == {"message_id": "provider-message-1", "recipient": fixture["to"]}
        return {"replies": list(replies)}

    monkeypatch.setattr("austra.integrations.gmail_replies.call_external", list_replies)
    assert capture_email_reply(database, context, tmp_path, department, job_id)["reply"] is None

    def force_due():
        database.execute("UPDATE outbox_jobs SET reply_check_at = now() WHERE id = %s", (job_id,))
        database.commit()

    replies.append(reply)
    force_due()
    assert check_due_gmail_reply(database, tmp_path)
    assert not check_due_gmail_reply(database, tmp_path)
    first = reply_status(database, context, department, job_id)
    assert (first["state"], first["tracking"], first["replies"]) == (
        "reply_received", "tracking", [reply],
    )
    other, other_department = tenant(database, "unrelated-tenant")
    with pytest.raises(LookupError):
        reply_status(database, other, other_department, job_id)
    assert not check_due_gmail_reply(database, tmp_path)

    def unavailable(*_arguments):
        raise ExternalToolError("Fixture Gmail outage")

    monkeypatch.setattr("austra.integrations.gmail_replies.call_external", unavailable)
    force_due()
    assert check_due_gmail_reply(database, tmp_path)
    paused = reply_status(database, context, department, job_id)
    assert (paused["tracking"], paused["replies"]) == ("retrying", [reply])

    monkeypatch.setattr("austra.integrations.gmail_replies.call_external", list_replies)
    replies.append(follow_up)
    force_due()
    assert check_due_gmail_reply(database, tmp_path)
    latest = capture_email_reply(database, context, tmp_path, department, job_id)
    assert latest["reply"] == reply
    assert latest["replies"] == [reply, follow_up]
    assert database.execute(
        "SELECT state, data->'gmail_replies' FROM capability_records WHERE id = %s", (email_id,)
    ).fetchone() == ("reply_received", [reply, follow_up])
    assert database.execute(
        "SELECT payload->>'trigger' FROM runtime_events WHERE event_type = 'capability.email_reply_captured' "
        "AND payload->>'job_id' = %s ORDER BY occurred_at", (str(job_id),)
    ).fetchall() == [("scheduled",), ("scheduled",)]
    assert list_notifications(database, other) == []
    assert len(list_notifications(database, context)) == 2
    assert latest.get("evidence") is None

    database.execute(
        "UPDATE integration_connections SET granted_scopes=array_append(granted_scopes,%s) WHERE id=%s",
        (GMAIL_CONTENT_SCOPE, UUID(connected["id"])),
    )

    reads = []
    gmail_listing_unavailable = False

    def include_documents(tool, arguments, credentials):
        if tool == "gmail_read_reply_content":
            assert arguments["thread_id"] == "thread-1"
            message = arguments["message_id"]
            content = {
                "body": "Quote reference: Q-100",
                "attachments": [{"name": "offer.docx" if message == reply["message_id"] else "quote.xlsx",
                                 "status": "extracted"}],
                "quoted_terms": {"quote_reference": "Q-100"},
            }
            first_read = message not in reads
            reads.append(message)
            if message == reply["message_id"] and first_read:
                raise ExternalContractError(tool, json.dumps(content))
            if first_read:
                raise ExternalToolError("Temporary read failure")
            return content
        if gmail_listing_unavailable:
            raise ExternalToolError("Temporary listing failure")
        return list_replies(tool, arguments, credentials)

    monkeypatch.setattr("austra.integrations.gmail_replies.call_external", include_documents)
    force_due()
    assert check_due_gmail_reply(database, tmp_path)
    snapshot = reply_status(database, context, department, job_id)
    assert snapshot["recovery"][reply["message_id"]]["reason"] == "legacy_mcp_envelope"
    assert snapshot["recovery"][follow_up["message_id"]]["state"] == "retrying"
    assert snapshot["replies"] == [reply, follow_up]
    assert follow_up["message_id"] not in snapshot["evidence"]
    assert snapshot["evidence"][reply["message_id"]]["attachments"][0]["name"] == "offer.docx"
    assert len(list_notifications(database, context)) == 2
    gmail_listing_unavailable = True
    database.execute(
        "UPDATE outbox_jobs SET reply_recovery=jsonb_set(reply_recovery, %s, to_jsonb(%s::text)) WHERE id=%s",
        ([follow_up["message_id"], "retry_at"], (datetime.now(UTC) - timedelta(minutes=1)).isoformat(), job_id),
    )
    force_due()
    assert check_due_gmail_reply(database, tmp_path)
    snapshot = reply_status(database, context, department, job_id)
    assert snapshot["recovery"][follow_up["message_id"]] == {
        "state": "recovered", "attempts": 2, "reason": "provider_unavailable",
    }
    assert snapshot["evidence"][follow_up["message_id"]]["attachments"][0]["name"] == "quote.xlsx"
    assert reads == [reply["message_id"], follow_up["message_id"], follow_up["message_id"]]
    assert len(sent) == 1
    state, data = database.execute(
        "SELECT state,data FROM capability_records WHERE id=%s", (po_id,),
    ).fetchone()
    assert state == "approved" and data["total"] == "0"
    assert data["supplier_response"]["supplier_quote"]["status"] == "needs_review"
    assert data["supplier_response"]["count"] == 2
    assert len(list_notifications(database, context)) == 2

    database.execute("UPDATE outbox_jobs SET follow_up_due_at=now() WHERE id=%s", (job_id,))
    database.commit()
    assert enqueue_due_follow_up(database)
    assert not enqueue_due_follow_up(database)
    messages = []

    def notify(template, action, arguments, credentials):
        assert template == "gmail" and action == "gmail.send_email"
        assert arguments["to"] == "mailbox@example.com"
        messages.append(arguments)
        return {"message_id": f"notification-{len(messages)}"}

    monkeypatch.setattr("austra.integrations.workflow_notifications.perform", notify)
    for _ in range(3):
        assert dispatch_owner_notification(database, tmp_path)
    assert not dispatch_owner_notification(database, tmp_path)
    assert {item[5] for item in list_notifications(database, context)} == {"sent"}


def test_reply_repair_stops_after_bounded_attempts():
    reply = {"message_id": "reply-1", "thread_id": "thread-1"}
    now = datetime.now(UTC)
    calls = []

    def invalid_response(tool, arguments, _credentials):
        calls.append(arguments)
        raise ExternalContractError(tool, '{"body": 123}')

    recoveries = {}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        evidence, recoveries = read_pending_replies([reply], set(), recoveries, {}, invalid_response,
                                                   now, manual=False)
        assert evidence == {}
        assert recoveries["reply-1"]["attempts"] == attempt
        now += timedelta(days=1)
    assert recoveries["reply-1"]["state"] == "needs_review"
    assert read_pending_replies([reply], set(), recoveries, {}, invalid_response, now, manual=False) == ({}, {})
    assert calls == [reply] * MAX_ATTEMPTS
