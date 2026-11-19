import os
from uuid import uuid4

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from psycopg import sql

from austra.access import Access
from austra.agent import ConnectionCheckError
from austra.files import Files
from austra.platform import auth as platform_auth
from austra.platform.auth import password_hash
from austra.web import create_app


def test_organization_model_key_is_private_and_testable(tmp_path, monkeypatch):
    url = os.environ.get("AUSTRA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("AUSTRA_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    schema = f"model_connection_test_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as admin_connection:
        admin_connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            database_url = psycopg.conninfo.make_conninfo(url, options=f"-c search_path={schema}")
            monkeypatch.setenv("AUSTRA_DATABASE_URL", database_url)
            monkeypatch.setenv("AUSTRA_SUPER_ADMIN_USERNAME", "superadmin")
            monkeypatch.setenv("AUSTRA_SUPER_ADMIN_PASSWORD", "test platform password 123")
            monkeypatch.setenv("AUSTRA_LEGACY_ORGANIZATION_SLUG", "legacy")
            monkeypatch.setenv("AUSTRA_CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode())
            verified = []

            def verify(key):
                verified.append(key)
                if key != "fixture-model-key":
                    raise ConnectionCheckError("Model rejected the key", 400)

            app = create_app(tmp_path, anthro_verifier=verify)
            with psycopg.connect(database_url) as db:
                first = db.execute(
                    "INSERT INTO organizations (slug, name) VALUES ('bt', 'BT') RETURNING id"
                ).fetchone()[0]
                second = db.execute(
                    "INSERT INTO organizations (slug, name) VALUES ('other', 'Other') RETURNING id"
                ).fetchone()[0]
                for organization_id in (first, second):
                    db.execute(
                        "INSERT INTO users (organization_id, username_normalized, "
                        "role, password_hash, must_change_password) "
                        "VALUES (%s, 'admin', 'org_admin', %s, false)",
                        (organization_id, password_hash("fixture admin password")),
                    )
                db.execute(
                    "INSERT INTO users (organization_id, username_normalized, "
                    "role, password_hash, must_change_password) "
                    "VALUES (%s, 'worker', 'employee', %s, false)",
                    (first, password_hash("fixture worker password")),
                )
            headers = {"X-Austra-Request": "1"}
            first_client = TestClient(app, base_url="http://bt.localhost:8000")
            assert (
                first_client.post("/api/connections/anthropic/test", headers=headers).status_code
                == 401
            )
            assert (
                first_client.post(
                    "/api/login", json={"username": "admin", "password": "fixture admin password"}
                ).status_code
                == 200
            )
            assert first_client.get("/api/config").json()["connectors"]["anthropic"] is False
            assert (
                first_client.post("/api/connections/anthropic/test", headers=headers).status_code
                == 400
            )
            saved = first_client.post(
                "/api/credentials/anthropic",
                headers=headers,
                json={"values": {"ANTHROPIC_API_KEY": "fixture-model-key"}},
            )
            assert saved.status_code == 200
            assert first_client.post("/api/connections/anthropic/test", headers=headers).json() == {
                "connected": True
            }
            assert first_client.post("/api/credentials/anthropic/test", headers=headers).json() == {
                "connected": True
            }
            assert first_client.get("/api/config").json()["connectors"]["anthropic"] is True
            second_client = TestClient(app, base_url="http://other.localhost:8000")
            second_client.post(
                "/api/login", json={"username": "admin", "password": "fixture admin password"}
            )
            assert second_client.get("/api/config").json()["connectors"]["anthropic"] is False
            assert (
                second_client.post("/api/connections/anthropic/test", headers=headers).status_code
                == 400
            )
            worker = TestClient(app, base_url="http://bt.localhost:8000")
            worker.post(
                "/api/login", json={"username": "worker", "password": "fixture worker password"}
            )
            assert (
                worker.post("/api/connections/anthropic/test", headers=headers).status_code == 403
            )
            assert verified == ["fixture-model-key"] * 3
            assert (
                b"fixture-model-key"
                not in next((tmp_path / "integration_secrets" / str(first)).iterdir()).read_bytes()
            )
        finally:
            admin_connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def test_legacy_admin_migrates_and_other_roles_are_denied(tmp_path, monkeypatch):
    url = os.environ.get("AUSTRA_TEST_DATABASE_URL")
    if not url:
        pytest.skip("AUSTRA_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    schema = f"saas_test_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as admin_connection:
        admin_connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            database_url = psycopg.conninfo.make_conninfo(url, options=f"-c search_path={schema}")
            monkeypatch.setenv("AUSTRA_DATABASE_URL", database_url)
            monkeypatch.setenv("AUSTRA_SUPER_ADMIN_USERNAME", "superadmin")
            monkeypatch.setenv("AUSTRA_SUPER_ADMIN_PASSWORD", "test platform password 123")
            monkeypatch.setenv("AUSTRA_LEGACY_ORGANIZATION_SLUG", "polysilicon-manufacturing")
            monkeypatch.setenv("AUSTRA_LEGACY_ORGANIZATION_NAME", "Polysilicon Manufacturing Plant")
            Access(tmp_path).setup("existing admin password")
            Files(tmp_path).create_chat("Show stock", "Admin", title="Current raw material stock")

            client = TestClient(
                create_app(tmp_path), base_url="http://polysilicon-manufacturing.localhost"
            )
            signed_in = client.post(
                "/api/login",
                json={
                    "username": "admin",
                    "password": "existing admin password",
                    "organization_slug": "polysilicon-manufacturing",
                },
            )
            assert signed_in.status_code == 200
            assert client.get("/api/config").json()["role"] == "org_admin"
            assert client.get("/organization/departments").status_code == 200
            assert client.get("/organization/people").status_code == 200
            assert (
                client.get("/api/config").json()["organization_slug"] == "polysilicon-manufacturing"
            )
            assert (
                client.post(
                    "/api/login",
                    headers={"X-Austra-Request": "1"},
                    json={
                        "username": "superadmin",
                        "password": "test platform password 123",
                        "organization_slug": "platform",
                    },
                ).status_code
                == 401
            )
            bare = TestClient(create_app(tmp_path), base_url="http://localhost:8000")
            assert bare.get("/api/config").status_code == 200
            assert bare.get("/", follow_redirects=False).headers["location"] == (
                "http://admin.localhost:8000/"
            )
            assert (
                bare.post("/api/login", json={"password": "test platform password 123"}).status_code
                == 404
            )
            departments = client.get("/api/org/departments").json()
            assert {item["slug"] for item in departments} == {
                "sales",
                "procurement",
                "inventory",
                "production",
                "planning-operations",
            }
            assert sum(item["object_count"] for item in departments) >= 1
            original_catalog = platform_auth.catalog_legacy_workspace
            catalog_calls = []

            def track_catalog(*args):
                catalog_calls.append(True)
                return original_catalog(*args)

            monkeypatch.setattr(platform_auth, "catalog_legacy_workspace", track_catalog)
            client.get("/api/org/departments")
            assert not catalog_calls
            Files(tmp_path).create_chat("Review supplier data", "Admin", title="Supplier review")
            refreshed = client.get("/api/org/departments").json()
            assert len(catalog_calls) == 1
            assert sum(item["object_count"] for item in refreshed) > sum(
                item["object_count"] for item in departments
            )
            client.get("/api/org/departments")
            assert len(catalog_calls) == 1
            monkeypatch.setattr(platform_auth, "catalog_legacy_workspace", original_catalog)
            assert client.get("/api/chats").status_code == 200

            employee = client.post(
                "/api/org/users",
                headers={"X-Austra-Request": "1"},
                json={"username": "worker"},
            )
            assert employee.status_code == 201
            assert employee.json()["temporary_password"]

            inventory_id = next(item["id"] for item in departments if item["slug"] == "inventory")
            assert client.get(f"/organization/departments/{inventory_id}").status_code == 200
            operator_role = client.post(
                f"/api/org/departments/{inventory_id}/roles",
                headers={"X-Austra-Request": "1"},
                json={
                    "slug": "operator",
                    "name": "Operator",
                    "permissions": ["records.read"],
                    "reason": "Read inventory",
                },
            ).json()["id"]
            manager_role = client.post(
                f"/api/org/departments/{inventory_id}/roles",
                headers={"X-Austra-Request": "1"},
                json={
                    "slug": "manager",
                    "name": "Manager",
                    "permissions": [
                        "department.members.manage",
                        "department.users.create",
                        "department.roles.assign",
                        "records.read",
                    ],
                    "reason": "Delegate inventory roster",
                },
            ).json()["id"]
            assert (
                client.put(
                    f"/api/org/departments/{inventory_id}/roles/{manager_role}/assignable-roles",
                    headers={"X-Austra-Request": "1"},
                    json={"role_ids": [operator_role]},
                ).status_code
                == 200
            )
            manager_user = client.post(
                "/api/org/users",
                headers={"X-Austra-Request": "1"},
                json={"username": "manager"},
            ).json()
            assert (
                client.post(
                    f"/api/org/departments/{inventory_id}/members/{manager_user['id']}/roles",
                    headers={"X-Austra-Request": "1"},
                    json={"role_id": manager_role},
                ).status_code
                == 200
            )
            manager = TestClient(
                create_app(tmp_path), base_url="http://polysilicon-manufacturing.localhost"
            )
            assert (
                manager.post(
                    "/api/login",
                    json={
                        "username": "manager",
                        "password": manager_user["temporary_password"],
                        "organization_slug": "polysilicon-manufacturing",
                    },
                ).status_code
                == 200
            )
            assert (
                manager.post(
                    "/api/auth/change-password",
                    headers={"X-Austra-Request": "1"},
                    json={
                        "current_password": manager_user["temporary_password"],
                        "new_password": "manager permanent password",
                    },
                ).status_code
                == 200
            )
            assert (
                manager.post(
                    "/api/login",
                    json={
                        "username": "manager",
                        "password": "manager permanent password",
                        "organization_slug": "polysilicon-manufacturing",
                    },
                ).status_code
                == 200
            )
            created_by_manager = manager.post(
                f"/api/org/departments/{inventory_id}/members",
                headers={"X-Austra-Request": "1"},
                json={
                    "username": "newoperator",
                    "role_id": operator_role,
                    "reason": "Inventory shift",
                },
            )
            assert created_by_manager.status_code == 200
            assert created_by_manager.json()["temporary_password"]
            operator = TestClient(
                create_app(tmp_path), base_url="http://polysilicon-manufacturing.localhost"
            )
            assert (
                operator.post(
                    "/api/login",
                    json={
                        "username": "newoperator",
                        "password": created_by_manager.json()["temporary_password"],
                        "organization_slug": "polysilicon-manufacturing",
                    },
                ).status_code
                == 200
            )
            assert (
                operator.post(
                    "/api/auth/change-password",
                    headers={"X-Austra-Request": "1"},
                    json={
                        "current_password": created_by_manager.json()["temporary_password"],
                        "new_password": "operator permanent password",
                    },
                ).status_code
                == 200
            )
            assert (
                operator.post(
                    "/api/login",
                    json={
                        "username": "newoperator",
                        "password": "operator permanent password",
                        "organization_slug": "polysilicon-manufacturing",
                    },
                ).status_code
                == 200
            )
            sales_id = next(item["id"] for item in departments if item["slug"] == "sales")
            assert operator.get(f"/api/org/departments/{inventory_id}/records").status_code == 200
            assert operator.get(f"/api/org/departments/{sales_id}/records").status_code == 403
            assert operator.get("/api/chats").status_code == 403
            assert (
                manager.post(
                    f"/api/org/departments/{inventory_id}/members",
                    headers={"X-Austra-Request": "1"},
                    json={
                        "username": "anothermanager",
                        "role_id": manager_role,
                        "reason": "Try escalation",
                    },
                ).status_code
                == 403
            )
            join = manager.post(
                f"/api/org/departments/{inventory_id}/members",
                headers={"X-Austra-Request": "1"},
                json={
                    "username": "worker",
                    "role_id": operator_role,
                    "reason": "Cross department assignment",
                },
            )
            assert join.status_code == 200
            assert join.json()["status"] == "pending"
            assert (
                client.post(
                    f"/api/org/department-join-requests/{join.json()['request_id']}/review",
                    headers={"X-Austra-Request": "1"},
                    json={"decision": "approved"},
                ).status_code
                == 200
            )

            assert (
                client.delete(
                    f"/api/org/departments/{inventory_id}/members/{created_by_manager.json()['user_id']}/roles/{operator_role}",
                    headers={"X-Austra-Request": "1"},
                ).status_code
                == 200
            )
            assert operator.get(f"/api/org/departments/{inventory_id}/records").status_code == 403
            assert (
                client.delete(
                    f"/api/org/departments/{inventory_id}/members/{manager_user['id']}/roles/{manager_role}",
                    headers={"X-Austra-Request": "1"},
                ).status_code
                == 200
            )
            assert (
                manager.post(
                    f"/api/org/departments/{inventory_id}/members",
                    headers={"X-Austra-Request": "1"},
                    json={
                        "username": "lateroperator",
                        "role_id": operator_role,
                        "reason": "After manager revocation",
                    },
                ).status_code
                == 403
            )

            worker = TestClient(
                create_app(tmp_path), base_url="http://polysilicon-manufacturing.localhost"
            )
            assert (
                worker.post(
                    "/api/login",
                    json={
                        "username": "worker",
                        "password": employee.json()["temporary_password"],
                        "organization_slug": "polysilicon-manufacturing",
                    },
                ).status_code
                == 200
            )
            assert worker.get("/api/config").json()["must_change_password"] is True
            assert worker.get("/api/chats").status_code == 403

            platform = TestClient(create_app(tmp_path), base_url="http://admin.localhost")
            assert (
                platform.post(
                    "/api/login",
                    json={
                        "username": "superadmin",
                        "password": "test platform password 123",
                        "organization_slug": "platform",
                    },
                ).status_code
                == 200
            )
            assert platform.get("/api/platform/organizations").status_code == 200
            assert platform.get("/organization/departments").status_code == 404
            assert platform.get("/api/config").json()["organization_slug"] == "platform"
            assert platform.get("/platform/organizations").status_code == 200
            assert client.get("/platform/organizations").status_code == 404
            assert (
                platform.get(
                    "/api/platform/organizations",
                    headers={"Host": "polysilicon-manufacturing.localhost"},
                ).status_code
                == 403
            )
            assert client.get("/api/chats", headers={"Host": "admin.localhost"}).status_code == 403
            assert client.get("/api/chats", headers={"Host": "other.localhost"}).status_code == 403
            assert (
                client.get("/api/chats", headers={"Host": "evil.localhost.example.com"}).status_code
                == 400
            )
            assert platform.get("/api/chats").status_code == 403
            legacy_organization_id = platform.get("/api/platform/organizations").json()[0]["id"]
            assert (
                platform.get(f"/platform/organizations/{legacy_organization_id}").status_code == 200
            )
            assert (
                platform.patch(
                    f"/api/platform/organizations/{legacy_organization_id}",
                    headers={"X-Austra-Request": "1"},
                    json={
                        "name": "Edited Plant",
                        "email": "plant@example.com",
                        "contact_number": "+91 555 0100",
                        "city": "Pune",
                        "country": "India",
                    },
                ).status_code
                == 200
            )
            create_app(tmp_path)
            assert platform.get("/api/platform/organizations").json()[0]["name"] == "Edited Plant"
            assert platform.get("/api/platform/support/objects").status_code == 403
            assert (
                platform.post(
                    f"/api/platform/organizations/{legacy_organization_id}/support-context",
                    headers={"X-Austra-Request": "1"},
                    json={"reason": "Review catalog isolation"},
                ).status_code
                == 200
            )
            assert platform.get("/api/platform/support/objects").status_code == 200
            assert (
                platform.delete(
                    "/api/platform/support-context", headers={"X-Austra-Request": "1"}
                ).status_code
                == 200
            )
            assert platform.get("/api/platform/support/objects").status_code == 403
            new_organization = platform.post(
                "/api/platform/organizations",
                headers={"X-Austra-Request": "1"},
                json={
                    "slug": "new-tenant",
                    "name": "New Tenant",
                    "admin_username": "owner",
                    "email": "office@example.com",
                    "contact_number": "+1 555 0100",
                    "city": "Austin",
                    "country": "USA",
                    "admin_full_name": "First Owner",
                    "admin_email": "owner@example.com",
                    "admin_contact_number": "+1 555 0101",
                },
            )
            assert new_organization.status_code == 201
            organization_id = new_organization.json()["id"]
            organization = next(
                item
                for item in platform.get("/api/platform/organizations").json()
                if item["id"] == organization_id
            )
            assert organization["email"] == "office@example.com"
            assert organization["admin_count"] == 1
            first_admin = platform.get(
                f"/api/platform/organizations/{organization_id}/admins"
            ).json()[0]
            assert first_admin["full_name"] == "First Owner"
            assert "password" not in first_admin
            assert (
                platform.patch(
                    f"/api/platform/organizations/{organization_id}",
                    headers={"X-Austra-Request": "1"},
                    json={
                        "name": "New Tenant Ltd",
                        "email": "new@example.com",
                        "contact_number": "+1 555 0120",
                        "city": "Dallas",
                        "country": "USA",
                    },
                ).status_code
                == 200
            )
            second_admin = platform.post(
                f"/api/platform/organizations/{organization_id}/admins",
                headers={"X-Austra-Request": "1"},
                json={
                    "username": "manager",
                    "full_name": "Second Admin",
                    "email": "manager@example.com",
                    "contact_number": "+1 555 0102",
                },
            )
            assert second_admin.status_code == 201
            assert (
                platform.patch(
                    f"/api/platform/organizations/{organization_id}/admins/{second_admin.json()['id']}",
                    headers={"X-Austra-Request": "1"},
                    json={
                        "username": "manager2",
                        "full_name": "Updated Admin",
                        "email": "updated@example.com",
                        "contact_number": "+1 555 0103",
                        "status": "active",
                    },
                ).status_code
                == 200
            )
            reset = platform.post(
                f"/api/platform/organizations/{organization_id}/admins/{second_admin.json()['id']}/reset-password",
                headers={"X-Austra-Request": "1"},
            )
            assert reset.status_code == 200
            assert reset.json()["temporary_password"] != second_admin.json()["temporary_password"]
            reset_admin = TestClient(create_app(tmp_path), base_url="http://new-tenant.localhost")
            assert (
                reset_admin.post(
                    "/api/login",
                    json={
                        "username": "manager2",
                        "password": second_admin.json()["temporary_password"],
                        "organization_slug": "new-tenant",
                    },
                ).status_code
                == 401
            )
            assert (
                reset_admin.post(
                    "/api/login",
                    json={
                        "username": "manager2",
                        "password": reset.json()["temporary_password"],
                        "organization_slug": "new-tenant",
                    },
                ).status_code
                == 200
            )
            assert reset_admin.get("/api/config").json()["must_change_password"] is True
            assert (
                len(platform.get(f"/api/platform/organizations/{organization_id}/admins").json())
                == 2
            )
            assert (
                client.get(f"/api/platform/organizations/{organization_id}/admins").status_code
                == 403
            )
            assert (
                platform.post(
                    "/api/platform/organizations",
                    headers={"X-Austra-Request": "1"},
                    json={"slug": "admin", "name": "Reserved", "admin_username": "owner"},
                ).status_code
                == 400
            )
            assert (
                platform.post(
                    "/api/platform/organizations",
                    headers={"X-Austra-Request": "1"},
                    json={"slug": "platform", "name": "Reserved", "admin_username": "owner"},
                ).status_code
                == 400
            )
            other_admin = TestClient(create_app(tmp_path), base_url="http://new-tenant.localhost")
            assert (
                other_admin.post(
                    "/api/login",
                    json={
                        "username": "owner",
                        "password": new_organization.json()["temporary_password"],
                        "organization_slug": "new-tenant",
                    },
                ).status_code
                == 200
            )
            assert (
                other_admin.post(
                    "/api/auth/change-password",
                    headers={"X-Austra-Request": "1"},
                    json={
                        "current_password": new_organization.json()["temporary_password"],
                        "new_password": "replacement admin password",
                    },
                ).status_code
                == 200
            )
            assert (
                other_admin.post(
                    "/api/login",
                    json={
                        "username": "owner",
                        "password": "replacement admin password",
                        "organization_slug": "new-tenant",
                    },
                ).status_code
                == 200
            )
            assert client.get("/api/org/onboarding-cases/current").status_code == 404
            current_case = other_admin.get("/api/org/onboarding-cases/current")
            assert current_case.status_code == 200
            assert current_case.json()["state"] == "created"
            assert current_case.json()["next_actions"] == ["start_collecting"]
            assert other_admin.get("/api/config").json()["legacy_workspace"] is False
            assert client.get("/api/config").json()["legacy_workspace"] is True
            assert other_admin.get("/organization/onboarding").status_code == 200
            assert other_admin.get("/organization/capability").status_code == 200
            assert other_admin.get("/api/chats").status_code == 403
            assert client.post("/api/org/users", json={"username": "unsafe"}).status_code == 403
            new_employee = client.post(
                "/api/org/users",
                headers={"X-Austra-Request": "1"},
                json={
                    "username": "hrassistant",
                    "full_name": "HR Assistant",
                    "email": "hr@example.com",
                    "contact_number": "+91 555 0110",
                },
            )
            assert new_employee.status_code == 201
            assert any(
                user["full_name"] == "HR Assistant" and user["email"] == "hr@example.com"
                for user in client.get("/api/org/users").json()
            )
            assert (
                client.patch(
                    f"/api/org/users/{new_employee.json()['id']}",
                    headers={"X-Austra-Request": "1"},
                    json={
                        "username": "hrassistant",
                        "full_name": "Senior HR Assistant",
                        "email": "senior.hr@example.com",
                        "contact_number": "+91 555 0111",
                        "status": "suspended",
                    },
                ).status_code
                == 200
            )
            suspended = TestClient(
                create_app(tmp_path), base_url="http://polysilicon-manufacturing.localhost"
            )
            assert (
                suspended.post(
                    "/api/login",
                    json={
                        "username": "hrassistant",
                        "password": new_employee.json()["temporary_password"],
                        "organization_slug": "polysilicon-manufacturing",
                    },
                ).status_code
                == 401
            )
        finally:
            admin_connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )
