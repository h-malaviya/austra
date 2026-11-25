"""Validated view metadata and SQL-backed, role-scoped operational projections."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import psycopg
from psycopg import sql

from austra.a2ui import REPORT_CATALOG_ID, Surface
from austra.tenant.context import TenantContext, tenant_transaction
from austra.tenant.permissions.evaluator import permits

VIEW_KINDS = {"form", "table", "queue", "chart", "dashboard", "report"}
MAX_ROWS = 200
AGGREGATE_VIEWS = {"chart", "dashboard", "report"}


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
        group_by = content.get("group_by")
        if group_by is not None and (kind != "chart" or group_by not in allowed):
            raise ValueError("Select a workflow field or state to group the chart")
        measure = content.get("measure")
        field_spec = next((item for item in capability["fields"] if item["key"] == measure), None)
        if kind in AGGREGATE_VIEWS and not (kind == "chart" and group_by and not measure) and (
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
                "title": content.get("title"),
                "group_by": group_by,
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


def visible_projection(context, department_id, descriptor, manifest) -> bool:
    if context.actor.role not in descriptor["roles"]:
        return False
    capability = next((item for item in manifest.get("capabilities", [])
                       if item["key"] == descriptor["capability"]), {})
    if capability.get("department_ids") and str(department_id) not in capability["department_ids"]:
        return False
    if descriptor.get("view") in AGGREGATE_VIEWS:
        return permits(context.actor, context.organization_id, department_id, "reports.read")
    if descriptor.get("view") != "form":
        return True
    key = f"command.{descriptor['capability']}.{descriptor['transition']}"
    now = datetime.now(UTC)
    rules = [r["content"] for r in manifest.get("decision_tables", [])
             if r["content"].get("decision_key") == key
             and datetime.fromisoformat(str(r["valid_from"])) <= now
             and (not r.get("valid_to") or now < datetime.fromisoformat(str(r["valid_to"])))]
    return len(rules) == 1 and permits(
        context.actor, context.organization_id, department_id, rules[0]["required_permission"]
    ) and (not rules[0].get("allowed_user_ids") or str(context.actor.user_id) in rules[0]["allowed_user_ids"])


def projection_data(
    connection: psycopg.Connection,
    context: TenantContext,
    department_id: UUID,
    key: str,
    manifest: dict,
    *,
    offset: int = 0,
    group_value: str | None = None,
) -> dict:
    context.require(
        "reports.read"
        if any(
            item.get("key") == key and item.get("view") in AGGREGATE_VIEWS
            for item in manifest.get("projections", [])
        )
        else "workflows.read",
        department_id,
    )
    context.require_active_department(connection, department_id)
    descriptor = next(
        (item for item in manifest.get("projections", []) if item["key"] == key), None
    )
    if descriptor is None or not visible_projection(context, department_id, descriptor, manifest):
        raise PermissionError("Projection is unavailable to this role or department")
    if offset < 0:
        raise ValueError("Offset must be nonnegative")
    states = descriptor["states"]
    group = descriptor.get("group_by")
    if group_value is not None and not group:
        raise ValueError("This view does not group records")
    group_expression = (
        group if group in {"state", "external_id"}
        else sql.SQL("data->>{}").format(sql.Literal(group)).as_string(connection)
        if group else None
    )
    with tenant_transaction(connection, context):
        base_where = "organization_id = %s AND department_id = %s AND kind = %s"
        params = [context.organization_id, department_id, descriptor["record_kind"]]
        if states:
            base_where += " AND state = ANY(%s)"
            params.append(states)
        if group_value is not None:
            base_where += f" AND coalesce({group_expression}, '') = %s"
            params.append(group_value)
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
                        field: (row[4] | {"external_id": row[1], "state": row[3]}).get(field)
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
        if (descriptor["view"] == "chart" and group_value is None
                and ("total" not in result or result["total"] is not None)):
            _add_chart(connection, result, group_expression, where, params)
        return result


def _add_chart(connection, result: dict, group_expression: str | None, where: str, params: list):
    descriptor = result["descriptor"]
    measure = descriptor["measure"]
    first_label = measure.replace("_", " ") if measure else "Records"
    if group_expression:
        aggregate = "sum((data->>%s)::numeric)" if measure else "count(*)"
        rows = connection.execute(
            f"SELECT coalesce({group_expression}, ''), {aggregate} AS amount, count(*) "
            f"FROM capability_records WHERE {where} GROUP BY 1 ORDER BY amount DESC, 1 LIMIT %s",
            (*([measure] if measure else []), *params, MAX_ROWS + 1),
        ).fetchall()
        result["chart_truncated"] = len(rows) > MAX_ROWS
        chart_rows = [
            {"key": key, "label": (key or "Unknown")[:120], "first": str(amount),
             "details": [{"label": "Records", "value": str(count)}]}
            for key, amount, count in rows[:MAX_ROWS]
        ]
    else:
        chart_rows = [
            {"label": row["external_id"][:120], "first": str(row["values"][measure]),
             "details": [{"label": key.replace("_", " "), "value": str(value if value is not None else "Unknown")}
                         for key, value in row["values"].items() if key != "external_id"][:30]}
            for row in result["records"] if row["values"].get(measure) is not None
        ]
    if not chart_rows:
        return
    surface = Surface(f"projection_{descriptor['key']}", REPORT_CATALOG_ID)
    title = descriptor.get("title") or descriptor["key"].replace("_", " ")
    chart = surface.add(
        "AustraBarChart", title=title, mode="single", unit=descriptor["unit"] or "",
        rows=[{**row, "second": None, "first_label": first_label, "second_label": ""}
              for row in chart_rows],
    )
    result["surface"] = {"id": surface.id, "messages": surface.messages(chart)}
