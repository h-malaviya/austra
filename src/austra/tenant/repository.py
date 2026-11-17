"""Tenant checked access to the legacy business catalog and its file paths."""

import json
from pathlib import Path
from uuid import UUID

import psycopg

from austra.tenant.context import TenantContext
from austra.tenant.permissions.evaluator import permits
from austra.tenant.permissions.keys import DEPARTMENT_PERMISSIONS


def list_objects(
    connection: psycopg.Connection, context: TenantContext, department_id: UUID
) -> list[tuple[str, str, str]]:
    if not any(
        permits(context.actor, context.organization_id, department_id, permission)
        for permission in DEPARTMENT_PERMISSIONS
    ):
        raise PermissionError("Department access required")
    return connection.execute(
        "SELECT kind, external_id, source_path FROM business_objects "
        "WHERE organization_id = %s AND department_id = %s AND archived_at IS NULL "
        "ORDER BY kind, external_id",
        (context.organization_id, department_id),
    ).fetchall()


def read_records(
    connection: psycopg.Connection, context: TenantContext, department_id: UUID, root: Path
) -> list[dict]:
    context.require("records.read", department_id)
    paths = connection.execute(
        "SELECT source_path FROM business_objects WHERE organization_id = %s "
        "AND department_id = %s AND kind = 'record' AND archived_at IS NULL",
        (context.organization_id, department_id),
    ).fetchall()
    resolved_root = root.resolve()
    records = []
    for (source_path,) in paths:
        path = (resolved_root / source_path).resolve()
        if not path.is_relative_to(resolved_root):
            raise ValueError("Invalid catalog path")
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        if not item.get("deleted_at"):
            records.append(item)
    return records
