"""Offline tests for the job API: /process enqueues (202), /jobs is owner-scoped.

No DB, no network: PostgresStore is replaced with an in-memory fake and the auth
dependency is overridden.
"""
from __future__ import annotations

import os
from uuid import UUID

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from promptdict.api import app as app_module
from promptdict.api.auth import get_current_owner

OWNER_A = "11111111-1111-1111-1111-111111111111"
OWNER_B = "22222222-2222-2222-2222-222222222222"


class FakeStore:
    """In-memory job store shared across instances (class-level)."""

    jobs: dict[str, str] = {}  # job_id -> owner_id

    def create_job(self, owner_id, job_id):
        FakeStore.jobs[job_id] = owner_id

    def get_job(self, owner_id, job_id):
        if FakeStore.jobs.get(job_id) != owner_id:
            return None  # not this owner's job -> API returns 404
        return {
            "job_id": job_id, "status": "pending", "stage": None,
            "total": 0, "processed": 0, "error": None,
            "created_at": None, "started_at": None, "finished_at": None,
        }

    def close(self):
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    FakeStore.jobs.clear()
    monkeypatch.setattr(app_module, "PostgresStore", FakeStore)
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(tmp_path))
    with TestClient(app_module.app) as c:
        yield c
    app_module.app.dependency_overrides.clear()


def _as(owner_id: str):
    app_module.app.dependency_overrides[get_current_owner] = lambda: UUID(owner_id)


def test_process_enqueues_and_returns_202(client, tmp_path):
    _as(OWNER_A)
    res = client.post(
        "/process",
        files={"file": ("export.json", b"[]", "application/json")},
    )
    assert res.status_code == 202
    job_id = res.json()["job_id"]
    assert FakeStore.jobs[job_id] == OWNER_A
    assert os.path.exists(tmp_path / f"{job_id}.json")  # spooled for the worker


def test_jobs_owner_can_poll_but_other_owner_gets_404(client):
    _as(OWNER_A)
    job_id = client.post(
        "/process", files={"file": ("export.json", b"[]", "application/json")}
    ).json()["job_id"]

    # Owner A can read it.
    assert client.get(f"/jobs/{job_id}").status_code == 200

    # Owner B cannot — 404, never leaking another owner's job.
    _as(OWNER_B)
    assert client.get(f"/jobs/{job_id}").status_code == 404


def test_jobs_requires_auth(client):
    # No dependency override + no Authorization header -> 401 before any DB call.
    app_module.app.dependency_overrides.clear()
    assert client.get("/jobs/whatever").status_code == 401
