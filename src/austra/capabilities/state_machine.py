"""Constrained, data-only workflow definitions and typed transitions."""

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from uuid import UUID

import psycopg

from austra.capabilities.contracts import CapabilityCommand
from austra.tenant.context import TenantContext

KEY = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
FIELD_TYPES = {"text", "decimal", "reference"}
PERMISSIONS = {"workflows.run", "workflows.approve"}


def compile_state_machine(source: dict, blueprint_hash: str) -> dict:
    definitions = []
    rules = []
    routes = []
    seen_kinds = set()
    for revision in source.get("revisions", []):
        if revision.get("kind") != "capability" or revision.get("content", {}).get("engine") != "state_machine":
            continue
        key = revision.get("key")
        content = revision["content"]
        kind = content.get("record_kind")
        if not isinstance(key, str) or not KEY.fullmatch(key) or not isinstance(kind, str) or not KEY.fullmatch(kind):
            raise ValueError("Capability and record kind need stable keys")
        if kind in seen_kinds:
            raise ValueError("Record kinds must be unique within a blueprint")
        seen_kinds.add(kind)
        fields = content.get("fields", [])
        field_map = {item.get("key"): item for item in fields if isinstance(item, dict)}
        if not fields or len(field_map) != len(fields) or any(
            not isinstance(name, str) or not KEY.fullmatch(name)
            or item.get("type") not in FIELD_TYPES
            or (item["type"] == "decimal" and not item.get("unit"))
            for name, item in field_map.items()
        ):
            raise ValueError("Capability fields need unique typed keys and decimal units")
        states = content.get("states", [])
        transitions = content.get("transitions", [])
        if not states or len(states) != len(set(states)) or any(
            not isinstance(state, str) or not KEY.fullmatch(state) for state in states
        ) or not transitions:
            raise ValueError("State machine needs named states and transitions")
        transition_keys = set()
        for transition in transitions:
            name = transition.get("key")
            origin = transition.get("from")
            if (not isinstance(name, str) or not KEY.fullmatch(name) or name in transition_keys
                or origin not in [None, *states] or transition.get("to") not in states
                or transition.get("permission") not in PERMISSIONS
                or any(field not in field_map and field != "external_id"
                       for field in transition.get("fields", []))
                or (origin is None and "external_id" not in transition.get("fields", []))):
                raise ValueError("Invalid state transition")
            transition_keys.add(name)
            decision_key = f"command.{key}.{name}"
            matching = [item for item in source.get("decision_tables", [])
                        if item.get("content", {}).get("decision_key") == decision_key]
            if not matching:
                raise ValueError(f"Dated authority required for {decision_key}")
            intervals = []
            for rule in matching:
                start = datetime.fromisoformat(str(rule.get("valid_from")))
                end = datetime.fromisoformat(str(rule["valid_to"])) if rule.get("valid_to") else None
                if (not rule.get("id") or start.tzinfo is None or (end is not None and
                    (end.tzinfo is None or end <= start)) or
                    rule["content"].get("required_permission") != transition["permission"]):
                    raise ValueError(f"Invalid dated authority for {decision_key}")
                if "limit" in rule["content"] or "unit" in rule["content"]:
                    raise ValueError("A numeric authority limit needs a typed decision guard")
                guard = rule["content"].get("guard")
                if guard is not None:
                    spec = field_map.get(guard.get("field")) if isinstance(guard, dict) else None
                    try:
                        threshold = Decimal(str(guard["value"]))
                    except (KeyError, TypeError, InvalidOperation) as error:
                        raise ValueError("Decision guard needs a decimal threshold") from error
                    if (spec is None or spec["type"] != "decimal" or
                        guard.get("unit") != spec["unit"] or
                        guard.get("op") not in {"lte", "gte"} or
                        not threshold.is_finite() or threshold < 0):
                        raise ValueError("Invalid typed decision guard")
                intervals.append((start, end))
                rules.append({"id": str(rule["id"]), "content": rule["content"],
                              "valid_from": start.isoformat(), "valid_to": end.isoformat() if end else None})
            intervals.sort(key=lambda interval: interval[0])
            if any(previous[1] is None or previous[1] > current[0]
                   for previous, current in pairwise(intervals)):
                raise ValueError(f"Overlapping authority for {decision_key}")
            routes.append({"capability": key, "transition": name, "from": origin,
                           "to": transition["to"]})
        examples = content.get("acceptance_examples", [])
        if not all(any(item.get("category") == category and item.get("input")
                       and item.get("expected") for item in examples)
                   for category in ("normal", "boundary", "adverse")):
            raise ValueError("Normal, boundary, and adverse examples are required")
        if not content.get("failure_path") or not content.get("owner"):
            raise ValueError("Workflow owner and failure path are required")
        definitions.append({"key": key, "record_kind": kind, "fields": fields,
                            "states": states, "transitions": transitions,
                            "failure_path": content["failure_path"], "owner": content["owner"],
                            "acceptance_examples": examples,
                            "idempotency": "organization_department_key",
                            "audit": "runtime_event_and_record_revision"})
    if not definitions:
        raise ValueError("No typed state machine capability")
    from austra.capabilities.projections import compile_projections
    integration_mappings = []
    for item in source.get("allowed_actions", []):
        content = item.get("content", {})
        definition = next((entry for entry in definitions
                           if entry["key"] == content.get("capability")), None)
        field_map = content.get("field_map")
        if (definition is None or content.get("effect") not in {"read", "proposal"}
            or not content.get("action_ids") or not isinstance(field_map, dict)
            or any(field not in {spec["key"] for spec in definition["fields"]}
                   for field in field_map.values())):
            raise ValueError("Integration mapping needs a scoped read or proposal action")
        integration_mappings.append({"capability": definition["key"],
                                     "action_ids": content["action_ids"],
                                     "field_map": field_map, "effect": content["effect"],
                                     "status": "mapping_only"})
    custom_modules = []
    standard_projections = []
    for item in source.get("projections", []):
        content = item.get("content", {})
        if content.get("view") != "custom_module":
            standard_projections.append(item)
            continue
        if (content.get("capability") not in {definition["key"] for definition in definitions}
            or not content.get("interaction") or not content.get("review_ticket")
            or not content.get("rollback_plan")):
            raise ValueError("Custom module needs capability, interaction, review ticket, and rollback")
        custom_modules.append({"key": item["key"], "capability": content["capability"],
                               "interaction": content["interaction"],
                               "review_ticket": content["review_ticket"],
                               "rollback_plan": content["rollback_plan"],
                               "status": "draft_review_required"})
    return {"engine": "state_machine", "blueprint_hash": blueprint_hash,
            "capabilities": definitions, "commands": ["transition_record"],
            "decision_tables": rules, "event_routes": routes,
            "mappings": source.get("typed_mappings", []),
            "integration_mappings": integration_mappings,
            "projections": compile_projections(standard_projections, definitions),
            "custom_module_drafts": custom_modules}


def execute_transition(connection: psycopg.Connection, context: TenantContext,
                       command: CapabilityCommand, event_id: UUID, manifest: dict,
                       rule: dict, save_record, load_record) -> dict:
    payload = command.payload
    definition = next((item for item in manifest["capabilities"]
                       if item["key"] == payload.capability_key), None)
    if definition is None:
        raise ValueError("Capability is not in the released build")
    transition = next((item for item in definition["transitions"]
                       if item["key"] == payload.transition_key), None)
    if transition is None:
        raise ValueError("Transition is not in the released build")
    current = None
    if transition["from"] is None:
        if payload.record_id is not None or not payload.external_id:
            raise ValueError("Creation needs an external ID and no record ID")
    else:
        if payload.record_id is None or payload.external_id is not None:
            raise ValueError("Transition needs a current record ID")
        current = load_record(connection, context, command, payload.record_id,
                              definition["record_kind"])
        if current["state"] != transition["from"]:
            raise ValueError("Transition is not allowed from current state")
        for spec in definition["fields"]:
            if (spec["type"] == "decimal" and spec["key"] in current["data"] and
                current["data"].get("units", {}).get(spec["key"]) != spec["unit"]):
                raise ValueError("Current record unit does not match approved capability")
    allowed = set(transition["fields"]) - {"external_id"}
    if set(payload.fields) != allowed:
        raise ValueError("Transition fields do not match approved definition")
    field_defs = {item["key"]: item for item in definition["fields"]}
    values = {}
    for name, value in payload.fields.items():
        if not isinstance(value, str) or not value.strip() or len(value) > 10000:
            raise ValueError("Invalid transition field value")
        spec = field_defs[name]
        if spec["type"] == "decimal":
            try:
                number = Decimal(value)
            except InvalidOperation as error:
                raise ValueError("Invalid decimal value") from error
            if not number.is_finite() or number < 0 or abs(number.as_tuple().exponent) > 6:
                raise ValueError("Decimal must be finite, nonnegative, and have at most six places")
            values[name] = format(number, "f")
        elif spec["type"] == "reference":
            values[name] = str(UUID(value))
        else:
            values[name] = value.strip()
    data = {**(current["data"] if current else {}), **values,
            "capability_key": definition["key"]}
    guard = rule["content"].get("guard")
    if guard is not None:
        candidate = data.get(guard["field"])
        if candidate is None:
            raise ValueError("Decision guard field is missing")
        try:
            amount = Decimal(str(candidate))
        except InvalidOperation as error:
            raise ValueError("Decision guard field is not decimal") from error
        threshold = Decimal(str(guard["value"]))
        if (guard["op"] == "lte" and amount > threshold or
            guard["op"] == "gte" and amount < threshold):
            raise ValueError("Decision guard denied the transition")
    units = {item["key"]: item["unit"] for item in definition["fields"]
             if item["type"] == "decimal" and item["key"] in data}
    data["units"] = units
    external_id = current["external_id"] if current else payload.external_id
    return {"record": save_record(connection, context, command, event_id,
                                  definition["record_kind"], external_id,
                                  transition["to"], data, current)}
