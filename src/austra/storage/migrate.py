"""Apply ordered PostgreSQL schema migrations."""

import hashlib
import os
import re
from importlib.resources import files

import psycopg

from austra.storage.schema_contract import validate_schema

MIGRATION_LOCK_ID = 20261116
MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


class MigrationError(RuntimeError):
    pass


def _versioned_history(connection: psycopg.Connection) -> bool:
    return bool(connection.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = 'schema_migrations' AND column_name = 'checksum'"
    ).fetchone())


def apply_migrations(connection: psycopg.Connection) -> list[str]:
    applied = []
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,))
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        migrations = files("austra.storage.migrations")
        scripts = sorted(
            (item for item in migrations.iterdir() if item.suffix == ".sql"),
            key=lambda item: item.name,
        )
        versions = {}
        checksums = {}
        for migration in scripts:
            match = MIGRATION_NAME.fullmatch(migration.name)
            if match is None:
                raise MigrationError(f"Invalid migration filename: {migration.name}")
            version = int(match[1])
            if version in versions:
                raise MigrationError(
                    f"Duplicate migration version {version}: {versions[version]} and {migration.name}"
                )
            versions[version] = migration.name
            checksums[migration.name] = hashlib.sha256(
                migration.read_bytes()
            ).hexdigest()
        versioned = _versioned_history(connection)
        if versioned:
            for name, version, checksum in connection.execute(
                "SELECT name, version, checksum FROM schema_migrations"
            ).fetchall():
                match = MIGRATION_NAME.fullmatch(name)
                expected = checksums.get(name)
                if match is None or expected is None or checksum != expected or version != int(match[1]):
                    raise MigrationError(f"Applied migration changed or missing: {name}")
        existing = {
            row[0] for row in connection.execute("SELECT name FROM schema_migrations").fetchall()
        }
        for migration in scripts:
            if migration.name in existing:
                continue
            try:
                connection.execute(migration.read_text(encoding="utf-8"))
            except Exception as error:
                raise MigrationError(
                    f"Migration {migration.name} failed (sha256 {checksums[migration.name]}): "
                    f"{error.__class__.__name__}"
                ) from error
            if _versioned_history(connection):
                if not versioned:
                    for name in existing:
                        match = MIGRATION_NAME.fullmatch(name)
                        if match is None or name not in checksums:
                            raise MigrationError(f"Legacy migration has no matching script: {name}")
                        connection.execute(
                            "UPDATE schema_migrations SET version = %s, checksum = %s, "
                            "baseline = true WHERE name = %s",
                            (int(match[1]), checksums[name], name),
                        )
                    versioned = True
                connection.execute(
                    "INSERT INTO schema_migrations (name, version, checksum) VALUES (%s, %s, %s)",
                    (migration.name, int(migration.name[:3]), checksums[migration.name]),
                )
            else:
                connection.execute(
                    "INSERT INTO schema_migrations (name) VALUES (%s)", (migration.name,)
                )
            existing.add(migration.name)
            applied.append(migration.name)
        validate_schema(connection)
    return applied


def main() -> None:
    database_url = os.environ.get("AUSTRA_DATABASE_URL")
    if not database_url:
        raise SystemExit("AUSTRA_DATABASE_URL is required")
    with psycopg.connect(database_url) as connection:
        apply_migrations(connection)


if __name__ == "__main__":
    main()
