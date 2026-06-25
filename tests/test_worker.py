"""Offline unit tests for the worker's claim→process→transition logic.

No DB, no network: the engine functions are patched to no-ops/raisers and a fake
store records the job transitions. (The real FOR UPDATE SKIP LOCKED claim SQL is
covered by the live integration test.)
"""
from __future__ import annotations

import pytest

from promptdict import worker

JOB_ID = "11111111-1111-1111-1111-111111111111"
OWNER_ID = "22222222-2222-2222-2222-222222222222"
STAGES = ["ingest", "embed", "cluster", "extract", "label", "checklist"]


class FakeStore:
    def __init__(self):
        self.stages: list[str] = []
        self.finished: tuple[str, str | None] | None = None

    def update_job(self, job_id, *, stage=None, processed=None, total=None):
        if stage is not None:
            self.stages.append(stage)

    def finish_job(self, job_id, status, error=None):
        self.finished = (status, error)


def _patch_engine(monkeypatch, raise_on: str | None = None):
    def make(name):
        def fn(*args, **kwargs):
            if raise_on == name:
                # Include fake "PII" to prove it never reaches the stored error.
                raise ValueError("boom on 4111-1111-1111-1111")
        return fn

    monkeypatch.setattr(worker, "ingest", make("ingest"))
    monkeypatch.setattr(worker, "embed_conversations", make("embed"))
    monkeypatch.setattr(worker, "cluster_conversations", make("cluster"))
    monkeypatch.setattr(worker, "extract_refinements", make("extract"))
    monkeypatch.setattr(worker, "label_clusters", make("label"))
    monkeypatch.setattr(worker, "build_checklists", make("checklist"))


def _job_file(tmp_path):
    f = tmp_path / f"{JOB_ID}.json"
    f.write_text("[]")
    return f


def test_process_job_success_runs_all_stages_and_deletes_file(tmp_path, monkeypatch):
    _patch_engine(monkeypatch)
    store = FakeStore()
    f = _job_file(tmp_path)

    worker.process_job(store, object(), {"id": JOB_ID, "owner_id": OWNER_ID}, str(tmp_path))

    assert store.stages == STAGES
    assert store.finished == ("done", None)
    assert not f.exists()  # raw upload deleted after processing


def test_process_job_error_stores_safe_message_and_deletes_file(tmp_path, monkeypatch):
    _patch_engine(monkeypatch, raise_on="extract")
    store = FakeStore()
    f = _job_file(tmp_path)

    worker.process_job(store, object(), {"id": JOB_ID, "owner_id": OWNER_ID}, str(tmp_path))

    status, error = store.finished
    assert status == "error"
    assert "ValueError" in error          # class is fine to surface
    assert "4111" not in error            # raw data/PII never leaked
    assert not f.exists()                  # temp upload still cleaned up on error


def test_run_once_returns_false_on_empty_queue():
    class EmptyStore:
        def claim_next_job(self):
            return None

    assert worker.run_once(EmptyStore(), object(), "/tmp") is False


def test_run_once_processes_a_claimed_job(tmp_path, monkeypatch):
    _patch_engine(monkeypatch)

    class OneJobStore(FakeStore):
        def __init__(self):
            super().__init__()
            self._claims = 0

        def claim_next_job(self):
            self._claims += 1
            return {"id": JOB_ID, "owner_id": OWNER_ID} if self._claims == 1 else None

    store = OneJobStore()
    _job_file(tmp_path)

    assert worker.run_once(store, object(), str(tmp_path)) is True
    assert store.finished == ("done", None)
