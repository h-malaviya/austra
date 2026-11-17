"""Independent checks of a pinned capability artifact before admin preview."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from austra.build_release.service import record_defect
from austra.capabilities.contracts import PAYLOADS, CapabilityCommand
from austra.capabilities.runtime import execute_command, explain_event
from austra.tenant.context import TenantContext, tenant_transaction
from austra.tenant.permissions.evaluator import AuthContext, DepartmentGrant


class _FixtureComplete(Exception):
    def __init__(self, cases: list[dict]):
        self.cases = cases


def _fixture_command(
    action: str,
    department_id: UUID,
    payload: dict,
    revisions: dict | None = None,
    key: str | None = None,
) -> CapabilityCommand:
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


def _sandbox_scenarios(
    connection: psycopg.Connection, manifest: dict, blueprint_hash: str
) -> list[dict]:
    """Exercise real services in a fixture organization, then roll back every row."""
    try:
        with connection.transaction():
            organization_id = connection.execute(
                "INSERT INTO organizations (slug, name) VALUES (%s, 'Verifier fixture') RETURNING id",
                (f"verifier-{uuid4().hex}",),
            ).fetchone()[0]
            admin_id = connection.execute(
                "INSERT INTO users (organization_id, username_normalized, role, password_hash, "
                "must_change_password) VALUES (%s, 'verifier', 'org_admin', 'fixture', false) RETURNING id",
                (organization_id,),
            ).fetchone()[0]
            department_id = connection.execute(
                "INSERT INTO departments (organization_id, slug, name) "
                "VALUES (%s, 'fixture', 'Fixture') RETURNING id",
                (organization_id,),
            ).fetchone()[0]
            context = TenantContext.from_auth(
                AuthContext(admin_id, organization_id, "org_admin", True, True)
            )
            blueprint_id = connection.execute(
                "INSERT INTO blueprints (organization_id, blueprint_version, content_hash, "
                "confirmed_by) VALUES (%s, 1, %s, %s) RETURNING id",
                (organization_id, blueprint_hash, admin_id),
            ).fetchone()[0]
            build_id = connection.execute(
                "INSERT INTO builds (organization_id, blueprint_id, build_revision, state) "
                "VALUES (%s, %s, 1, 'released') RETURNING id",
                (organization_id, blueprint_id),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO capability_build_artifacts (organization_id, build_id, "
                "blueprint_hash, manifest, fixtures, rollback_plan) "
                "VALUES (%s, %s, %s, %s, '{}'::jsonb, 'Restore prior release')",
                (organization_id, build_id, blueprint_hash, Jsonb(manifest)),
            )
            connection.execute(
                "INSERT INTO releases (organization_id, blueprint_id, build_id, approved_by) "
                "VALUES (%s, %s, %s, %s)",
                (organization_id, blueprint_id, build_id, admin_id),
            )
            if manifest.get("engine") == "state_machine":
                definition = manifest["capabilities"][0]
                creation = next(item for item in definition["transitions"] if item["from"] is None)
                fields = {name: "1" for name in creation["fields"] if name != "external_id"}
                payload = {
                    "capability_key": definition["key"],
                    "transition_key": creation["key"],
                    "external_id": "FIXTURE-1",
                    "fields": fields,
                }
                command = _fixture_command(
                    "transition_record", department_id, payload, key="creation-once"
                )
                first = execute_command(connection, context, command)
                same = execute_command(connection, context, command)
                cases = [
                    {
                        "name": "typed creation",
                        "category": "normal",
                        "passed": first["record"]["state"] == creation["to"],
                    },
                    {"name": "idempotent retry", "category": "retry", "passed": first == same},
                ]
                try:
                    execute_command(
                        connection,
                        context,
                        _fixture_command(
                            "transition_record",
                            department_id,
                            {**payload, "record_id": first["record"]["id"], "external_id": None},
                            {first["record"]["id"]: 1},
                        ),
                    )
                except ValueError:
                    cases.append(
                        {"name": "invalid transition", "category": "adverse", "passed": True}
                    )
                else:
                    cases.append(
                        {"name": "invalid transition", "category": "adverse", "passed": False}
                    )
                raise _FixtureComplete(cases)
            connection.execute(
                "INSERT INTO capability_connection_health (organization_id, provider_key, state) "
                "VALUES (%s, 'email', 'healthy')",
                (organization_id,),
            )
            lot_data = {
                "lot_id": "LOT-1",
                "product": "item",
                "status": "released",
                "verification_status": "verified",
                "on_hand_kg": "10",
                "reserved_kg": "0",
                "qc_hold_kg": "0",
                "available_kg": "10",
            }
            lot_id = connection.execute(
                "INSERT INTO capability_records (organization_id, department_id, kind, "
                "external_id, state, data) VALUES (%s, %s, 'stock_lot', 'LOT-1', "
                "'available', %s) RETURNING id",
                (organization_id, department_id, Jsonb(lot_data)),
            ).fetchone()[0]
            cases = []

            def check(name: str, category: str, passed: bool) -> None:
                cases.append({"name": name, "category": category, "passed": bool(passed)})

            order = execute_command(
                connection,
                context,
                _fixture_command(
                    "submit_order",
                    department_id,
                    {"external_id": "SO-1", "product": "item", "quantity_kg": "30"},
                ),
            )
            order_id = order["record"]["id"]
            reserve = _fixture_command(
                "reserve_stock",
                department_id,
                {"order_id": order_id, "lot_id": str(lot_id), "quantity_kg": "10"},
                {order_id: 1, lot_id: 1},
                "reserve-once",
            )
            first = execute_command(connection, context, reserve)
            check("normal reservation", "normal", first["order"]["state"] == "partially_reserved")
            check(
                "reservation retry", "retry", execute_command(connection, context, reserve) == first
            )
            try:
                execute_command(
                    connection,
                    context,
                    _fixture_command(
                        "reserve_stock",
                        department_id,
                        {"order_id": order_id, "lot_id": str(lot_id), "quantity_kg": "1"},
                        {order_id: 2, lot_id: 2},
                    ),
                )
            except ValueError:
                check("exhausted lot", "adverse", True)
            else:
                check("exhausted lot", "adverse", False)
            limit_rule = next(
                item
                for item in manifest["decision_tables"]
                if item["content"]["decision_key"] == "command.approve_po"
            )
            limit = Decimal(str(limit_rule["content"]["limit"]))
            currency = limit_rule["content"]["unit"]
            po = execute_command(
                connection,
                context,
                _fixture_command(
                    "draft_po",
                    department_id,
                    {
                        "order_id": order_id,
                        "external_id": "PO-1",
                        "supplier": "Fixture",
                        "quantity_kg": "1",
                        "unit_price_per_kg": str(limit),
                        "currency": currency,
                    },
                    {order_id: 2},
                    "po-once",
                ),
            )
            check(
                "PO retry",
                "retry",
                execute_command(
                    connection,
                    context,
                    _fixture_command(
                        "draft_po",
                        department_id,
                        {
                            "order_id": order_id,
                            "external_id": "PO-1",
                            "supplier": "Fixture",
                            "quantity_kg": "1",
                            "unit_price_per_kg": str(limit),
                            "currency": currency,
                        },
                        {order_id: 2},
                        "po-once",
                    ),
                )["record"]
                == po["record"],
            )
            po_id = po["record"]["id"]
            approved = execute_command(
                connection,
                context,
                _fixture_command(
                    "approve_po", department_id, {"po_id": po_id}, {po_id: 1, order_id: 2}
                ),
            )
            check("approval at limit", "boundary", approved["record"]["state"] == "approved")
            above = execute_command(
                connection,
                context,
                _fixture_command(
                    "draft_po",
                    department_id,
                    {
                        "order_id": order_id,
                        "external_id": "PO-2",
                        "supplier": "Fixture",
                        "quantity_kg": "1",
                        "unit_price_per_kg": str(limit + Decimal(1)),
                        "currency": currency,
                    },
                    {order_id: 2},
                ),
            )
            above_id = above["record"]["id"]
            escalated = execute_command(
                connection,
                context,
                _fixture_command(
                    "approve_po", department_id, {"po_id": above_id}, {above_id: 1, order_id: 2}
                ),
            )
            check("above limit escalation", "adverse", escalated["escalation_required"])
            check(
                "pinned explanation",
                "history",
                explain_event(connection, context, department_id, UUID(escalated["event_id"]))[
                    "build_id"
                ]
                == str(build_id),
            )
            employee_id = connection.execute(
                "INSERT INTO users (organization_id, username_normalized, role, password_hash, "
                "must_change_password) VALUES (%s, 'employee', 'employee', 'fixture', false) "
                "RETURNING id",
                (organization_id,),
            ).fetchone()[0]
            employee = TenantContext.from_auth(
                AuthContext(
                    employee_id,
                    organization_id,
                    "employee",
                    True,
                    True,
                    grants=(
                        DepartmentGrant(
                            department_id, frozenset({"workflows.run", "workflows.read"})
                        ),
                    ),
                )
            )
            try:
                execute_command(
                    connection,
                    employee,
                    _fixture_command(
                        "approve_po", department_id, {"po_id": above_id}, {above_id: 2}
                    ),
                )
            except PermissionError:
                check("approval permission", "permission", True)
            else:
                check("approval permission", "permission", False)
            email = execute_command(
                connection,
                context,
                _fixture_command(
                    "propose_email",
                    department_id,
                    {
                        "po_id": po_id,
                        "external_id": "MAIL-1",
                        "recipient": "fixture@example.com",
                        "subject": "Proposal",
                        "body": "Review PO-1",
                    },
                    {po_id: 2},
                ),
            )
            check(
                "email preview",
                "normal",
                email["preview"]["approval_required"]
                and email["preview"]["recipient"] == "fixture@example.com",
            )
            connection.execute(
                "UPDATE capability_connection_health SET state = 'disconnected' "
                "WHERE organization_id = %s",
                (organization_id,),
            )
            email_id = email["record"]["id"]
            queued_command = _fixture_command(
                "approve_email",
                department_id,
                {"email_id": email_id},
                {email_id: 1, po_id: 2},
                "email-once",
            )
            queued = execute_command(connection, context, queued_command)
            check(
                "email retry",
                "retry",
                execute_command(connection, context, queued_command) == queued,
            )
            check("disconnected repair", "failure", queued["effect"] == "repair_connection")
            check(
                "outbox once",
                "retry",
                connection.execute(
                    "SELECT count(*) FROM outbox_jobs WHERE event_id = %s", (queued["event_id"],)
                ).fetchone()[0]
                == 1,
            )
            raise _FixtureComplete(cases)
    except _FixtureComplete as complete:
        return complete.cases


def verify_manifest(manifest: dict, blueprint_hash: str, fixtures: dict) -> dict:
    if manifest.get("engine") == "state_machine":
        cases = [
            {
                "name": "locked hash",
                "category": "normal",
                "passed": manifest.get("blueprint_hash") == blueprint_hash,
            },
            {
                "name": "typed definitions",
                "category": "normal",
                "passed": bool(manifest.get("capabilities"))
                and manifest.get("commands") == ["transition_record"],
            },
            {
                "name": "dated rules",
                "category": "boundary",
                "passed": all(
                    item.get("id") and datetime.fromisoformat(item["valid_from"]).tzinfo
                    for item in manifest.get("decision_tables", [])
                ),
            },
            {
                "name": "acceptance examples",
                "category": "adverse",
                "passed": all(
                    len(examples) >= 3
                    for examples in fixtures.get("acceptance_examples", {}).values()
                ),
            },
            {
                "name": "rollback supplied",
                "category": "rollback",
                "passed": bool(fixtures.get("rollback_plan")),
            },
        ]
        return {
            "passed": all(item["passed"] for item in cases),
            "cases": cases,
            "blueprint_hash": blueprint_hash,
        }
    cases: list[dict] = []

    def check(name: str, category: str, condition: bool) -> None:
        cases.append({"name": name, "category": category, "passed": bool(condition)})

    check("locked hash", "normal", manifest.get("blueprint_hash") == blueprint_hash)
    legacy_actions = set(PAYLOADS) - {"transition_record"}
    check("command set", "normal", set(manifest.get("commands", [])) == legacy_actions)
    rules = {
        item.get("content", {}).get("decision_key"): item
        for item in manifest.get("decision_tables", [])
    }
    check(
        "dated rules",
        "boundary",
        all(
            f"command.{action}" in rules
            and bool(rules[f"command.{action}"].get("id"))
            and datetime.fromisoformat(rules[f"command.{action}"]["valid_from"]).tzinfo is not None
            for action in legacy_actions
        ),
    )
    check(
        "approval separation",
        "permission",
        all(
            rules.get(f"command.{action}", {}).get("content", {}).get("required_permission")
            == ("workflows.approve" if action.startswith("approve_") else "workflows.run")
            for action in legacy_actions
        ),
    )
    approval = rules.get("command.approve_po", {}).get("content", {})
    try:
        limit = Decimal(str(approval["limit"]))
        boundary = Decimal(str(fixtures.get("boundary", {}).get("approval_total", "NaN")))
        boundary_ok = limit.is_finite() and limit >= 0 and boundary == limit
        adverse_ok = limit + Decimal("0.000001") > limit
    except (KeyError, ValueError, ArithmeticError):
        boundary_ok = adverse_ok = False
    check("approval at limit", "boundary", boundary_ok)
    check("above limit escalates", "adverse", adverse_ok)
    base = {
        "action": "submit_order",
        "department_id": str(uuid4()),
        "idempotency_key": "retry",
        "correlation_id": str(uuid4()),
        "payload": {"external_id": "SO-1", "product": "item", "quantity_kg": "1"},
    }
    first = CapabilityCommand.model_validate(base)
    retry = CapabilityCommand.model_validate({**base, "correlation_id": str(uuid4())})
    changed = CapabilityCommand.model_validate(
        {**base, "payload": {**base["payload"], "quantity_kg": "2"}}
    )
    check("retry identity", "retry", first.digest() == retry.digest() != changed.digest())
    try:
        CapabilityCommand.model_validate(
            {**base, "payload": {**base["payload"], "quantity_kg": "NaN"}}
        )
    except ValueError:
        invalid_rejected = True
    else:
        invalid_rejected = False
    check("invalid quantity rejected", "failure", invalid_rejected)
    check(
        "disconnected provider fixture",
        "failure",
        fixtures.get("adverse", {}).get("provider") == "disconnected",
    )
    check("rollback supplied", "rollback", bool(fixtures.get("rollback_plan")))
    return {
        "passed": all(item["passed"] for item in cases),
        "cases": cases,
        "blueprint_hash": blueprint_hash,
    }


def verify_build(connection: psycopg.Connection, context: TenantContext, build_id: UUID) -> dict:
    context.require("workflows.approve")
    with tenant_transaction(connection, context):
        row = connection.execute(
            "SELECT a.blueprint_hash, a.manifest, a.fixtures, a.rollback_plan, b.state, "
            "p.content_hash FROM capability_build_artifacts a "
            "JOIN builds b ON b.id = a.build_id JOIN blueprints p ON p.id = b.blueprint_id "
            "WHERE a.organization_id = %s AND a.build_id = %s FOR UPDATE OF a",
            (context.organization_id, build_id),
        ).fetchone()
        if row is None or row[4] != "testing":
            raise ValueError("Built artifact in testing state required")
    fixtures = {**row[2], "rollback_plan": row[3]}
    result = verify_manifest(row[1], row[0], fixtures)
    result["passed"] = result["passed"] and row[0] == row[5]
    if result["passed"]:
        try:
            scenarios = _sandbox_scenarios(connection, row[1], row[0])
        except (ValueError, LookupError, PermissionError, psycopg.Error) as error:
            scenarios = [
                {
                    "name": "isolated runtime journey",
                    "category": "failure",
                    "passed": False,
                    "error": type(error).__name__,
                }
            ]
        result["cases"].extend(scenarios)
        result["passed"] = all(case["passed"] for case in result["cases"])
    with tenant_transaction(connection, context):
        connection.execute(
            "UPDATE capability_build_artifacts SET verification = %s "
            "WHERE organization_id = %s AND build_id = %s",
            (Jsonb(result), context.organization_id, build_id),
        )
    if not result["passed"]:
        failed = [case["name"] for case in result["cases"] if not case["passed"]]
        revision = connection.execute(
            "SELECT revision FROM builds WHERE organization_id = %s AND id = %s",
            (context.organization_id, build_id),
        ).fetchone()[0]
        defect_id = record_defect(
            connection,
            context,
            build_id,
            revision,
            "All isolated verifier scenarios pass",
            ", ".join(failed),
            context.actor.user_id,
            uuid4(),
            result,
        )
        result["defect_id"] = str(defect_id)
        with tenant_transaction(connection, context):
            connection.execute(
                "UPDATE capability_build_artifacts SET verification = %s "
                "WHERE organization_id = %s AND build_id = %s",
                (Jsonb(result), context.organization_id, build_id),
            )
    return result
