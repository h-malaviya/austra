from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from austra.tenant.permissions.evaluator import (
    AuthContext,
    DepartmentGrant,
    SupportContext,
    permits,
)


def employee(organization_id, *grants, **changes):
    values = {
        "user_id": uuid4(),
        "organization_id": organization_id,
        "role": "employee",
        "user_active": True,
        "organization_active": True,
        "grants": grants,
    }
    values.update(changes)
    return AuthContext(**values)


def test_employee_permissions_are_a_union_within_one_department_only():
    organization_id, sales_id, hr_id = uuid4(), uuid4(), uuid4()
    context = employee(
        organization_id,
        DepartmentGrant(sales_id, frozenset({"records.read"})),
        DepartmentGrant(sales_id, frozenset({"records.write"})),
        DepartmentGrant(hr_id, frozenset({"reports.read"})),
    )

    assert permits(context, organization_id, sales_id, "records.read")
    assert permits(context, organization_id, sales_id, "records.write")
    assert permits(context, organization_id, hr_id, "reports.read")
    assert not permits(context, organization_id, hr_id, "records.read")
    assert not permits(context, uuid4(), sales_id, "records.read")
    assert not permits(context, organization_id, None, "records.read")
    assert not permits(context, organization_id, sales_id, "users.manage")


@pytest.mark.parametrize(
    "inactive_field",
    ["department_active", "membership_active", "role_active", "assignment_active"],
)
def test_revocation_and_archive_deny_access(inactive_field):
    organization_id, department_id = uuid4(), uuid4()
    grant = DepartmentGrant(department_id, frozenset({"records.read"}), **{inactive_field: False})
    context = employee(organization_id, grant)

    assert not permits(context, organization_id, department_id, "records.read")


def test_admin_manages_department_with_no_manager():
    organization_id, department_id = uuid4(), uuid4()
    context = AuthContext(uuid4(), organization_id, "org_admin", True, True)

    assert permits(context, organization_id, department_id, "departments.manage")
    assert permits(context, organization_id, department_id, "records.read")
    assert not permits(context, uuid4(), department_id, "records.read")


def test_first_password_change_and_suspension_deny_business_access():
    organization_id, department_id = uuid4(), uuid4()
    grant = DepartmentGrant(department_id, frozenset({"chat.use"}))

    assert not permits(
        employee(organization_id, grant, must_change_password=True),
        organization_id,
        department_id,
        "chat.use",
    )
    assert not permits(
        employee(organization_id, grant, user_active=False),
        organization_id,
        department_id,
        "chat.use",
    )
    assert not permits(
        employee(organization_id, grant, organization_active=False),
        organization_id,
        department_id,
        "chat.use",
    )


def test_super_admin_requires_current_reasoned_support_context_for_read():
    organization_id = uuid4()
    now = datetime.now(UTC)
    context = AuthContext(
        uuid4(),
        None,
        "super_admin",
        True,
        True,
        support=SupportContext(
            organization_id, "Investigate customer issue", now + timedelta(minutes=5)
        ),
    )

    assert permits(context, organization_id, None, "records.read", now=now)
    assert not permits(context, organization_id, None, "records.write", now=now)
    assert not permits(context, uuid4(), None, "records.read", now=now)
    assert not permits(
        context, organization_id, None, "records.read", now=now + timedelta(minutes=6)
    )


def test_unassigned_employee_has_no_business_access():
    organization_id = uuid4()
    assert not permits(employee(organization_id), organization_id, uuid4(), "chat.use")
