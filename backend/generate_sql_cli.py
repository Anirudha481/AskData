"""
generate_sql_cli.py
Command-line script to test the SQL generator interactively.

Usage (from the backend/ directory):
    python generate_sql_cli.py "How many customers are there?"
    python generate_sql_cli.py --question "List top 5 products by revenue"
    python generate_sql_cli.py                  # interactive prompt loop

The script reads .env automatically via pydantic-settings.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Allow running from the backend/ directory
sys.path.insert(0, str(Path(__file__).parent))

from app.core.sql_generator import generate_sql
from app.core.schema_reader import read_schema, schema_to_prompt_text
from app.core.config import settings


def _separator(char: str = "-", width: int = 70) -> str:
    return char * width


def run_one(question: str, schema_text: str) -> None:
    """Generate SQL for one question and pretty-print the result."""
    print(f"\n{'=' * 70}")
    print(f"QUESTION: {question}")
    print(_separator())

    t0 = time.perf_counter()
    result = generate_sql(question, schema_text=schema_text)
    latency = time.perf_counter() - t0

    print(f"KIND    : {result.kind}")
    print(f"LATENCY : {latency:.2f}s")
    print(_separator())

    if result.kind == "sql":
        print(f"GENERATED SQL:\n{result.sql}")
    elif result.kind == "clarification":
        print(f"CLARIFICATION NEEDED:\n{result.clarification_question}")
    else:
        print(f"ERROR:\n{result.error_message}")
        if result.raw_llm_response:
            print(f"\nRAW LLM RESPONSE:\n{result.raw_llm_response[:500]}")

    print(_separator())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AskData Stage 2 — SQL Generator CLI"
    )
    parser.add_argument(
        "question",
        nargs="?",
        help="Natural-language question (omit for interactive loop)",
    )
    parser.add_argument(
        "--question", "-q",
        dest="question_flag",
        help="Alternative way to pass the question",
    )
    args = parser.parse_args()

    question_arg = args.question or args.question_flag

    # ── Load schema once (shared across all questions in a loop) ──────────
    print("Loading schema from database...")
    try:
        schema = read_schema(dsn=settings.admin_dsn)
        schema_text = schema_to_prompt_text(schema)
        table_count = len(schema["table_names"])
        print(f"Schema loaded: {table_count} tables\n")
    except Exception as exc:
        print(f"ERROR: Could not load schema.\n{exc}")
        print("Make sure docker compose is running and .env is configured.")
        sys.exit(1)

    if question_arg:
        # Single-shot mode
        run_one(question_arg, schema_text)
    else:
        # Interactive loop
        print("Interactive mode — type a question and press Enter.")
        print("Type 'quit' or Ctrl-C to exit.\n")
        while True:
            try:
                question = input("Question> ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nBye!")
                break
            if not question:
                continue
            if question.lower() in ("quit", "exit", "q"):
                print("Bye!")
                break
            run_one(question, schema_text)


if __name__ == "__main__":
    main()
