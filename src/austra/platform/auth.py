"""Durable SaaS login and one time bootstrap from operator configuration."""

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import psycopg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from austra.storage.identity import resolve_session
from austra.storage.legacy_catalog import catalog_legacy_workspace, legacy_catalog_signature
from austra.storage.legacy_inventory import record_legacy_inventory
from austra.storage.migrate import apply_migrations
from austra.tenant.permissions.evaluator import AuthContext

SESSION_HOURS = 12
MIN_PASSWORD_LENGTH = 12
USERNAME_PATTERN = re.compile(r"^[a-z0-9._-]{3,64}$")
HASHER = PasswordHasher()


def normalize_username(username: str) -> str:
    normalized = username.strip().casefold()
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError("Use 3 to 64 letters, numbers, periods, underscores, or hyphens")
    return normalized


def password_hash(password: str) -> str:
    if not MIN_PASSWORD_LENGTH <= len(password) <= 200:
        raise ValueError("Use a password between 12 and 200 characters")
    return HASHER.hash(password)


def verify_password(stored: str, password: str) -> bool:
    if stored.startswith("pbkdf2_sha256$"):
        _, iterations, salt, digest = stored.split("$", 3)
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(iterations)
        )
        return hmac.compare_digest(candidate, bytes.fromhex(digest))
    try:
        return HASHER.verify(stored, password)
    except (InvalidHashError, VerifyMismatchError):
        return False


def temporary_password() -> str:
    return secrets.token_urlsafe(24)


class SaasIdentity:
    def __init__(self, database_url: str, legacy_root: Path) -> None:
        self.database_url = database_url
        self.legacy_root = legacy_root
        self._legacy_catalog_lock = threading.Lock()
        self._legacy_catalog_signature: tuple[tuple[str, int], ...] | None = None

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.database_url)

    def bootstrap(self) -> None:
        username = normalize_username(os.environ["AUSTRA_SUPER_ADMIN_USERNAME"])
        password = os.environ["AUSTRA_SUPER_ADMIN_PASSWORD"]
        with self.connect() as connection:
            apply_migrations(connection)
            existing = connection.execute(
                "SELECT id, username_normalized, password_hash FROM users "
                "WHERE role = 'super_admin' AND deleted_at IS NULL FOR UPDATE"
            ).fetchone()
            if not existing:
                connection.execute(
                    "INSERT INTO users (username_normalized, role, password_hash, "
                    "must_change_password) VALUES (%s, 'super_admin', %s, false)",
                    (username, password_hash(password)),
                )
            elif existing[1] != username or not verify_password(existing[2], password):
                connection.execute(
                    "UPDATE users SET username_normalized = %s, password_hash = %s WHERE id = %s",
                    (username, password_hash(password), existing[0]),
                )
                connection.execute(
                    "UPDATE sessions SET revoked_at = now() WHERE user_id = %s "
                    "AND revoked_at IS NULL",
                    (existing[0],),
                )
            self._bootstrap_legacy_admin(connection)
        self._legacy_catalog_signature = legacy_catalog_signature(self.legacy_root)

    def refresh_legacy_catalog_if_changed(
        self, connection: psycopg.Connection, organization_id: UUID
    ) -> bool:
        signature = legacy_catalog_signature(self.legacy_root)
        if signature == self._legacy_catalog_signature:
            return False
        with self._legacy_catalog_lock:
            signature = legacy_catalog_signature(self.legacy_root)
            if signature == self._legacy_catalog_signature:
                return False
            catalog_legacy_workspace(connection, organization_id, self.legacy_root)
            self._legacy_catalog_signature = signature
            return True

    def _bootstrap_legacy_admin(self, connection: psycopg.Connection) -> None:
        legacy_path = self.legacy_root / "admin.json"
        if not legacy_path.is_file():
            return
        slug = os.environ["AUSTRA_LEGACY_ORGANIZATION_SLUG"]
        name = os.environ["AUSTRA_LEGACY_ORGANIZATION_NAME"]
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise ValueError("Invalid legacy organization slug")
        organization = connection.execute(
            "INSERT INTO organizations (slug, name) VALUES (%s, %s) "
            "ON CONFLICT (slug) DO NOTHING RETURNING id",
            (slug, name),
        ).fetchone()
        if organization is None:
            organization = connection.execute(
                "SELECT id FROM organizations WHERE slug = %s AND deleted_at IS NULL", (slug,)
            ).fetchone()
        if organization is None:
            raise ValueError("Legacy organization is unavailable")
        existing = connection.execute(
            "SELECT 1 FROM users WHERE organization_id = %s AND role = 'org_admin' "
            "AND deleted_at IS NULL",
            (organization[0],),
        ).fetchone()
        if existing:
            catalog_legacy_workspace(connection, organization[0], self.legacy_root)
            record_legacy_inventory(connection, organization[0], self.legacy_root)
            return
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
        salt, digest = legacy["salt"], legacy["hash"]
        bytes.fromhex(salt)
        bytes.fromhex(digest)
        connection.execute(
            "INSERT INTO users (organization_id, username_normalized, role, "
            "password_hash, must_change_password) "
            "VALUES (%s, 'admin', 'org_admin', %s, false)",
            (organization[0], f"pbkdf2_sha256$600000${salt}${digest}"),
        )
        catalog_legacy_workspace(connection, organization[0], self.legacy_root)
        record_legacy_inventory(connection, organization[0], self.legacy_root)

    def login(self, username: str, password: str, organization_slug: str) -> str:
        try:
            normalized = normalize_username(username)
        except ValueError as error:
            raise ValueError("Invalid username or password") from error
        with self.connect() as connection:
            if organization_slug == "platform":
                row = connection.execute(
                    "SELECT id, password_hash, status, must_change_password FROM users "
                    "WHERE organization_id IS NULL AND username_normalized = %s "
                    "AND deleted_at IS NULL",
                    (normalized,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT u.id, u.password_hash, u.status, u.must_change_password "
                    "FROM users u JOIN organizations o ON o.id = u.organization_id "
                    "WHERE o.slug = %s AND o.status = 'active' AND o.deleted_at IS NULL "
                    "AND u.username_normalized = %s AND u.deleted_at IS NULL",
                    (organization_slug, normalized),
                ).fetchone()
            if not row or row[2] != "active" or not verify_password(row[1], password):
                raise ValueError("Invalid username or password")
            if row[1].startswith("pbkdf2_sha256$") or HASHER.check_needs_rehash(row[1]):
                connection.execute(
                    "UPDATE users SET password_hash = %s WHERE id = %s",
                    (password_hash(password), row[0]),
                )
            token = secrets.token_urlsafe(32)
            connection.execute(
                "INSERT INTO sessions (user_id, token_hash, expires_at) VALUES (%s, %s, %s)",
                (
                    row[0],
                    hashlib.sha256(token.encode()).hexdigest(),
                    datetime.now(UTC) + timedelta(hours=SESSION_HOURS),
                ),
            )
            return token

    def session(self, token: str | None) -> tuple[AuthContext | None, float]:
        if not token:
            return None, 0
        with self.connect() as connection:
            context = resolve_session(connection, token)
            if context is None:
                return None, 0
            row = connection.execute(
                "SELECT extract(epoch FROM expires_at - now()) FROM sessions "
                "WHERE token_hash = %s AND revoked_at IS NULL",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
            return context, max(0, float(row[0])) if row else 0

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self.connect() as connection:
            connection.execute(
                "UPDATE sessions SET revoked_at = now() "
                "WHERE token_hash = %s AND revoked_at IS NULL",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )

    def change_password(self, user_id: UUID, current: str, replacement: str) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT password_hash FROM users WHERE id = %s AND deleted_at IS NULL FOR UPDATE",
                (user_id,),
            ).fetchone()
            if not row or not verify_password(row[0], current):
                raise ValueError("Current password is incorrect")
            connection.execute(
                "UPDATE users SET password_hash = %s, must_change_password = false WHERE id = %s",
                (password_hash(replacement), user_id),
            )
            connection.execute(
                "UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
                (user_id,),
            )
