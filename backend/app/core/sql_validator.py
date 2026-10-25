"""Validate and normalize generated SQL before it reaches PostgreSQL."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import sqlglot
import yaml
from sqlglot import exp
from sqlglot.errors import ParseError

from app.core.config import settings


class SQLValidationError(ValueError):
    """Raised when a generated query violates the read-only SQL policy."""


FORBIDDEN_NODE_NAMES = {
    "Alter",
    "Command",
    "Copy",
    "Create",
    "Delete",
    "Drop",
    "Grant",
    "Insert",
    "Merge",
    "Revoke",
    "Set",
    "Transaction",
    "TruncateTable",
    "Update",
    "Use",
}

DANGEROUS_FUNCTIONS = {
    "dblink",
    "lo_export",
    "lo_import",
    "nextval",
    "pg_advisory_lock",
    "pg_advisory_lock_shared",
    "pg_notify",
    "pg_read_binary_file",
    "pg_read_file",
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",
    "pg_terminate_backend",
    "set_config",
    "setval",
}


def load_allowed_tables(config_path: str | Path | None = None) -> set[str]:
    """Load the table allowlist from the project's schema configuration."""
    path = Path(config_path or settings.schema_config_path)
    if not path.is_absolute() and not path.is_file():
        path = Path(__file__).resolve().parents[2] / path

    with path.open(encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file) or {}

    tables = config.get("tables")
    if not isinstance(tables, dict) or not tables:
        raise ValueError(f"No tables are configured in {path}")
    return {str(table).casefold() for table in tables}


def _contains_sql_comment(sql: str) -> bool:
    """Detect comments outside quoted strings and identifiers."""
    index = 0
    while index < len(sql):
        if sql[index] == "'":
            index += 1
            while index < len(sql):
                if sql[index] == "\\":
                    index += 2
                elif sql[index] == "'":
                    if index + 1 < len(sql) and sql[index + 1] == "'":
                        index += 2
                    else:
                        index += 1
                        break
                else:
                    index += 1
            continue

        if sql[index] == '"':
            index += 1
            while index < len(sql):
                if sql[index] == '"':
                    if index + 1 < len(sql) and sql[index + 1] == '"':
                        index += 2
                    else:
                        index += 1
                        break
                else:
                    index += 1
            continue

        if sql[index] == "$":
            delimiter_end = sql.find("$", index + 1)
            if delimiter_end != -1:
                tag = sql[index : delimiter_end + 1]
                if tag[1:-1].replace("_", "").isalnum() or tag == "$$":
                    body_end = sql.find(tag, delimiter_end + 1)
                    if body_end != -1:
                        index = body_end + len(tag)
                        continue

        if sql.startswith("--", index):
            return True
        if sql.startswith("/*", index):
            return True
        index += 1
    return False


def _cte_names(statement: exp.Expression) -> set[str]:
    return {
        cte.alias_or_name.casefold()
        for cte in statement.find_all(exp.CTE)
        if cte.alias_or_name
    }


def validate_sql(
    sql: str,
    allowed_tables: Iterable[str] | None = None,
    default_limit: int | None = None,
) -> str:
    """Return one safe PostgreSQL SELECT, with a default LIMIT if necessary."""
    if not isinstance(sql, str) or not sql.strip():
        raise SQLValidationError("Query must be a non-empty SQL string.")
    if _contains_sql_comment(sql):
        raise SQLValidationError("SQL comments are not allowed.")

    try:
        statements = sqlglot.parse(sql, read="postgres")
    except ParseError as exc:
        raise SQLValidationError(f"Could not parse SQL: {exc}") from exc

    if len(statements) != 1 or statements[0] is None:
        raise SQLValidationError("Exactly one SELECT statement is allowed.")

    statement = statements[0]
    if not isinstance(statement, exp.Select):
        raise SQLValidationError("Only a SELECT statement is allowed.")

    if any(type(node).__name__ in FORBIDDEN_NODE_NAMES for node in statement.walk()):
        raise SQLValidationError("DDL and DML statements are not allowed.")
    if statement.args.get("into") is not None:
        raise SQLValidationError("SELECT INTO is not allowed.")
    if statement.args.get("locks"):
        raise SQLValidationError("Locking SELECT queries are not allowed.")

    for function in statement.find_all(exp.Func):
        name = function.name.casefold()
        if name in DANGEROUS_FUNCTIONS or name.startswith("pg_"):
            raise SQLValidationError(f"Dangerous function '{name}' is not allowed.")

    configured_tables = {
        table.casefold()
        for table in (
            load_allowed_tables() if allowed_tables is None else allowed_tables
        )
    }
    cte_names = _cte_names(statement)
    for table in statement.find_all(exp.Table):
        name = table.name.casefold()
        if not name:
            raise SQLValidationError("Table-valued functions are not allowed.")
        if table.db or table.catalog:
            schema = table.db.casefold()
            if table.catalog or schema not in {"", "public"}:
                raise SQLValidationError(
                    f"Schema-qualified table '{table.sql()}' is not allowed."
                )
        if name.startswith("pg_") or name == "information_schema":
            raise SQLValidationError("System catalog tables are not allowed.")
        if not table.db and name in cte_names:
            continue
        if name not in configured_tables:
            raise SQLValidationError(f"Table '{name}' is not allowlisted.")

    limit = statement.args.get("limit")
    if limit is None:
        row_limit = (
            settings.query_row_limit if default_limit is None else default_limit
        )
        if row_limit <= 0:
            raise ValueError("The default row limit must be a positive integer.")
        statement = statement.limit(row_limit)

    return statement.sql(dialect="postgres")
