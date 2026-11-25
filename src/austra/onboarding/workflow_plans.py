"""Department-scoped planning conversations and administrator handoff."""

import re
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from austra.onboarding.decisions import unique_decisions
from austra.onboarding.discovery import (
    candidate_proposal,
    generate_discovery_turn,
    generate_workflow_draft,
)
from austra.onboarding.evidence import (
    blob_root,
    exclude_source,
    process_job,
    save_file,
    set_source_access,
)
from austra.onboarding.service import draft_case, ensure_case
from austra.semantics.service import extract_claims, propose_candidate, review_candidate
from austra.semantics.workflow_definition import Definition, define_workflow
from austra.tenant.context import TenantContext, tenant_transaction
from austra.tenant.permissions.evaluator import permits

MAX_TURNS = 24


class PlanStart(BaseModel):
    department_id: UUID


class PlanTurn(BaseModel):
    body: str = Field(min_length=1, max_length=8000)
    revision: int = Field(ge=1)


class PlanSubmit(BaseModel):
    revision: int = Field(ge=1)


class PlanReview(PlanSubmit):
    resolutions: dict[str, str] = Field(default_factory=dict, max_length=30)


def _plan(db, context, identifier, *, owner=True, lock=False):
    with tenant_transaction(db, context):
        row = db.execute(
            "SELECT id,department_id,revision,messages,unresolved,proposal,submitted_at,case_id,owner_user_id "
            "FROM department_workflow_plans WHERE organization_id=%s AND id=%s "
            "AND (%s IS FALSE OR owner_user_id=%s)" + (" FOR UPDATE" if lock else ""),
            (context.organization_id, identifier, owner, context.actor.user_id),
        ).fetchone()
    if not row:
        raise LookupError("Workflow planning conversation not found")
    return dict(zip(("id", "department_id", "revision", "messages", "unresolved", "proposal", "submitted_at",
                     "case_id", "owner_user_id"), row, strict=True))


def _create(db, context, department):
    context.require("workflows.draft", department)
    context.require_active_department(db, department)
    with tenant_transaction(db, context):
        return db.execute(
            "INSERT INTO department_workflow_plans (organization_id,department_id,owner_user_id) "
            "VALUES (%s,%s,%s) RETURNING id", (context.organization_id, department, context.actor.user_id),
        ).fetchone()[0]


def _revise(db, context, identifier, revision, *, message=None, prepare=False, submit=False, model_key=None):
    plan = _plan(db, context, identifier)
    context.require("workflows.draft", plan["department_id"])
    context.require_active_department(db, plan["department_id"])
    if plan["submitted_at"] or plan["revision"] != revision:
        raise ValueError("The plan changed. Reopen the latest version.")
    messages, unresolved, proposal = plan["messages"], plan["unresolved"], plan["proposal"]
    if submit:
        if not proposal or not proposal.get("suggested_definition"):
            raise ValueError("Prepare an editable definition before submitting for admin review")
    else:
        key = model_key(context.organization_id)
        if not key:
            raise ValueError("Ask your organization admin to connect a model")
        # Exit the initial read transaction before calling the provider.
        db.commit()
        org_name = db.execute("SELECT name FROM organizations WHERE id=%s", (context.organization_id,)).fetchone()[0]
        db.commit()
        if prepare:
            if len(messages) < 4:
                raise ValueError("Discuss the workflow and answer Austra’s questions before preparing the proposal")
            draft = generate_workflow_draft(
                key, org_name, messages, {"unresolved_questions": unresolved}, use_result_tool=True,
            )
            proposal = candidate_proposal(draft, unresolved)
        else:
            if len(messages) >= MAX_TURNS * 2:
                raise ValueError("Prepare this workflow proposal or start a new conversation")
            if re.search(r"sk-ant-|-----BEGIN .*PRIVATE KEY-----", message, re.IGNORECASE):
                raise ValueError("Ask your administrator to enter credentials in Connected apps")
            messages = [*messages, {"role": "user", "body": message.strip()}]
            if not messages[-1]["body"]:
                raise ValueError("Enter your workflow requirement")
            turn = generate_discovery_turn(key, org_name, "", messages, {"unresolved_questions": unresolved})
            unresolved = unique_decisions([*unresolved, *turn.unresolved_questions], turn.resolved_questions)
            messages.append({"role": "assistant", "body": turn.reply})
            proposal = None
    with tenant_transaction(db, context):
        updated = db.execute(
            "UPDATE department_workflow_plans SET revision=revision+1,messages=%s,unresolved=%s,proposal=%s,"
            "submitted_at=CASE WHEN %s THEN now() END,updated_at=now() WHERE organization_id=%s AND id=%s "
            "AND owner_user_id=%s AND revision=%s AND submitted_at IS NULL RETURNING id",
            (Jsonb(messages), Jsonb(unresolved), Jsonb(proposal) if proposal else None, submit,
             context.organization_id, identifier, context.actor.user_id, revision),
        ).fetchone()
        if not updated:
            raise ValueError("The plan changed. Reopen the latest version.")
    return _plan(db, context, identifier)


def _import(db, context, plan_id):
    context.require("policies.approve")
    with db.transaction():
        plan = _plan(db, context, plan_id, owner=False, lock=True)
        if not plan["submitted_at"]:
            raise ValueError("The manager must submit this plan first")
        if plan["case_id"]:
            return {"case_id": plan["case_id"]}
        context.require_active_department(db, plan["department_id"])
        ensure_case(db, context)
        proposal = plan["proposal"]
        case = draft_case(db, context, proposal["name"], plan_id, create_only=True)
        if not case:
            raise ValueError("An organization case is required")
        root = blob_root()
        manager_brief = "\n\n".join(item["body"] for item in plan["messages"] if item["role"] == "user")
        source = save_file(db, context, root, f"Workflow brief {plan_id}.txt", manager_brief.encode())
        set_source_access(db, context, source["source_id"], access_label="department", department_id=plan["department_id"])
        if process_job(db, context, root, source["revision_id"])["state"] != "complete":
            raise ValueError("The manager’s brief could not be indexed")
        propose_candidate(db, context, case["id"], proposal)
        with tenant_transaction(db, context):
            db.execute(
                "UPDATE department_workflow_plans SET case_id=%s WHERE organization_id=%s AND id=%s",
                (case["id"], context.organization_id, plan_id),
            )
    return {"case_id": case["id"], "source_id": source["source_id"]}


def _prepare_review(db, context, plan_id, payload):
    context.require("policies.approve")
    with db.transaction():
        plan = _plan(db, context, plan_id, owner=False, lock=True)
        if plan["revision"] != payload.revision or not plan["case_id"]:
            raise ValueError("Open the current submitted workflow plan before preparing it")
        case_id = plan["case_id"]
        candidate = review_candidate(db, context, case_id)
        if not candidate or not candidate["suggested_definition"]:
            raise ValueError("An editable workflow candidate is required")
        if candidate["revisions"]:
            return {"case_id": case_id}
        if set(payload.resolutions) != set(candidate["open_decisions"]):
            raise ValueError("Answer every decision before preparing the release candidate")
        with tenant_transaction(db, context):
            source = db.execute(
                "SELECT s.id,c.id FROM evidence_sources s JOIN evidence_revisions r "
                "ON r.organization_id=s.organization_id AND r.source_id=s.id JOIN evidence_chunks c "
                "ON c.organization_id=r.organization_id AND c.revision_id=r.id "
                "WHERE s.organization_id=%s AND s.display_name=%s AND s.department_id=%s AND r.status='complete' "
                "ORDER BY r.revision DESC, c.locator LIMIT 1",
                (context.organization_id, f"Workflow brief {plan_id}.txt", plan["department_id"]),
            ).fetchone()
        if source is None:
            raise LookupError("The manager’s supporting brief is unavailable")
        exclude_source(db, context, source[0], False)
        claim = extract_claims(db, context, source[1])[0]
        definition = Definition.model_validate({
            **candidate["suggested_definition"], "department_ids": [plan["department_id"]],
            "content_hash": candidate["content_hash"], "claim_id": claim["id"],
            "rationale": "The administrator reviewed this workflow against the submitted department conversation.",
            "resolutions": payload.resolutions,
        })
        define_workflow(db, context, case_id, definition)
        return {"case_id": case_id}


def install_workflow_plan_routes(app: FastAPI, identity, model_key):
    def run(request, operation):
        actor, _ = identity.session(request.cookies.get("austra_session"))
        if not actor or request.state.organization_slug in {None, "platform"}:
            raise HTTPException(403, "Organization session required")
        try:
            context = TenantContext.from_auth(actor)
            with identity.connect() as db:
                return operation(db, context)
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        except RuntimeError as error:
            raise HTTPException(503, str(error)) from error

    @app.get("/api/org/workflow-plans")
    def list_plans(request: Request):
        def load(db, context):
            departments = db.execute(
                "SELECT id,name FROM departments WHERE organization_id=%s AND archived_at IS NULL ORDER BY name",
                (context.organization_id,),
            ).fetchall()
            names = dict(departments)
            allowed = [{"id": key, "name": name} for key, name in departments if permits(
                context.actor, context.organization_id, key, "workflows.draft",
            )]
            with tenant_transaction(db, context):
                rows = db.execute(
                    "SELECT id,department_id,coalesce(proposal->>'name','New workflow'),submitted_at,"
                    "case_id,owner_user_id=%s,owner_user_id FROM department_workflow_plans "
                    "WHERE organization_id=%s ORDER BY updated_at DESC LIMIT 50",
                    (context.actor.user_id, context.organization_id),
                ).fetchall()
            owners = dict(db.execute("SELECT id,coalesce(nullif(full_name,''),username_normalized) FROM users "
                                     "WHERE organization_id=%s AND id=ANY(%s)",
                                     (context.organization_id, [row[-1] for row in rows])).fetchall())
            return {"departments": allowed, "plans": [
                {"id": key, "department": names[dep], "name": title, "submitted_at": submitted,
                 "case_id": case, "mine": mine, "owner": owners.get(owner)}
                for key, dep, title, submitted, case, mine, owner in rows if dep in names
            ]}
        return run(request, load)

    @app.post("/api/org/workflow-plans", status_code=201)
    def start_plan(payload: PlanStart, request: Request):
        return run(request, lambda db, c: _plan(db, c, _create(db, c, payload.department_id)))

    @app.get("/api/org/workflow-plans/{plan_id}")
    def open_plan(plan_id: UUID, request: Request):
        return run(request, lambda db, c: _plan(db, c, plan_id, owner=False))

    @app.post("/api/org/workflow-plans/{plan_id}/messages")
    def plan_message(plan_id: UUID, payload: PlanTurn, request: Request):
        return run(request, lambda db, c: _revise(
            db, c, plan_id, payload.revision, message=payload.body, model_key=model_key,
        ))

    @app.post("/api/org/workflow-plans/{plan_id}/prepare")
    def prepare_plan(plan_id: UUID, payload: PlanSubmit, request: Request):
        return run(request, lambda db, c: _revise(db, c, plan_id, payload.revision, prepare=True, model_key=model_key))

    @app.post("/api/org/workflow-plans/{plan_id}/submit")
    def submit_plan(plan_id: UUID, payload: PlanSubmit, request: Request):
        return run(request, lambda db, c: _revise(db, c, plan_id, payload.revision, submit=True))

    @app.post("/api/org/workflow-plans/{plan_id}/import")
    def import_plan(plan_id: UUID, request: Request):
        return run(request, lambda db, c: _import(db, c, plan_id))

    @app.post("/api/org/workflow-plans/{plan_id}/prepare-review")
    def prepare_review(plan_id: UUID, payload: PlanReview, request: Request):
        return run(request, lambda db, c: _prepare_review(db, c, plan_id, payload))
