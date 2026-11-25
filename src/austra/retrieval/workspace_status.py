"""Read-only status for an administrator asking what Austra has actually saved or run."""

import re

import psycopg
from sqlalchemy import select

from austra.onboarding.decisions import unique_decisions
from austra.storage.orm import OrganizationProfileRevision
from austra.storage.schema_contract import mapped_query
from austra.tenant.context import TenantContext, tenant_transaction

SECRET_REQUEST = re.compile(
    r"\b(?:show|reveal|display|tell|give|read|print|copy|export|what is|what's)\b"
    r".{0,100}\b(?:api key|token|secret|password|credential)\b",
    re.IGNORECASE,
)


def secret_disclosure_requested(question: str) -> bool:
    return bool(SECRET_REQUEST.search(question))


def workspace_status_requested(question: str) -> bool:
    lower = question.casefold()
    if re.search(r"\b(?:file|attachment|document|note|source)\b", lower) and re.search(
        r"\b(?:chang\w*|authoriz\w*|approv\w*|trust\w*|evidence)\b", lower
    ):
        return False
    process = re.search(r"\b(?:handoff|workflow|process|draft|proposal|incident)\b", lower)
    status = re.search(
        r"\b(?:where are we|what do you (?:currently )?know|summari[sz]e|status|"
        r"approved|connected|sent|saved|built|running|pending|ready)\b", lower
    )
    connection_status = re.search(r"\b(?:crm|slack|connected apps?)\b", lower) and re.search(
        r"\b(?:connected|sent|status)\b", lower
    )
    return bool((process and status) or connection_status)


def blocked_action_requested(question: str) -> bool:
    return bool(
        re.search(r"\b(?:mark|approve|send|email)\b", question, re.IGNORECASE)
        and re.search(r"\b(?:client|customer|scope change|draft)\b", question, re.IGNORECASE)
        and re.search(r"\b(?:now|please|go ahead|can you|could you)\b", question, re.IGNORECASE)
    )


def blocked_action_answer(connection: psycopg.Connection, context: TenantContext, question: str = "") -> dict:
    if context.actor.role != "org_admin":
        return {
            "status": "not_applicable",
            "answer": (
                "I can't approve a workflow or send a client message from Ask. "
                "Ask an organization admin to review the workflow and its permissions."
            ),
            "citation_ids": [],
        }
    with tenant_transaction(connection, context):
        rows = connection.execute(
            "SELECT DISTINCT ON (case_id) proposal, c.state FROM blueprint_candidates b JOIN onboarding_cases c "
            "ON (c.organization_id, c.id) = (b.organization_id, b.case_id) WHERE b.organization_id = %s "
            "ORDER BY case_id, candidate_order DESC",
            (context.organization_id,),
        ).fetchall()
        connections = connection.execute(
            "SELECT count(*) FROM integration_connections WHERE organization_id = %s "
            "AND revoked_at IS NULL",
            (context.organization_id,),
        ).fetchone()[0]
    named = [row for row in rows if row[0].get("name") and row[0]["name"].casefold() in question.casefold()]
    row = rows[0] if len(rows) == 1 else named[0] if len(named) == 1 else None
    if row and row[1] == "locked_candidate":
        answer = "This workflow has a confirmed blueprint. Use Workflows to run a released action with the required permissions."
    elif row:
        proposal = row[0]
        decisions = unique_decisions(proposal.get("open_decisions", []))
        name = proposal.get("name") or "Workflow draft"
        answer = f"I can't mark **{name}** approved or send a client message from Ask. "
        answer += "It is an unapproved draft."
        if decisions:
            answer += " These decisions are still open:\n" + "\n".join(
                f"- {item}" for item in decisions
            )
        answer += "\n\nA person must review and confirm the exact candidate before any workflow can run."
    elif rows:
        answer = "Choose the named workflow in Workflow reviews to check its approval status."
    else:
        answer = "I can't mark a workflow approved or send a client message from Ask. No workflow draft is saved."
    if not connections:
        answer += " Austra has no connected app for sending the message."
    return {"status": "not_applicable", "answer": answer, "citation_ids": []}


def workspace_status(connection: psycopg.Connection, context: TenantContext) -> dict:
    if context.actor.role != "org_admin":
        raise PermissionError("Organization administrator required")
    with tenant_transaction(connection, context):
        profile = mapped_query(
            connection,
            select(OrganizationProfileRevision.id, OrganizationProfileRevision.about_organization)
            .where(OrganizationProfileRevision.organization_id == context.organization_id)
            .order_by(OrganizationProfileRevision.profile_order.desc())
            .limit(1),
        ).fetchone()
        candidates = connection.execute(
            "SELECT b.proposal, c.state, r.state FROM onboarding_cases c "
            "JOIN LATERAL (SELECT proposal FROM blueprint_candidates b WHERE b.organization_id = "
            "c.organization_id AND b.case_id = c.id ORDER BY candidate_order DESC LIMIT 1) b ON true "
            "LEFT JOIN releases r ON (r.organization_id, r.blueprint_id) = (c.organization_id, c.blueprint_id) "
            "AND r.state = 'active' WHERE c.organization_id = %s ORDER BY c.created_at, c.id",
            (context.organization_id,),
        ).fetchall()
        connections = connection.execute(
            "SELECT template_key, health FROM integration_connections "
            "WHERE organization_id = %s AND revoked_at IS NULL ORDER BY template_key",
            (context.organization_id,),
        ).fetchall()
        sends = connection.execute(
            "SELECT (SELECT count(*) FROM integration_executions "
            "WHERE organization_id = %s AND state = 'succeeded') + "
            "(SELECT count(*) FROM capability_action_receipts WHERE organization_id = %s)",
            (context.organization_id, context.organization_id),
        ).fetchone()[0]
    lines = []
    citations = []
    if profile:
        lines.append(f"Your confirmed company profile says: {profile[1]}")
        citations.append(str(profile[0]))
    else:
        lines.append("No administrator-confirmed company profile is saved.")
    for proposal, state, release in candidates:
        decisions = unique_decisions(proposal.get("open_decisions", []))
        name = proposal.get("name") or "Workflow draft"
        if release or state == "locked_candidate":
            lines.append(f"**{name}** is released and active." if release else f"**{name}** has a confirmed blueprint.")
            continue
        lines.append(
            f"**{name}** is saved as an unapproved draft. "
            f"{len(decisions)} distinct decision(s) remain open."
        )
        if decisions:
            label = "Open decisions" if len(decisions) <= 6 else "Open decisions (first 6)"
            lines.append(label + ":\n" + "\n".join(f"- {item}" for item in decisions[:6]))
    if not candidates:
        lines.append("The onboarding conversation may contain working notes; no workflow draft is saved.")
    app_status = ", ".join(f"{name} ({health})" for name, health in connections)
    lines.append(f"Connected app records: {app_status or 'none'}.")
    lines.append(
        "Austra has no recorded successful external sends. This does not show what "
        "happened outside Austra."
        if not sends else
        "Austra has recorded external actions; check the action receipts for the "
        "action and recipient."
    )
    return {"status": "supported", "answer": "\n\n".join(lines), "citation_ids": citations}
