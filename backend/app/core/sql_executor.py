"""Execute validated read-only queries with a database-side timeout."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg2

from app.core.config import settings
from app.core.sql_validator import load_allowed_tables, validate_sql


@dataclass
class ExecutionResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    sql: str


def execute_readonly(
    sql: str,
    *,
    dsn: str | None = None,
    allowed_tables: set[str] | None = None,
) -> ExecutionResult:
    """Validate and execute SQL in an explicitly read-only timed transaction."""
    if settings.query_timeout_seconds <= 0:
        raise ValueError("QUERY_TIMEOUT_SECONDS must be a positive integer.")

    safe_sql = validate_sql(sql, allowed_tables=allowed_tables)
    connection = psycopg2.connect(dsn or settings.readonly_dsn)

    try:
        connection.set_session(readonly=True, autocommit=False)
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SET LOCAL statement_timeout = %s",
                (settings.query_timeout_seconds * 1000,),
            )
            cursor.execute(safe_sql)
            columns = [description[0] for description in cursor.description or []]
            rows = [
                dict(zip(columns, row, strict=True))
                for row in cursor.fetchall()
            ]
            return ExecutionResult(
                columns=columns,
                rows=rows,
                row_count=len(rows),
                sql=safe_sql,
            )
    finally:
        connection.close()
