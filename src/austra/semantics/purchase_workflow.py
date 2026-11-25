"""Prepare a reviewed sales order, stock, purchase and email capability."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from austra.capabilities.builder import CAPABILITY_KEY
from austra.connectors import GMAIL_SCOPE
from austra.onboarding.evidence import exclude_source, preview_source, process_job, save_file
from austra.onboarding.service import draft_case, ensure_case
from austra.semantics.service import (
    add_edge,
    add_revision,
    decide_claim,
    extract_claims,
    propose_candidate,
)
from austra.tenant.context import tenant_transaction

COMMANDS = (
    "submit_order", "reserve_stock", "draft_po", "approve_po", "propose_email", "approve_email",
)


def prepare_purchase_workflow(db, context, root: Path, draft: dict, department: UUID, limit: Decimal):
    context.require("policies.approve")
    context.require_active_department(db, department)
    if draft.get("kind") != "workflow" or draft.get("status") != "approved":
        raise ValueError("Approve the workflow proposal before preparing a build")
    org = context.organization_id
    source_id = UUID(draft["id"])
    name = draft["name"].replace("_", " ").title()[:80]
    rationale = "Approved draft and administrator-selected department and USD purchase approval limit."
    with db.transaction():
        db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (str(org),))
        ensure_case(db, context)
        with tenant_transaction(db, context):
            prior = db.execute(
                "SELECT id FROM onboarding_cases WHERE organization_id = %s AND lower(workflow_name) = lower(%s)",
                (org, name),
            ).fetchone()
        if prior:
            raise ValueError("Review the existing candidate for this workflow in Workflow reviews")
        case = draft_case(db, context, name, source_id, create_only=True)
        saved = save_file(
            db, context, root, f"{draft['name']}-{draft['id']}.txt",
            draft["content"].encode("utf-8"),
        )
        revision = process_job(db, context, root, saved["revision_id"])
        if revision["state"] != "complete":
            raise ValueError("The approved workflow text could not be extracted")
        exclude_source(db, context, saved["source_id"], False, rationale)
        passage = preview_source(db, context, saved["source_id"])[0]
        claim = extract_claims(db, context, passage["id"])[0]["id"]
        decide_claim(db, context, claim, "confirmed", rationale)
        revision_ids, edge_ids = [], []

        def add(kind, key, content):
            result = add_revision(
                db, context, kind=kind, stable_key=key, content=content, rationale=rationale,
                claim_ids=[claim], owner_user_id=context.actor.user_id, valid_from=datetime.now(UTC),
            )["id"]
            revision_ids.append(str(result))
            return result

        def edge(origin, relation, target):
            edge_ids.append(str(add_edge(db, context, origin, relation, target, rationale)))

        capability = add("capability", CAPABILITY_KEY, {"department_ids": [str(department)]})
        route = add("workflow", "sales_order_purchase_route", {
            "capability": CAPABILITY_KEY,
            "states": ["order", "stock_review", "purchase_review", "email_review", "sent", "reply_received"],
            "transitions": [{"from": a, "to": b} for a, b in (
                ("order", "stock_review"), ("stock_review", "purchase_review"),
                ("purchase_review", "email_review"), ("email_review", "sent"), ("sent", "reply_received"),
            )],
        })
        edge(route, "invokes", capability)
        journey = add("projection", "journey", {"title": "Sales order to purchase request"})
        edge(journey, "projects", capability)
        integration = add("integration", "purchase_request_gmail", {
            "action_ids": ["gmail.send_email"],
            "requested_scopes": GMAIL_SCOPE.split(),
            "reply_tracking": "Scheduled checks of the original Gmail thread after approved delivery",
            "reply_processing": "Read and extract supported reply documents with Gmail read consent",
            "owner_notifications": "Notify the sender in Austra, with optional email and follow-up reminders",
        })
        edge(capability, "uses", integration)
        for action in COMMANDS:
            policy = {
                "decision_key": f"command.{action}", "scope": str(department), "effect": action,
                "required_permission": "workflows.approve" if action.startswith("approve_") else "workflows.run",
            }
            if action == "approve_po":
                policy.update(limit=str(limit), unit="USD")
            edge(add("policy", f"purchase_{action}", policy), "governs", capability)
        proposal = propose_candidate(db, context, case["id"], {
            "name": name, "revision_ids": revision_ids, "edge_ids": edge_ids,
            "behavior": {
                "reads": [], "decisions": [], "actions": [], "approvers": [],
                "manual_steps": [draft["summary"], "Approve the purchase request and exact email before sending."],
                "outputs": ["Purchase request, Gmail delivery receipt and a timeline of matching replies."],
            },
            "acceptance_examples": [
                {"category": "normal", "input": "Insufficient verified stock",
                 "expected": "Reserve available stock and review only the remaining demand."},
                {"category": "normal", "input": "Two replies arrive separately in the approved Gmail thread",
                 "expected": "Record both replies once and keep checking after the first."},
                {"category": "normal", "input": "Supplier replies with a quote while the owner is away",
                 "expected": "Save the reply, extract supported documents and notify the owner."},
                {"category": "boundary", "input": "Verified stock covers demand",
                 "expected": "No purchase quantity is available to draft."},
                {"category": "adverse", "input": "Gmail is disconnected or approval has changed",
                 "expected": "Keep the email queued for repair without sending."},
            ],
        })
        return {"case_id": case["id"], **proposal}
