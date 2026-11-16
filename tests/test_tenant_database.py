"""Run with AUSTRA_TEST_DATABASE_URL against a disposable PostgreSQL database."""

import hashlib
import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from austra.storage.identity import resolve_session
from austra.storage.migrate import apply_migrations
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


def test_migration_session_scope_and_revocation(database):
    assert apply_migrations(database) == [
        "001_identity.sql",
        "002_business_catalog.sql",
        "003_organization_contacts.sql",
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
