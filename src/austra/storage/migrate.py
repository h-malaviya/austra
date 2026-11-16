"""Apply ordered PostgreSQL schema migrations."""

import os
from importlib.resources import files

import psycopg

MIGRATION_LOCK_ID = 20261116


def apply_migrations(connection: psycopg.Connection) -> list[str]:
    applied = []
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,))
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        existing = {
            row[0] for row in connection.execute("SELECT name FROM schema_migrations").fetchall()
        }
        migrations = files("austra.storage.migrations")
        for migration in sorted(migrations.iterdir(), key=lambda item: item.name):
            if migration.suffix != ".sql" or migration.name in existing:
                continue
            connection.execute(migration.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO schema_migrations (name) VALUES (%s)", (migration.name,)
            )
            applied.append(migration.name)
    return applied


def main() -> None:
    database_url = os.environ.get("AUSTRA_DATABASE_URL")
    if not database_url:
        raise SystemExit("AUSTRA_DATABASE_URL is required")
    with psycopg.connect(database_url) as connection:
        apply_migrations(connection)


if __name__ == "__main__":
    main()
