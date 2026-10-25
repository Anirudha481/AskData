"""
schema_reader.py
Reads table structure (columns, types, PKs, FKs) directly from
the PostgreSQL information_schema. Merges with optional descriptions
from schema_config.yml. Returns a plain-text schema block ready to
be inserted into an LLM prompt.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
import yaml


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def get_connection(dsn: str | None = None) -> psycopg2.extensions.connection:
    """
    Return a psycopg2 connection.
    Priority: explicit dsn arg > DATABASE_URL env var > individual env vars.
    """
    if dsn is None:
        dsn = os.getenv("DATABASE_URL")
    if dsn is None:
        host = os.getenv("DB_HOST", "localhost")
        port = os.getenv("DB_PORT", "5432")
        dbname = os.getenv("DB_NAME", "northwind")
        user = os.getenv("DB_USER", "postgres")
        password = os.getenv("DB_PASSWORD", "postgres")
        dsn = f"postgresql://{user}:{password}@{host}:{port}/{dbname}"
    return psycopg2.connect(dsn)


# ---------------------------------------------------------------------------
# Raw schema extraction
# ---------------------------------------------------------------------------

def _fetch_columns(conn: psycopg2.extensions.connection, schema: str = "public") -> dict[str, list[dict]]:
    """Return {table_name: [column dicts]} for all tables in *schema*."""
    sql = """
        SELECT
            c.table_name,
            c.column_name,
            c.data_type,
            c.character_maximum_length,
            c.is_nullable,
            c.column_default
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON t.table_name = c.table_name
         AND t.table_schema = c.table_schema
        WHERE c.table_schema = %s
          AND t.table_type = 'BASE TABLE'
        ORDER BY c.table_name, c.ordinal_position;
    """
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(sql, (schema,))
        rows = cur.fetchall()

    tables: dict[str, list[dict]] = {}
    for row in rows:
        tname = row["table_name"]
        tables.setdefault(tname, []).append(dict(row))
    return tables


def _fetch_primary_keys(conn: psycopg2.extensions.connection, schema: str = "public") -> dict[str, list[str]]:
    """Return {table_name: [pk_column_names]}."""
    sql = """
        SELECT
            tc.table_name,
            kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema   = kcu.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY'
          AND tc.table_schema    = %s
        ORDER BY tc.table_name, kcu.ordinal_position;
    """
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(sql, (schema,))
        rows = cur.fetchall()

    pks: dict[str, list[str]] = {}
    for row in rows:
        pks.setdefault(row["table_name"], []).append(row["column_name"])
    return pks


def _fetch_foreign_keys(conn: psycopg2.extensions.connection, schema: str = "public") -> dict[str, list[dict]]:
    """Return {table_name: [fk dicts with from_col, to_table, to_col]}."""
    sql = """
        SELECT
            tc.table_name                  AS from_table,
            kcu.column_name                AS from_col,
            ccu.table_name                 AS to_table,
            ccu.column_name                AS to_col
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema    = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name
         AND ccu.table_schema    = tc.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema    = %s
        ORDER BY tc.table_name, kcu.column_name;
    """
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(sql, (schema,))
        rows = cur.fetchall()

    fks: dict[str, list[dict]] = {}
    for row in rows:
        fks.setdefault(row["from_table"], []).append(dict(row))
    return fks


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def _load_config(config_path: str | Path | None) -> dict[str, Any]:
    """Load schema_config.yml if it exists; return empty dict otherwise."""
    if config_path is None:
        config_path = Path(__file__).parent / "schema_config.yml"
    config_path = Path(config_path)
    if not config_path.exists():
        return {}
    with config_path.open() as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def read_schema(
    conn: psycopg2.extensions.connection | None = None,
    dsn: str | None = None,
    schema: str = "public",
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """
    Read the full schema from the database and return a structured dict.

    Returns:
        {
          "tables": {
            "orders": {
              "description": "...",       # from config, may be empty
              "columns": [
                {
                  "name": "order_id",
                  "type": "integer",
                  "nullable": False,
                  "pk": True,
                  "fk": None,           # or "orders.order_id"
                  "description": "...", # from config, may be empty
                }
              ]
            }
          },
          "table_names": ["categories", "customers", ...]
        }
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection(dsn)

    try:
        columns_by_table = _fetch_columns(conn, schema)
        pks_by_table = _fetch_primary_keys(conn, schema)
        fks_by_table = _fetch_foreign_keys(conn, schema)
    finally:
        if own_conn:
            conn.close()

    config = _load_config(config_path)
    table_configs: dict[str, Any] = config.get("tables", {})

    result: dict[str, Any] = {"tables": {}, "table_names": sorted(columns_by_table.keys())}

    for table_name, raw_cols in columns_by_table.items():
        tcfg = table_configs.get(table_name, {}) or {}
        col_cfgs: dict[str, str] = {}
        if isinstance(tcfg, dict):
            col_cfgs = tcfg.get("columns", {}) or {}

        pk_cols = set(pks_by_table.get(table_name, []))
        fk_lookup: dict[str, str] = {}
        for fk in fks_by_table.get(table_name, []):
            fk_lookup[fk["from_col"]] = f"{fk['to_table']}.{fk['to_col']}"

        columns = []
        for rc in raw_cols:
            col_name = rc["column_name"]
            # Normalise type: use 'character varying' -> 'varchar' etc.
            dtype = rc["data_type"]
            if rc["character_maximum_length"]:
                dtype = f"{dtype}({rc['character_maximum_length']})"
            columns.append(
                {
                    "name": col_name,
                    "type": dtype,
                    "nullable": rc["is_nullable"] == "YES",
                    "pk": col_name in pk_cols,
                    "fk": fk_lookup.get(col_name),
                    "description": col_cfgs.get(col_name, ""),
                }
            )

        result["tables"][table_name] = {
            "description": tcfg.get("description", "") if isinstance(tcfg, dict) else "",
            "columns": columns,
        }

    return result


def schema_to_prompt_text(schema: dict[str, Any]) -> str:
    """
    Convert the structured schema dict into a compact text block for the LLM prompt.

    Format per table:
        TABLE orders -- Customer orders with shipping and date information.
          order_id       integer         PK
          customer_id    varchar(5)      FK->customers.customer_id
          order_date     date
    """
    lines: list[str] = ["DATABASE SCHEMA (PostgreSQL – Northwind)\n"]

    for table_name in schema["table_names"]:
        tdata = schema["tables"][table_name]
        desc_suffix = f" -- {tdata['description']}" if tdata.get("description") else ""
        lines.append(f"TABLE {table_name}{desc_suffix}")

        for col in tdata["columns"]:
            tags: list[str] = []
            if col["pk"]:
                tags.append("PK")
            if col["fk"]:
                tags.append(f"FK->{col['fk']}")
            if not col["nullable"] and not col["pk"]:
                tags.append("NOT NULL")
            if col["description"]:
                tags.append(f"-- {col['description']}")

            tag_str = "  " + "  ".join(tags) if tags else ""
            lines.append(f"  {col['name']:<30} {col['type']:<25}{tag_str}")

        lines.append("")  # blank line between tables

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI usage: python schema_reader.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    schema = read_schema()
    # Print as structured JSON for inspection
    print(json.dumps(schema, indent=2, default=str))
    print("\n" + "=" * 70 + "\n")
    print(schema_to_prompt_text(schema))
