"""FastAPI service exposing the promptdict engine to the web app.

Thin driver: reuses the CLI's production gateway construction and the existing engine
functions. Every DB/engine call is scoped to the verified owner_id from the JWT.
The sanitization gateway and egress rule are untouched — embeddings/extraction still
go only through ``gateway.embed`` / ``gateway.extract``.
"""
from __future__ import annotations

import os
import tempfile
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import Depends, FastAPI, File, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from ..checklists import build_checklists
from ..clustering import cluster_conversations
from ..embedding import embed_conversations
from ..extraction import extract_refinements, label_clusters
from ..pipeline import ingest
from ..store import PostgresStore
from .auth import get_current_owner

# Capped slice for the synchronous MVP: process at most this many items per stage so
# a request returns quickly. 8d removes this cap with async/background processing.
SLICE = 10


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Build the SAME production gateway the CLI uses (Mistral providers behind the
    # SanitizingGateway), once, on startup. Reuses the existing wiring — no dupes.
    from ..cli import _production_gateway

    app.state.gateway = _production_gateway()
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


@app.post("/process")
def process(
    request: Request,
    file: UploadFile = File(...),
    owner: UUID = Depends(get_current_owner),
):
    """Upload an export and run the capped pipeline for the verified owner. The raw
    upload is written to a temp file and deleted in a finally block — never retained."""
    owner_id = str(owner)
    gateway = request.app.state.gateway

    tmp_path: str | None = None
    store = PostgresStore()
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(file.file.read())

        ingested = ingest(tmp_path, store, owner_id)
        embedded = embed_conversations(store, gateway, owner_id, limit=SLICE)
        clustered = cluster_conversations(store, owner_id)
        extract_refinements(store, gateway, owner_id, limit=SLICE)
        label_clusters(store, gateway, owner_id, limit=SLICE)
        build_checklists(store, owner_id)

        forgotten_rows = sum(
            row["total_count"]
            for row in store.iter_checklists(owner_id)
            if row["scope"] == "global"
        )
        return {
            "ingested": ingested.total,
            "embedded": embedded,
            "clusters": clustered.n_clusters,
            "forgotten_rows": forgotten_rows,
        }
    finally:
        store.close()
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)  # raw upload is never retained on disk


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
