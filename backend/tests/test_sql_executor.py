from unittest.mock import Mock, patch

import pytest

from app.core import sql_executor
from app.core.sql_validator import SQLValidationError


def _fake_connection():
    cursor = Mock()
    cursor.description = [("customer_id",), ("company_name",)]
    cursor.fetchall.return_value = [("ALFKI", "Alfreds Futterkiste")]
    cursor_context = Mock()
    cursor_context.__enter__ = Mock(return_value=cursor)
    cursor_context.__exit__ = Mock(return_value=False)

    connection = Mock()
    connection.cursor.return_value = cursor_context
    return connection, cursor


def test_executor_validates_then_runs_with_read_only_and_timeout_settings():
    connection, cursor = _fake_connection()
    with patch.object(sql_executor.psycopg2, "connect", return_value=connection) as connect:
        result = sql_executor.execute_readonly(
            "SELECT customer_id, company_name FROM customers",
            dsn="postgresql://reader:test@localhost/northwind",
            allowed_tables={"customers"},
        )

    connect.assert_called_once_with("postgresql://reader:test@localhost/northwind")
    connection.set_session.assert_called_once_with(readonly=True, autocommit=False)
    cursor.execute.assert_any_call("SET TRANSACTION READ ONLY")
    cursor.execute.assert_any_call("SET LOCAL statement_timeout = %s", (10000,))
    assert cursor.execute.call_args_list[-1].args[0].startswith("SELECT")
    assert result.columns == ["customer_id", "company_name"]
    assert result.rows == [
        {"customer_id": "ALFKI", "company_name": "Alfreds Futterkiste"}
    ]
    assert result.row_count == 1
    assert result.sql.endswith("LIMIT 1000")
    connection.close.assert_called_once()


def test_executor_never_connects_for_blocked_sql():
    with patch.object(sql_executor.psycopg2, "connect") as connect:
        with pytest.raises(SQLValidationError):
            sql_executor.execute_readonly(
                "DELETE FROM customers",
                allowed_tables={"customers"},
            )

    connect.assert_not_called()


def test_executor_surfaces_database_errors_and_closes_connection():
    connection, cursor = _fake_connection()
    cursor.execute.side_effect = RuntimeError("database rejected query")

    with patch.object(sql_executor.psycopg2, "connect", return_value=connection):
        with pytest.raises(RuntimeError, match="database rejected query"):
            sql_executor.execute_readonly(
                "SELECT customer_id FROM customers",
                allowed_tables={"customers"},
            )

    connection.close.assert_called_once()
