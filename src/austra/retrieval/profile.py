"""Narrow read lane for administrator-confirmed company profile facts."""

import re

import psycopg

from austra.tenant.context import TenantContext, tenant_transaction
from austra.tenant.permissions.evaluator import permits

PROFILE_QUESTION = (
    ("website_url", re.compile(r"\b(?:our|company|business)\s+(?:website|site|url)\b|\bwebsite\s+(?:url|address)\b", re.IGNORECASE)),
    ("social_url", re.compile(r"\b(?:our|company|business)\s+(?:linkedin|social)\b|\blinkedin\s+(?:page|url)\b", re.IGNORECASE)),
    ("desired_outcome", re.compile(r"\b(?:our|company|business)\s+(?:goal|desired outcome)\b|\bwhat\s+(?:are\s+we|do\s+we)\s+trying\s+to\s+achieve\b", re.IGNORECASE)),
    ("organization_name", re.compile(r"\b(?:our|company|business)\s+name\b|\bwhat\s+is\s+(?:this|the)\s+company\s+called\b", re.IGNORECASE)),
    ("about_organization", re.compile(r"\b(?:what\s+does\s+(?:our\s+company|our\s+business|[A-Z][\w-]*)\s+(?:do|make|produce|manufacture)|what\s+do\s+we\s+do|who\s+are\s+we|(?:our|company|business)\s+(?:profile|description)|tell\s+me\s+about\s+(?:our|the)\s+(?:company|business))\b", re.IGNORECASE)),
)
PROCESS_TERMS = re.compile(
    r"\b(?:workflow|process|policy|rule|approve|approval|authorize|execute|send|create|customer records?)\b",
    re.IGNORECASE,
)


def profile_fields(question: str, organization_name: str = "") -> list[str]:
    if PROCESS_TERMS.search(question):
        return []
    if organization_name:
        question = re.sub(r"\b" + r"[\s-]+".join(re.escape(part) for part in re.split(r"[\s-]+", organization_name)) + r"\b(?:['’]s)?",
                          "our company", question, flags=re.IGNORECASE)
    if re.search(r"\b(?!company\b|business\b)[\w-]+['’]s\s+(?:(?:official|public)\s+)?(?:website|site|profile)\b",
                 question, re.IGNORECASE):
        return []
    if re.search(r"\bwhat does (?!our (?:company|business)\b)\S+", question, re.IGNORECASE):
        return []
    fields = [field for field, pattern in PROFILE_QUESTION if pattern.search(question)]
    if re.search(r"\b(?:our|company|business|its|official|public|confirmed|on file)\b", question, re.IGNORECASE):
        if re.search(r"\b(?:website|website url)\b", question, re.IGNORECASE):
            fields.append("website_url")
        if re.search(r"\b(?:company description|company summary|company profile)\b", question, re.IGNORECASE):
            fields.append("about_organization")
    return list(dict.fromkeys(fields))


def profile_field(question: str) -> str | None:
    if PROCESS_TERMS.search(question):
        return None
    matches = profile_fields(question)
    return matches[0] if len(matches) == 1 else None


OWN_ROLE_QUESTION = re.compile(
    r"\b(?:my role|my job|what role did i tell you|what do i do here|about me)\b",
    re.IGNORECASE,
)


def own_role_requested(question: str) -> bool:
    return bool(OWN_ROLE_QUESTION.search(question))


def own_role_answer(
    connection: psycopg.Connection, context: TenantContext, question: str
) -> dict:
    user = connection.execute(
        "SELECT full_name, username_normalized, role FROM users "
        "WHERE organization_id = %s AND id = %s",
        (context.organization_id, context.actor.user_id),
    ).fetchone()
    if user is None:
        raise PermissionError("Signed-in user not found")
    with tenant_transaction(connection, context):
        description = None
        if context.actor.role == "org_admin":
            row = connection.execute(
                "SELECT about_admin FROM organization_tours WHERE organization_id = %s",
                (context.organization_id,),
            ).fetchone()
            description = row[0] if row else None
        company = None
        if all(
            permits(context.actor, context.organization_id, None, permission)
            for permission in ("evidence.read", "policies.read")
        ):
            company = connection.execute(
                "SELECT id, about_organization, organization_name FROM organization_profile_revisions "
                "WHERE organization_id = %s ORDER BY profile_order DESC LIMIT 1",
                (context.organization_id,),
            ).fetchone()
            if company and "about_organization" not in profile_fields(question, company[2]):
                company = None
    role_names = {
        "org_admin": "organization administrator",
        "department_manager": "department manager",
        "employee": "employee",
    }
    name = user[0] or user[1]
    if description:
        role_text = f"Your saved profile says: {description}"
    else:
        role_text = (
            f"I don't have a personal role description saved for you, {name}. "
            f"Your account role is {role_names.get(user[2], user[2])}."
        )
    parts = []
    if company:
        parts.append(f"Your confirmed company profile says: {company[1]}")
    parts.append(role_text)
    return {
        "status": "supported", "answer": "\n\n".join(parts),
        "citation_ids": [str(company[0])] if company else [],
    }
