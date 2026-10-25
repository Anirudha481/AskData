"""
smoke_test_generator.py
Validates the SQL generator's prompt-building logic WITHOUT calling the LLM.
Run this to confirm Stage 2 is wired up correctly even before adding API keys.

Usage:
    cd backend
    python smoke_test_generator.py
"""

from __future__ import annotations

import sys
import json
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from app.core.sql_generator import (
    build_messages,
    _parse_llm_response,
    FEW_SHOT_EXAMPLES,
    generate_sql,
)
from app.core.schema_reader import read_schema, schema_to_prompt_text
from app.core.config import settings


PASS = "[PASS]"
FAIL = "[FAIL]"


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = PASS if condition else FAIL
    print(f"  {mark}  {label}", f"({detail})" if detail else "")
    return condition


def main() -> None:
    all_ok = True

    # ── 1. Schema loading ─────────────────────────────────────────────────
    print("\n[1] Schema loading")
    try:
        schema = read_schema(dsn=settings.admin_dsn)
        schema_text = schema_to_prompt_text(schema)
        all_ok &= check("Schema loaded", len(schema["table_names"]) > 0,
                         f"{len(schema['table_names'])} tables")
        all_ok &= check("Schema text non-empty", len(schema_text) > 100)
        all_ok &= check("Schema contains 'orders'", "TABLE orders" in schema_text)
    except Exception as exc:
        print(f"  {FAIL}  Could not load schema: {exc}")
        sys.exit(1)

    # ── 2. Prompt structure ───────────────────────────────────────────────
    print("\n[2] Prompt structure")
    msgs = build_messages("How many customers are there?", schema_text)
    all_ok &= check("Message list has 2 entries", len(msgs) == 2,
                     f"got {len(msgs)}")
    all_ok &= check("First message is system", msgs[0]["role"] == "system")
    all_ok &= check("Second message is user", msgs[1]["role"] == "user")
    all_ok &= check("System prompt contains schema", "TABLE orders" in msgs[0]["content"])
    all_ok &= check("System prompt contains security rules", "IGNORE" in msgs[0]["content"])
    all_ok &= check("System prompt contains few-shot examples",
                     "customer_count" in msgs[0]["content"])

    # ── 3. Retry prompt structure ─────────────────────────────────────────
    print("\n[3] Retry prompt structure")
    retry_msgs = build_messages(
        "How many customers are there?",
        schema_text,
        previous_sql="SELECT * FORM customers;",
        sql_error='ERROR:  syntax error at or near "FORM"',
    )
    all_ok &= check("Retry has 4 messages", len(retry_msgs) == 4,
                     f"got {len(retry_msgs)}")
    all_ok &= check("Retry last message is user", retry_msgs[-1]["role"] == "user")
    all_ok &= check("Retry message mentions the error",
                     "syntax error" in retry_msgs[-1]["content"])

    # ── 4. Response parser ────────────────────────────────────────────────
    print("\n[4] Response parser")

    good_sql = json.dumps({"kind": "sql", "sql": "SELECT COUNT(*) FROM customers;"})
    k, sql, _ = _parse_llm_response(good_sql)
    all_ok &= check("Parses SQL response", k == "sql" and "COUNT" in sql)

    clarify = json.dumps({"kind": "clarification",
                           "clarification_question": "Which year do you mean?"})
    k, _, cq = _parse_llm_response(clarify)
    all_ok &= check("Parses clarification response", k == "clarification" and "year" in cq)

    # Model sometimes wraps in markdown
    md_wrapped = "```json\n" + good_sql + "\n```"
    k, sql2, _ = _parse_llm_response(md_wrapped)
    all_ok &= check("Strips markdown code fences", k == "sql" and "COUNT" in sql2)

    bad_json = "Here is your query: SELECT * FROM orders"
    k, _, err = _parse_llm_response(bad_json)
    all_ok &= check("Handles missing JSON gracefully", k == "error" and err != "")

    # ── 5. generate_sql() with mocked LLM ────────────────────────────────
    print("\n[5] generate_sql() with mocked LLM")

    mock_sql_response = json.dumps({
        "kind": "sql",
        "sql": "SELECT COUNT(*) AS customer_count FROM customers;"
    })
    with patch("app.core.sql_generator.call_llm", return_value=mock_sql_response):
        result = generate_sql(
            "How many customers are there?",
            schema_text=schema_text,
        )
    all_ok &= check("Kind is sql", result.kind == "sql")
    all_ok &= check("SQL contains COUNT", "COUNT" in result.sql)

    mock_clarify_response = json.dumps({
        "kind": "clarification",
        "clarification_question": "Which year do you mean by 'last year'?"
    })
    with patch("app.core.sql_generator.call_llm", return_value=mock_clarify_response):
        result2 = generate_sql(
            "What was the revenue last year?",
            schema_text=schema_text,
        )
    all_ok &= check("Kind is clarification", result2.kind == "clarification")
    all_ok &= check("Clarification text populated",
                     "year" in result2.clarification_question.lower())

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    if all_ok:
        print("[OK] All Stage 2 smoke tests passed. Prompt building is correct.")
        print("     To test against a real LLM, run: python generate_sql_cli.py")
    else:
        print("[FAIL] Some checks failed. See above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
