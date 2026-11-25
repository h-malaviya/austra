"""Bind a human-reviewed chat proposal to a typed, internal workflow."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from austra.capabilities.email_automation import EmailAutomation
from austra.capabilities.reply_mapping import EmailDocument, ReplyMapping
from austra.capabilities.state_machine import compile_state_machine
from austra.semantics.service import add_edge, add_revision, decide_claim, propose_candidate
from austra.storage.identity import active_department_grants
from austra.tenant.context import tenant_transaction


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class WorkflowField(StrictInput):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    type: Literal["text", "decimal", "reference"]
    unit: str | None = Field(default=None, max_length=32)


class Guard(StrictInput):
    field: str
    op: Literal["lte", "gte"]
    value: str
    unit: str


class Transition(StrictInput):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    origin: str | None = Field(alias="from")
    to: str
    permission: Literal["workflows.run", "workflows.approve"]
    fields: list[str] = Field(max_length=32)
    guard: Guard | None = None
    allowed_user_ids: list[UUID] = Field(default_factory=list, max_length=100)
    allow_automatic_email: bool = False


class WorkflowProjection(StrictInput):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,29}$")
    title: str = Field(min_length=1, max_length=100)
    view: Literal["chart", "queue", "table"]
    fields: list[str] = Field(min_length=1, max_length=30)
    states: list[str] = Field(default_factory=list, max_length=32)
    group_by: str | None = None
    measure: str | None = None


class WorkflowShape(StrictInput):
    fields: list[WorkflowField] = Field(min_length=1, max_length=32)
    states: list[str] = Field(min_length=1, max_length=32)
    transitions: list[Transition] = Field(min_length=1, max_length=32)
    failure_path: str = Field(min_length=1, max_length=500)
    projections: list[WorkflowProjection] = Field(default_factory=list, max_length=12)
    reply_mapping: ReplyMapping | None = None
    email_document: EmailDocument | None = None
    automatic_email: EmailAutomation | None = None


class Definition(WorkflowShape):
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim_id: UUID
    rationale: str = Field(min_length=1, max_length=2000)
    resolutions: dict[str, str] = Field(max_length=30)
    department_ids: list[UUID] = Field(default_factory=list, max_length=100)


def preflight_email_workflow(shape: WorkflowShape, examples: list[dict]) -> None:
    """Check a model proposal using the release compiler before anyone can approve it."""
    if not shape.automatic_email:
        return
    for rule in shape.automatic_email.rules:
        if (rule.on_mismatch.body or rule.on_mismatch.transition) and not any(
            check.source != "record" and not check.blocking for check in rule.checks
        ):
            raise ValueError(
                f"{rule.state}: The discrepancy branch has an action but all checks hold for review. "
                "Set blocking=false on checks whose readable mismatch should trigger that action."
            )
    capability = shape.model_dump(by_alias=True, exclude_none=True, exclude={"projections"})
    capability.update(engine="state_machine", record_kind="proposed", owner="pending", acceptance_examples=examples)
    rules = []
    for step in shape.transitions:
        content = {"decision_key": f"command.proposed.{step.key}", "required_permission": step.permission,
                   "allow_automatic_email": step.allow_automatic_email}
        if step.guard:
            content["guard"] = step.guard.model_dump()
        rules.append({"id": step.key, "content": content, "valid_from": "2000-01-01T00:00:00+00:00"})
    rules.append({"id": "record_email", "content": {"decision_key": "command.approve_email", "scope": "proposed",
                                                   "required_permission": "workflows.approve"}})
    compile_state_machine({"revisions": [{"kind": "capability", "key": "proposed", "content": capability}],
                           "decision_tables": rules, "projections": []}, "draft")


def define_workflow(db, context, case_id: UUID, definition: Definition) -> dict:
    context.require("policies.approve")
    if context.actor.role != "org_admin":
        raise PermissionError("Organization admin required")
    org = context.organization_id
    with db.transaction():
        with tenant_transaction(db, context):
            case = db.execute(
                "SELECT state, coalesce(workflow_root_id, id) FROM onboarding_cases "
                "WHERE organization_id = %s AND id = %s FOR UPDATE",
                (org, case_id),
            ).fetchone()
            if not case or case[0] == "locked_candidate":
                raise ValueError("An editable workflow is required")
            row = db.execute(
                "SELECT content_hash, proposal FROM blueprint_candidates WHERE organization_id = %s "
                "AND case_id = %s ORDER BY candidate_order DESC LIMIT 1",
                (org, case_id),
            ).fetchone()
            if not row or row[0] != definition.content_hash:
                raise ValueError("The draft changed. Review the latest version.")
            proposal = row[1]
            plan = db.execute(
                "SELECT department_id,owner_user_id FROM department_workflow_plans "
                "WHERE organization_id=%s AND case_id=%s", (org, case_id),
            ).fetchone()
        if plan and definition.department_ids != [plan[0]]:
            raise ValueError("Use the department that submitted this workflow plan")
        for setting in proposal.get("email_requirements", []):
            if getattr(definition, setting) is None:
                raise ValueError(f"Complete the {setting.replace('_', ' ')} before admin review")
        if definition.department_ids:
            active = db.execute("SELECT id FROM departments WHERE organization_id = %s AND id = ANY(%s) "
                                "AND archived_at IS NULL", (org, definition.department_ids)).fetchall()
            if {row[0] for row in active} != set(definition.department_ids):
                raise ValueError("Select active departments in this organization")
        users = {u for t in definition.transitions for u in t.allowed_user_ids}
        for user in users:
            account = db.execute("SELECT role FROM users WHERE organization_id = %s AND id = %s "
                                 "AND deleted_at IS NULL AND status = 'active'", (org, user)).fetchone()
            if not account:
                raise ValueError("Select active users in this organization")
            if account[0] == "org_admin":
                continue
            grants = active_department_grants(db, user)
            for transition in definition.transitions:
                if user in transition.allowed_user_ids and not any(
                    transition.permission in grant.permissions and
                    (not definition.department_ids or grant.department_id in definition.department_ids)
                    for grant in grants
                ):
                    raise ValueError("A designated reviewer lacks the required department permission")
        if set(proposal.get("open_decisions", [])) != set(definition.resolutions) or any(
            not answer.strip() or len(answer) > 2000 for answer in definition.resolutions.values()
        ):
            raise ValueError("Record an answer for every open decision")
        reserved_fields = {"external_id", "state", "units", "capability_key"}
        if any(field.key in reserved_fields for field in definition.fields):
            raise ValueError("Choose field names distinct from system record fields")
        if not any(t.origin is None for t in definition.transitions):
            raise ValueError("Define a starting transition")
        key = "workflow_" + case[1].hex
        now = datetime.now(UTC)
        owner = plan[1] if plan else context.actor.user_id
        if plan and not any("workflows.draft" in grant.permissions and grant.department_id == plan[0]
                            for grant in active_department_grants(db, owner)):
            submitter = db.execute("SELECT role FROM users WHERE organization_id=%s AND id=%s AND status='active' "
                                   "AND deleted_at IS NULL", (org, owner)).fetchone()
            if submitter != ("org_admin",):
                raise ValueError("The submitting workflow owner no longer has department access")
        decide_claim(db, context, definition.claim_id, "confirmed", definition.rationale)
        ids, edges = [], []

        def revision(kind, stable_key, content):
            rev = add_revision(
                db,
                context,
                kind=kind,
                stable_key=stable_key,
                content=content,
                rationale=definition.rationale,
                claim_ids=[definition.claim_id],
                owner_user_id=owner,
                valid_from=now,
            )["id"]
            ids.append(str(rev))
            return rev

        capability = {
            "engine": "state_machine",
            "record_kind": key,
            "owner": str(owner),
            "failure_path": definition.failure_path,
            "states": definition.states,
            "department_ids": [str(d) for d in definition.department_ids],
            "fields": [f.model_dump(exclude_none=True) for f in definition.fields],
            "transitions": [
                t.model_dump(by_alias=True, exclude={"guard", "allowed_user_ids", "allow_automatic_email"})
                for t in definition.transitions
            ],
            "acceptance_examples": proposal["acceptance_examples"],
        }
        for setting in ("reply_mapping", "email_document", "automatic_email"):
            if value := getattr(definition, setting):
                capability[setting] = value.model_dump(exclude_none=True)
        cap_id = revision("capability", key, capability)
        rules = []
        projections = []
        if definition.reply_mapping or definition.email_document or definition.automatic_email:
            rule = {
                "decision_key": "command.approve_email",
                "effect": "queue_reviewed_record_email",
                "scope": key,
                "required_permission": "workflows.approve",
            }
            rule_id = revision("policy", f"{key}_record_email", rule)
            edges.append(str(add_edge(db, context, rule_id, "governs", cap_id, definition.rationale)))
            rules.append({"id": str(rule_id), "content": rule, "valid_from": now.isoformat()})
        for t in definition.transitions:
            rule = {
                "decision_key": f"command.{key}.{t.key}",
                "effect": t.to,
                "scope": key,
                "required_permission": t.permission,
            }
            if t.allowed_user_ids:
                rule["allowed_user_ids"] = [str(u) for u in t.allowed_user_ids]
            if t.guard:
                rule["guard"] = t.guard.model_dump()
            if t.allow_automatic_email:
                rule["allow_automatic_email"] = True
            rule_id = revision("policy", f"{key}_{t.key}", rule)
            edges.append(
                str(add_edge(db, context, rule_id, "governs", cap_id, definition.rationale))
            )
            rules.append({"id": str(rule_id), "content": rule, "valid_from": now.isoformat()})
            view = {
                "view": "form",
                "capability": key,
                "roles": ["org_admin", "employee"],
                "fields": t.fields or ["external_id"],
                "transition": t.key,
            }
            projections.append((f"{key}_{t.key}", {**view, "states": [t.origin] if t.origin else []}))
        fields_by_key = {field.key: field for field in definition.fields}
        for projection in definition.projections:
            content = projection.model_dump(exclude={"key"}, exclude_none=True)
            measure = fields_by_key.get(projection.measure)
            if projection.measure and not measure:
                raise ValueError("Select a workflow field for the chart measure")
            projections.append((
                f"view_{key}_{projection.key}",
                {**content, "capability": key, "roles": ["org_admin", "employee"],
                 "unit": measure.unit if measure else None},
            ))
        for view_key, view in projections:
            proj_id = revision("projection", view_key, view)
            edges.append(
                str(add_edge(db, context, proj_id, "projects", cap_id, definition.rationale))
            )
        compile_state_machine(
            {
                "revisions": [{"kind": "capability", "key": key, "content": capability}],
                "decision_tables": rules,
                "projections": [{"key": key, "content": view} for key, view in projections],
            },
            definition.content_hash,
        )
        clarifications = [
            {"question": q, "answer": a, "answered_by": str(owner), "answered_at": now.isoformat()}
            for q, a in definition.resolutions.items()
        ]
        return propose_candidate(
            db,
            context,
            case_id,
            {
                **proposal,
                "revision_ids": ids,
                "edge_ids": edges,
                "open_decisions": [],
                "suggested_definition": definition.model_dump(
                    mode="json", by_alias=True,
                    include={"fields", "states", "transitions", "projections", "failure_path",
                             "reply_mapping", "email_document", "automatic_email"},
                ),
                "clarifications": [*proposal.get("clarifications", []), *clarifications],
            },
        )
