"""Integration test: ingest(upsert) -> embed -> cluster -> extract, live.

SKIPPED unless the needed env vars are set (conftest loads repo-root .env): real
embeddings/extraction need MISTRAL_API_KEY, the DB needs DATABASE_URL, and creating
a throwaway auth user needs the Supabase service-role creds.

It upserts synthetic conversations where a follow-up clearly adds a known requirement
("make it a table" -> format), runs the pipeline, and asserts a forgotten refinement
(in_first_prompt=False) of the expected kind exists — and that re-running is
idempotent. Cleans up the temp user (cascade) in finally. Synthetic data only.
"""
from __future__ import annotations

import os
import uuid as uuidlib

import pytest

REQUIRED_ENV = (
    "MISTRAL_API_KEY",
    "DATABASE_URL",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
)
_missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
pytestmark = pytest.mark.skipif(
    bool(_missing),
    reason="extraction integration test needs env vars; missing: " + ", ".join(_missing),
)


def _admin_client():
    from supabase import create_client

    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
    )


def _build_gateway():
    from promptdict.cloud import SanitizingGateway
    from promptdict.providers import MistralEmbeddingProvider, MistralLLMProvider
    from promptdict.sanitize import default_sanitizer

    return SanitizingGateway(
        default_sanitizer("en"),
        llm=MistralLLMProvider(),
        embeddings=MistralEmbeddingProvider(),
    )


def _refinements(store, owner_id, conversation_id):
    cur = store._conn.cursor()
    return cur.execute(
        "SELECT kind, turn_index, in_first_prompt, note FROM refinements "
        "WHERE owner_id = %s AND conversation_id = %s ORDER BY turn_index, kind",
        (uuidlib.UUID(owner_id), uuidlib.UUID(conversation_id)),
    ).fetchall()


def test_pipeline_extracts_forgotten_format_refinement_idempotently():
    for mod in ("mistralai", "psycopg", "supabase", "pgvector", "hdbscan", "numpy"):
        pytest.importorskip(mod)

    from promptdict.clustering import cluster_conversations
    from promptdict.embedding import embed_conversations
    from promptdict.extraction import extract_refinements
    from promptdict.models import Conversation, Message
    from promptdict.store import PostgresStore

    admin = _admin_client()
    created = admin.auth.admin.create_user(
        {
            "email": f"promptdict-extract+{uuidlib.uuid4().hex}@example.com",
            "password": uuidlib.uuid4().hex + "Aa1!",
            "email_confirm": True,
        }
    )
    user_id = created.user.id

    # The "format" conversation: a follow-up clearly asks to reformat as a table.
    conv_format = Conversation(
        source="claude", external_id="forgot-format", owner_id=user_id,
        title="sleep tips",
        messages=[
            Message(role="user", text="Give me 10 tips for sleeping better.", idx=0),
            Message(role="assistant", text="Here are ten tips: ...", idx=1),
            Message(role="user", text="Make it a table.", idx=2),
        ],
    )
    # A second, different-shape conversation so clustering/extraction see >1 task.
    conv_other = Conversation(
        source="claude", external_id="explain-task", owner_id=user_id,
        title="explainer",
        messages=[
            Message(role="user", text="Explain how photosynthesis works.", idx=0),
            Message(role="assistant", text="Photosynthesis is ...", idx=1),
            Message(role="user", text="Explain it for a 10 year old.", idx=2),
        ],
    )

    store = PostgresStore()
    try:
        store.upsert(conv_format)
        store.upsert(conv_other)

        gateway = _build_gateway()
        embed_conversations(store, gateway, user_id)
        cluster_conversations(store, user_id)

        n1 = extract_refinements(store, gateway, user_id)
        assert n1 == 2  # both conversations processed

        rows = _refinements(store, user_id, conv_format.conversation_id)
        forgotten_kinds = {r[0] for r in rows if r[2] is False}
        assert forgotten_kinds, "expected at least one forgotten refinement"
        assert "format" in forgotten_kinds, f"expected 'format', got {forgotten_kinds}"

        # Idempotent: nothing left unextracted, and the rows don't multiply.
        n2 = extract_refinements(store, gateway, user_id)
        assert n2 == 0
        rows_again = _refinements(store, user_id, conv_format.conversation_id)
        assert len(rows_again) == len(rows)
    finally:
        store.close()
        admin.auth.admin.delete_user(user_id)
