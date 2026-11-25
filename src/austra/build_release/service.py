"""Build revisions, reproducible defects, release, and rollback."""

from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from austra.tenant.context import TenantContext, tenant_transaction

BUILD_TRANSITIONS = {
    "building": {"testing"},
    "testing": {"admin_preview", "defect_found"},
    "admin_preview": {"accepted", "defect_found"},
    "defect_found": {"fixing"},
    "fixing": {"testing"},
    "accepted": {"ready_for_release"},
    "ready_for_release": {"released"},
    "released": {"rolled_back"},
    "rolled_back": set(),
}


def _transition(
    connection: psycopg.Connection,
    context: TenantContext,
    build_id: UUID,
    target: str,
    expected_revision: int,
    correlation_id: UUID,
) -> tuple[UUID, int]:
    row = connection.execute(
        "SELECT blueprint_id, state, revision FROM builds WHERE organization_id = %s "
        "AND id = %s FOR UPDATE",
        (context.organization_id, build_id),
    ).fetchone()
    if row is None:
        raise LookupError("Build not found")
    blueprint_id, state, revision = row
    if revision != expected_revision:
        raise ValueError("Stale build revision")
    if target not in BUILD_TRANSITIONS[state]:
        raise ValueError(f"Illegal build transition: {state} -> {target}")
    if target in {"accepted", "ready_for_release", "released"}:
        open_defect = connection.execute(
            "SELECT 1 FROM defects WHERE organization_id = %s AND build_id = %s "
            "AND state = 'open' LIMIT 1",
            (context.organization_id, build_id),
        ).fetchone()
        if open_defect:
            raise ValueError("Open defects block release")
    connection.execute(
        "UPDATE builds SET state = %s, revision = revision + 1, updated_at = now() "
        "WHERE organization_id = %s AND id = %s",
        (target, context.organization_id, build_id),
    )
    connection.execute(
        "INSERT INTO lifecycle_events (organization_id, actor_user_id, resource_type, "
        "resource_id, from_state, to_state, correlation_id) "
        "VALUES (%s, %s, 'build', %s, %s, %s, %s)",
        (context.organization_id, context.actor.user_id, build_id, state, target, correlation_id),
    )
    return blueprint_id, revision + 1


def create_build(
    connection: psycopg.Connection, context: TenantContext, blueprint_id: UUID
) -> dict:
    context.require("workflows.approve")
    with tenant_transaction(connection, context):
        blueprint = connection.execute(
            "SELECT status FROM blueprints WHERE organization_id = %s AND id = %s FOR UPDATE",
            (context.organization_id, blueprint_id),
        ).fetchone()
        if blueprint is None:
            raise LookupError("Blueprint not found")
        compiled = connection.execute(
            "SELECT 1 FROM blueprint_manifests WHERE organization_id = %s AND blueprint_id = %s",
            (context.organization_id, blueprint_id),
        ).fetchone()
        if compiled is None:
            raise ValueError("A confirmed, compiled blueprint is required")
        if blueprint[0] == "superseded":
            raise ValueError("Superseded blueprint cannot be built")
        if blueprint[0] == "candidate":
            locked = connection.execute(
                "SELECT 1 FROM onboarding_cases WHERE organization_id = %s "
                "AND blueprint_id = %s AND state = 'locked_candidate'",
                (context.organization_id, blueprint_id),
            ).fetchone()
            if locked is None:
                raise ValueError("Blueprint candidate is not locked")
        revision = connection.execute(
            "SELECT coalesce(max(build_revision), 0) + 1 FROM builds "
            "WHERE organization_id = %s AND blueprint_id = %s",
            (context.organization_id, blueprint_id),
        ).fetchone()[0]
        build_id = connection.execute(
            "INSERT INTO builds (organization_id, blueprint_id, build_revision) "
            "VALUES (%s, %s, %s) RETURNING id",
            (context.organization_id, blueprint_id, revision),
        ).fetchone()[0]
    return {"id": build_id, "blueprint_id": blueprint_id, "build_revision": revision}


def transition_build(
    connection: psycopg.Connection,
    context: TenantContext,
    build_id: UUID,
    target: str,
    expected_revision: int,
    correlation_id: UUID,
) -> int:
    context.require("workflows.approve")
    if target in {"defect_found", "released", "rolled_back"}:
        raise ValueError("Use defect, publish, or rollback operation")
    with tenant_transaction(connection, context):
        if target in {"admin_preview", "accepted", "ready_for_release"}:
            patch = connection.execute(
                "SELECT 1 FROM defects WHERE organization_id = %s AND patch_build_id = %s LIMIT 1",
                (context.organization_id, build_id),
            ).fetchone()
            artifact = connection.execute(
                "SELECT verification, preview_state FROM capability_build_artifacts "
                "WHERE organization_id = %s AND build_id = %s",
                (context.organization_id, build_id),
            ).fetchone()
            if patch and artifact is None:
                raise ValueError("Patch build needs independent verification")
            if artifact and (not artifact[0] or not artifact[0].get("passed")):
                raise ValueError("Independent capability verification has not passed")
            if (
                artifact
                and target in {"accepted", "ready_for_release"}
                and artifact[1] != "accepted"
            ):
                raise ValueError("Admin preview has not been accepted")
        _blueprint_id, revision = _transition(
            connection, context, build_id, target, expected_revision, correlation_id
        )
    return revision


def record_defect(
    connection: psycopg.Connection,
    context: TenantContext,
    build_id: UUID,
    expected_revision: int,
    expected: str,
    observed: str,
    owner_user_id: UUID,
    correlation_id: UUID,
    trace: dict | None = None,
) -> UUID:
    context.require("workflows.approve")
    if not expected.strip() or not observed.strip():
        raise ValueError("Expected and observed behavior are required")
    with tenant_transaction(connection, context):
        _transition(
            connection, context, build_id, "defect_found", expected_revision, correlation_id
        )
        defect_id = connection.execute(
            "INSERT INTO defects (organization_id, build_id, expected, observed, owner_user_id, trace) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (
                context.organization_id,
                build_id,
                expected,
                observed,
                owner_user_id,
                Jsonb(trace or {}),
            ),
        ).fetchone()[0]
    return defect_id


def preview_build(
    connection: psycopg.Connection,
    context: TenantContext,
    build_id: UUID,
    expected_revision: int,
    accepted: bool,
    expected: str | None = None,
    observed: str | None = None,
    trace: dict | None = None,
) -> dict:
    context.require("workflows.approve")
    if not accepted and (not expected or not observed):
        raise ValueError("Failed preview requires expected and observed results")
    with tenant_transaction(connection, context):
        row = connection.execute(
            "SELECT b.state, b.revision, a.verification FROM builds b "
            "JOIN capability_build_artifacts a ON a.build_id = b.id "
            "WHERE b.organization_id = %s AND b.id = %s FOR UPDATE OF b",
            (context.organization_id, build_id),
        ).fetchone()
        if row is None or row[0] != "admin_preview" or row[1] != expected_revision:
            raise ValueError("Current admin preview revision required")
        if not row[2] or not row[2].get("passed"):
            raise ValueError("Independent verification must pass before preview")
        connection.execute(
            "UPDATE capability_build_artifacts SET preview_state = %s, preview_by = %s, "
            "preview_at = now() WHERE organization_id = %s AND build_id = %s",
            (
                "accepted" if accepted else "failed",
                context.actor.user_id,
                context.organization_id,
                build_id,
            ),
        )
    if accepted:
        return {"accepted": True, "build_id": build_id}
    defect_id = record_defect(
        connection,
        context,
        build_id,
        expected_revision,
        expected,
        observed,
        context.actor.user_id,
        uuid4(),
        trace,
    )
    return {"accepted": False, "defect_id": defect_id, "build_id": build_id}


def fix_defect(
    connection: psycopg.Connection,
    context: TenantContext,
    defect_id: UUID,
    expected_revision: int,
    correlation_id: UUID,
) -> dict:
    context.require("workflows.approve")
    with tenant_transaction(connection, context):
        row = connection.execute(
            "SELECT build_id FROM defects WHERE organization_id = %s AND id = %s "
            "AND state = 'open' FOR UPDATE",
            (context.organization_id, defect_id),
        ).fetchone()
        if row is None:
            raise LookupError("Open defect not found")
        build = connection.execute(
            "SELECT blueprint_id, build_revision, state, revision FROM builds "
            "WHERE organization_id = %s AND id = %s FOR UPDATE",
            (context.organization_id, row[0]),
        ).fetchone()
        if build[2] != "defect_found" or build[3] != expected_revision:
            raise ValueError("Build is not ready for fixing")
        next_revision = build[1] + 1
        new_build_id = connection.execute(
            "INSERT INTO builds (organization_id, blueprint_id, build_revision, state) "
            "VALUES (%s, %s, %s, 'fixing') RETURNING id",
            (context.organization_id, build[0], next_revision),
        ).fetchone()[0]
        connection.execute(
            "UPDATE defects SET state = 'fixed', fixed_at = now() "
            "WHERE organization_id = %s AND id = %s",
            (context.organization_id, defect_id),
        )
        connection.execute(
            "INSERT INTO lifecycle_events (organization_id, actor_user_id, resource_type, "
            "resource_id, from_state, to_state, correlation_id) "
            "VALUES (%s, %s, 'build', %s, 'defect_found', 'fixing', %s)",
            (context.organization_id, context.actor.user_id, new_build_id, correlation_id),
        )
    return {"id": new_build_id, "blueprint_id": build[0], "build_revision": next_revision}


def workflow_keys(manifest: dict) -> set[tuple[str, str]]:
    keys = {("projection", view["key"]) for view in manifest.get("projections", [])}
    if manifest.get("engine") == "state_machine":
        for cap in manifest["capabilities"]:
            keys.update({("capability", cap["key"]), ("record", cap["record_kind"])})
    else:
        keys.update(("record", kind) for kind in ("sales_order", "stock_lot", "purchase_order", "email_proposal"))
    return keys


def publish(
    connection: psycopg.Connection,
    context: TenantContext,
    build_id: UUID,
    expected_revision: int,
    correlation_id: UUID,
) -> UUID:
    context.require("workflows.approve")
    with tenant_transaction(connection, context):
        gate = connection.execute(
            "SELECT p.content_hash, m.manifest, a.blueprint_hash, a.verification, "
            "a.preview_state, a.rollback_plan, a.manifest FROM builds b "
            "JOIN blueprints p ON p.id = b.blueprint_id "
            "JOIN blueprint_manifests m ON m.blueprint_id = p.id "
            "LEFT JOIN capability_build_artifacts a ON a.build_id = b.id "
            "WHERE b.organization_id = %s AND b.id = %s",
            (context.organization_id, build_id),
        ).fetchone()
        if gate is None:
            raise LookupError("Build not found")
        patch = connection.execute(
            "SELECT 1 FROM defects WHERE organization_id = %s AND patch_build_id = %s LIMIT 1",
            (context.organization_id, build_id),
        ).fetchone()
        if patch and (
            gate[0] != gate[2]
            or not gate[3]
            or not gate[3].get("passed")
            or gate[4] != "accepted"
            or not gate[5]
        ):
            raise ValueError("Patch release gates have not passed")
        if any(item.get("kind") == "capability" for item in gate[1].get("revisions", [])):
            if (
                gate[0] != gate[2]
                or not gate[3]
                or not gate[3].get("passed")
                or gate[4] != "accepted"
                or not gate[5]
            ):
                raise ValueError("Capability release gates have not passed")
            if any(
                item.get("key") == "so_inventory_po_email"
                for item in gate[1].get("revisions", [])
                if item.get("kind") == "capability"
            ):
                healthy = connection.execute(
                    "SELECT state FROM capability_connection_health "
                    "WHERE organization_id = %s AND provider_key = 'email'",
                    (context.organization_id,),
                ).fetchone()
                if healthy is None or healthy[0] != "healthy":
                    raise ValueError("Required email connection is not healthy")
        blueprint_id, _revision = _transition(
            connection, context, build_id, "released", expected_revision, correlation_id
        )
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                           (str(context.organization_id),))
        scope = connection.execute(
            "SELECT CASE WHEN workflow_name IS NOT NULL THEN coalesce(workflow_root_id, id) END "
            "FROM onboarding_cases WHERE organization_id = %s AND blueprint_id = %s",
            (context.organization_id, blueprint_id),
        ).fetchone()
        lineage = scope[0] if scope else None
        # Preserve the rollback chain of builds published before workflow-specific releases.
        if connection.execute(
            "SELECT 1 FROM releases WHERE organization_id = %s AND blueprint_id = %s "
            "AND workflow_case_id IS NULL LIMIT 1", (context.organization_id, blueprint_id),
        ).fetchone():
            lineage = None
        if gate[6]:
            neighbors = connection.execute(
                "SELECT DISTINCT a.manifest FROM releases r JOIN capability_build_artifacts a "
                "ON (a.organization_id, a.build_id) = (r.organization_id, r.build_id) "
                "WHERE r.organization_id = %s AND r.workflow_case_id IS DISTINCT FROM %s",
                (context.organization_id, lineage),
            ).fetchall()
            keys = workflow_keys(gate[6])
            if any(keys & workflow_keys(row[0]) for row in neighbors):
                raise ValueError("A different workflow owns a capability, record kind or view with the same key")
        previous = connection.execute(
            "SELECT id, blueprint_id FROM releases WHERE organization_id = %s "
            "AND workflow_case_id IS NOT DISTINCT FROM %s AND state = 'active' FOR UPDATE",
            (context.organization_id, lineage),
        ).fetchone()
        if previous:
            connection.execute(
                "UPDATE releases SET state = 'rolled_back', rolled_back_at = now() "
                "WHERE organization_id = %s AND id = %s",
                (context.organization_id, previous[0]),
            )
            if previous[1] != blueprint_id:
                connection.execute(
                    "UPDATE blueprints SET status = 'superseded' "
                    "WHERE organization_id = %s AND id = %s",
                    (context.organization_id, previous[1]),
                )
        connection.execute(
            "UPDATE blueprints SET status = 'active' WHERE organization_id = %s AND id = %s",
            (context.organization_id, blueprint_id),
        )
        release_id = connection.execute(
            "INSERT INTO releases (organization_id, blueprint_id, build_id, "
            "rollback_release_id, approved_by, workflow_case_id) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (
                context.organization_id,
                blueprint_id,
                build_id,
                previous[0] if previous else None,
                context.actor.user_id, lineage,
            ),
        ).fetchone()[0]
        connection.execute(
            "UPDATE defects SET state = 'fixed', fixed_at = now() WHERE organization_id = %s "
            "AND patch_build_id = %s AND state = 'open'",
            (context.organization_id, build_id),
        )
    return release_id


def rollback(
    connection: psycopg.Connection,
    context: TenantContext,
    release_id: UUID,
    correlation_id: UUID,
) -> UUID:
    context.require("workflows.approve")
    with tenant_transaction(connection, context):
        row = connection.execute(
            "SELECT build_id, blueprint_id, rollback_release_id FROM releases "
            "WHERE organization_id = %s AND id = %s AND state = 'active' FOR UPDATE",
            (context.organization_id, release_id),
        ).fetchone()
        if row is None or row[2] is None:
            raise ValueError("No rollback release available")
        build_id, blueprint_id, previous_id = row
        revision = connection.execute(
            "SELECT revision FROM builds WHERE organization_id = %s AND id = %s",
            (context.organization_id, build_id),
        ).fetchone()[0]
        _transition(connection, context, build_id, "rolled_back", revision, correlation_id)
        connection.execute(
            "UPDATE releases SET state = 'rolled_back', rolled_back_at = now() "
            "WHERE organization_id = %s AND id = %s",
            (context.organization_id, release_id),
        )
        previous_blueprint = connection.execute(
            "UPDATE releases SET state = 'active', rolled_back_at = NULL "
            "WHERE organization_id = %s AND id = %s RETURNING blueprint_id",
            (context.organization_id, previous_id),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO lifecycle_events (organization_id, actor_user_id, resource_type, "
            "resource_id, from_state, to_state, correlation_id) "
            "VALUES (%s, %s, 'release', %s, 'active', 'rolled_back', %s)",
            (context.organization_id, context.actor.user_id, release_id, correlation_id),
        )
        connection.execute(
            "INSERT INTO lifecycle_events (organization_id, actor_user_id, resource_type, "
            "resource_id, from_state, to_state, correlation_id) "
            "VALUES (%s, %s, 'release', %s, 'rolled_back', 'active', %s)",
            (context.organization_id, context.actor.user_id, previous_id, correlation_id),
        )
        if blueprint_id != previous_blueprint:
            connection.execute(
                "UPDATE blueprints SET status = 'superseded' WHERE organization_id = %s "
                "AND id = %s",
                (context.organization_id, blueprint_id),
            )
            connection.execute(
                "UPDATE blueprints SET status = 'active' WHERE organization_id = %s AND id = %s",
                (context.organization_id, previous_blueprint),
            )
    return previous_id
