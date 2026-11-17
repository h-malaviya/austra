"""Authenticated scope for tenant repositories and transaction-local RLS."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

import psycopg

from austra.tenant.permissions.evaluator import AuthContext, permits
from austra.tenant.permissions.keys import Permission


@dataclass(frozen=True)
class TenantContext:
    actor: AuthContext
    organization_id: UUID

    @classmethod
    def from_auth(cls, actor: AuthContext) -> "TenantContext":
        organization_id = actor.organization_id
        if actor.role == "super_admin" and actor.support is not None:
            organization_id = actor.support.organization_id
        if organization_id is None or not actor.user_active or not actor.organization_active:
            raise PermissionError("Active organization context required")
        if actor.must_change_password:
            raise PermissionError("Password change required")
        if actor.role == "super_admin" and not permits(
            actor, organization_id, None, "records.read"
        ):
            raise PermissionError("Active support context required")
        return cls(actor, organization_id)

    def require(self, permission: Permission, department_id: UUID | None = None) -> None:
        if not permits(self.actor, self.organization_id, department_id, permission):
            raise PermissionError(f"{permission} denied")

    @property
    def can_write(self) -> bool:
        return self.actor.role == "org_admin" or any(
            "workflows.run" in grant.permissions
            and grant.department_active
            and grant.membership_active
            and grant.role_active
            and grant.assignment_active
            for grant in self.actor.grants
        )


@contextmanager
def tenant_transaction(
    connection: psycopg.Connection, context: TenantContext
) -> Iterator[psycopg.Connection]:
    """Run new-table queries as the non-owner role with a local authenticated scope."""
    with connection.transaction():
        connection.execute("SET LOCAL ROLE austra_tenant_app")
        department_ids = ",".join(
            str(grant.department_id)
            for grant in context.actor.grants
            if grant.department_active
            and grant.membership_active
            and grant.role_active
            and grant.assignment_active
        )
        connection.execute(
            "SELECT set_config('austra.organization_id', %s, true), "
            "set_config('austra.actor_id', %s, true), "
            "set_config('austra.department_ids', %s, true), "
            "set_config('austra.all_departments', %s, true), "
            "set_config('austra.can_write', %s, true)",
            (
                str(context.organization_id),
                str(context.actor.user_id),
                department_ids,
                "on" if context.actor.role == "org_admin" else "off",
                "on" if context.can_write else "off",
            ),
        )
        yield connection
        connection.execute("RESET ROLE")
        connection.execute("RESET austra.organization_id")
        connection.execute("RESET austra.actor_id")
        connection.execute("RESET austra.department_ids")
        connection.execute("RESET austra.all_departments")
        connection.execute("RESET austra.can_write")
