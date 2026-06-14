"""Integration test for build_checklists against the live DB. No LLM/embeddings —
clusters and refinements are seeded directly, then aggregated.

SKIPPED unless DATABASE_URL + a Supabase server secret (SUPABASE_SECRET_KEY or the
legacy SUPABASE_SERVICE_ROLE_KEY) are set (needed to create/delete a throwaway auth
user). Synthetic data only; cleaned up in finally.
"""
from __future__ import annotations

import os
import uuid as uuidlib

import pytest

from promptdict.config import load_settings

REQUIRED_ENV = ("DATABASE_URL", "SUPABASE_URL")
_missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
if not load_settings().supabase_secret:
    _missing.append("SUPABASE_SECRET_KEY (or legacy SUPABASE_SERVICE_ROLE_KEY)")
pytestmark = pytest.mark.skipif(
    bool(_missing),
    reason="checklist integration test needs env vars; missing: " + ", ".join(_missing),
)

DUMMY_VEC = [0.0] * 1024


def _admin_client():
    from supabase import create_client

    settings = load_settings()
    return create_client(settings.supabase_url, settings.supabase_secret)


def _find(rows, scope, kind, cluster_id=None):
    for r in rows:
        if r["scope"] == scope and r["kind"] == kind and r["cluster_id"] == cluster_id:
            return r
    return None


def test_build_checklists_global_includes_noise_per_cluster_excludes_idempotent():
    for mod in ("psycopg", "supabase", "pgvector"):
        pytest.importorskip(mod)

    from promptdict.checklists import build_checklists
    from promptdict.models import Conversation, Message
    from promptdict.store import PostgresStore

    admin = _admin_client()
    created = admin.auth.admin.create_user(
        {
            "email": f"promptdict-checklist+{uuidlib.uuid4().hex}@example.com",
            "password": uuidlib.uuid4().hex + "Aa1!",
            "email_confirm": True,
        }
    )
    user_id = created.user.id

    def _conv(ext, day):
        return Conversation(
            source="claude", external_id=ext, owner_id=user_id, title=ext,
            created_at=f"2026-02-{day:02d}T00:00:00+00:00",
            messages=[Message(role="user", text=f"{ext} request", idx=0)],
        )

    store = PostgresStore()
    try:
        convs = {ext: _conv(ext, i + 1)
                 for i, ext in enumerate(["A1", "A2", "B1", "NOISE"])}
        for c in convs.values():
            store.upsert(c)

        # Two clusters; NOISE deliberately left unclustered.
        store.replace_clusters(user_id, [
            (DUMMY_VEC, [convs["A1"].conversation_id, convs["A2"].conversation_id]),
            (DUMMY_VEC, [convs["B1"].conversation_id]),
        ])

        # Seed forgotten refinements with each conversation's cluster_id.
        assign: dict[str, str | None] = {}
        for conv, cluster_id in store.iter_unextracted(user_id):
            assign[conv.external_id] = cluster_id
            kind = "length" if conv.external_id == "B1" else "format"
            store.replace_refinements(
                user_id, conv.conversation_id,
                [(kind, 2, False, f"{kind} note", cluster_id)],
            )

        cluster_a, cluster_b = assign["A1"], assign["B1"]
        assert assign["A1"] == assign["A2"] and cluster_a is not None
        assert assign["NOISE"] is None

        result = build_checklists(store, user_id)
        assert result.clusters == 2

        rows = store.iter_checklists(user_id)

        # GLOBAL: format counted on A1, A2 and the NOISE conversation -> 3 distinct.
        assert _find(rows, "global", "format")["conversation_count"] == 3
        assert _find(rows, "global", "length")["conversation_count"] == 1

        # PER-CLUSTER: cluster A's format excludes NOISE and B -> 2.
        assert _find(rows, "cluster", "format", cluster_a)["conversation_count"] == 2
        assert _find(rows, "cluster", "length", cluster_b)["conversation_count"] == 1

        # Noise contributes to global only — no cluster row outside A and B.
        cluster_ids = {r["cluster_id"] for r in rows if r["scope"] == "cluster"}
        assert cluster_ids == {cluster_a, cluster_b}

        # Idempotent rebuild: same number of rows.
        before = len(rows)
        build_checklists(store, user_id)
        assert len(store.iter_checklists(user_id)) == before
    finally:
        store.close()
        admin.auth.admin.delete_user(user_id)
