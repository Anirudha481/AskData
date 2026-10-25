import json

import psycopg2
from fastapi.testclient import TestClient

from app.api import routes
from app.core.sql_executor import ExecutionResult
from app.core.sql_generator import GeneratorResult
from app.main import app


client = TestClient(app)


def _patch_query_dependencies(monkeypatch, tmp_path, generated_results):
    monkeypatch.setattr(routes.settings, "audit_log_path", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(
        routes,
        "read_schema",
        lambda **kwargs: {
            "table_names": ["customers"],
            "tables": {
                "customers": {
                    "description": "",
                    "columns": [
                        {
                            "name": "customer_id",
                            "type": "varchar",
                            "nullable": False,
                            "pk": True,
                            "fk": None,
                            "description": "",
                        }
                    ],
                }
            },
        },
    )
    monkeypatch.setattr(routes, "load_allowed_tables", lambda path=None: {"customers"})
    calls = []

    def fake_generate(question, **kwargs):
        calls.append((question, kwargs))
        return generated_results.pop(0)

    monkeypatch.setattr(routes, "generate_sql", fake_generate)
    return calls


def test_query_happy_path_returns_results_chart_explanation_and_audit(monkeypatch, tmp_path):
    sql = (
        "SELECT country, COUNT(*) AS customer_count FROM customers "
        "GROUP BY country ORDER BY customer_count DESC"
    )
    calls = _patch_query_dependencies(
        monkeypatch,
        tmp_path,
        [GeneratorResult(kind="sql", sql=sql)],
    )
    executed = []

    def fake_execute(query, **kwargs):
        executed.append(query)
        return ExecutionResult(
            columns=["country", "customer_count"],
            rows=[{"country": "USA", "customer_count": 13}],
            row_count=1,
            sql=query,
        )

    monkeypatch.setattr(routes, "execute_readonly", fake_execute)
    response = client.post("/api/query", json={"question": "Count customers by country"})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["row_count"] == 1
    assert body["chart"] == {
        "type": "bar",
        "x_axis": "country",
        "y_axis": "customer_count",
    }
    assert "Groups rows" in body["explanation"]
    assert "USA" not in body["explanation"]
    assert executed[0].endswith("LIMIT 1000")
    assert calls[0][0] == "Count customers by country"
    audit = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8"))
    assert audit["validation_outcome"] == "passed"
    assert audit["row_count"] == 1
    assert audit["sql"] == body["sql"]


def test_query_returns_clarification_without_executing(monkeypatch, tmp_path):
    _patch_query_dependencies(
        monkeypatch,
        tmp_path,
        [
            GeneratorResult(
                kind="clarification",
                clarification_question="Which year do you mean?",
            )
        ],
    )
    monkeypatch.setattr(
        routes,
        "execute_readonly",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("executed")),
    )

    response = client.post("/api/query", json={"question": "What about last year?"})

    assert response.status_code == 200
    assert response.json()["status"] == "clarification"
    assert response.json()["message"] == "Which year do you mean?"


def test_query_blocks_generated_unsafe_sql_before_database(monkeypatch, tmp_path):
    _patch_query_dependencies(
        monkeypatch,
        tmp_path,
        [GeneratorResult(kind="sql", sql="DROP TABLE customers")],
    )
    monkeypatch.setattr(
        routes,
        "execute_readonly",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("executed")),
    )

    response = client.post("/api/query", json={"question": "Delete customers"})

    assert response.status_code == 422
    assert response.json()["status"] == "blocked"
    assert "Only a SELECT" in response.json()["message"]


def test_query_retries_sql_error_once_and_validates_repaired_query(monkeypatch, tmp_path):
    original = GeneratorResult(
        kind="sql",
        sql="SELECT unknown_customer_function(customer_id) FROM customers",
    )
    repaired = GeneratorResult(kind="sql", sql="SELECT customer_id FROM customers")
    calls = _patch_query_dependencies(monkeypatch, tmp_path, [original, repaired])
    executions = []

    def fake_execute(query, **kwargs):
        executions.append(query)
        if len(executions) == 1:
            raise psycopg2.errors.UndefinedFunction(
                'function unknown_customer_function(character varying) does not exist'
            )
        return ExecutionResult(
            columns=["customer_id"],
            rows=[{"customer_id": "ALFKI"}],
            row_count=1,
            sql=query,
        )

    monkeypatch.setattr(routes, "execute_readonly", fake_execute)
    response = client.post("/api/query", json={"question": "List customers"})

    assert response.status_code == 200
    assert response.json()["retry_count"] == 1
    assert response.json()["status"] == "success"
    assert calls[1][1]["previous_sql"].endswith("LIMIT 1000")
    assert "unknown_customer_function" in calls[1][1]["sql_error"]
    assert len(executions) == 2


def test_retry_query_is_validated_before_second_execution(monkeypatch, tmp_path):
    calls = _patch_query_dependencies(
        monkeypatch,
        tmp_path,
        [
            GeneratorResult(kind="sql", sql="SELECT * FROM customers"),
            GeneratorResult(kind="sql", sql="DELETE FROM customers"),
        ],
    )
    execution_count = 0

    def fake_execute(query, **kwargs):
        nonlocal execution_count
        execution_count += 1
        raise psycopg2.errors.SyntaxError("syntax error")

    monkeypatch.setattr(routes, "execute_readonly", fake_execute)
    response = client.post("/api/query", json={"question": "List customers"})

    assert response.status_code == 422
    assert response.json()["status"] == "blocked"
    assert response.json()["retry_count"] == 1
    assert execution_count == 1
    assert len(calls) == 2


def test_query_does_not_retry_database_connection_errors(monkeypatch, tmp_path):
    _patch_query_dependencies(
        monkeypatch,
        tmp_path,
        [GeneratorResult(kind="sql", sql="SELECT * FROM customers")],
    )
    executions = []

    def fake_execute(query, **kwargs):
        executions.append(query)
        raise psycopg2.OperationalError("connection refused")

    monkeypatch.setattr(routes, "execute_readonly", fake_execute)
    response = client.post("/api/query", json={"question": "List customers"})

    assert response.status_code == 502
    assert response.json()["status"] == "error"
    assert response.json()["retry_count"] == 0
    assert len(executions) == 1


def test_query_input_is_trimmed_and_blank_is_rejected():
    response = client.post("/api/query", json={"question": "   "})
    assert response.status_code == 422


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
