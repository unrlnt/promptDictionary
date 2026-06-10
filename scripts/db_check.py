"""Read-only smoke test for the promptdict database.

Connects via ``DATABASE_URL`` and verifies the schema is online, printing PASS/FAIL
per check:

  * the ``vector`` extension is installed,
  * all eight expected tables exist,
  * ``conversations.embedding`` is a ``vector`` column with 1024 dimensions.

SELECT only — this script never writes. Run it AFTER applying the migration:

    pip install -e ".[cloud]"     # provides psycopg + python-dotenv
    # put DATABASE_URL in .env (see .env.example), then:
    python scripts/db_check.py

Exit code is 0 if every check passes, 1 otherwise.
"""
from __future__ import annotations

import sys

from promptdict.config import load_settings

EXPECTED_TABLES = [
    "profiles",
    "teams",
    "team_members",
    "conversations",
    "messages",
    "clusters",
    "refinements",
    "team_checklist_items",
]
EXPECTED_EMBEDDING_DIM = 1024


def _report(label: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"[{status}] {label}{suffix}")
    return ok


def main() -> int:
    settings = load_settings()
    database_url = settings.database_url
    if not database_url:
        print("[FAIL] DATABASE_URL is not set (see .env.example).")
        return 1

    try:
        import psycopg
    except ImportError:
        print("[FAIL] psycopg not installed. Run: pip install -e \".[cloud]\"")
        return 1

    results: list[bool] = []
    # Read-only connection; SET guards against any accidental write.
    with psycopg.connect(database_url) as conn:
        conn.execute("SET default_transaction_read_only = on")

        # 1. pgvector extension installed.
        row = conn.execute(
            "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        results.append(_report("vector extension installed", row is not None))

        # 2. All eight tables exist.
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = ANY(%s)",
            (EXPECTED_TABLES,),
        ).fetchall()
        present = {r[0] for r in rows}
        for table in EXPECTED_TABLES:
            results.append(_report(f"table public.{table} exists", table in present))

        # 3. conversations.embedding is vector(1024).
        row = conn.execute(
            "SELECT format_type(a.atttypid, a.atttypmod), a.atttypmod "
            "FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = 'conversations' "
            "AND a.attname = 'embedding' AND a.attnum > 0 AND NOT a.attisdropped"
        ).fetchone()
        if row is None:
            results.append(_report("conversations.embedding is vector(1024)", False,
                                   "column not found"))
        else:
            type_str, typmod = row
            ok = type_str.startswith("vector") and typmod == EXPECTED_EMBEDDING_DIM
            results.append(_report("conversations.embedding is vector(1024)", ok,
                                   f"found {type_str}"))

    passed = sum(results)
    total = len(results)
    print(f"\n{passed}/{total} checks passed.")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
