"""Local-dev / pipeline CLI — seam #4's entry point.

    python -m promptdict.cli ingest <export.json>                 # SQLite (offline dev)
    python -m promptdict.cli ingest <export.json> \
        --store postgres --owner-id <uuid>                        # Supabase Postgres
    python -m promptdict.cli list                                 # SQLite
    python -m promptdict.cli embed   --owner-id <uuid>            # embed via the gateway
    python -m promptdict.cli cluster --owner-id <uuid>            # cluster task types
    python -m promptdict.cli extract --owner-id <uuid>            # mine refinements (gateway)
    python -m promptdict.cli label   --owner-id <uuid>            # label clusters (gateway)
    python -m promptdict.cli checklist build --owner-id <uuid>    # aggregate (no egress)
    python -m promptdict.cli checklist show  --owner-id <uuid>    # print checklists

Ingest stores RAW conversations privately; sanitization happens only at cloud
egress (`embed`), never here. The SQLite path needs no cloud deps; `embed` and
`cluster` require the `cloud` extra and `.env` (DATABASE_URL, MISTRAL_API_KEY).
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


def _production_gateway():
    """Build the egress gateway wired to the real Mistral providers. Imported
    lazily so offline SQLite commands need no cloud dependencies."""
    from .cloud import SanitizingGateway
    from .providers import MistralEmbeddingProvider, MistralLLMProvider
    from .sanitize import default_sanitizer

    return SanitizingGateway(
        default_sanitizer("en"),
        llm=MistralLLMProvider(),
        embeddings=MistralEmbeddingProvider(),
    )


def _cmd_ingest(args: argparse.Namespace) -> int:
    if args.store == "postgres":
        if not args.owner_id:
            return _fail("--owner-id is required when --store postgres")
        from .store import PostgresStore
        store = PostgresStore()
        try:
            result = ingest_pipeline(args.path, store, owner_id=args.owner_id)
        finally:
            store.close()
    else:
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


def _cmd_embed(args: argparse.Namespace) -> int:
    from .embedding import embed_conversations
    from .store import PostgresStore

    store = PostgresStore()
    try:
        gateway = _production_gateway()
        n = embed_conversations(store, gateway, args.owner_id)
    finally:
        store.close()
    print(f"Embedded {n} conversation(s) for owner {args.owner_id}.")
    return 0


def _cmd_cluster(args: argparse.Namespace) -> int:
    from .clustering import cluster_conversations
    from .store import PostgresStore

    store = PostgresStore()
    try:
        result = cluster_conversations(store, args.owner_id)
    finally:
        store.close()
    print(
        f"Clustered {result.n_conversations} embedded conversation(s) for owner "
        f"{args.owner_id}: {result.n_clusters} cluster(s), {result.n_noise} noise."
    )
    return 0


def _cmd_extract(args: argparse.Namespace) -> int:
    from .extraction import extract_refinements
    from .store import PostgresStore

    store = PostgresStore()
    try:
        gateway = _production_gateway()
        n = extract_refinements(store, gateway, args.owner_id)
    finally:
        store.close()
    print(f"Extracted refinements for {n} conversation(s) for owner {args.owner_id}.")
    return 0


def _cmd_label(args: argparse.Namespace) -> int:
    from .extraction import label_clusters
    from .store import PostgresStore

    store = PostgresStore()
    try:
        gateway = _production_gateway()
        n = label_clusters(store, gateway, args.owner_id)
    finally:
        store.close()
    print(f"Labelled {n} cluster(s) for owner {args.owner_id}.")
    return 0


def _cmd_checklist_build(args: argparse.Namespace) -> int:
    from .checklists import build_checklists
    from .store import PostgresStore

    store = PostgresStore()
    try:
        result = build_checklists(store, args.owner_id)
    finally:
        store.close()
    print(
        f"Built checklists for owner {args.owner_id}: {result.global_kinds} global "
        f"kind(s), {result.cluster_rows} per-cluster row(s) across "
        f"{result.clusters} cluster(s)."
    )
    return 0


def _checklist_line(r: dict) -> str:
    tag = f"  [{r['graduation']}]" if r.get("graduation") else ""
    note = r["sample_notes"][0] if r["sample_notes"] else ""
    eg = f'  e.g. "{note}"' if note else ""
    return (f"  {r['rank']}. {r['kind']:<12} conv={r['conversation_count']} "
            f"total={r['total_count']}{tag}{eg}")


def _cmd_checklist_show(args: argparse.Namespace) -> int:
    from .store import PostgresStore

    store = PostgresStore()
    try:
        rows = store.iter_checklists(args.owner_id)
        labels = dict(store.iter_clusters(args.owner_id))
    finally:
        store.close()

    if not rows:
        print("No checklists yet. Run `checklist build --owner-id <uuid>` first.")
        return 0

    if not args.cluster:
        print("Global checklist — most-forgotten requirements:")
        for r in sorted((x for x in rows if x["scope"] == "global"),
                        key=lambda x: x["rank"]):
            print(_checklist_line(r))

    by_cluster: dict[str, list[dict]] = {}
    for r in (x for x in rows if x["scope"] == "cluster"):
        by_cluster.setdefault(r["cluster_id"], []).append(r)

    for cid, items in by_cluster.items():
        if args.cluster and cid != args.cluster:
            continue
        label = labels.get(cid) or "(unlabelled)"
        print(f'\nCluster: {label}  [{cid}]')
        for r in sorted(items, key=lambda x: x["rank"]):
            print(_checklist_line(r))
    return 0


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="promptdict", description=__doc__)
    parser.add_argument("--db", help="SQLite DB path (default: promptdict.db or $PROMPTDICT_DB)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="Import a ChatGPT/Claude export.")
    p_ingest.add_argument("path", help="Path to the export JSON file.")
    p_ingest.add_argument("--store", choices=("sqlite", "postgres"), default="sqlite",
                          help="Target store (default: sqlite for offline dev).")
    p_ingest.add_argument("--owner-id", help="Auth-user UUID (required for --store postgres).")
    p_ingest.set_defaults(func=_cmd_ingest)

    p_list = sub.add_parser("list", help="List stored conversations (SQLite).")
    p_list.set_defaults(func=_cmd_list)

    p_embed = sub.add_parser("embed", help="Embed an owner's conversations via the gateway.")
    p_embed.add_argument("--owner-id", required=True, help="Auth-user UUID.")
    p_embed.set_defaults(func=_cmd_embed)

    p_cluster = sub.add_parser("cluster", help="Cluster an owner's embedded conversations.")
    p_cluster.add_argument("--owner-id", required=True, help="Auth-user UUID.")
    p_cluster.set_defaults(func=_cmd_cluster)

    p_extract = sub.add_parser("extract", help="Extract refinements via the gateway.")
    p_extract.add_argument("--owner-id", required=True, help="Auth-user UUID.")
    p_extract.set_defaults(func=_cmd_extract)

    p_label = sub.add_parser("label", help="Label an owner's clusters via the gateway.")
    p_label.add_argument("--owner-id", required=True, help="Auth-user UUID.")
    p_label.set_defaults(func=_cmd_label)

    p_checklist = sub.add_parser("checklist", help="Build or show aggregated checklists.")
    csub = p_checklist.add_subparsers(dest="checklist_cmd", required=True)
    p_cb = csub.add_parser("build", help="Aggregate refinements into checklists.")
    p_cb.add_argument("--owner-id", required=True, help="Auth-user UUID.")
    p_cb.set_defaults(func=_cmd_checklist_build)
    p_cs = csub.add_parser("show", help="Print an owner's checklists (read-only).")
    p_cs.add_argument("--owner-id", required=True, help="Auth-user UUID.")
    p_cs.add_argument("--cluster", help="Show only this cluster's checklist.")
    p_cs.set_defaults(func=_cmd_checklist_show)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
