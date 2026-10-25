"""Run the fixed question set through the API and compare with reference SQL."""

from __future__ import annotations

import json
import math
import re
import statistics
import sys
import time
from datetime import date, datetime, time as datetime_time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import sqlglot
from fastapi.testclient import TestClient
from sqlglot import exp

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.sql_executor import execute_readonly
from app.core.sql_validator import load_allowed_tables
from app.main import app

QUESTIONS_PATH = PROJECT_ROOT / "benchmarks" / "questions.json"
RESULTS_DIR = BACKEND_DIR / "benchmarks" / "results"


def _canonical_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, Decimal):
        return {"number": str(value.normalize())}
    if isinstance(value, int):
        return {"number": str(Decimal(value).normalize())}
    if isinstance(value, float):
        if math.isnan(value):
            return {"float": "NaN"}
        if math.isinf(value):
            return {"float": "Infinity" if value > 0 else "-Infinity"}
        return {"number": str(Decimal(str(value)).normalize())}
    if isinstance(value, (datetime, date, datetime_time)):
        return {"datetime": value.isoformat()}
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    return {"value": str(value)}


def _row_values(rows: list[dict[str, Any]]) -> list[list[Any]]:
    return [list(row.values()) for row in rows]


def _is_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float, Decimal))
    )


def _values_match(generated: Any, reference: Any) -> bool:
    if _is_number(generated) and isinstance(reference, str):
        try:
            return Decimal(str(generated)).normalize() == Decimal(reference).normalize()
        except InvalidOperation:
            return False
    if isinstance(generated, str) and _is_number(reference):
        try:
            return Decimal(generated).normalize() == Decimal(str(reference)).normalize()
        except InvalidOperation:
            return False
    return _canonical_value(generated) == _canonical_value(reference)


def _rows_match(generated: list[Any], reference: list[Any]) -> bool:
    return len(generated) == len(reference) and all(
        _values_match(generated_value, reference_value)
        for generated_value, reference_value in zip(generated, reference, strict=True)
    )


def _has_order_by(sql: str) -> bool:
    statement = sqlglot.parse_one(sql, read="postgres")
    return isinstance(statement, exp.Query) and statement.args.get("order") is not None


def result_sets_match(
    generated_rows: list[dict[str, Any]],
    reference_rows: list[dict[str, Any]],
    *,
    ordered: bool,
) -> bool:
    """Compare positional result values, ignoring aliases and optionally row order."""
    generated = _row_values(generated_rows)
    reference = _row_values(reference_rows)
    if len(generated) != len(reference):
        return False
    if not generated:
        return True
    row_width = len(generated[0])
    if any(len(row) != row_width for row in generated + reference):
        return False
    if not ordered:
        unmatched = list(reference)
        for generated_row in generated:
            match_index = next(
                (
                    index
                    for index, reference_row in enumerate(unmatched)
                    if _rows_match(generated_row, reference_row)
                ),
                None,
            )
            if match_index is None:
                return False
            unmatched.pop(match_index)
        return not unmatched
    return len(generated) == len(reference) and all(
        _rows_match(generated_row, reference_row)
        for generated_row, reference_row in zip(generated, reference, strict=True)
    )


def _load_questions(path: Path = QUESTIONS_PATH) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as questions_file:
        questions = json.load(questions_file)
    if not isinstance(questions, list) or not questions:
        raise ValueError(f"Benchmark file {path} must contain a non-empty list.")
    required = {"id", "question", "reference_sql"}
    for index, question in enumerate(questions):
        missing = required - question.keys()
        if missing:
            raise ValueError(
                f"Benchmark question at index {index} is missing fields: "
                f"{', '.join(sorted(missing))}"
            )
    return questions


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _rate_limit_delay(message: str, retry_number: int) -> float:
    match = re.search(r"try again in\s+([0-9]+(?:\.[0-9]+)?)\s*s", message, re.IGNORECASE)
    if match:
        return min(float(match.group(1)) + 0.25, 60.0)
    return min(float(2 ** (retry_number - 1)), 60.0)


def _post_with_rate_limit_retries(
    client: TestClient,
    question: str,
    *,
    max_retries: int = 3,
    sleep_fn: Any = time.sleep,
) -> tuple[Any, dict[str, Any], int]:
    """Retry API calls only for provider rate-limit errors."""
    retries = 0
    while True:
        response = client.post("/api/query", json={"question": question})
        payload = response.json()
        message = str(payload.get("message") or "")
        is_rate_limited = (
            response.status_code == 429
            or (
                response.status_code == 502
                and "429" in message
                and "rate limit" in message.casefold()
            )
        )
        if not is_rate_limited or retries >= max_retries:
            return response, payload, retries

        retries += 1
        delay = _rate_limit_delay(message, retries)
        print(f"  Rate limit; retry {retries}/{max_retries} in {delay:.2f}s")
        sleep_fn(delay)


def run_benchmark(
    *,
    questions_path: Path = QUESTIONS_PATH,
    results_dir: Path = RESULTS_DIR,
    client: TestClient | None = None,
) -> dict[str, Any]:
    """Run all benchmark items through the API and save a timestamped report."""
    questions = _load_questions(questions_path)
    allowed_tables = load_allowed_tables()
    latencies: list[float] = []
    failures: list[dict[str, Any]] = []
    rate_limit_retries = 0
    owns_client = client is None
    api_client = client or TestClient(app)

    try:
        for question in questions:
            started_at = time.perf_counter()
            generated_sql: str | None = None
            retries = 0
            try:
                response, payload, retries = _post_with_rate_limit_retries(
                    api_client,
                    question["question"],
                )
                rate_limit_retries += retries
                elapsed = time.perf_counter() - started_at
                latencies.append(elapsed)
                generated_sql = payload.get("sql")
                failure: dict[str, Any] | None = None

                if response.status_code != 200 or payload.get("status") != "success":
                    failure = {
                        "reason": (
                            f"Pipeline returned HTTP {response.status_code}, "
                            f"status={payload.get('status')}: {payload.get('message')}"
                        ),
                    }
                elif not generated_sql:
                    failure = {"reason": "Pipeline returned no generated SQL."}
                else:
                    reference = execute_readonly(
                        question["reference_sql"],
                        allowed_tables=allowed_tables,
                    )
                    ordered = _has_order_by(question["reference_sql"])
                    if not result_sets_match(
                        payload.get("rows", []),
                        reference.rows,
                        ordered=ordered,
                    ):
                        failure = {
                            "reason": "Generated and reference result sets differ.",
                            "reference_row_count": reference.row_count,
                            "generated_row_count": payload.get("row_count", 0),
                        }

                if failure:
                    failures.append(
                        {
                            "id": question["id"],
                            "difficulty": question.get("difficulty"),
                            "question": question["question"],
                            "rate_limit_retries": retries,
                            "generated_sql": generated_sql,
                            "reference_sql": question["reference_sql"],
                            **failure,
                        }
                    )
                print(
                    f"[{'PASS' if failure is None else 'FAIL'}] "
                    f"{question['id']:>2}/{len(questions)} "
                    f"{elapsed:.2f}s"
                    f"{f' ({retries} rate-limit retries)' if retries else ''} — "
                    f"{question['question']}"
                )
            except Exception as exc:
                latencies.append(time.perf_counter() - started_at)
                failures.append(
                    {
                        "id": question["id"],
                        "difficulty": question.get("difficulty"),
                        "question": question["question"],
                        "rate_limit_retries": retries,
                        "generated_sql": generated_sql,
                        "reference_sql": question["reference_sql"],
                        "reason": f"Benchmark execution failed: {exc}",
                    }
                )
                print(f"[FAIL] {question['id']:>2}/{len(questions)} — {exc}")
    finally:
        if owns_client:
            api_client.close()

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    total = len(questions)
    correct = total - len(failures)
    report = {
        "timestamp": timestamp,
        "total": total,
        "correct": correct,
        "score": f"{correct}/{total}",
        "median_latency_seconds": statistics.median(latencies) if latencies else None,
        "p95_latency_seconds": _percentile(latencies, 0.95),
        "rate_limit_retries": rate_limit_retries,
        "failures": failures,
    }
    results_dir.mkdir(parents=True, exist_ok=True)
    output_path = results_dir / f"benchmark-{timestamp}.json"
    report["report_path"] = str(output_path)
    output_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )

    print(f"\nAccuracy: {correct}/{total}")
    if latencies:
        print(f"Median end-to-end latency: {statistics.median(latencies):.3f}s")
        print(f"95th percentile latency: {_percentile(latencies, 0.95):.3f}s")
    print(f"Report saved: {output_path}")
    return report


if __name__ == "__main__":
    run_benchmark()
