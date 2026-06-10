"""Local-dev CLI — seam #4's entry point.

    python -m promptdict.cli ingest <export.json>
    python -m promptdict.cli list

Ingest stores RAW conversations into a local SQLite store (private; sanitization
happens only at cloud egress, never here). The DB path defaults to ``promptdict.db``
(gitignored) and can be overridden with ``--db`` or ``$PROMPTDICT_DB``.
"""
from __future__ import annotations

import argparse
import os
import sys

from .pipeline import ingest as ingest_pipeline
from .store import SQLiteStore

DEFAULT_OWNER = "local"


def _db_path(args: argparse.Namespace) -> str:
    return args.db or os.environ.get("PROMPTDICT_DB", "promptdict.db")


def _cmd_ingest(args: argparse.Namespace) -> int:
    store = SQLiteStore(_db_path(args))
    try:
        result = ingest_pipeline(args.path, store)
    finally:
        store.close()
    print(
        f"Ingested {result.total} conversation(s) from source "
        f"'{result.source}' — {result.new_or_changed} new or changed."
    )
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    store = SQLiteStore(_db_path(args))
    try:
        rows = store.list_conversations(DEFAULT_OWNER)
    finally:
        store.close()
    if not rows:
        print("No conversations stored yet. Try `ingest <export.json>`.")
        return 0
    for r in rows:
        title = r.title or "(untitled)"
        print(f"[{r.source}] {title} — {r.n_messages} msg(s)  id={r.external_id}")
    print(f"\n{len(rows)} conversation(s).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="promptdict", description=__doc__)
    parser.add_argument("--db", help="SQLite DB path (default: promptdict.db or $PROMPTDICT_DB)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="Import a ChatGPT/Claude export.")
    p_ingest.add_argument("path", help="Path to the export JSON file.")
    p_ingest.set_defaults(func=_cmd_ingest)

    p_list = sub.add_parser("list", help="List stored conversations.")
    p_list.set_defaults(func=_cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
