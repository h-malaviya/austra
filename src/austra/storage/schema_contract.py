"""Fail startup when live tables disagree with ORM-mapped columns."""

import psycopg
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Executable

from austra.storage.orm import Base

EXPECTED_TYPES = {
    "UUID": "uuid",
    "BIGINT": "int8",
    "INTEGER": "int4",
    "TEXT": "text",
    "JSONB": "jsonb",
    "BOOLEAN": "bool",
    "TIMESTAMP WITH TIME ZONE": "timestamptz",
}


class SchemaContractError(RuntimeError):
    pass


def mapped_query(connection: psycopg.Connection, statement: Executable):
    """Use ORM column definitions while retaining the caller's RLS transaction."""
    compiled = statement.compile(dialect=postgresql.dialect(paramstyle="pyformat"))
    return connection.execute(str(compiled), compiled.params)


def validate_schema(connection: psycopg.Connection) -> None:
    tables = list(Base.metadata.tables)
    actual = {
        (table, column): data_type
        for table, column, data_type in connection.execute(
            "SELECT table_name, column_name, udt_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ANY(%s)",
            (tables,),
        ).fetchall()
    }
    problems = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            key = (table.name, column.name)
            expected = EXPECTED_TYPES[column.type.compile(dialect=postgresql.dialect()).upper()]
            observed = actual.get(key)
            if observed != expected:
                problems.append(f"{table.name}.{column.name}: expected {expected}, found {observed or 'missing'}")
    if problems:
        raise SchemaContractError("Schema mismatch after migrations: " + "; ".join(problems))
