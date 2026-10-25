import json
from decimal import Decimal

from fastapi.testclient import TestClient

from benchmarks import run_benchmark as benchmark


def test_result_comparison_ignores_aliases_and_unordered_row_order():
    generated = [{"new_alias": 2}, {"new_alias": 1}]
    reference = [{"old_alias": 1}, {"old_alias": 2}]

    assert benchmark.result_sets_match(generated, reference, ordered=False)
    assert not benchmark.result_sets_match(generated, reference, ordered=True)


def test_result_comparison_handles_numeric_types_and_multiple_columns():
    generated = [{"count": 2, "avg": 2.5}]
    reference = [{"different_count_name": 2.0, "different_avg_name": 2.50}]

    assert benchmark.result_sets_match(generated, reference, ordered=True)


def test_result_comparison_treats_json_decimal_strings_as_numbers():
    generated = [{"amount": "28.83"}, {"amount": "267868.18"}]
    reference = [{"expected": Decimal("267868.18")}, {"expected": Decimal("28.830")}]

    assert benchmark.result_sets_match(generated, reference, ordered=False)


def test_result_comparison_does_not_treat_numeric_text_as_equal_to_other_text():
    generated = [{"code": "001"}]
    reference = [{"code": "1"}]

    assert not benchmark.result_sets_match(generated, reference, ordered=True)


def test_result_comparison_rejects_different_projection_width():
    generated = [{"name": "A", "extra_id": 1}]
    reference = [{"name": "A"}]

    assert not benchmark.result_sets_match(generated, reference, ordered=True)


def test_percentile_interpolates_between_values():
    assert benchmark._percentile([1, 2, 3, 4, 5], 0.95) == 4.8


def test_rate_limit_retry_waits_for_provider_delay_and_recovers():
    class Response:
        status_code = 502

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class Client:
        def __init__(self):
            self.responses = iter(
                [
                    Response(
                        {
                            "status": "error",
                            "message": (
                                "Groq API error 429: Rate limit reached. "
                                "Please try again in 6.4s."
                            ),
                        }
                    ),
                    Response({"status": "success"}),
                ]
            )

        def post(self, path, json):
            assert path == "/api/query"
            return next(self.responses)

    delays = []
    response, payload, retries = benchmark._post_with_rate_limit_retries(
        Client(),
        "How many customers?",
        sleep_fn=delays.append,
    )

    assert response.status_code == 502
    assert payload["status"] == "success"
    assert retries == 1
    assert delays == [6.65]


def test_rate_limit_retry_count_is_bounded():
    class Response:
        status_code = 502

        def json(self):
            return {
                "status": "error",
                "message": "Groq API error 429: Rate limit reached",
            }

    class Client:
        def __init__(self):
            self.calls = 0

        def post(self, path, json):
            self.calls += 1
            return Response()

    client = Client()
    response, payload, retries = benchmark._post_with_rate_limit_retries(
        client,
        "How many customers?",
        max_retries=2,
        sleep_fn=lambda delay: None,
    )

    assert response.status_code == 502
    assert payload["status"] == "error"
    assert retries == 2
    assert client.calls == 3


def test_runner_saves_report_and_failure_sql(tmp_path, monkeypatch):
    questions_path = tmp_path / "questions.json"
    questions_path.write_text(
        json.dumps(
            [
                {
                    "id": 1,
                    "difficulty": "single-table",
                    "question": "How many customers?",
                    "reference_sql": "SELECT COUNT(*) AS count FROM customers",
                },
                {
                    "id": 2,
                    "difficulty": "join-filter",
                    "question": "Show customer names",
                    "reference_sql": "SELECT company_name FROM customers",
                },
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(benchmark, "load_allowed_tables", lambda: {"customers"})
    responses = iter(
        [
            {
                "status": "success",
                "sql": "SELECT COUNT(*) AS generated_count FROM customers",
                "rows": [{"generated_count": 3}],
                "row_count": 1,
            },
            {
                "status": "error",
                "sql": "SELECT * FROM unknown",
                "rows": [],
                "row_count": 0,
                "message": "failed query",
            },
        ]
    )

    class FakeResponse:
        status_code = 200

        def json(self):
            return next(responses)

    class FakeClient:
        def post(self, path, json):
            assert path == "/api/query"
            return FakeResponse()

    reference_rows = iter(
        [
            type("Execution", (), {"rows": [{"count": 3}], "row_count": 1})(),
        ]
    )
    monkeypatch.setattr(
        benchmark,
        "execute_readonly",
        lambda *args, **kwargs: next(reference_rows),
    )

    report = benchmark.run_benchmark(
        questions_path=questions_path,
        results_dir=tmp_path / "results",
        client=FakeClient(),
    )

    with open(report["report_path"], encoding="utf-8") as report_file:
        saved_report = json.load(report_file)
    assert report["score"] == "1/2"
    assert report["median_latency_seconds"] is not None
    assert report["p95_latency_seconds"] is not None
    assert saved_report["failures"][0]["generated_sql"] == "SELECT * FROM unknown"
    assert saved_report["failures"][0]["reference_sql"] == "SELECT company_name FROM customers"
