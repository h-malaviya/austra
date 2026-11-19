"""Phase 0 integration tests use isolated schemas and the actual non-owner role."""

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from austra.build_release.monitoring import (
    list_reviews,
    record_metric,
    review_dashboard,
    scan_command_expiry,
    scan_policy_expiry,
    triage_feedback,
)
from austra.build_release.service import (
    create_build,
    fix_defect,
    publish,
    record_defect,
    rollback,
    transition_build,
)
from austra.capabilities.events import emit_event, finish_job, lease_job
from austra.onboarding.service import current_case, open_case, transition_case
from austra.skills import PLATFORM_SKILLS, load_skill, load_skills, skill_cards
from austra.storage.legacy_inventory import record_legacy_inventory
from austra.storage.migrate import apply_migrations
from austra.tenant.context import TenantContext, tenant_transaction
from austra.tenant.permissions.evaluator import AuthContext, DepartmentGrant, SupportContext
from austra.tenant.repository import read_records


@pytest.fixture
def database():
    url = os.environ.get("AUSTRA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("AUSTRA_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    schema = f"proactive_test_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            with psycopg.connect(url, options=f"-c search_path={schema}") as connection:
                assert "016_discovery_state.sql" in apply_migrations(connection)
                yield connection
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def make_organization(connection, slug):
    organization_id = connection.execute(
        "INSERT INTO organizations (slug, name) VALUES (%s, %s) RETURNING id",
        (slug, slug),
    ).fetchone()[0]
    admin_id = connection.execute(
        "INSERT INTO users (organization_id, username_normalized, role, password_hash, "
        "must_change_password) VALUES (%s, 'admin', 'org_admin', 'test', false) RETURNING id",
        (organization_id,),
    ).fetchone()[0]
    actor = AuthContext(admin_id, organization_id, "org_admin", True, True)
    event_id = connection.execute(
        "INSERT INTO audit_events (organization_id, actor_user_id, channel, action, "
        "outcome, correlation_id) VALUES (%s, %s, 'test', 'organization.created', "
        "'success', %s) RETURNING id",
        (organization_id, admin_id, uuid4()),
    ).fetchone()[0]
    return TenantContext.from_auth(actor), event_id


def locked_blueprint(connection, context, event_id):
    case_id = open_case(connection, context.organization_id, event_id)
    assert open_case(connection, context.organization_id, event_id) == case_id
    revision = 1
    for state in ("collecting", "clarifying", "ready_for_review"):
        result = transition_case(
            connection,
            context,
            case_id,
            state,
            revision,
            uuid4(),
        )
        revision = result["revision"]
    blueprint_id = connection.execute(
        "INSERT INTO blueprints (organization_id, blueprint_version, content_hash, confirmed_by) "
        "VALUES (%s, 1, %s, %s) RETURNING id",
        (context.organization_id, "a" * 64, context.actor.user_id),
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO blueprint_manifests (organization_id, blueprint_id, manifest) "
        "VALUES (%s, %s, %s)",
        (context.organization_id, blueprint_id, Jsonb({})),
    )
    connection.execute(
        "UPDATE onboarding_cases SET state = 'locked_candidate', blueprint_id = %s "
        "WHERE organization_id = %s AND id = %s",
        (blueprint_id, context.organization_id, case_id),
    )
    return case_id, blueprint_id


def ready_build(connection, context, blueprint_id):
    build = create_build(connection, context, blueprint_id)
    revision = 1
    for state in ("testing", "admin_preview", "accepted", "ready_for_release"):
        revision = transition_build(connection, context, build["id"], state, revision, uuid4())
    return build, revision


def test_rls_role_tenant_department_and_support_expiry(database):
    first, first_event = make_organization(database, "first")
    second, second_event = make_organization(database, "second")
    first_case = open_case(database, first.organization_id, first_event)
    second_case = open_case(database, second.organization_id, second_event)
    assert open_case(database, first.organization_id, first_event) == first_case
    assert (
        database.execute(
            "SELECT count(*) FROM outbox_jobs WHERE organization_id = %s AND topic = 'onboarding.case_opened'",
            (first.organization_id,),
        ).fetchone()[0]
        == 1
    )
    with tenant_transaction(database, first):
        role = database.execute("SELECT current_user").fetchone()[0]
        assert role == "austra_tenant_app"
        assert database.execute(
            "SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ).fetchone() == (False,)
        assert database.execute("SELECT id FROM onboarding_cases").fetchall() == [(first_case,)]
        with pytest.raises(psycopg.errors.InsufficientPrivilege), database.transaction():
            database.execute(
                "INSERT INTO onboarding_cases (organization_id, created_event_id) VALUES (%s, %s)",
                (second.organization_id, uuid4()),
            )
    with tenant_transaction(database, second):
        assert database.execute("SELECT id FROM onboarding_cases").fetchall() == [(second_case,)]

    department_one = database.execute(
        "INSERT INTO departments (organization_id, slug, name) "
        "VALUES (%s, 'one', 'One') RETURNING id",
        (first.organization_id,),
    ).fetchone()[0]
    department_two = database.execute(
        "INSERT INTO departments (organization_id, slug, name) "
        "VALUES (%s, 'two', 'Two') RETURNING id",
        (first.organization_id,),
    ).fetchone()[0]
    employee_id = database.execute(
        "INSERT INTO users (organization_id, username_normalized, role, password_hash, "
        "must_change_password) VALUES (%s, 'worker', 'employee', 'test', false) RETURNING id",
        (first.organization_id,),
    ).fetchone()[0]
    employee = TenantContext.from_auth(
        AuthContext(
            employee_id,
            first.organization_id,
            "employee",
            True,
            True,
            grants=(DepartmentGrant(department_one, frozenset({"workflows.run", "records.read"})),),
        )
    )
    emit_event(database, first, "test.created", "one", uuid4(), {}, department_id=department_one)
    emit_event(database, first, "test.created", "two", uuid4(), {}, department_id=department_two)
    with tenant_transaction(database, employee):
        assert database.execute("SELECT department_id FROM runtime_events").fetchall() == [
            (department_one,)
        ]
        with pytest.raises(psycopg.errors.InsufficientPrivilege), database.transaction():
            database.execute(
                "INSERT INTO runtime_events (organization_id, department_id, event_type, "
                "idempotency_key, correlation_id) VALUES (%s, %s, 'test', 'wrong', %s)",
                (first.organization_id, department_two, uuid4()),
            )
    with pytest.raises(PermissionError):
        emit_event(database, employee, "test", "wrong", uuid4(), {}, department_id=department_two)
    support_user = database.execute(
        "INSERT INTO users (username_normalized, role, password_hash, must_change_password) "
        "VALUES ('support', 'super_admin', 'test', false) RETURNING id"
    ).fetchone()[0]
    support = TenantContext.from_auth(
        AuthContext(
            support_user,
            None,
            "super_admin",
            True,
            True,
            support=SupportContext(
                first.organization_id, "Investigate", datetime.now(UTC) + timedelta(minutes=5)
            ),
        )
    )
    assert current_case(database, support)["id"] == first_case
    with (
        tenant_transaction(database, support),
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        database.transaction(),
    ):
        database.execute(
            "INSERT INTO onboarding_cases (organization_id, created_event_id) VALUES (%s, %s)",
            (first.organization_id, uuid4()),
        )
    with pytest.raises(PermissionError):
        TenantContext.from_auth(
            AuthContext(
                support_user,
                None,
                "super_admin",
                True,
                True,
                support=SupportContext(
                    first.organization_id, "Investigate", datetime.now(UTC) - timedelta(seconds=1)
                ),
            )
        )
    assert database.execute("SELECT current_user").fetchone()[0] != "austra_tenant_app"


def test_lifecycle_defect_patch_release_and_rollback(database):
    context, event_id = make_organization(database, "cycle")
    case_id = open_case(database, context.organization_id, event_id)
    with pytest.raises(ValueError, match="Illegal onboarding"):
        transition_case(
            database, context, case_id, "locked_candidate", 1, uuid4(), content_hash="a" * 64
        )
    with pytest.raises(ValueError, match="Stale case"):
        transition_case(database, context, case_id, "collecting", 2, uuid4())
    _, blueprint_id = locked_blueprint(database, context, event_id)
    assert current_case(database, context)["blueprint_id"] == blueprint_id
    build = create_build(database, context, blueprint_id)
    with pytest.raises(ValueError, match="Illegal build"):
        transition_build(database, context, build["id"], "accepted", 1, uuid4())
    revision = transition_build(database, context, build["id"], "testing", 1, uuid4())
    defect_id = record_defect(
        database,
        context,
        build["id"],
        revision,
        "Expected stock",
        "Missing stock",
        context.actor.user_id,
        uuid4(),
    )
    with pytest.raises(ValueError, match="Illegal build"):
        transition_build(database, context, build["id"], "admin_preview", revision + 1, uuid4())
    fixed = fix_defect(database, context, defect_id, revision + 1, uuid4())
    assert fixed["blueprint_id"] == blueprint_id
    assert fixed["build_revision"] == build["build_revision"] + 1
    assert database.execute(
        "SELECT count(*), max(blueprint_version) FROM blueprints WHERE organization_id = %s",
        (context.organization_id,),
    ).fetchone() == (1, 1)
    revision = transition_build(database, context, fixed["id"], "testing", 1, uuid4())
    revision = transition_build(database, context, fixed["id"], "admin_preview", revision, uuid4())
    preview_defect = record_defect(
        database,
        context,
        fixed["id"],
        revision,
        "Expected preview",
        "Wrong preview",
        context.actor.user_id,
        uuid4(),
    )
    fixed_again = fix_defect(database, context, preview_defect, revision + 1, uuid4())
    assert fixed_again["build_revision"] == 3
    revision = 1
    for state in ("testing", "admin_preview", "accepted", "ready_for_release"):
        revision = transition_build(database, context, fixed_again["id"], state, revision, uuid4())
    first_release = publish(database, context, fixed_again["id"], revision, uuid4())
    patch, patch_revision = ready_build(database, context, blueprint_id)
    patch_release = publish(database, context, patch["id"], patch_revision, uuid4())
    assert patch["build_revision"] == 4
    assert rollback(database, context, patch_release, uuid4()) == first_release
    assert (
        database.execute(
            "SELECT id FROM releases WHERE organization_id = %s AND state = 'active'",
            (context.organization_id,),
        ).fetchone()[0]
        == first_release
    )
    with pytest.raises(ValueError, match="No rollback"):
        rollback(database, context, first_release, uuid4())
    assert (
        database.execute(
            "SELECT blueprint_version FROM blueprints WHERE id = %s", (blueprint_id,)
        ).fetchone()[0]
        == 1
    )
    assert (
        database.execute(
            "SELECT count(*) FROM releases WHERE organization_id = %s",
            (context.organization_id,),
        ).fetchone()[0]
        == 2
    )
    assert (
        database.execute(
            "SELECT state FROM builds WHERE organization_id = %s AND id = %s",
            (context.organization_id, patch["id"]),
        ).fetchone()[0]
        == "rolled_back"
    )
    assert (
        database.execute(
            "SELECT count(*) FROM lifecycle_events WHERE organization_id = %s "
            "AND resource_type = 'release' AND correlation_id IS NOT NULL",
            (context.organization_id,),
        ).fetchone()[0]
        == 2
    )


def test_expiry_boundary_review_owner_and_tenant_metrics(database):
    first, event_id = make_organization(database, "monitor-first")
    second, _ = make_organization(database, "monitor-second")
    _, blueprint_id = locked_blueprint(database, first, event_id)
    fixed_time = datetime(2026, 11, 17, tzinfo=UTC)
    expires = fixed_time + timedelta(days=7)
    object_id = database.execute(
        "INSERT INTO semantic_objects (organization_id, kind, stable_key) "
        "VALUES (%s, 'policy', 'expiry_fixture') RETURNING id",
        (first.organization_id,),
    ).fetchone()[0]
    revision_id = database.execute(
        "INSERT INTO semantic_revisions (organization_id, object_id, revision, "
        "owner_user_id, rationale, content, valid_from, valid_to) "
        "VALUES (%s, %s, 1, %s, 'fixture', '{}'::jsonb, %s, %s) RETURNING id",
        (
            first.organization_id,
            object_id,
            first.actor.user_id,
            fixed_time - timedelta(days=1),
            expires,
        ),
    ).fetchone()[0]
    database.execute(
        "INSERT INTO blueprint_members (organization_id, blueprint_id, "
        "revision_id) VALUES (%s, %s, %s)",
        (first.organization_id, blueprint_id, revision_id),
    )
    build, revision = ready_build(database, first, blueprint_id)
    publish(database, first, build["id"], revision, uuid4())
    database.execute(
        "INSERT INTO capability_build_artifacts (organization_id, build_id, blueprint_hash, "
        "manifest, fixtures, rollback_plan) VALUES (%s, %s, %s, %s, '{}'::jsonb, 'restore')",
        (
            first.organization_id,
            build["id"],
            "a" * 64,
            Jsonb(
                {
                    "decision_tables": [
                        {
                            "id": str(revision_id),
                            "valid_to": expires.isoformat(),
                            "content": {"decision_key": "command.submit_order"},
                        }
                    ]
                }
            ),
        ),
    )
    assert scan_policy_expiry(database, first, fixed_time - timedelta(microseconds=1)) == []
    scan_command_expiry(database, first, "submit_order", fixed_time)
    case_ids = scan_policy_expiry(database, first, fixed_time)
    assert len(case_ids) == 1
    assert scan_policy_expiry(database, first, expires) == case_ids
    case = list_reviews(database, first)[0]
    assert case["owner_user_id"] == first.actor.user_id
    assert case["citations"][0]["semantic_revision_id"] == str(revision_id)
    assert list_reviews(database, second) == []
    record_metric(database, first, "retrieval_miss")
    record_metric(database, first, "task_resolved")
    record_metric(database, first, "task_cost_usd", 0.25)
    assert review_dashboard(database, first)["cost_per_resolved_task_usd"] == Decimal("0.25")
    assert review_dashboard(database, second)["metrics"] == {}
    with (
        tenant_transaction(database, first),
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        database.transaction(),
    ):
        database.execute(
            "INSERT INTO operational_metrics (organization_id, metric, value) "
            "VALUES (%s, 'retrieval_miss', 1)",
            (second.organization_id,),
        )


def test_feedback_triage_patch_versus_next_blueprint_case(database):
    context, event_id = make_organization(database, "triage")
    _, blueprint_id = locked_blueprint(database, context, event_id)
    build, revision = ready_build(database, context, blueprint_id)
    release_id = publish(database, context, build["id"], revision, uuid4())
    citation = [{"build_id": str(build["id"]), "blueprint_id": str(blueprint_id)}]
    bug = triage_feedback(
        database,
        context,
        release_id,
        "implementation_bug",
        "approved result",
        "wrong result",
        citation,
    )
    assert (
        triage_feedback(
            database,
            context,
            release_id,
            "implementation_bug",
            "approved result",
            "wrong result",
            citation,
        )
        == bug
    )
    assert bug["blueprint_id"] == blueprint_id
    assert (
        database.execute(
            "SELECT blueprint_id FROM builds WHERE id = %s", (bug["patch_build_id"],)
        ).fetchone()[0]
        == blueprint_id
    )
    patch_revision = transition_build(
        database, context, bug["patch_build_id"], "testing", 1, uuid4()
    )
    with pytest.raises(ValueError, match="independent verification"):
        transition_build(
            database, context, bug["patch_build_id"], "admin_preview", patch_revision, uuid4()
        )
    assert (
        database.execute(
            "SELECT count(*) FROM blueprints WHERE organization_id = %s", (context.organization_id,)
        ).fetchone()[0]
        == 1
    )
    meaning = triage_feedback(
        database,
        context,
        release_id,
        "business_meaning_change",
        "new rule requested",
        "old rule followed",
        citation,
    )
    assert meaning["active_blueprint_id"] == blueprint_id
    assert current_case(database, context)["id"] == meaning["clarification_case_id"]
    assert current_case(database, context)["state"] == "clarifying"
    assert len(list_reviews(database, context)) == 2
    with pytest.raises(ValueError, match="Classify"):
        triage_feedback(database, context, release_id, "unreviewed", "a", "b", citation)
    with pytest.raises(ValueError, match="citation is outside"):
        triage_feedback(
            database,
            context,
            release_id,
            "implementation_bug",
            "a",
            "b",
            [{"build_id": str(uuid4())}],
        )


def test_event_retry_transaction_and_inventory(database, tmp_path):
    context, _event_id = make_organization(database, "events")
    correlation = uuid4()
    event_id = emit_event(
        database, context, "sample.created", "key", correlation, {"value": 1}, outbox_topic="sample"
    )
    assert (
        emit_event(
            database,
            context,
            "sample.created",
            "key",
            correlation,
            {"value": 1},
            outbox_topic="sample",
        )
        == event_id
    )
    with pytest.raises(ValueError, match="Idempotency key"):
        emit_event(database, context, "sample.created", "key", correlation, {"value": 2})
    assert (
        database.execute(
            "SELECT count(*) FROM outbox_jobs WHERE organization_id = %s",
            (context.organization_id,),
        ).fetchone()[0]
        == 1
    )
    job = lease_job(database, context, "sample")
    assert job["attempts"] == 1
    assert lease_job(database, context, "sample") is None
    database.execute(
        "UPDATE outbox_jobs SET leased_until = now() - interval '1 second' WHERE id = %s",
        (job["id"],),
    )
    reclaimed = lease_job(database, context, "sample")
    assert reclaimed["id"] == job["id"] and reclaimed["attempts"] == 2
    finish_job(database, context, job["id"], False)
    failed_reviews = list_reviews(database, context)
    assert failed_reviews[0]["trigger"] == "failed_run"
    assert failed_reviews[0]["owner_user_id"] == context.actor.user_id
    database.execute("UPDATE outbox_jobs SET available_at = now() WHERE id = %s", (job["id"],))
    retried = lease_job(database, context, "sample")
    assert retried["id"] == job["id"] and retried["attempts"] == 3
    finish_job(database, context, job["id"], True)
    assert lease_job(database, context, "sample") is None
    with pytest.raises(psycopg.errors.RaiseException), database.transaction():
        database.execute("DELETE FROM runtime_events WHERE id = %s", (event_id,))

    record_path = tmp_path / "records" / "sample" / "one.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(json.dumps({"id": "one", "value": 5}), encoding="utf-8")
    department_id = database.execute(
        "INSERT INTO departments (organization_id, slug, name) "
        "VALUES (%s, 'sample', 'Sample') RETURNING id",
        (context.organization_id,),
    ).fetchone()[0]
    database.execute(
        "INSERT INTO business_objects (organization_id, department_id, kind, external_id, "
        "source_path) VALUES (%s, %s, 'record', 'one', %s)",
        (context.organization_id, department_id, record_path.relative_to(tmp_path).as_posix()),
    )
    assert record_legacy_inventory(database, context.organization_id, tmp_path) == {"record": 1}
    assert record_legacy_inventory(database, context.organization_id, tmp_path) == {}
    assert (
        database.execute(
            "SELECT sha256 FROM legacy_inventory WHERE organization_id = %s",
            (context.organization_id,),
        ).fetchone()[0]
        == hashlib.sha256(record_path.read_bytes()).hexdigest()
    )
    assert read_records(database, context, department_id, tmp_path) == [{"id": "one", "value": 5}]
    other, _ = make_organization(database, "other-record")
    assert read_records(database, other, department_id, tmp_path) == []


def test_feature_index_and_skill_loader_preserve_platform_behavior():
    root = Path(__file__).parents[1]
    entries = json.loads((root / "src/austra/feature_index.json").read_text(encoding="utf-8"))
    assert len({item["feature"] for item in entries}) == len(entries)
    for item in entries:
        for path in item["public_entrypoints"] + item["legacy_seams"]:
            assert (root / "src/austra" / path).is_file()
        for path in item["tests"]:
            assert (root / path).is_file()
    assert tuple(card.skill_id for card in skill_cards()) == PLATFORM_SKILLS
    assert load_skills() == "\n\n".join(load_skill(name) for name in PLATFORM_SKILLS)
    assert all(len(card.content_hash) == 64 for card in skill_cards())
