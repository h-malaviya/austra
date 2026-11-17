"""Validated view metadata and SQL-backed, role-scoped operational projections."""

from decimal import Decimal
from uuid import UUID

import psycopg

from austra.tenant.context import TenantContext, tenant_transaction

VIEW_KINDS = {"form", "table", "queue", "chart", "dashboard", "report"}
MAX_ROWS = 200


def compile_projections(revisions: list[dict], definitions: list[dict]) -> list[dict]:
    capabilities = {item["key"]: item for item in definitions}
    descriptors = []
    keys = set()
    for revision in revisions:
        content = revision.get("content", {})
        capability = capabilities.get(content.get("capability"))
        if capability is None:
            continue
        key = revision.get("key")
        kind = content.get("view")
        fields = content.get("fields", [])
        allowed = {item["key"] for item in capability["fields"]} | {"external_id", "state"}
        if (
            not key
            or key in keys
            or kind not in VIEW_KINDS
            or not isinstance(fields, list)
            or not fields
            or any(field not in allowed for field in fields)
            or not content.get("roles")
            or any(role not in {"org_admin", "employee"} for role in content["roles"])
            or any(state not in capability["states"] for state in content.get("states", []))
        ):
            raise ValueError("Invalid projection descriptor")
        measure = content.get("measure")
        field_spec = next((item for item in capability["fields"] if item["key"] == measure), None)
        if kind in {"chart", "dashboard", "report"} and (
            not field_spec
            or field_spec["type"] != "decimal"
            or content.get("unit") != field_spec["unit"]
        ):
            raise ValueError("Aggregate projection needs an approved decimal field and unit")
        if kind == "form" and (
            content.get("transition") not in {item["key"] for item in capability["transitions"]}
        ):
            raise ValueError("Form needs an approved transition")
        keys.add(key)
        descriptors.append(
            {
                "key": key,
                "view": kind,
                "capability": capability["key"],
                "record_kind": capability["record_kind"],
                "fields": fields,
                "roles": content["roles"],
                "states": content.get("states", []),
                "measure": measure,
                "unit": content.get("unit"),
                "transition": content.get("transition"),
            }
        )
    return descriptors


def projection_data(
    connection: psycopg.Connection,
    context: TenantContext,
    department_id: UUID,
    key: str,
    manifest: dict,
    *,
    offset: int = 0,
) -> dict:
    context.require(
        "reports.read"
        if any(
            item.get("key") == key and item.get("view") in {"chart", "dashboard", "report"}
            for item in manifest.get("projections", [])
        )
        else "workflows.read",
        department_id,
    )
    descriptor = next(
        (item for item in manifest.get("projections", []) if item["key"] == key), None
    )
    if descriptor is None or context.actor.role not in descriptor["roles"]:
        raise PermissionError("Projection is unavailable to this role")
    if offset < 0:
        raise ValueError("Offset must be nonnegative")
    states = descriptor["states"]
    with tenant_transaction(connection, context):
        base_where = "organization_id = %s AND department_id = %s AND kind = %s"
        params = [context.organization_id, department_id, descriptor["record_kind"]]
        if states:
            base_where += " AND state = ANY(%s)"
            params.append(states)
        demo = connection.execute(
            f"SELECT count(*) FROM capability_records WHERE {base_where} "
            "AND data->>'source_label' IN ('synthetic_demo', 'demo')",
            params,
        ).fetchone()[0]
        held = connection.execute(
            f"SELECT count(*) FROM capability_records WHERE {base_where} "
            "AND coalesce(data->>'source_label', 'live') NOT IN ('synthetic_demo', 'demo') "
            "AND (coalesce(data->>'verification_status', 'verified') <> 'verified' "
            "OR coalesce(data->>'qc_status', 'released') <> 'released')",
            params,
        ).fetchone()[0]
        where = (
            base_where + " "
            "AND (coalesce(data->>'source_label', 'live') NOT IN ('synthetic_demo', 'demo')) "
            "AND (coalesce(data->>'verification_status', 'verified') = 'verified') "
            "AND (coalesce(data->>'qc_status', 'released') = 'released')"
        )
        count = connection.execute(
            f"SELECT count(*) FROM capability_records WHERE {where}", params
        ).fetchone()[0]
        rows = connection.execute(
            f"SELECT id, external_id, revision, state, data FROM capability_records WHERE {where} "
            "ORDER BY created_at, id LIMIT %s OFFSET %s",
            (*params, MAX_ROWS, offset),
        ).fetchall()
        result = {
            "descriptor": descriptor,
            "record_count": count,
            "excluded_demo_count": demo,
            "excluded_hold_count": held,
            "complete": offset == 0 and len(rows) == count,
            "offset": offset,
            "records": [
                {
                    "id": str(row[0]),
                    "external_id": row[1],
                    "revision": row[2],
                    "state": row[3],
                    "values": {
                        field: row[4].get(field) if field != "state" else row[3]
                        for field in descriptor["fields"]
                    },
                }
                for row in rows
            ],
        }
        if descriptor["measure"]:
            measure = descriptor["measure"]
            # A missing or malformed quantity is an unresolved source, never zero.
            invalid = connection.execute(
                f"SELECT count(*) FROM capability_records WHERE {where} "
                "AND (data->>%s IS NULL OR data->>%s !~ '^[0-9]+(\\.[0-9]{1,6})?$' "
                "OR data->'units'->>%s IS DISTINCT FROM %s)",
                (*params, measure, measure, measure, descriptor["unit"]),
            ).fetchone()[0]
            if invalid:
                result["total"] = None
                result["unresolved_count"] = invalid
            else:
                total = connection.execute(
                    f"SELECT coalesce(sum((data->>%s)::numeric), 0) FROM capability_records WHERE {where}",
                    (measure, *params),
                ).fetchone()[0]
                result["total"] = format(Decimal(total), "f")
                result["unit"] = descriptor["unit"]
                result["unresolved_count"] = 0
        return result
