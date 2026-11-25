"""Run with AUSTRA_TEST_DATABASE_URL against a disposable PostgreSQL database."""

import hashlib
import os
from importlib.resources import files
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from austra.storage.identity import resolve_session
from austra.storage.migrate import MigrationError, apply_migrations
from austra.storage.schema_contract import SchemaContractError
from austra.tenant.permissions.evaluator import permits


@pytest.fixture
def database():
    url = os.environ.get("AUSTRA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("AUSTRA_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    schema = f"rbac_test_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            with psycopg.connect(url, options=f"-c search_path={schema}") as connection:
                yield connection
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_candidate_order_migration_repairs_legacy_table_and_backfills_profile(database):
    apply_migrations(database)
    organization_id = database.execute(
        "INSERT INTO organizations (slug, name) VALUES ('legacy', 'Legacy') RETURNING id"
    ).fetchone()[0]
    user_id = database.execute(
        "INSERT INTO users (organization_id, username_normalized, role, password_hash) "
        "VALUES (%s, 'admin', 'org_admin', 'test') RETURNING id",
        (organization_id,),
    ).fetchone()[0]
    case_id = database.execute(
        "INSERT INTO onboarding_cases (organization_id, created_event_id, desired_outcome) "
        "VALUES (%s, %s, 'working summary') RETURNING id",
        (organization_id, uuid4()),
    ).fetchone()[0]
    database.execute(
        "INSERT INTO organization_tours (organization_id, about_organization, "
        "desired_outcome, completed_at, discovery_state) "
        "VALUES (%s, 'Legacy makes parts', 'reduce delays', now(), "
        "'{\"working_summary\":\"working summary\"}'::jsonb)",
        (organization_id,),
    )
    database.execute(
        "INSERT INTO blueprint_candidates (organization_id, case_id, proposal, "
        "content_hash, proposed_by) VALUES (%s, %s, '{}'::jsonb, %s, %s)",
        (organization_id, case_id, "a" * 64, user_id),
    )
    database.execute(
        "INSERT INTO onboarding_messages (organization_id, position, role, body) "
        "VALUES (%s, 1, 'user', 'Hi, I''m John and I run operations')",
        (organization_id,),
    )
    database.execute("DROP TABLE organization_profile_revisions")
    database.execute("ALTER TABLE blueprint_candidates DROP COLUMN candidate_order")
    database.execute("ALTER TABLE claim_decisions DROP COLUMN decision_order")
    database.execute(
        "DELETE FROM schema_migrations WHERE name = '021_profile_memory_and_candidate_order.sql'"
    )
    database.execute(
        "DELETE FROM schema_migrations WHERE name = '022_backfill_introduced_admin_names.sql'"
    )
    database.execute(
        "DELETE FROM schema_migrations WHERE name = '023_claim_decision_order.sql'"
    )
    assert apply_migrations(database) == [
        "021_profile_memory_and_candidate_order.sql",
        "022_backfill_introduced_admin_names.sql",
        "023_claim_decision_order.sql",
    ]
    assert database.execute(
        "SELECT candidate_order FROM blueprint_candidates WHERE case_id = %s", (case_id,)
    ).fetchone()[0] == 1
    assert database.execute(
        "SELECT count(*) FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = 'claim_decisions' AND column_name = 'decision_order'"
    ).fetchone()[0] == 1
    assert database.execute(
        "SELECT desired_outcome FROM onboarding_cases WHERE id = %s", (case_id,)
    ).fetchone()[0] == "reduce delays"
    assert database.execute(
        "SELECT organization_name, about_organization, source "
        "FROM organization_profile_revisions WHERE organization_id = %s",
        (organization_id,),
    ).fetchone() == ("Legacy", "Legacy makes parts", "legacy_backfill")
    assert database.execute(
        "SELECT full_name FROM users WHERE id = %s", (user_id,)
    ).fetchone()[0] == "John"


def test_upgrade_separates_existing_named_drafts_without_changing_hashes(database):
    scripts = sorted(files("austra.storage.migrations").iterdir(), key=lambda p: p.name)
    upgrade = next(p for p in scripts if p.name.startswith("027_"))
    database.execute("CREATE TABLE schema_migrations (name text PRIMARY KEY, applied_at timestamptz DEFAULT now())")
    for script in scripts:
        if script.name.endswith(".sql") and script.name < upgrade.name:
            database.execute(script.read_text(encoding="utf-8"))
    org = database.execute("INSERT INTO organizations (slug, name) VALUES ('old', 'Old') RETURNING id").fetchone()[0]
    user = database.execute(
        "INSERT INTO users (organization_id, username_normalized, role, password_hash) "
        "VALUES (%s, 'admin', 'org_admin', 'fixture') RETURNING id", (org,),
    ).fetchone()[0]
    original = database.execute(
        "INSERT INTO onboarding_cases (organization_id, created_event_id) VALUES (%s, %s) RETURNING id",
        (org, uuid4()),
    ).fetchone()[0]
    hashes = {}
    for number, name in enumerate(("Allocation", "Quality Review", "Allocation"), start=1):
        digest = str(number) * 64
        candidate = database.execute(
            "INSERT INTO blueprint_candidates (organization_id, case_id, proposal, content_hash, proposed_by) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (org, original, psycopg.types.json.Jsonb({"name": name, "revision_ids": []}), digest, user),
        ).fetchone()[0]
        hashes[candidate] = digest
    database.execute(upgrade.read_text(encoding="utf-8"))
    rows = database.execute(
        "SELECT b.id, b.content_hash, b.case_id, c.workflow_name FROM blueprint_candidates b "
        "JOIN onboarding_cases c ON (b.organization_id, b.case_id) = (c.organization_id, c.id) "
        "ORDER BY b.candidate_order"
    ).fetchall()
    assert {r[0]: r[1] for r in rows} == hashes
    assert rows[0][2] == rows[2][2] == original
    assert rows[1][2] != original and rows[1][3] == "Quality Review"


def test_versioned_migrations_detect_checksum_drift(database):
    apply_migrations(database)
    database.execute(
        "UPDATE schema_migrations SET checksum = %s WHERE version = 25", ("0" * 64,)
    )
    with pytest.raises(MigrationError, match="025_evidence_review_state.sql"):
        apply_migrations(database)


def test_orm_schema_contract_detects_missing_column(database):
    apply_migrations(database)
    database.execute("ALTER TABLE blueprint_candidates DROP COLUMN candidate_order")
    with pytest.raises(SchemaContractError, match="blueprint_candidates.candidate_order"):
        apply_migrations(database)


def test_repair_missing_defect_patch_link_on_baselined_database(database):
    apply_migrations(database)
    database.execute("ALTER TABLE defects DROP COLUMN patch_build_id")
    with pytest.raises(SchemaContractError, match="defects.patch_build_id"):
        apply_migrations(database)
    database.execute("DELETE FROM schema_migrations WHERE version = 28")
    assert apply_migrations(database) == ["028_defect_patch_build_scope.sql"]
    assert database.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'defects'::regclass AND conname = 'defects_patch_build_scope'"
    ).fetchone()[0] == "FOREIGN KEY (organization_id, patch_build_id) REFERENCES builds(organization_id, id)"


def test_migration_session_scope_and_revocation(database):
    assert apply_migrations(database) == [
        "001_identity.sql",
        "002_business_catalog.sql",
        "003_organization_contacts.sql",
        "004_proactive_foundation.sql",
        "005_evidence_intake.sql",
        "006_evidence_revision_identity.sql",
        "007_semantic_blueprints.sql",
        "008_retrieval.sql",
        "009_retrieval_rls.sql",
        "010_capability_runtime.sql",
        "011_connections.sql",
        "012_capability_descriptors.sql",
        "013_proactive_monitoring.sql",
        "014_guided_tour.sql",
        "015_model_led_onboarding.sql",
        "016_discovery_state.sql",
        "017_social_page_status.sql",
        "018_conversation_memory.sql",
        "019_memory_compaction_columns.sql",
        "020_nullable_memory_summary.sql",
        "021_profile_memory_and_candidate_order.sql",
        "022_backfill_introduced_admin_names.sql",
        "023_claim_decision_order.sql",
        "024_versioned_migration_history.sql",
        "025_evidence_review_state.sql",
        "026_admin_evidence_review.sql",
        "027_workflow_cases.sql",
        "028_defect_patch_build_scope.sql",
        "029_gmail_reply_tracking.sql",
        "030_workflow_chat_context.sql",
        "031_workflow_mail_processing.sql",
        "032_workflow_recovery.sql",
        "033_department_plans_and_automatic_replies.sql",
        "034_automatic_reply_recovery.sql",
    ]
    assert apply_migrations(database) == []
    organization_id = database.execute(
        "INSERT INTO organizations (slug, name) VALUES ('acme', 'Acme') RETURNING id"
    ).fetchone()[0]
    other_organization_id = database.execute(
        "INSERT INTO organizations (slug, name) VALUES ('other', 'Other') RETURNING id"
    ).fetchone()[0]
    other_user_id = database.execute(
        """INSERT INTO users (organization_id, username_normalized, role, password_hash)
           VALUES (%s, 'alex', 'employee', 'test-hash') RETURNING id""",
        (other_organization_id,),
    ).fetchone()[0]
    user_id = database.execute(
        """INSERT INTO users (organization_id, username_normalized, role,
                              password_hash, must_change_password)
           VALUES (%s, 'alex', 'employee', 'test-hash', false) RETURNING id""",
        (organization_id,),
    ).fetchone()[0]
    token = uuid4().hex
    database.execute(
        "INSERT INTO sessions (user_id, token_hash, expires_at) "
        "VALUES (%s, %s, now() + interval '1 hour')",
        (user_id, hashlib.sha256(token.encode()).hexdigest()),
    )
    department_id = database.execute(
        "INSERT INTO departments (organization_id, slug, name) "
        "VALUES (%s, 'sales', 'Sales') RETURNING id",
        (organization_id,),
    ).fetchone()[0]
    membership_id = database.execute(
        "INSERT INTO department_memberships (organization_id, department_id, user_id) "
        "VALUES (%s, %s, %s) RETURNING id",
        (organization_id, department_id, user_id),
    ).fetchone()[0]
    role_id = database.execute(
        "INSERT INTO department_roles (organization_id, department_id, slug, name, "
        "current_version) VALUES (%s, %s, 'reader', 'Reader', 1) RETURNING id",
        (organization_id, department_id),
    ).fetchone()[0]
    version_id = database.execute(
        "INSERT INTO role_versions (role_id, version, change_reason, approved_by) "
        "VALUES (%s, 1, 'Initial access', %s) RETURNING id",
        (role_id, user_id),
    ).fetchone()[0]
    database.execute(
        "INSERT INTO role_permissions (role_version_id, permission_key) "
        "VALUES (%s, 'records.read')",
        (version_id,),
    )
    database.execute(
        "INSERT INTO role_assignments (organization_id, department_id, "
        "department_membership_id, role_id, assigned_by) VALUES (%s, %s, %s, %s, %s)",
        (organization_id, department_id, membership_id, role_id, user_id),
    )

    context = resolve_session(database, token)
    assert context is not None
    assert permits(context, organization_id, department_id, "records.read")
    assert not permits(context, other_organization_id, department_id, "records.read")


    assert resolve_session(database, "invalid-token") is None

    with pytest.raises(psycopg.errors.ForeignKeyViolation), database.transaction():
        database.execute(
            "INSERT INTO department_memberships (organization_id, department_id, user_id) "
            "VALUES (%s, %s, %s)",
            (other_organization_id, department_id, other_user_id),
        )

    database.execute("UPDATE departments SET archived_at = now() WHERE id = %s", (department_id,))
    archived_context = resolve_session(database, token)
    assert archived_context is not None
    assert not permits(archived_context, organization_id, department_id, "records.read")

    database.execute("UPDATE sessions SET revoked_at = now() WHERE user_id = %s", (user_id,))
    assert resolve_session(database, token) is None


def test_memory_compaction_migration_repairs_previously_applied_schema(database):
    apply_migrations(database)
    database.execute(
        "ALTER TABLE conversation_memory_summaries "
        "DROP COLUMN working_content, DROP COLUMN compacted_at, "
        "DROP COLUMN compacted_message_count"
    )
    database.execute(
        "DELETE FROM schema_migrations WHERE name = '019_memory_compaction_columns.sql'"
    )
    organization_id = database.execute(
        "INSERT INTO organizations (slug, name) VALUES ('legacy', 'Legacy') RETURNING id"
    ).fetchone()[0]
    user_id = database.execute(
        "INSERT INTO users (organization_id, username_normalized, role, password_hash) "
        "VALUES (%s, 'legacy', 'employee', 'fixture') RETURNING id",
        (organization_id,),
    ).fetchone()[0]
    database.execute(
        "INSERT INTO conversation_memory_summaries "
        "(organization_id, owner_user_id, chat_kind, chat_id, source_message_id, content) "
        "VALUES (%s, %s, 'onboarding', %s, %s, 'Existing summary')",
        (organization_id, user_id, uuid4(), uuid4()),
    )

    assert apply_migrations(database) == ["019_memory_compaction_columns.sql"]
    assert database.execute(
        "SELECT content, working_content, compacted_at, compacted_message_count "
        "FROM conversation_memory_summaries"
    ).fetchone() == ("Existing summary", "", None, 0)


def test_nullable_memory_summary_migration_repairs_existing_schema(database):
    apply_migrations(database)
    database.execute("ALTER TABLE conversation_memory_summaries ALTER COLUMN content SET NOT NULL")
    database.execute(
        "DELETE FROM schema_migrations WHERE name = '020_nullable_memory_summary.sql'"
    )

    assert apply_migrations(database) == ["020_nullable_memory_summary.sql"]
    assert database.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "AND table_name = 'conversation_memory_summaries' AND column_name = 'content'"
    ).fetchone() == ("YES",)
