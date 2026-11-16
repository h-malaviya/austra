"""Record scoped administrative actions without credential contents."""

from uuid import UUID

import psycopg

from austra.tenant.permissions.evaluator import AuthContext


def record_event(
    connection: psycopg.Connection,
    actor: AuthContext,
    action: str,
    *,
    resource_id: UUID | None = None,
    department_id: UUID | None = None,
    reason: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO audit_events (organization_id, department_id, actor_user_id, "
        "channel, action, resource_id, outcome, reason, correlation_id) "
        "VALUES (%s, %s, %s, 'api', %s, %s, 'success', %s, gen_random_uuid())",
        (actor.organization_id, department_id, actor.user_id, action, resource_id, reason),
    )
