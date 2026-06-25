"""Integration test for the async job flow: enqueue -> worker -> poll -> checklists.

SKIPPED unless MISTRAL_API_KEY + DATABASE_URL + SUPABASE_URL + a Supabase server
secret are set. Creates two throwaway users, enqueues a small synthetic export for
user A via the API, runs the worker in-process to completion, polls /jobs, asserts
checklists, and asserts user B gets 404 on A's job. Cleans up in finally.
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
    reason="jobs integration test needs env vars; missing: " + ", ".join(_missing),
)

EXPORT = [
    {
        "uuid": "jobs-c1",
        "name": "sleep tips",
        "chat_messages": [
            {"sender": "human", "text": "Give me 10 tips for sleeping better."},
            {"sender": "assistant", "text": "Here are ten tips: ..."},
            {"sender": "human", "text": "Make it a table."},
        ],
    },
]


def _admin():
    from supabase import create_client

    s = load_settings()
    return create_client(s.supabase_url, s.supabase_secret)


def _make_user(admin):
    """Create a throwaway user, return (id, access_token). Sign-in uses a separate
    client so it doesn't strip the admin client's service-role context."""
    from supabase import create_client

    s = load_settings()
    email = f"promptdict-jobs+{uuidlib.uuid4().hex}@example.com"
    password = uuidlib.uuid4().hex + "Aa1!"
    created = admin.auth.admin.create_user(
        {"email": email, "password": password, "email_confirm": True}
    )
    signer = create_client(s.supabase_url, s.supabase_secret)
    session = signer.auth.sign_in_with_password({"email": email, "password": password})
    return created.user.id, session.session.access_token


def test_enqueue_worker_poll_checklists_owner_scoped(tmp_path, monkeypatch):
    for mod in ("fastapi", "supabase", "psycopg", "mistralai", "pgvector", "hdbscan"):
        pytest.importorskip(mod)

    from fastapi.testclient import TestClient

    from promptdict import worker
    from promptdict.api import app as app_module
    from promptdict.cli import _production_gateway
    from promptdict.store import PostgresStore

    # Shared upload spool for both the API (writes) and the worker (reads).
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(tmp_path))

    admin = _admin()
    user_a = user_b = None
    wstore = None
    try:
        user_a, token_a = _make_user(admin)
        user_b, token_b = _make_user(admin)
        auth_a = {"Authorization": f"Bearer {token_a}"}
        auth_b = {"Authorization": f"Bearer {token_b}"}

        with TestClient(app_module.app) as client:
            # Enqueue for A -> 202 + job_id.
            res = client.post(
                "/process",
                files={"file": ("export.json", json.dumps(EXPORT).encode(), "application/json")},
                headers=auth_a,
            )
            assert res.status_code == 202, res.text
            job_id = res.json()["job_id"]

            # Run the worker in-process until A's job is done (uncapped pipeline).
            wstore = PostgresStore()
            gateway = _production_gateway()
            for _ in range(8):
                worker.run_once(wstore, gateway, str(tmp_path))
                job = client.get(f"/jobs/{job_id}", headers=auth_a).json()
                if job["status"] in ("done", "error"):
                    break
            assert job["status"] == "done", job

            # Checklists for A show a forgotten kind.
            data = client.get("/checklists", headers=auth_a).json()
            assert len(data["global"]) >= 1
            assert {item["kind"] for item in data["global"]}

            # Owner isolation: B cannot see A's job.
            assert client.get(f"/jobs/{job_id}", headers=auth_b).status_code == 404
    finally:
        if wstore is not None:
            wstore.close()
        for uid in (user_a, user_b):
            if uid:
                admin.auth.admin.delete_user(uid)
