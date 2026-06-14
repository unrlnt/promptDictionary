"""Integration test: ingest(upsert) -> embed -> cluster, against the live stack.

SKIPPED unless the env vars it needs are all set (conftest loads repo-root .env).
It needs MISTRAL_API_KEY (real embeddings) and DATABASE_URL plus the Supabase
a Supabase server secret — SUPABASE_SECRET_KEY or the legacy
SUPABASE_SERVICE_ROLE_KEY — (to create/delete a throwaway auth user).

It upserts synthetic conversations in 3 deliberately distinct "task shapes" (two
near-identical conversations per shape), embeds them through the gateway, clusters
them, and asserts that conversations sharing a shape land in the same non-null
cluster_id — and that everything is owner-scoped. Cleans up the temp user (cascade)
in a finally block. Synthetic data only; inputs are tiny to keep cost negligible.
"""
from __future__ import annotations

import os
import uuid as uuidlib

import pytest

from promptdict.config import load_settings

REQUIRED_ENV = ("MISTRAL_API_KEY", "DATABASE_URL", "SUPABASE_URL")
_missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
if not load_settings().supabase_secret:
    _missing.append("SUPABASE_SECRET_KEY (or legacy SUPABASE_SERVICE_ROLE_KEY)")
pytestmark = pytest.mark.skipif(
    bool(_missing),
    reason="clustering integration test needs env vars; missing: " + ", ".join(_missing),
)

# Three distinct task shapes; both conversations in a shape use identical user
# text so their embeddings coincide and must land in the same cluster.
SHAPES = {
    "finance": "Summarize this quarterly financial report into five bullet points.",
    "code": "Write a Python function that parses a CSV file into dictionaries.",
    "travel": "Plan a three-day sightseeing itinerary for a trip to Kyoto.",
}


def _admin_client():
    from supabase import create_client

    settings = load_settings()
    return create_client(settings.supabase_url, settings.supabase_secret)


def _build_gateway():
    from promptdict.cloud import SanitizingGateway
    from promptdict.providers import MistralEmbeddingProvider, MistralLLMProvider
    from promptdict.sanitize import default_sanitizer

    return SanitizingGateway(
        default_sanitizer("en"),
        llm=MistralLLMProvider(),
        embeddings=MistralEmbeddingProvider(),
    )


def _assignments(store, owner_id, external_ids):
    """external_id -> cluster_id (str|None), owner-scoped, read straight from DB."""
    cur = store._conn.cursor()
    out = {}
    for ext in external_ids:
        row = cur.execute(
            "SELECT cluster_id FROM conversations "
            "WHERE owner_id = %s AND source = %s AND external_id = %s",
            (uuidlib.UUID(owner_id), "claude", ext),
        ).fetchone()
        out[ext] = None if (row is None or row[0] is None) else str(row[0])
    return out


def test_embed_then_cluster_groups_by_shape_owner_scoped():
    for mod in ("mistralai", "psycopg", "supabase", "pgvector", "hdbscan", "numpy"):
        pytest.importorskip(mod)

    from promptdict.clustering import cluster_conversations
    from promptdict.embedding import embed_conversations
    from promptdict.models import Conversation, Message
    from promptdict.store import PostgresStore

    admin = _admin_client()
    created = admin.auth.admin.create_user(
        {
            "email": f"promptdict-cluster+{uuidlib.uuid4().hex}@example.com",
            "password": uuidlib.uuid4().hex + "Aa1!",
            "email_confirm": True,
        }
    )
    user_id = created.user.id

    # external_ids grouped by shape, two per shape.
    ext_by_shape = {shape: [f"{shape}-{i}" for i in range(2)] for shape in SHAPES}
    all_exts = [e for exts in ext_by_shape.values() for e in exts]

    store = PostgresStore()
    try:
        for shape, text in SHAPES.items():
            for ext in ext_by_shape[shape]:
                store.upsert(Conversation(
                    source="claude", external_id=ext, owner_id=user_id,
                    title=f"{shape} task",
                    messages=[
                        Message(role="user", text=text, idx=0),
                        Message(role="assistant", text="Sure, here you go.", idx=1),
                    ],
                ))

        gateway = _build_gateway()
        assert embed_conversations(store, gateway, user_id) == len(all_exts)

        result = cluster_conversations(store, user_id)
        assert result.n_conversations == len(all_exts)

        assigned = _assignments(store, user_id, all_exts)
        # Each shape's two conversations share one non-null cluster_id.
        for shape, exts in ext_by_shape.items():
            cid0, cid1 = assigned[exts[0]], assigned[exts[1]]
            assert cid0 is not None, f"{shape} left unclustered"
            assert cid0 == cid1, f"{shape} pair split across clusters"
        # Distinct shapes occupy distinct clusters.
        assert len({assigned[exts[0]] for exts in ext_by_shape.values()}) == len(SHAPES)

        # ISOLATION: an unrelated owner has no embedded rows and no clusters.
        other = str(uuidlib.uuid4())
        assert store.iter_embedded(other) == []
        assert cluster_conversations(store, other).n_clusters == 0
        # Re-running for `other` didn't disturb this owner's assignments.
        after = _assignments(store, user_id, all_exts)
        for shape, exts in ext_by_shape.items():
            assert after[exts[0]] == after[exts[1]] is not None
    finally:
        store.close()
        admin.auth.admin.delete_user(user_id)
