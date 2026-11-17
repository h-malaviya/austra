"""Phase 5 gates against disposable tenant schemas and fake providers."""

import os
from dataclasses import replace
from uuid import UUID, uuid4

import psycopg
import pytest
from cryptography.fernet import Fernet
from psycopg import sql
from psycopg.types.json import Jsonb

from austra.build_release.monitoring import list_reviews
from austra.connectors import GMAIL_SCOPE
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
from austra.integrations.email_jobs import deliver_email_job, preview_email_job
from austra.integrations.execution import execute_proposal
from austra.integrations.gateway import approve_workflow, preview_action, receive_webhook
from austra.integrations.providers import safe_mcp_url
from austra.integrations.secrets import ConnectionSecrets
from austra.mcp_gateway import ExternalToolError
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
                assert apply_migrations(db)[-1] == "013_proactive_monitoring.sql"
                yield db
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def tenant(db, slug):
    organization = db.execute("INSERT INTO organizations (slug, name) "
                              "VALUES (%s, %s) RETURNING id", (slug, slug)).fetchone()[0]
    actor = db.execute("INSERT INTO users (organization_id, username_normalized, role, "
                       "password_hash, must_change_password) VALUES "
                       "(%s, 'admin', 'org_admin', 'fixture', false) RETURNING id",
                       (organization,)).fetchone()[0]
    department = db.execute("INSERT INTO departments (organization_id, slug, name) "
                            "VALUES (%s, 'operations', 'Operations') RETURNING id",
                            (organization,)).fetchone()[0]
    context = TenantContext.from_auth(AuthContext(actor, organization, "org_admin", True, True))
    return context, department


def release(db, context, action_name, scopes):
    blueprint = db.execute("INSERT INTO blueprints (organization_id, blueprint_version, "
                           "content_hash, status, confirmed_by) VALUES "
                           "(%s, 1, %s, 'active', %s) RETURNING id",
                           (context.organization_id, "a" * 64, context.actor.user_id)).fetchone()[0]
    build = db.execute("INSERT INTO builds (organization_id, blueprint_id, build_revision, "
                       "state) VALUES (%s, %s, 1, 'released') RETURNING id",
                       (context.organization_id, blueprint)).fetchone()[0]
    gmail = action_name == "gmail.send_email"
    rule = {"id": str(uuid4()), "valid_from": "2026-01-01T00:00:00+00:00",
            "valid_to": None, "content": {
                "decision_key": "command.approve_email" if gmail
                else f"integration.{action_name}",
                "required_permission": "workflows.approve" if gmail else "integrations.use"}}
    manifest = {"blueprint_hash": "a" * 64, "decision_tables": [rule],
                "allowed_actions": [{"content": {"action_ids": [action_name],
                                                  "requested_scopes": scopes}}]}
    db.execute("INSERT INTO capability_build_artifacts (organization_id, build_id, "
               "blueprint_hash, manifest, fixtures, rollback_plan, preview_state) "
               "VALUES (%s, %s, %s, %s, '{}'::jsonb, 'fixture rollback', 'accepted')",
               (context.organization_id, build, "a" * 64, Jsonb(manifest)))
    db.execute("INSERT INTO releases (organization_id, blueprint_id, build_id, "
               "approved_by) VALUES (%s, %s, %s, %s)",
               (context.organization_id, blueprint, build, context.actor.user_id))
    return build


def github_connection(db, context, root, monkeypatch):
    monkeypatch.setattr("austra.integrations.connections.github_identity",
                        lambda _token: ("fixture-account", ("repo",)))
    monkeypatch.setattr("austra.integrations.providers.github_identity",
                        lambda _token: ("fixture-account", ("repo",)))
    return connect_key(db, context, root, "github", {"token": "private-fixture-token"})


def test_schema_drift_opens_owned_review(database, tmp_path, monkeypatch):
    context, _department = tenant(database, "drift-fixture")
    original = [{"name": "find", "input_schema": {"type": "object", "properties": {}}}]
    changed = [{"name": "find", "input_schema": {"type": "object", "properties": {
        "query": {"type": "string"}}}}]
    monkeypatch.setattr("austra.integrations.connections.discover", lambda *_args: original)
    connection = connect_key(database, context, tmp_path, "remote_mcp",
                             {"token": "fixture-secret"}, "https://example.com/mcp", "fixture")
    monkeypatch.setattr("austra.integrations.connections.discover", lambda *_args: changed)
    assert check_health(database, context, tmp_path, UUID(connection["id"]))["health"] == "schema_review"
    cases = list_reviews(database, context)
    assert len(cases) == 1
    assert cases[0]["trigger"] == "schema_drift"
    assert cases[0]["owner_user_id"] == context.actor.user_id
    assert cases[0]["citations"][0]["connection_id"] == connection["id"]
    assert check_health(database, context, tmp_path, UUID(connection["id"]))["health"] == "schema_review"
    assert len(list_reviews(database, context)) == 1


def approved(db, context, root, department, connection, build, action_name, monkeypatch):
    monkeypatch.setattr("austra.integrations.gateway.perform",
                        lambda *_args: {"items": []})
    fixture = ({"owner": "fixture", "repo": "repo", "title": "Issue"}
               if action_name == "github.create_issue" else {"per_page": 1})
    result = approve_workflow(db, context, root, department, UUID(connection["id"]), build,
                              action_name, fixture, {"event_id": "fixture-event"})
    return UUID(result["approval_id"]), fixture


def test_catalog_and_vault_keep_private_fields_out_of_metadata(database, tmp_path, monkeypatch):
    context, department = tenant(database, "catalog-fixture")
    connection = github_connection(database, context, tmp_path, monkeypatch)
    listed = list_connections(database, context)
    assert listed[0]["account"] == "fixture-account"
    assert "private-fixture-token" not in str(listed)
    assert "private-fixture-token" not in str(TEMPLATES["github"].public())
    stored = (tmp_path / "integration_secrets" / str(context.organization_id)
              / connection["id"]).read_bytes()
    assert b"private-fixture-token" not in stored
    assert ConnectionSecrets(tmp_path).read(context.organization_id,
                                             UUID(connection["id"]))["token"] == "private-fixture-token"
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
        approve_workflow(database, context, tmp_path, department, UUID(connection["id"]),
                         build, "github.create_issue",
                         {"owner": "fixture", "repo": "repo", "title": "Issue"},
                         {"event_id": "fixture-event", "payload": {"title": "Other"}},
                         {"title": "title"})
    approval_id, arguments = approved(database, context, tmp_path, department, connection,
                                      build, "github.create_issue", monkeypatch)
    preview = preview_action(database, context, department, approval_id, arguments, [],
                             "Open the approved issue")
    calls = []

    def send(*_args):
        calls.append(True)
        return {"id": 42}

    monkeypatch.setattr("austra.integrations.execution.perform", send)
    proposal_id = UUID(preview["proposal_id"])
    first = execute_proposal(database, context, tmp_path, department, proposal_id, "first")
    assert first == {"state": "succeeded", "receipt": "42"}
    assert execute_proposal(database, context, tmp_path, department, proposal_id,
                            "first")["receipt"] == "42"
    assert len(calls) == 1
    second = preview_action(database, context, department, approval_id, arguments, [],
                            "Open the approved issue")
    monkeypatch.setattr("austra.integrations.execution.perform", lambda *_args: {})
    uncertain = execute_proposal(database, context, tmp_path, department,
                                 UUID(second["proposal_id"]), "second")
    assert uncertain["state"] == "uncertain"
    assert execute_proposal(database, context, tmp_path, department,
                            UUID(second["proposal_id"]), "second")["state"] == "uncertain"
    monkeypatch.setitem(TEMPLATES, "github", replace(TEMPLATES["github"], rate_per_minute=2))
    third = preview_action(database, context, department, approval_id, arguments, [],
                           "Open the approved issue")
    with pytest.raises(ValueError, match="rate limit"):
        execute_proposal(database, context, tmp_path, department,
                         UUID(third["proposal_id"]), "third")
    monkeypatch.setitem(TEMPLATES, "github", replace(TEMPLATES["github"], rate_per_minute=60))
    timeout_preview = preview_action(database, context, department, approval_id, arguments, [],
                                     "Open the approved issue")
    def timeout(*_args):
        raise TimeoutError("provider timeout")
    monkeypatch.setattr("austra.integrations.execution.perform", timeout)
    assert execute_proposal(database, context, tmp_path, department,
                            UUID(timeout_preview["proposal_id"]), "timeout")["state"] == "uncertain"
    assert receive_webhook(database, context, UUID(connection["id"]), "event-1", {"x": 1})
    assert not receive_webhook(database, context, UUID(connection["id"]), "event-1", {"x": 1})
    with pytest.raises(ValueError):
        receive_webhook(database, context, UUID(connection["id"]), "event-1", {"x": 2})
    revoke(database, context, tmp_path, UUID(connection["id"]))
    with pytest.raises(LookupError):
        preview_action(database, context, department, approval_id, arguments, [],
                       "Open the approved issue")
    assert list_connections(database, context) == []
    assert other_department


def test_expired_scope_and_schema_drift_stop_actions(database, tmp_path, monkeypatch):
    context, department = tenant(database, "repair-fixture")
    connection = github_connection(database, context, tmp_path, monkeypatch)
    build = release(database, context, "github.list_repositories", ["repo"])
    approval_id, arguments = approved(database, context, tmp_path, department, connection,
                                      build, "github.list_repositories", monkeypatch)
    with tenant_transaction(database, context):
        database.execute("UPDATE integration_connections SET granted_scopes = '{}' "
                         "WHERE organization_id = %s AND id = %s",
                         (context.organization_id, UUID(connection["id"])))
    with pytest.raises(PermissionError):
        preview_action(database, context, department, approval_id, arguments, [])
    with tenant_transaction(database, context):
        database.execute("UPDATE integration_connections SET granted_scopes = ARRAY['repo'] "
                         "WHERE organization_id = %s AND id = %s",
                         (context.organization_id, UUID(connection["id"])))
    monkeypatch.setattr("austra.integrations.connections.github_identity",
                        lambda _token: (_ for _ in ()).throw(ExternalToolError("expired")))
    assert check_health(database, context, tmp_path, UUID(connection["id"]))["health"] == "repair"
    with pytest.raises(ValueError):
        preview_action(database, context, department, approval_id, arguments, [])
    assert ConnectionSecrets(tmp_path).read(context.organization_id,
                                             UUID(connection["id"]))["token"]

    tools = [{"name": "create", "input_schema": {"type": "object"},
              "read_only": True, "description": "safe read"}]
    assert _remote_catalog(tools)[0]["effect"] == "unreviewed"
    with pytest.raises(ValueError):
        safe_mcp_url("https://127.0.0.1/mcp")
    monkeypatch.setattr("austra.integrations.connections.discover", lambda *_: tools)
    remote = connect_key(database, context, tmp_path, "remote_mcp", {"token": "private"},
                         "https://mcp.example/tools", "fixture workspace")
    public = connect_key(database, context, tmp_path, "remote_mcp", {"token": ""},
                         "https://mcp.example/tools", "public documentation")
    assert public["health"] == "healthy"
    review_remote(database, context, UUID(remote["id"]), "remote_mcp.create", "write", [], "id")
    monkeypatch.setattr("austra.integrations.connections.discover",
                        lambda *_: [{"name": "create", "input_schema": {"type": "object",
                                                                           "required": ["x"]}}])
    assert check_health(database, context, tmp_path, UUID(remote["id"]))["health"] == "schema_review"


def test_gmail_job_reconnect_preview_and_idempotent_delivery(database, tmp_path, monkeypatch):
    context, department = tenant(database, "mail-fixture")
    monkeypatch.setattr("austra.integrations.connections.gmail_identity",
                        lambda _credentials: "mailbox@example.com")
    scopes = tuple(GMAIL_SCOPE.split())
    connected = connect_gmail(database, context, tmp_path, "refresh-token",
                              "mailbox@example.com", scopes, "client", "secret")
    build = release(database, context, "gmail.send_email", list(scopes))
    fixture = {"to": "buyer@example.com", "subject": "Purchase order", "body": "Approved"}
    approved_result = approve_workflow(
        database, context, tmp_path, department, UUID(connected["id"]), build,
        "gmail.send_email", fixture, {"event_id": "mail-trigger"})
    assert approved_result["preview"]["arguments"] == fixture
    with tenant_transaction(database, context):
        email_id = database.execute(
            "INSERT INTO capability_records (organization_id, department_id, kind, "
            "external_id, state, data) VALUES (%s, %s, 'email_proposal', 'MAIL-1', "
            "'queued', '{}'::jsonb) RETURNING id",
            (context.organization_id, department)).fetchone()[0]
        event_id = database.execute(
            "INSERT INTO runtime_events (organization_id, department_id, event_type, "
            "idempotency_key, correlation_id) VALUES (%s, %s, 'capability.command', %s, %s) "
            "RETURNING id", (context.organization_id, department, "mail-fixture",
                             uuid4())).fetchone()[0]
        job_id = database.execute(
            "INSERT INTO outbox_jobs (organization_id, department_id, event_id, topic, "
            "payload, state) VALUES (%s, %s, %s, 'capability.email', %s, 'repair') "
            "RETURNING id", (context.organization_id, department, event_id,
                             Jsonb({"email_id": str(email_id), "recipient": fixture["to"],
                                    "subject": fixture["subject"], "body": fixture["body"]}))
        ).fetchone()[0]
    with tenant_transaction(database, context):
        database.execute("UPDATE integration_connections SET health = 'repair' WHERE "
                         "organization_id = %s AND id = %s",
                         (context.organization_id, UUID(connected["id"])))
    reconnected = connect_gmail(database, context, tmp_path, "new-refresh-token",
                                "mailbox@example.com", scopes, "client", "secret",
                                UUID(connected["id"]))
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
    first = deliver_email_job(database, context, tmp_path, department, job_id, proposal_id)
    assert first == {"state": "delivered", "receipt": "provider-message-1"}
    assert deliver_email_job(database, context, tmp_path, department, job_id,
                             proposal_id) == first
    assert len(sent) == 1
