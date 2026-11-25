"""Append-only company facts explicitly confirmed by an organization administrator."""

import re

import psycopg

from austra.tenant.context import TenantContext

INTRODUCTION = re.compile(
    r"(?i:\b(?:my name is|i am|i'm|i’m))\s+([A-Z][a-z]{1,30})"
    r"(?:\s+([A-Z][a-z]{1,30}))?\b"
)
NON_NAMES = frozenset({"The", "A", "An", "Responsible", "Working", "Here", "From", "With"})


def introduced_name(messages: list[str]) -> str | None:
    for body in messages:
        match = INTRODUCTION.search(body)
        if match and match[1] not in NON_NAMES:
            return " ".join(part for part in match.groups() if part)
    return None


def record_profile(
    connection: psycopg.Connection,
    context: TenantContext,
    source: str,
    organization_name: str,
) -> None:
    connection.execute(
        "INSERT INTO organization_profile_revisions (organization_id, organization_name, "
        "about_organization, desired_outcome, website_url, social_url, confirmed_by, "
        "confirmed_at, tour_revision, source) "
        "SELECT t.organization_id, %s, t.about_organization, t.desired_outcome, "
        "t.website_url, t.social_url, %s, now(), t.revision, %s "
        "FROM organization_tours t "
        "WHERE t.organization_id = %s AND t.completed_at IS NOT NULL "
        "AND nullif(trim(t.about_organization), '') IS NOT NULL",
        (organization_name, context.actor.user_id, source, context.organization_id),
    )
