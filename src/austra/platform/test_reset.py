"""Reset one explicitly enabled test organization to its initial onboarding state."""

from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg import sql

from austra.integrations.model_credentials import ANTHROPIC_CONNECTION_ID
from austra.onboarding.service import open_case

RESET_ORDER = (
    "audit_events", "blueprint_candidates", "blueprint_edges", "blueprint_manifests",
    "blueprint_members", "business_objects", "capability_action_receipts",
    "capability_build_artifacts", "capability_command_results", "capability_connection_health",
    "capability_record_revisions", "claim_conflicts", "claim_decisions",
    "conversation_memory_notes", "conversation_memory_summaries", "defects",
    "department_join_requests", "department_workflow_plans", "evidence_jobs", "integration_executions",
    "integration_webhook_deliveries", "legacy_inventory", "lifecycle_events",
    "onboarding_messages", "operational_metrics", "organization_chat_messages",
    "organization_profile_revisions", "organization_tours", "releases", "review_cases", "role_assignment_rules",
    "role_assignments", "role_permissions", "semantic_revision_claims",
    "workflow_notifications", "workflow_automatic_replies", "workflow_reply_evidence",
    "semantic_revision_embeddings", "support_contexts", "semantic_edges", "outbox_jobs",
    "capability_records", "source_claims", "department_memberships",
    "integration_action_proposals", "organization_chats", "role_versions",
    "semantic_revisions", "runtime_events", "department_roles", "evidence_chunks",
    "integration_approvals", "sessions", "builds", "evidence_revisions",
    "integration_connections", "semantic_objects", "evidence_sources", "departments",
    "onboarding_cases", "blueprints", "users",
)


def reset_test_organization(
    connection: psycopg.Connection, organization_id: UUID, actor_id: UUID
) -> None:
    """Keep the tenant identity, admin login, and Anthropic credential only."""
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND column_name = 'organization_id'"
        )
    }
    if tables != (set(RESET_ORDER) - {"role_permissions", "role_versions", "sessions"}):
        raise RuntimeError("Test reset table list does not match the database schema")
    with connection.transaction():
        connection.execute("SELECT id FROM organizations WHERE id = %s FOR UPDATE", (organization_id,))
        immutable_triggers = connection.execute(
            "SELECT c.relname, t.tgname FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND NOT t.tgisinternal "
            "AND t.tgname LIKE '%%immutable' AND c.relname = ANY(%s)",
            (list(RESET_ORDER),),
        ).fetchall()
        for table, trigger in immutable_triggers:
            connection.execute(
                sql.SQL("ALTER TABLE {} DISABLE TRIGGER {}").format(
                    sql.Identifier(table), sql.Identifier(trigger)
                )
            )
        for table in RESET_ORDER:
            if table == "role_permissions":
                connection.execute(
                    "DELETE FROM role_permissions WHERE role_version_id IN ("
                    "SELECT v.id FROM role_versions v JOIN department_roles r ON r.id = v.role_id "
                    "WHERE r.organization_id = %s)", (organization_id,)
                )
            elif table == "role_versions":
                connection.execute(
                    "DELETE FROM role_versions WHERE role_id IN ("
                    "SELECT id FROM department_roles WHERE organization_id = %s)",
                    (organization_id,),
                )
            elif table == "sessions":
                connection.execute(
                    "DELETE FROM sessions WHERE user_id IN ("
                    "SELECT id FROM users WHERE organization_id = %s)", (organization_id,)
                )
            elif table == "users":
                connection.execute(
                    "DELETE FROM users WHERE organization_id = %s AND role <> 'org_admin'",
                    (organization_id,),
                )
                connection.execute(
                    "UPDATE users SET full_name = NULL, email = NULL, contact_number = NULL "
                    "WHERE organization_id = %s AND role = 'org_admin'", (organization_id,)
                )
            else:
                connection.execute(
                    sql.SQL("DELETE FROM {} WHERE organization_id = %s").format(
                        sql.Identifier(table)
                    ), (organization_id,)
                )
        for table, trigger in immutable_triggers:
            connection.execute(
                sql.SQL("ALTER TABLE {} ENABLE TRIGGER {}").format(
                    sql.Identifier(table), sql.Identifier(trigger)
                )
            )
        connection.execute(
            "UPDATE organizations SET email = NULL, contact_number = NULL, city = NULL, "
            "country = NULL WHERE id = %s", (organization_id,)
        )
        event_id = connection.execute(
            "INSERT INTO audit_events (organization_id, actor_user_id, channel, action, "
            "resource_id, outcome, reason, correlation_id) "
            "VALUES (%s, %s, 'api', 'onboarding.case_initialized', %s, 'success', "
            "'Platform test reset', %s) RETURNING id",
            (organization_id, actor_id, organization_id, uuid4()),
        ).fetchone()[0]
        open_case(connection, organization_id, event_id)


def clear_test_files(workspace: Path, organization_id: UUID) -> None:
    """Remove tenant artifacts after the database reset has committed."""
    _remove_test_files(workspace / "evidence_blobs" / str(organization_id))
    _remove_test_files(
        workspace / "integration_secrets" / str(organization_id),
        preserve={str(ANTHROPIC_CONNECTION_ID)},
    )


def _remove_test_files(directory: Path, preserve: set[str] | None = None) -> None:
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        if path.name not in (preserve or set()) and (path.is_file() or path.is_symlink()):
            path.unlink()
