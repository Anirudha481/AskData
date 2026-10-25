"""
verify_stage1.py
Run this script to confirm Stage 1 is working:
  - Connects to PostgreSQL
  - Reads the Northwind schema
  - Prints the prompt-ready schema block
  - Reports table count and column count

Usage:
    cd backend
    python verify_stage1.py
"""

import sys
from pathlib import Path

# Allow running from the backend directory
sys.path.insert(0, str(Path(__file__).parent))

from app.core.schema_reader import read_schema, schema_to_prompt_text


def main() -> None:
    print("Connecting to database and reading schema...\n")
    try:
        schema = read_schema()
    except Exception as exc:
        print(f"ERROR: Could not connect to the database.\n{exc}")
        print(
            "\nMake sure PostgreSQL is running (docker compose up -d) and "
            "the env vars DB_HOST / DB_USER / DB_PASSWORD / DB_NAME are set."
        )
        sys.exit(1)

    table_count = len(schema["table_names"])
    col_count = sum(len(t["columns"]) for t in schema["tables"].values())

    print(f"[OK] Connected successfully")
    print(f"[OK] Tables found: {table_count}")
    print(f"[OK] Total columns: {col_count}")
    print(f"[OK] Table names: {', '.join(schema['table_names'])}\n")

    prompt_block = schema_to_prompt_text(schema)
    print("=" * 70)
    print("PROMPT-READY SCHEMA BLOCK:")
    print("=" * 70)
    print(prompt_block)
    print("=" * 70)
    print(f"\nStage 1 verification complete. Schema is ready to use in prompts.")


if __name__ == "__main__":
    main()
