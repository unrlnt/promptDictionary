"""Integration test for PostgresStore against the live Supabase schema.

SKIPPED unless DATABASE_URL, SUPABASE_URL, and SUPABASE_SERVICE_ROLE_KEY are all
set, so the offline unit suite stays green without a database. When they are set,
this test:

  * creates a THROWAWAY auth user via the service-role admin API and uses its id
    as owner_id (auth.users.id is an FK target for conversations/messages);
  * exercises upsert dedup, get(), and iter_conversations() ordering;
  * proves owner isolation (a different owner_id sees none of the rows);
  * deletes the temp user in a finally block (cascade removes its data) and
    asserts the rows are gone.

All conversation data is synthetic. No real chat exports, no real people.
"""
from __future__ import annotations

import os
import uuid as uuidlib

import pytest

REQUIRED_ENV = ("DATABASE_URL", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")
# conftest.py loads repo-root .env before this is evaluated, so a skip here means
# the var is genuinely absent from the environment and .env.
_missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
pytestmark = pytest.mark.skipif(
    bool(_missing),
    reason="PostgresStore integration test needs env vars; missing: "
           + ", ".join(_missing),
)


def _admin_client():
    """Service-role Supabase client for the auth admin API."""
    from supabase import create_client

    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
    )


def _synthetic_conversation(owner_id: str):
    from promptdict.models import Conversation, Message

    return Conversation(
        source="claude",
        external_id="synthetic-conv-1",
        owner_id=owner_id,
        title="Synthetic offsite planning",
        messages=[
            Message(role="user", text="Help me plan a synthetic offsite.", idx=0),
            Message(role="assistant", text="Sure — how many people?", idx=1),
        ],
    )


def test_postgres_store_roundtrip_dedup_and_isolation():
    # Ensure the optional deps are present; skip loudly if not.
    pytest.importorskip("supabase")
    pytest.importorskip("psycopg")

    from promptdict.models import Message
    from promptdict.store import PostgresStore

    admin = _admin_client()

    # --- create a throwaway auth user -------------------------------------
    email = f"promptdict-test+{uuidlib.uuid4().hex}@example.com"
    created = admin.auth.admin.create_user(
        {
            "email": email,
            "password": uuidlib.uuid4().hex + "Aa1!",
            "email_confirm": True,
        }
    )
    user_id = created.user.id

    store = PostgresStore()
    try:
        conv = _synthetic_conversation(user_id)

        # first upsert -> new
        assert store.upsert(conv) is True
        # identical re-upsert -> not changed (content-hash dedup)
        assert store.upsert(conv) is False
        # mutate a message then upsert -> changed
        conv.messages[1] = Message(role="assistant", text="Edited reply.", idx=1)
        assert store.upsert(conv) is True

        # get() returns the conversation with messages in idx order
        got = store.get(user_id, conv.conversation_id)
        assert got is not None
        assert [m.idx for m in got.messages] == [0, 1]
        assert [m.text for m in got.messages] == [
            "Help me plan a synthetic offsite.",
            "Edited reply.",
        ]

        # iter_conversations() returns it, messages in idx order
        listed = list(store.iter_conversations(user_id))
        assert len(listed) == 1
        assert listed[0].conversation_id == conv.conversation_id
        assert [m.idx for m in listed[0].messages] == [0, 1]

        # ISOLATION: an unrelated owner sees none of these rows
        other_owner = str(uuidlib.uuid4())
        assert list(store.iter_conversations(other_owner)) == []
        assert store.get(other_owner, conv.conversation_id) is None
    finally:
        store.close()
        admin.auth.admin.delete_user(user_id)

    # cleanup verified: deleting the user cascaded away the conversation/messages
    verify = PostgresStore()
    try:
        assert list(verify.iter_conversations(user_id)) == []
    finally:
        verify.close()
