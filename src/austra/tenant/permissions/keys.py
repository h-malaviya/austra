"""Permission keys with code owned meaning."""

from typing import Literal

Permission = Literal[
    "chat.use",
    "records.read",
    "records.write",
    "evidence.read",
    "policies.read",
    "policies.draft",
    "policies.approve",
    "workflows.read",
    "workflows.draft",
    "workflows.approve",
    "workflows.run",
    "reports.read",
    "integrations.use",
    "department.members.manage",
    "department.users.create",
    "department.roles.assign",
    "departments.manage",
    "roles.manage",
    "users.manage",
    "audit.read",
]

DEPARTMENT_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        "chat.use",
        "records.read",
        "records.write",
        "evidence.read",
        "policies.read",
        "policies.draft",
        "workflows.read",
        "workflows.draft",
        "workflows.run",
        "reports.read",
        "integrations.use",
        "department.members.manage",
        "department.users.create",
        "department.roles.assign",
    }
)
ADMIN_ONLY_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        "policies.approve",
        "workflows.approve",
        "departments.manage",
        "roles.manage",
        "users.manage",
        "audit.read",
    }
)
SUPPORT_READ_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        "records.read",
        "evidence.read",
        "policies.read",
        "workflows.read",
        "reports.read",
        "audit.read",
    }
)
