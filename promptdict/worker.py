"""Background worker: claim queued jobs and run the full (uncapped) pipeline.

Postgres-backed queue, no Redis/broker. Claims one pending job at a time with
``FOR UPDATE SKIP LOCKED`` (safe to run multiple workers), then runs the whole
pipeline for that job's owner, updating progress as it goes. All cloud calls still
go through the gateway (egress rule intact). Run with: ``python -m promptdict.worker``.
"""
from __future__ import annotations

import logging
import os
import time

from .checklists import build_checklists
from .clustering import cluster_conversations
from .embedding import embed_conversations
from .extraction import extract_refinements, label_clusters
from .pipeline import ingest
from .store import PostgresStore

log = logging.getLogger("promptdict.worker")

POLL_INTERVAL = 2.0  # seconds between polls when the queue is empty
UPLOAD_DIR = os.environ.get("PROMPTDICT_UPLOAD_DIR", "/tmp/promptdict-uploads")


def _safe_error(exc: Exception) -> str:
    """A user-facing message with NO raw data/PII — just the failure class."""
    return f"Processing failed ({type(exc).__name__})."


def process_job(store: PostgresStore, gateway, job: dict, upload_dir: str) -> None:
    """Run the full pipeline for one claimed job, owner-scoped, updating progress.

    On success: status=done and the temp upload is deleted. On error: status=error
    with a safe message; the temp upload is still deleted. Never raises."""
    job_id = job["id"]
    owner_id = job["owner_id"]
    path = os.path.join(upload_dir, f"{job_id}.json")

    def progress(stage):
        return lambda done, total: store.update_job(job_id, processed=done, total=total)

    try:
        store.update_job(job_id, stage="ingest", processed=0, total=0)
        ingest(path, store, owner_id)

        store.update_job(job_id, stage="embed", processed=0, total=0)
        embed_conversations(store, gateway, owner_id, progress=progress("embed"))

        store.update_job(job_id, stage="cluster", processed=0, total=0)
        cluster_conversations(store, owner_id)

        store.update_job(job_id, stage="extract", processed=0, total=0)
        extract_refinements(store, gateway, owner_id, progress=progress("extract"))

        store.update_job(job_id, stage="label", processed=0, total=0)
        label_clusters(store, gateway, owner_id, progress=progress("label"))

        store.update_job(job_id, stage="checklist", processed=0, total=0)
        build_checklists(store, owner_id)

        store.finish_job(job_id, "done")
        log.info("job %s done (owner %s)", job_id, owner_id)
    except Exception as exc:  # noqa: BLE001 — one bad job must not kill the worker
        log.exception("job %s failed", job_id)  # full detail to worker logs only
        store.finish_job(job_id, "error", error=_safe_error(exc))
    finally:
        if os.path.exists(path):
            os.remove(path)  # raw upload never retained after processing


def run_once(store: PostgresStore, gateway, upload_dir: str) -> bool:
    """Claim and process a single job. Returns True if one was processed, False if
    the queue was empty."""
    job = store.claim_next_job()
    if job is None:
        return False
    process_job(store, gateway, job, upload_dir)
    return True


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    from .cli import _production_gateway  # reuse the exact CLI/API gateway wiring

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    store = PostgresStore()
    gateway = _production_gateway()
    log.info("worker started; polling every %.0fs (uploads: %s)", POLL_INTERVAL, UPLOAD_DIR)
    try:
        while True:
            if not run_once(store, gateway, UPLOAD_DIR):
                time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        log.info("worker stopping")
    finally:
        store.close()


if __name__ == "__main__":
    main()
