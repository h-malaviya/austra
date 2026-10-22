"""Versioned, additive migrations for existing local databases."""

import sqlite3
from datetime import UTC, datetime

from austra.schema import SCHEMA

PO_TERMS_MIGRATION = "20261022_po_terms_v2"


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    applied = connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ?", (PO_TERMS_MIGRATION,)
    ).fetchone()
    if applied is not None:
        return
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(purchase_orders)")}
    for name in ("grade", "freight_terms", "tax_terms"):
        if name not in columns:
            connection.execute(
                f"ALTER TABLE purchase_orders ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
            )
    connection.execute(
        "INSERT INTO schema_migrations VALUES (?, ?)",
        (PO_TERMS_MIGRATION, datetime.now(UTC).isoformat()),
    )
