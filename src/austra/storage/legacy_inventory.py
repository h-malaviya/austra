"""Immutable pre-migration file counts and checksums for the legacy catalog."""

import hashlib
from pathlib import Path
from uuid import UUID

import psycopg


def record_legacy_inventory(
    connection: psycopg.Connection, organization_id: UUID, root: Path
) -> dict[str, int]:
    rows = connection.execute(
        "SELECT kind, source_path FROM business_objects WHERE organization_id = %s "
        "AND archived_at IS NULL",
        (organization_id,),
    ).fetchall()
    with connection.transaction():
        connection.execute("SET LOCAL ROLE austra_tenant_app")
        connection.execute(
            "SELECT set_config('austra.organization_id', %s, true), "
            "set_config('austra.can_write', 'on', true), "
            "set_config('austra.all_departments', 'on', true)",
            (str(organization_id),),
        )
        counts: dict[str, int] = {}
        resolved_root = root.resolve()
        for kind, source_path in rows:
            path = (resolved_root / source_path).resolve()
            if not path.is_relative_to(resolved_root) or not path.is_file():
                continue
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            result = connection.execute(
                "INSERT INTO legacy_inventory (organization_id, source_path, kind, byte_count, sha256) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING source_path",
                (organization_id, source_path, kind, path.stat().st_size, digest.hexdigest()),
            ).fetchone()
            if result is not None:
                counts[kind] = counts.get(kind, 0) + 1
        connection.execute("RESET ROLE")
        connection.execute("RESET austra.organization_id")
        connection.execute("RESET austra.can_write")
        connection.execute("RESET austra.all_departments")
    return counts
