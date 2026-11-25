"""First capability slice against a disposable tenant schema and fake provider."""

import os
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.types.json import Jsonb

from austra.build_release.service import (
    create_build,
    fix_defect,
    preview_build,
    publish,
    rollback,
    transition_build,
)
from austra.capabilities.builder import build_capability
from austra.capabilities.contracts import CapabilityCommand
from austra.capabilities.effects import deliver_email
from austra.capabilities.projections import projection_data
from austra.capabilities.routes import install_capability_routes
from austra.capabilities.runtime import capability_view, execute_command, explain_event
from austra.capabilities.verifier import verify_build
from austra.semantics.purchase_workflow import prepare_purchase_workflow
from austra.semantics.service import confirm_candidate
from austra.storage.migrate import apply_migrations
from austra.tenant.context import TenantContext, tenant_transaction
from austra.tenant.permissions.evaluator import AuthContext, DepartmentGrant


@pytest.fixture
def database():
    url = os.environ.get("AUSTRA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("AUSTRA_TEST_DATABASE_URL is required")
    schema = f"capability_test_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            with psycopg.connect(url, options=f"-c search_path={schema}") as connection:
                assert "016_discovery_state.sql" in apply_migrations(connection)
                yield connection
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def setup_tenant(connection):
    organization_id = connection.execute(
        "INSERT INTO organizations (slug, name) VALUES ('capability-fixture', 'Fixture') RETURNING id"
    ).fetchone()[0]
    user_id = connection.execute(
        "INSERT INTO users (organization_id, username_normalized, role, password_hash, "
        "must_change_password) VALUES (%s, 'admin', 'org_admin', 'fixture', false) RETURNING id",
        (organization_id,),
    ).fetchone()[0]
    department_id = connection.execute(
        "INSERT INTO departments (organization_id, slug, name) "
        "VALUES (%s, 'operations', 'Operations') RETURNING id",
        (organization_id,),
    ).fetchone()[0]
    context = TenantContext.from_auth(
        AuthContext(user_id, organization_id, "org_admin", True, True)
    )
    return context, department_id


def source_manifest():
    actions = (
        "submit_order",
        "reserve_stock",
        "draft_po",
        "approve_po",
        "propose_email",
        "approve_email",
    )
    rules = []
    for action in actions:
        content = {
            "decision_key": f"command.{action}",
            "required_permission": "workflows.approve"
            if action.startswith("approve_")
            else "workflows.run",
            "scope": "operations",
            "effect": action,
        }
        if action == "approve_po":
            content.update({"limit": "100", "unit": "USD"})
        rules.append(
            {
                "id": str(uuid4()),
                "content": content,
                "valid_from": "2020-01-01T00:00:00+00:00",
                "valid_to": None,
            }
        )
    return {
        "revisions": [{"kind": "capability", "key": "so_inventory_po_email"}],
        "decision_tables": rules,
        "event_routes": [{"kind": "workflow", "content": {"capability": "so_inventory_po_email"}}],
        "typed_mappings": [],
        "projections": [{"kind": "projection", "key": "journey"}],
    }


def blueprint(connection, context, source=None):
    content_hash = "a" * 64
    blueprint_id = connection.execute(
        "INSERT INTO blueprints (organization_id, blueprint_version, content_hash, confirmed_by) "
        "VALUES (%s, 1, %s, %s) RETURNING id",
        (context.organization_id, content_hash, context.actor.user_id),
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO onboarding_cases (organization_id, created_event_id, state, blueprint_id) "
        "VALUES (%s, %s, 'locked_candidate', %s)",
        (context.organization_id, uuid4(), blueprint_id),
    )
    connection.execute(
        "INSERT INTO blueprint_manifests (organization_id, blueprint_id, manifest) "
        "VALUES (%s, %s, %s)",
        (context.organization_id, blueprint_id, Jsonb(source or source_manifest())),
    )
    return blueprint_id


def build_to_preview(connection, context, blueprint_id):
    build = create_build(connection, context, blueprint_id)
    built = build_capability(connection, context, build["id"], "Restore prior release")
    assert built["blueprint_hash"] == "a" * 64
    revision = transition_build(connection, context, build["id"], "testing", 1, uuid4())
    verification = verify_build(connection, context, build["id"])
    assert verification["passed"], verification
    assert (
        connection.execute(
            "SELECT count(*) FROM organizations WHERE slug LIKE 'verifier-%'"
        ).fetchone()[0]
        == 0
    )
    revision = transition_build(
        connection, context, build["id"], "admin_preview", revision, uuid4()
    )
    return build, revision


def release_build(connection, context, build, revision):
    preview_build(connection, context, build["id"], revision, True)
    revision = transition_build(connection, context, build["id"], "accepted", revision, uuid4())
    revision = transition_build(
        connection, context, build["id"], "ready_for_release", revision, uuid4()
    )
    return publish(connection, context, build["id"], revision, uuid4())


def command(action, department_id, payload, revisions=None, key=None):
    return CapabilityCommand.model_validate(
        {
            "action": action,
            "department_id": department_id,
            "idempotency_key": key or str(uuid4()),
            "correlation_id": str(uuid4()),
            "expected_revisions": revisions or {},
            "payload": payload,
        }
    )


def stock_lot(connection, context, department_id):
    data = {
        "product": "widget",
        "status": "released",
        "verification_status": "verified",
        "on_hand_kg": "10",
        "reserved_kg": "0",
        "qc_hold_kg": "0",
        "available_kg": "10",
        "lot_id": "LOT-1",
    }
    return connection.execute(
        "INSERT INTO capability_records (organization_id, department_id, kind, external_id, "
        "state, data) VALUES (%s, %s, 'stock_lot', 'LOT-1', 'available', %s) RETURNING id",
        (context.organization_id, department_id, Jsonb(data)),
    ).fetchone()[0]


def test_approved_legacy_purchase_workflow_reaches_release(database, tmp_path):
    context, department = setup_tenant(database)
    draft = {"id": uuid4().hex, "kind": "workflow", "status": "approved",
             "name": "demo_sales_order", "summary": "Reserve remaining stock and review the purchase.",
             "content": "Reserve verified stock. Approve the remaining purchase and exact email separately."}
    prepared = prepare_purchase_workflow(database, context, tmp_path, draft, department, Decimal(0))
    assert prepared["issues"] == []
    blueprint = confirm_candidate(database, context, prepared["case_id"], prepared["content_hash"])
    build = create_build(database, context, blueprint["blueprint_id"])
    build_capability(database, context, build["id"], "Restore the previous release")
    revision = transition_build(database, context, build["id"], "testing", 1, uuid4())
    verified = verify_build(database, context, build["id"])
    assert verified["passed"], verified
    revision = transition_build(database, context, build["id"], "admin_preview", revision, uuid4())
    with tenant_transaction(database, context):
        database.execute(
            "INSERT INTO capability_connection_health (organization_id, provider_key, state) "
            "VALUES (%s, 'email', 'healthy')", (context.organization_id,),
        )
    release_build(database, context, build, revision)
    assert capability_view(database, context, department)["build_id"] == str(build["id"])


def quality_manifest():
    key = "quality_batches"
    transitions = [
        {
            "key": "receive",
            "from": None,
            "to": "received",
            "permission": "workflows.run",
            "fields": ["external_id", "quantity_kg"],
        },
        {
            "key": "release",
            "from": "received",
            "to": "released",
            "permission": "workflows.approve",
            "fields": [],
        },
    ]
    rules = [
        {
            "id": str(uuid4()),
            "valid_from": "2020-01-01T00:00:00+00:00",
            "valid_to": None,
            "content": {
                "decision_key": f"command.{key}.{item['key']}",
                "required_permission": item["permission"],
                "scope": "quality",
                "effect": item["key"],
                **(
                    {"guard": {"field": "quantity_kg", "op": "lte", "value": "10", "unit": "kg"}}
                    if item["key"] == "release"
                    else {}
                ),
            },
        }
        for item in transitions
    ]
    content = {
        "engine": "state_machine",
        "record_kind": "quality_batch",
        "owner": "quality_lead",
        "failure_path": "hold_for_review",
        "fields": [{"key": "quantity_kg", "type": "decimal", "unit": "kg"}],
        "states": ["received", "released", "held"],
        "transitions": transitions,
        "acceptance_examples": [
            {"category": "normal", "input": "receive 2 kg", "expected": "received"},
            {"category": "boundary", "input": "receive 0 kg", "expected": "received"},
            {"category": "adverse", "input": "release without authority", "expected": "denied"},
        ],
    }
    return {
        "revisions": [{"kind": "capability", "key": key, "content": content}],
        "decision_tables": rules,
        "typed_mappings": [],
        "allowed_actions": [],
        "projections": [
            {
                "key": "receive_form",
                "content": {
                    "view": "form",
                    "capability": key,
                    "fields": ["external_id", "quantity_kg"],
                    "roles": ["org_admin", "employee"],
                    "transition": "receive",
                },
            },
            {
                "key": "quality_queue",
                "content": {
                    "view": "queue",
                    "capability": key,
                    "fields": ["external_id", "state", "quantity_kg"],
                    "roles": ["org_admin", "employee"],
                    "states": ["received"],
                },
            },
            {
                "key": "released_weight",
                "content": {
                    "view": "dashboard",
                    "capability": key,
                    "fields": ["external_id", "quantity_kg"],
                    "roles": ["org_admin"],
                    "states": ["released"],
                    "measure": "quantity_kg",
                    "unit": "kg",
                },
            },
            {
                "key": "custom_inspection",
                "content": {
                    "view": "custom_module",
                    "capability": key,
                    "interaction": "offline microscope capture",
                    "review_ticket": "BIT-600",
                    "rollback_plan": "Revert reviewed module release",
                },
            },
        ],
    }


def test_second_organization_quality_workflow_and_projection_isolation(database):
    first, first_department = setup_tenant(database)
    first_blueprint = blueprint(database, first)
    database.execute(
        "INSERT INTO capability_connection_health (organization_id, provider_key, state) "
        "VALUES (%s, 'email', 'healthy')",
        (first.organization_id,),
    )
    first_build, first_revision = build_to_preview(database, first, first_blueprint)
    release_build(database, first, first_build, first_revision)
    first_manifest = database.execute(
        "SELECT manifest FROM capability_build_artifacts "
        "WHERE organization_id = %s AND build_id = %s",
        (first.organization_id, first_build["id"]),
    ).fetchone()[0]

    second_organization = database.execute(
        "INSERT INTO organizations (slug, name) VALUES ('quality-fixture', 'Quality') RETURNING id"
    ).fetchone()[0]
    second_user = database.execute(
        "INSERT INTO users (organization_id, username_normalized, role, password_hash, "
        "must_change_password) VALUES (%s, 'admin', 'org_admin', 'fixture', false) RETURNING id",
        (second_organization,),
    ).fetchone()[0]
    second_department = database.execute(
        "INSERT INTO departments (organization_id, slug, name) "
        "VALUES (%s, 'quality', 'Quality') RETURNING id",
        (second_organization,),
    ).fetchone()[0]
    second = TenantContext.from_auth(
        AuthContext(second_user, second_organization, "org_admin", True, True)
    )
    second_blueprint = blueprint(database, second, quality_manifest())
    second_build, second_revision = build_to_preview(database, second, second_blueprint)
    release_build(database, second, second_build, second_revision)
    view = capability_view(database, second, second_department)
    assert view["capabilities"][0]["key"] == "quality_batches"
    artifact = database.execute(
        "SELECT manifest FROM capability_build_artifacts "
        "WHERE organization_id = %s AND build_id = %s",
        (second_organization, second_build["id"]),
    ).fetchone()[0]
    assert artifact["custom_module_drafts"][0]["status"] == "draft_review_required"
    assert (
        database.execute(
            "SELECT manifest FROM capability_build_artifacts "
            "WHERE organization_id = %s AND build_id = %s",
            (first.organization_id, first_build["id"]),
        ).fetchone()[0]
        == first_manifest
    )

    payload = {
        "capability_key": "quality_batches",
        "transition_key": "receive",
        "external_id": "BATCH-1",
        "fields": {"quantity_kg": "2.125"},
    }
    received = execute_command(
        database, second, command("transition_record", second_department, payload, key="batch-1")
    )
    record_id = received["record"]["id"]
    assert (
        execute_command(
            database,
            second,
            command("transition_record", second_department, payload, key="batch-1"),
        )
        == received
    )
    employee_id = database.execute(
        "INSERT INTO users (organization_id, username_normalized, role, password_hash, "
        "must_change_password) VALUES (%s, 'worker', 'employee', 'fixture', false) RETURNING id",
        (second_organization,),
    ).fetchone()[0]
    employee = TenantContext.from_auth(
        AuthContext(
            employee_id,
            second_organization,
            "employee",
            True,
            True,
            grants=(
                DepartmentGrant(
                    second_department,
                    frozenset({"workflows.run", "workflows.read", "reports.read"}),
                ),
            ),
        )
    )
    release_payload = {
        "capability_key": "quality_batches",
        "transition_key": "release",
        "record_id": record_id,
        "fields": {},
    }
    with pytest.raises(PermissionError):
        execute_command(
            database,
            employee,
            command("transition_record", second_department, release_payload, {record_id: 1}),
        )
    with pytest.raises(PermissionError):
        projection_data(database, employee, second_department, "released_weight", artifact)
    altered_projection = {
        **artifact,
        "projections": [
            {**item, "roles": ["org_admin", "employee"]}
            if item["key"] == "released_weight"
            else item
            for item in artifact["projections"]
        ],
    }
    assert (
        projection_data(
            database, employee, second_department, "released_weight", altered_projection
        )["record_count"]
        == 0
    )
    with pytest.raises(PermissionError):
        execute_command(
            database,
            employee,
            command("transition_record", second_department, release_payload, {record_id: 1}),
        )
    assert (
        execute_command(
            database,
            second,
            command("transition_record", second_department, release_payload, {record_id: 1}),
        )["record"]["state"]
        == "released"
    )
    oversized = execute_command(
        database,
        second,
        command(
            "transition_record",
            second_department,
            {**payload, "external_id": "BATCH-LARGE", "fields": {"quantity_kg": "11"}},
        ),
    )
    with pytest.raises(ValueError, match="guard"):
        execute_command(
            database,
            second,
            command(
                "transition_record",
                second_department,
                {**release_payload, "record_id": oversized["record"]["id"]},
                {oversized["record"]["id"]: 1},
            ),
        )
    with pytest.raises(ValueError, match="released build"):
        execute_command(database, first, command("transition_record", first_department, payload))
    with pytest.raises(PermissionError):
        projection_data(database, first, first_department, "released_weight", first_manifest)

    for external_id, amount, source_label, qc_status in (
        ("BATCH-2", "3.375", "live", "released"),
        ("DEMO", "100", "synthetic_demo", "released"),
        ("QC-HOLD", "50", "live", "held"),
    ):
        database.execute(
            "INSERT INTO capability_records (organization_id, department_id, kind, "
            "external_id, state, data) VALUES (%s, %s, 'quality_batch', %s, 'released', %s)",
            (
                second_organization,
                second_department,
                external_id,
                Jsonb(
                    {
                        "quantity_kg": amount,
                        "source_label": source_label,
                        "qc_status": qc_status,
                        "units": {"quantity_kg": "kg"},
                    }
                ),
            ),
        )
    dashboard = projection_data(database, second, second_department, "released_weight", artifact)
    sql_total = database.execute(
        "SELECT sum((data->>'quantity_kg')::numeric) FROM capability_records "
        "WHERE organization_id = %s AND department_id = %s AND kind = 'quality_batch' "
        "AND state = 'released' AND coalesce(data->>'source_label', 'live') = 'live' "
        "AND coalesce(data->>'qc_status', 'released') = 'released'",
        (second_organization, second_department),
    ).fetchone()[0]
    assert dashboard["complete"] and dashboard["record_count"] == 2
    assert dashboard["excluded_demo_count"] == 1
    assert dashboard["excluded_hold_count"] == 1
    assert dashboard["total"] == format(sql_total, "f") == "5.500"
    assert dashboard["unit"] == "kg"
    database.execute(
        "INSERT INTO capability_records (organization_id, department_id, kind, external_id, "
        "state, data) VALUES (%s, %s, 'quality_batch', 'UNKNOWN-UNIT', 'released', %s)",
        (
            second_organization,
            second_department,
            Jsonb({"quantity_kg": "1", "units": {"quantity_kg": "lb"}}),
        ),
    )
    unresolved = projection_data(database, second, second_department, "released_weight", artifact)
    assert unresolved["total"] is None and unresolved["unresolved_count"] == 1


def test_journey_idempotency_authority_history_and_provider(database):
    context, department_id = setup_tenant(database)
    blueprint_id = blueprint(database, context)
    build, revision = build_to_preview(database, context, blueprint_id)
    with pytest.raises(ValueError, match="preview"):
        transition_build(database, context, build["id"], "accepted", revision, uuid4())
    with pytest.raises(ValueError, match="connection"):
        release_build(database, context, build, revision)
    database.execute(
        "INSERT INTO capability_connection_health (organization_id, provider_key, state) "
        "VALUES (%s, 'email', 'healthy')",
        (context.organization_id,),
    )
    # The failed publication rolls back its transaction, so the build is still ready.
    release = publish(database, context, build["id"], 5, uuid4())
    lot_id = stock_lot(database, context, department_id)
    order = execute_command(
        database,
        context,
        command(
            "submit_order",
            department_id,
            {"external_id": "SO-1", "product": "widget", "quantity_kg": "14"},
            key="so-1",
        ),
    )
    order_id = order["record"]["id"]
    reserve = command(
        "reserve_stock",
        department_id,
        {"order_id": order_id, "lot_id": lot_id, "quantity_kg": "10"},
        {order_id: 1, lot_id: 1},
        "reserve-1",
    )
    reserved = execute_command(database, context, reserve)
    assert execute_command(database, context, reserve) == reserved
    assert reserved["order"]["state"] == "partially_reserved"
    assert (
        database.execute(
            "SELECT count(*) FROM capability_record_revisions WHERE record_id = %s", (lot_id,)
        ).fetchone()[0]
        == 1
    )
    with pytest.raises(ValueError, match="revision"):
        execute_command(
            database,
            context,
            command(
                "reserve_stock",
                department_id,
                {"order_id": order_id, "lot_id": lot_id, "quantity_kg": "1"},
                {order_id: 1, lot_id: 1},
            ),
        )
    po = execute_command(
        database,
        context,
        command(
            "draft_po",
            department_id,
            {
                "order_id": order_id,
                "external_id": "PO-1",
                "supplier": "Supplier",
                "quantity_kg": "2",
                "unit_price_per_kg": "60",
                "currency": "USD",
            },
            {order_id: 2},
            "po-1",
        ),
    )
    po_id = po["record"]["id"]
    escalated = execute_command(
        database,
        context,
        command("approve_po", department_id, {"po_id": po_id}, {po_id: 1, order_id: 2}),
    )
    assert escalated["escalation_required"]
    assert escalated["record"]["state"] == "escalated"
    history = explain_event(database, context, department_id, UUID(escalated["event_id"]))
    assert history["blueprint_id"] == str(blueprint_id)
    assert history["build_id"] == str(build["id"])
    assert history["authority"]["content"]["limit"] == "100"
    po2 = execute_command(
        database,
        context,
        command(
            "draft_po",
            department_id,
            {
                "order_id": order_id,
                "external_id": "PO-2",
                "supplier": "Supplier",
                "quantity_kg": "2",
                "unit_price_per_kg": "50",
                "currency": "USD",
            },
            {order_id: 2},
        ),
    )
    po2_id = po2["record"]["id"]
    with pytest.raises(ValueError, match="unfilled demand"):
        execute_command(
            database,
            context,
            command(
                "draft_po",
                department_id,
                {
                    "order_id": order_id,
                    "external_id": "PO-3",
                    "supplier": "Supplier",
                    "quantity_kg": "1",
                    "unit_price_per_kg": "1",
                    "currency": "USD",
                },
                {order_id: 2},
            ),
        )
    approved = execute_command(
        database,
        context,
        command("approve_po", department_id, {"po_id": po2_id}, {po2_id: 1, order_id: 2}),
    )
    assert approved["record"]["state"] == "approved"
    proposal = execute_command(
        database,
        context,
        command(
            "propose_email",
            department_id,
            {
                "po_id": po2_id,
                "external_id": "MAIL-1",
                "recipient": "buyer@example.com",
                "subject": "Purchase proposal",
                "body": "Please review PO-2.",
            },
            {po2_id: 2},
        ),
    )
    assert proposal["preview"]["approval_required"]
    assert proposal["preview"]["source_records"][0]["revision"] == 2
    with pytest.raises(ValueError, match="email proposal"):
        execute_command(
            database,
            context,
            command(
                "propose_email",
                department_id,
                {
                    "po_id": po2_id,
                    "external_id": "MAIL-2",
                    "recipient": "buyer@example.com",
                    "subject": "Duplicate",
                    "body": "Duplicate",
                },
                {po2_id: 2},
            ),
        )
    email_id = proposal["record"]["id"]
    database.execute(
        "UPDATE capability_connection_health SET state = 'disconnected' WHERE organization_id = %s",
        (context.organization_id,),
    )
    approve_email = command(
        "approve_email", department_id, {"email_id": email_id}, {email_id: 1, po2_id: 2}, "mail-1"
    )
    queued = execute_command(database, context, approve_email)
    assert queued["effect"] == "repair_connection"
    assert execute_command(database, context, approve_email) == queued
    assert (
        database.execute(
            "SELECT count(*) FROM outbox_jobs WHERE event_id = %s", (queued["event_id"],)
        ).fetchone()[0]
        == 1
    )
    job_id = database.execute(
        "SELECT id FROM outbox_jobs WHERE event_id = %s", (queued["event_id"],)
    ).fetchone()[0]
    assert (
        deliver_email(
            database,
            context,
            job_id,
            lambda _key: None,
            lambda *_args: pytest.fail("disconnected send"),
        )["state"]
        == "repair"
    )
    database.execute(
        "UPDATE capability_connection_health SET state = 'healthy' WHERE organization_id = %s",
        (context.organization_id,),
    )
    sent = []
    receipt = deliver_email(
        database,
        context,
        job_id,
        lambda _key: None,
        lambda payload, key: sent.append((payload, key)) or "sandbox-1",
    )
    assert receipt["state"] == "delivered"
    assert (
        deliver_email(
            database,
            context,
            job_id,
            lambda _key: "sandbox-1",
            lambda *_args: pytest.fail("duplicate send"),
        )["receipt"]
        == "sandbox-1"
    )
    assert len(sent) == 1
    view = capability_view(database, context, department_id)
    assert view["complete"] and view["record_count"] == len(view["records"])
    assert view["build_id"] == str(build["id"])
    assert release


def test_preview_defect_rebuild_permission_disconnect_and_rollback(database):
    context, department_id = setup_tenant(database)
    blueprint_id = blueprint(database, context)
    database.execute(
        "INSERT INTO capability_connection_health (organization_id, provider_key, state) "
        "VALUES (%s, 'email', 'healthy')",
        (context.organization_id,),
    )
    first, revision = build_to_preview(database, context, blueprint_id)
    result = preview_build(
        database,
        context,
        first["id"],
        revision,
        False,
        "one email",
        "two emails",
        {"fixture": "duplicate-webhook"},
    )
    assert result["defect_id"]
    second = fix_defect(database, context, result["defect_id"], revision + 1, uuid4())
    assert second["blueprint_id"] == first["blueprint_id"]
    assert second["build_revision"] == first["build_revision"] + 1
    build_capability(database, context, second["id"], "Restore prior release")
    revision = transition_build(database, context, second["id"], "testing", 1, uuid4())
    assert verify_build(database, context, second["id"])["passed"]
    revision = transition_build(database, context, second["id"], "admin_preview", revision, uuid4())
    first_release = release_build(database, context, second, revision)
    third, revision = build_to_preview(database, context, blueprint_id)
    second_release = release_build(database, context, third, revision)
    assert rollback(database, context, second_release, uuid4()) == first_release
    employee_id = database.execute(
        "INSERT INTO users (organization_id, username_normalized, role, password_hash, "
        "must_change_password) VALUES (%s, 'worker', 'employee', 'fixture', false) RETURNING id",
        (context.organization_id,),
    ).fetchone()[0]
    employee = TenantContext.from_auth(
        AuthContext(
            employee_id,
            context.organization_id,
            "employee",
            True,
            True,
            grants=(
                DepartmentGrant(department_id, frozenset({"workflows.run", "workflows.read"})),
            ),
        )
    )
    with tenant_transaction(database, employee):
        assert (
            database.execute("SELECT count(*) FROM releases WHERE state = 'active'").fetchone()[0]
            == 1
        )
        assert database.execute("SELECT count(*) FROM blueprints").fetchone()[0] == 1
        assert (
            database.execute("SELECT count(*) FROM capability_build_artifacts").fetchone()[0] == 3
        )
    with pytest.raises(PermissionError):
        execute_command(
            database, employee, command("approve_po", department_id, {"po_id": str(uuid4())})
        )
    database.execute(
        "UPDATE capability_connection_health SET state = 'disconnected' WHERE organization_id = %s",
        (context.organization_id,),
    )
    assert (
        database.execute(
            "SELECT state FROM capability_connection_health WHERE organization_id = %s",
            (context.organization_id,),
        ).fetchone()[0]
        == "disconnected"
    )


def test_chat_ui_and_api_share_the_command_handler():
    app = FastAPI()
    install_capability_routes(app, object())
    endpoints = {route.path: route.endpoint for route in app.routes}
    assert (
        endpoints["/api/org/capability-commands"] is endpoints["/api/org/capability-chat/commands"]
    )
    assert endpoints["/api/org/capability-commands"] is endpoints["/api/org/capability-ui/commands"]


def test_chat_ui_and_api_return_one_pinned_decision(database):
    context, department_id = setup_tenant(database)
    blueprint_id = blueprint(database, context)
    database.execute(
        "INSERT INTO capability_connection_health (organization_id, provider_key, state) "
        "VALUES (%s, 'email', 'healthy')",
        (context.organization_id,),
    )
    build, revision = build_to_preview(database, context, blueprint_id)
    release_build(database, context, build, revision)
    schema = database.execute("SELECT current_schema()").fetchone()[0]
    database.commit()

    class FixtureIdentity:
        def session(self, _cookie):
            return context.actor, 3600

        def connect(self):
            return psycopg.connect(
                os.environ["AUSTRA_TEST_DATABASE_URL"], options=f"-c search_path={schema}"
            )

    app = FastAPI()

    @app.middleware("http")
    async def fixture_host(request, call_next):
        request.state.organization_slug = "capability-fixture"
        return await call_next(request)

    install_capability_routes(app, FixtureIdentity())
    payload = command(
        "submit_order",
        department_id,
        {"external_id": "SO-HTTP", "product": "item", "quantity_kg": "1"},
        key="same-command",
    )
    with TestClient(app) as client:
        decisions = [
            client.post(path, json=payload.model_dump(mode="json")).json()
            for path in (
                "/api/org/capability-commands",
                "/api/org/capability-chat/commands",
                "/api/org/capability-ui/commands",
            )
        ]
    assert decisions[0] == decisions[1] == decisions[2]
    assert decisions[0]["build_id"] == str(build["id"])
    assert decisions[0]["a2ui"]["kind"] == "capability"


def test_failed_verifier_creates_reproducible_defect(database):
    context, _department_id = setup_tenant(database)
    blueprint_id = blueprint(database, context)
    build = create_build(database, context, blueprint_id)
    build_capability(database, context, build["id"], "Restore prior release")
    database.execute(
        "UPDATE capability_build_artifacts SET manifest = jsonb_set(manifest, "
        "'{decision_tables,0,valid_from}', %s) "
        "WHERE organization_id = %s AND build_id = %s",
        (Jsonb("2099-01-01T00:00:00+00:00"), context.organization_id, build["id"]),
    )
    transition_build(database, context, build["id"], "testing", 1, uuid4())
    result = verify_build(database, context, build["id"])
    assert not result["passed"]
    defect = database.execute(
        "SELECT state, trace FROM defects WHERE organization_id = %s AND id = %s",
        (context.organization_id, UUID(result["defect_id"])),
    ).fetchone()
    assert defect[0] == "open"
    assert any(not case["passed"] for case in defect[1]["cases"])
    assert (
        database.execute(
            "SELECT count(*) FROM organizations WHERE slug LIKE 'verifier-%'"
        ).fetchone()[0]
        == 0
    )


def test_runtime_record_rls_rejects_other_tenant_and_department(database):
    context, department_id = setup_tenant(database)
    lot_id = stock_lot(database, context, department_id)
    other_id = database.execute(
        "INSERT INTO organizations (slug, name) VALUES ('other-fixture', 'Other') RETURNING id"
    ).fetchone()[0]
    other_user = database.execute(
        "INSERT INTO users (organization_id, username_normalized, role, password_hash, "
        "must_change_password) VALUES (%s, 'admin', 'org_admin', 'fixture', false) RETURNING id",
        (other_id,),
    ).fetchone()[0]
    other = TenantContext.from_auth(AuthContext(other_user, other_id, "org_admin", True, True))
    with tenant_transaction(database, other):
        assert (
            database.execute(
                "SELECT id FROM capability_records WHERE id = %s", (lot_id,)
            ).fetchone()
            is None
        )
    wrong_department = database.execute(
        "INSERT INTO departments (organization_id, slug, name) "
        "VALUES (%s, 'other', 'Other') RETURNING id",
        (context.organization_id,),
    ).fetchone()[0]
    employee_id = database.execute(
        "INSERT INTO users (organization_id, username_normalized, role, password_hash, "
        "must_change_password) VALUES (%s, 'worker', 'employee', 'fixture', false) RETURNING id",
        (context.organization_id,),
    ).fetchone()[0]
    employee = TenantContext.from_auth(
        AuthContext(
            employee_id,
            context.organization_id,
            "employee",
            True,
            True,
            grants=(
                DepartmentGrant(wrong_department, frozenset({"workflows.run", "workflows.read"})),
            ),
        )
    )
    with tenant_transaction(database, employee):
        assert (
            database.execute(
                "SELECT id FROM capability_records WHERE id = %s", (lot_id,)
            ).fetchone()
            is None
        )
