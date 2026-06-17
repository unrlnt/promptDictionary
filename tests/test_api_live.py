"""Integration test for the FastAPI service: upload -> process -> checklists.

SKIPPED unless MISTRAL_API_KEY + DATABASE_URL + SUPABASE_URL + a Supabase server
secret (SUPABASE_SECRET_KEY or legacy SUPABASE_SERVICE_ROLE_KEY) are set. Creates a
throwaway auth user, signs in for a REAL access token, drives the API via TestClient,
and asserts owner-scoped results. Cleans up the user (cascade) in finally.
"""
from __future__ import annotations

import json
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
    reason="API integration test needs env vars; missing: " + ", ".join(_missing),
)

# Synthetic export (Claude format). conv 1 has a clear forgotten requirement:
# the follow-up "Make it a table." -> format.
EXPORT = [
    {
        "uuid": "api-c1",
        "name": "sleep tips",
        "chat_messages": [
            {"sender": "human", "text": "Give me 10 tips for sleeping better."},
            {"sender": "assistant", "text": "Here are ten tips: ..."},
            {"sender": "human", "text": "Make it a table."},
        ],
    },
    {
        "uuid": "api-c2",
        "name": "explainer",
        "chat_messages": [
            {"sender": "human", "text": "Explain how photosynthesis works."},
            {"sender": "assistant", "text": "Photosynthesis is ..."},
            {"sender": "human", "text": "Explain it for a 10 year old."},
        ],
    },
]


def _admin_and_token():
    """Create a throwaway user and sign in for a real access token.

    Sign-in happens on a SEPARATE client: calling sign_in_with_password mutates a
    client's auth session to the signed-in user, which would strip the admin
    client's service-role context and make later admin calls (delete_user) 403.
    """
    from supabase import create_client

    settings = load_settings()
    admin = create_client(settings.supabase_url, settings.supabase_secret)
    signer = create_client(settings.supabase_url, settings.supabase_secret)

    email = f"promptdict-api+{uuidlib.uuid4().hex}@example.com"
    password = uuidlib.uuid4().hex + "Aa1!"
    created = admin.auth.admin.create_user(
        {"email": email, "password": password, "email_confirm": True}
    )
    signed_in = signer.auth.sign_in_with_password(
        {"email": email, "password": password}
    )
    return admin, created.user.id, signed_in.session.access_token


def test_process_then_checklists_owner_scoped_and_401():
    for mod in ("fastapi", "supabase", "psycopg", "mistralai", "pgvector", "hdbscan"):
        pytest.importorskip(mod)

    from fastapi.testclient import TestClient

    from promptdict.api.app import app

    admin, user_id, token = _admin_and_token()
    auth_header = {"Authorization": f"Bearer {token}"}
    export_bytes = json.dumps(EXPORT).encode("utf-8")

    try:
        with TestClient(app) as client:
            # No token -> 401.
            no_auth = client.post(
                "/process",
                files={"file": ("export.json", export_bytes, "application/json")},
            )
            assert no_auth.status_code == 401

            # Valid token -> process the capped slice.
            res = client.post(
                "/process",
                files={"file": ("export.json", export_bytes, "application/json")},
                headers=auth_header,
            )
            assert res.status_code == 200, res.text
            summary = res.json()
            assert summary["ingested"] == 2
            assert set(summary) == {"ingested", "embedded", "clusters", "forgotten_rows"}

            # Checklists for this owner show at least one global forgotten kind.
            listed = client.get("/checklists", headers=auth_header)
            assert listed.status_code == 200, listed.text
            data = listed.json()
            assert len(data["global"]) >= 1
            global_kinds = {item["kind"] for item in data["global"]}
            assert global_kinds, "expected at least one forgotten kind globally"
    finally:
        admin.auth.admin.delete_user(user_id)
