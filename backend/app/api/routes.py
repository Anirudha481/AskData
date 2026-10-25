"""HTTP endpoints for generating, validating, and executing read-only queries."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Literal

import psycopg2
import sqlglot
from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlglot import exp

from app.core.config import settings
from app.core.schema_reader import read_schema, schema_to_prompt_text
from app.core.sql_executor import ExecutionResult, execute_readonly
from app.core.sql_generator import GeneratorResult, generate_sql
from app.core.sql_validator import SQLValidationError, load_allowed_tables, validate_sql

router = APIRouter(prefix="/api")
_audit_lock = threading.Lock()


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)

    @field_validator("question")
    @classmethod
    def question_must_not_be_blank(cls, question: str) -> str:
        question = question.strip()
        if not question:
            raise ValueError("Question must not be blank.")
        return question


class ChartSuggestion(BaseModel):
    type: Literal["bar", "line", "none"] = "none"
    x_axis: str | None = None
    y_axis: str | None = None


class QueryResponse(BaseModel):
    status: Literal["success", "clarification", "blocked", "error"]
    question: str
    sql: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    chart: ChartSuggestion = Field(default_factory=ChartSuggestion)
    explanation: str | None = None
    message: str | None = None
    retry_count: int = 0
    latency_seconds: float


def _audit_path() -> Path:
    path = Path(settings.audit_log_path)
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parents[2] / path


def _append_audit(
    *,
    question: str,
    sql: str | None,
    validation_outcome: str,
    row_count: int,
    latency_seconds: float,
    response_status: str,
) -> None:
    record = {
        "timestamp": time.time(),
        "question": question,
        "sql": sql,
        "validation_outcome": validation_outcome,
        "row_count": row_count,
        "latency_seconds": latency_seconds,
        "status": response_status,
    }
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _audit_lock, path.open("a", encoding="utf-8") as audit_file:
        audit_file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _sql_explanation(sql: str) -> str:
    statement = sqlglot.parse_one(sql, read="postgres")
    cte_names = {
        cte.alias_or_name.casefold()
        for cte in statement.find_all(exp.CTE)
        if cte.alias_or_name
    }
    tables = sorted(
        {
            table.name
            for table in statement.find_all(exp.Table)
            if table.name and table.name.casefold() not in cte_names
        }
    )
    parts = [
        f"Reads from {', '.join(tables)}." if tables else "Reads computed values."
    ]
    if statement.args.get("where"):
        parts.append("Filters rows using the WHERE condition.")
    if statement.args.get("joins"):
        parts.append("Combines related tables using joins.")
    if statement.args.get("group"):
        parts.append("Groups rows by the selected grouping columns.")
    if statement.args.get("having"):
        parts.append("Filters grouped results using HAVING.")
    if statement.args.get("order"):
        parts.append("Sorts the results using ORDER BY.")
    if statement.args.get("limit"):
        parts.append("Caps the returned rows with LIMIT.")
    return " ".join(parts)


def _chart_suggestion(sql: str, columns: list[str]) -> ChartSuggestion:
    statement = sqlglot.parse_one(sql, read="postgres")
    if not statement.args.get("group"):
        return ChartSuggestion()

    projections = statement.expressions
    aggregate_projections = [
        projection
        for projection in projections
        if next(projection.find_all(exp.AggFunc), None) is not None
    ]
    categories = [
        projection
        for projection in projections
        if next(projection.find_all(exp.AggFunc), None) is None
    ]
    if not aggregate_projections or not categories:
        return ChartSuggestion()

    x_axis = categories[0].alias_or_name
    y_axis = aggregate_projections[0].alias_or_name
    if not x_axis or not y_axis or x_axis not in columns or y_axis not in columns:
        return ChartSuggestion()

    date_axis = bool(
        re.search(r"(date|time|year|month|quarter)", x_axis, re.IGNORECASE)
    )
    return ChartSuggestion(
        type="line" if date_axis else "bar",
        x_axis=x_axis,
        y_axis=y_axis,
    )


def _response(
    *,
    question: str,
    result_status: Literal["success", "clarification", "blocked", "error"],
    started_at: float,
    sql: str | None = None,
    validation_outcome: str = "not_attempted",
    row_count: int = 0,
    message: str | None = None,
    execution: ExecutionResult | None = None,
    retry_count: int = 0,
    http_response: Response | None = None,
) -> QueryResponse:
    latency = round(time.perf_counter() - started_at, 6)
    if result_status == "blocked" and http_response:
        http_response.status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    elif result_status == "error" and http_response:
        http_response.status_code = status.HTTP_502_BAD_GATEWAY

    response = QueryResponse(
        status=result_status,
        question=question,
        sql=sql,
        columns=execution.columns if execution else [],
        rows=execution.rows if execution else [],
        row_count=execution.row_count if execution else row_count,
        chart=_chart_suggestion(execution.sql, execution.columns)
        if execution
        else ChartSuggestion(),
        explanation=_sql_explanation(execution.sql) if execution else None,
        message=message,
        retry_count=retry_count,
        latency_seconds=latency,
    )
    _append_audit(
        question=question,
        sql=sql,
        validation_outcome=validation_outcome,
        row_count=response.row_count,
        latency_seconds=latency,
        response_status=result_status,
    )
    return response


@router.post("/query", response_model=QueryResponse)
def query_data(request: QueryRequest, response: Response) -> QueryResponse:
    """Generate, validate, and execute one natural-language data question."""
    started_at = time.perf_counter()
    question = request.question
    generated_sql: str | None = None
    validation_outcome = "not_attempted"
    retry_count = 0

    try:
        schema = read_schema(
            dsn=settings.admin_dsn,
            config_path=settings.schema_config_path,
        )
        schema_text = schema_to_prompt_text(schema)
        allowed_tables = load_allowed_tables(settings.schema_config_path)
        generated: GeneratorResult = generate_sql(question, schema_text=schema_text)
    except Exception as exc:
        return _response(
            question=question,
            result_status="error",
            started_at=started_at,
            validation_outcome=validation_outcome,
            message=f"Could not prepare the query: {exc}",
            http_response=response,
        )

    if generated.kind == "clarification":
        return _response(
            question=question,
            result_status="clarification",
            started_at=started_at,
            validation_outcome="not_required",
            message=generated.clarification_question,
            http_response=response,
        )
    if generated.kind == "error":
        return _response(
            question=question,
            result_status="error",
            started_at=started_at,
            validation_outcome=validation_outcome,
            message=generated.error_message,
            http_response=response,
        )

    generated_sql = generated.sql
    try:
        validated_sql = validate_sql(generated_sql, allowed_tables=allowed_tables)
        validation_outcome = "passed"
    except SQLValidationError as exc:
        return _response(
            question=question,
            result_status="blocked",
            started_at=started_at,
            sql=generated_sql,
            validation_outcome=f"blocked: {exc}",
            message=str(exc),
            http_response=response,
        )

    first_error: psycopg2.Error | None = None
    try:
        execution = execute_readonly(
            validated_sql,
            allowed_tables=allowed_tables,
        )
        return _response(
            question=question,
            result_status="success",
            started_at=started_at,
            sql=execution.sql,
            validation_outcome=validation_outcome,
            execution=execution,
            http_response=response,
        )
    except psycopg2.Error as exc:
        first_error = exc
        if not isinstance(exc, (psycopg2.ProgrammingError, psycopg2.DataError)):
            return _response(
                question=question,
                result_status="error",
                started_at=started_at,
                sql=validated_sql,
                validation_outcome="passed; execution_error",
                message=f"Database query failed: {exc}",
                http_response=response,
            )

    retry_count = 1
    try:
        repaired: GeneratorResult = generate_sql(
            question,
            schema_text=schema_text,
            previous_sql=validated_sql,
            sql_error=str(first_error),
        )
    except Exception as exc:
        return _response(
            question=question,
            result_status="error",
            started_at=started_at,
            sql=validated_sql,
            validation_outcome="passed; retry_generation_error",
            message=f"Could not retry the database query: {exc}",
            retry_count=retry_count,
            http_response=response,
        )

    if repaired.kind == "clarification":
        return _response(
            question=question,
            result_status="clarification",
            started_at=started_at,
            sql=validated_sql,
            validation_outcome="passed; retry_requested_clarification",
            message=repaired.clarification_question,
            retry_count=retry_count,
            http_response=response,
        )
    if repaired.kind == "error":
        return _response(
            question=question,
            result_status="error",
            started_at=started_at,
            sql=validated_sql,
            validation_outcome="passed; retry_generation_error",
            message=repaired.error_message,
            retry_count=retry_count,
            http_response=response,
        )

    generated_sql = repaired.sql
    try:
        retry_sql = validate_sql(generated_sql, allowed_tables=allowed_tables)
        validation_outcome = "passed; retry_validated"
    except SQLValidationError as exc:
        return _response(
            question=question,
            result_status="blocked",
            started_at=started_at,
            sql=generated_sql,
            validation_outcome=f"retry_blocked: {exc}",
            message=str(exc),
            retry_count=retry_count,
            http_response=response,
        )

    try:
        execution = execute_readonly(retry_sql, allowed_tables=allowed_tables)
    except psycopg2.Error as exc:
        return _response(
            question=question,
            result_status="error",
            started_at=started_at,
            sql=retry_sql,
            validation_outcome="passed; retry_execution_error",
            message=f"Database query failed after one retry: {exc}",
            retry_count=retry_count,
            http_response=response,
        )

    return _response(
        question=question,
        result_status="success",
        started_at=started_at,
        sql=execution.sql,
        validation_outcome=validation_outcome,
        execution=execution,
        retry_count=retry_count,
        http_response=response,
    )
