"""FastAPI service exposing the promptdict engine to the web app.

Thin driver. Uploads are NOT processed inline: /process saves the file to a shared
location and enqueues a job; the worker (promptdict.worker) runs the full, uncapped
pipeline through the gateway. The API only enqueues and reads, always scoped to the
owner_id from the verified JWT. The sanitization gateway and egress rule are
untouched (the API makes no provider calls at all).
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ..store import PostgresStore
from .auth import get_current_owner

# System prompt for the conversational rewrite. Static app text (no user data), so
# it passes through the gateway's `chat` unchanged; only the user message is
# sanitized before egress.
IMPROVE_SYSTEM_PROMPT = (
    "You are a prompt engineering assistant. The user wants to improve their AI "
    "prompt. You have access to patterns from their past conversations showing what "
    "they tend to forget. Rewrite their prompt to be more complete and effective by "
    "incorporating the relevant patterns. Return ONLY the improved prompt, no "
    "explanation, no preamble."
)
# Below this cosine similarity we don't trust the cluster match; fall back to global.
SIMILARITY_FLOOR = 0.3


class ImproveRequest(BaseModel):
    prompt: str


def _gateway(request: Request):
    """Lazily build the egress gateway once and cache it on app state. Kept off the
    startup path so the service still boots without Mistral config — only /improve
    needs it. Reuses the exact wiring the CLI/worker use (sanitizer + providers)."""
    gw = getattr(request.app.state, "gateway", None)
    if gw is None:
        from ..cli import _production_gateway

        gw = _production_gateway()
        request.app.state.gateway = gw
    return gw


def _note_line(item: dict) -> str:
    """One bullet per checklist item: the forgotten requirement plus an example note."""
    note = item["sample_notes"][0] if item.get("sample_notes") else None
    return f"- {item['kind']}" + (f": {note}" if note else "")

# Where uploaded files wait for the worker. Shared with the worker via a named
# volume in compose (PROMPTDICT_UPLOAD_DIR=/uploads). The file is named <job_id>.json
# so the worker locates it from the job id alone (no DB path column needed).
UPLOAD_DIR = os.environ.get("PROMPTDICT_UPLOAD_DIR", "/tmp/promptdict-uploads")


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    app.state.upload_dir = UPLOAD_DIR
    yield


app = FastAPI(title="promptdict API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.environ.get("WEB_ORIGIN", "http://localhost:3000")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/process", status_code=status.HTTP_202_ACCEPTED)
def process(
    request: Request,
    file: UploadFile = File(...),
    owner: UUID = Depends(get_current_owner),
):
    """Save the upload and enqueue a job for the verified owner; return its id (202).
    Processing happens in the worker, not here. The owner is taken only from the JWT."""
    owner_id = str(owner)
    job_id = str(uuid.uuid4())
    dest = os.path.join(request.app.state.upload_dir, f"{job_id}.json")

    with open(dest, "wb") as out:
        out.write(file.file.read())

    store = PostgresStore()
    try:
        store.create_job(owner_id, job_id)
    except Exception:
        if os.path.exists(dest):
            os.remove(dest)  # don't leave an orphan upload if enqueue fails
        raise
    finally:
        store.close()

    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def get_job(job_id: str, owner: UUID = Depends(get_current_owner)):
    """Poll a job's progress — only if it belongs to the verified owner (else 404)."""
    owner_id = str(owner)
    store = PostgresStore()
    try:
        try:
            job = store.get_job(owner_id, job_id)
        except ValueError:
            job = None  # malformed job id
    finally:
        store.close()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/checklists")
def checklists(owner: UUID = Depends(get_current_owner)):
    """Return the verified owner's checklists: global + per-cluster (with labels)."""
    owner_id = str(owner)
    store = PostgresStore()
    try:
        rows = store.iter_checklists(owner_id)
        labels = dict(store.iter_clusters(owner_id))
    finally:
        store.close()

    by_cluster: dict[str, list[dict]] = {}
    for row in rows:
        if row["scope"] == "cluster":
            by_cluster.setdefault(row["cluster_id"], []).append(row)

    return {
        "global": sorted(
            (r for r in rows if r["scope"] == "global"), key=lambda r: r["rank"]
        ),
        "clusters": [
            {
                "cluster_id": cid,
                "label": labels.get(cid),
                "items": sorted(items, key=lambda r: r["rank"]),
            }
            for cid, items in by_cluster.items()
        ],
    }


@app.post("/improve")
def improve(
    request: Request,
    body: ImproveRequest,
    owner: UUID = Depends(get_current_owner),
):
    """Rewrite the user's draft prompt using their forgotten-pattern checklist for the
    closest matching cluster (falling back to the global checklist). Every outbound
    call goes through the sanitizing gateway — the API touches no provider directly."""
    owner_id = str(owner)
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=422, detail="prompt must not be empty")

    gateway = _gateway(request)

    # 1) Embed the prompt (sanitized inside the gateway, same as conversation embeds).
    embedding = gateway.embed([prompt])[0]

    store = PostgresStore()
    try:
        # 2) Closest cluster by centroid cosine similarity.
        match = store.nearest_cluster(owner_id, embedding)
        # 3) Reuse the same checklist read as GET /checklists.
        rows = store.iter_checklists(owner_id)
    finally:
        store.close()

    # 4) Trust the cluster match only above the floor; otherwise use the global list.
    if match is not None and match["similarity"] >= SIMILARITY_FLOOR:
        cluster_label = match["label"]
        items = [
            r for r in rows
            if r["scope"] == "cluster" and r["cluster_id"] == match["cluster_id"]
        ]
    else:
        cluster_label = None
        items = [r for r in rows if r["scope"] == "global"]
    items = sorted(items, key=lambda r: r["rank"])

    # 5) Conversational rewrite through the gateway (user message sanitized on egress).
    bullets = "\n".join(_note_line(it) for it in items)
    user_message = (
        f"My prompt: {prompt}\n\n"
        f"Things I tend to forget based on my past conversations:\n{bullets}"
    )
    improved = gateway.chat(IMPROVE_SYSTEM_PROMPT, user_message)

    # 6) Return the rewrite plus which cluster (if any) informed it.
    return {"improved_prompt": improved, "cluster_label": cluster_label}
