import pytest

from app.core.sql_validator import SQLValidationError, validate_sql


ALLOWED_TABLES = {"customers", "orders", "order_details"}


def test_valid_select_passes_and_adds_default_limit():
    result = validate_sql(
        "SELECT customer_id, company_name FROM customers",
        allowed_tables=ALLOWED_TABLES,
        default_limit=1000,
    )

    assert result.endswith("LIMIT 1000")


def test_existing_limit_is_preserved():
    result = validate_sql(
        "SELECT customer_id FROM customers LIMIT 5",
        allowed_tables=ALLOWED_TABLES,
    )

    assert result.endswith("LIMIT 5")


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE customers",
        "DELETE FROM customers",
        "UPDATE customers SET company_name = 'x'",
        "INSERT INTO customers (customer_id) VALUES ('x')",
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE customers",
        "SELECT 1 -- comment hides the rest",
        "SELECT 1 /* hidden statement */",
        "SELECT pg_sleep(10)",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_catalog.pg_typeof(customer_id) FROM customers",
        "SELECT customer_id FROM customers FOR UPDATE",
        "SELECT * FROM private.customers",
        "SELECT * FROM pg_catalog.pg_tables",
        "SELECT * FROM pg_tables",
        "SELECT nextval('some_sequence')",
        "SELECT * INTO copied_customers FROM customers",
        "SELECT * FROM unconfigured_table",
    ],
)
def test_unsafe_or_invalid_queries_are_rejected(sql):
    with pytest.raises(SQLValidationError):
        validate_sql(sql, allowed_tables=ALLOWED_TABLES)


def test_comment_markers_inside_string_literals_are_not_comments():
    result = validate_sql(
        "SELECT '-- not a comment' AS text FROM customers",
        allowed_tables=ALLOWED_TABLES,
    )

    assert "-- not a comment" in result
    assert "LIMIT" in result


def test_select_with_allowlisted_tables_and_cte_passes():
    result = validate_sql(
        "WITH recent AS (SELECT customer_id FROM customers) "
        "SELECT recent.customer_id FROM recent JOIN orders "
        "ON recent.customer_id = orders.customer_id",
        allowed_tables=ALLOWED_TABLES,
    )

    assert "LIMIT 1000" in result


def test_rejects_non_positive_default_limit():
    with pytest.raises(ValueError, match="positive"):
        validate_sql(
            "SELECT customer_id FROM customers",
            allowed_tables=ALLOWED_TABLES,
            default_limit=0,
        )


def test_empty_allowlist_does_not_fall_back_to_configured_tables():
    with pytest.raises(SQLValidationError, match="not allowlisted"):
        validate_sql("SELECT * FROM customers", allowed_tables=set())
