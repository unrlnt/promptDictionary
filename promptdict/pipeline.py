"""Ingestion pipeline.

Ingest stores the user's RAW text into their own private, RLS-protected rows.
Sanitization is NOT done here -- it happens at the cloud egress boundary
(see cloud.SanitizingGateway), the only place text leaves the private store.

This is the attach point for the later stages, each of which reads normalized
Conversations from the store and writes its own derived rows:

    ingest (raw, private)
        |-> embed + cluster        via gateway.embed   (step 2)
        |-> extract refinements     via gateway.extract (step 2)
        |-> detect graduation                            (step 3)
        |-> build checklist/template                     (step 3)
"""
from __future__ import annotations

from dataclasses import dataclass

from .adapters import load_conversations
from .store import Store


@dataclass
class IngestResult:
    source: str
    total: int
    new_or_changed: int


def ingest(path: str, store: Store, owner_id: str | None = None) -> IngestResult:
    """Load an export and upsert it into ``store``. When ``owner_id`` is given
    (e.g. ingesting into Postgres for a specific auth user), it is set on every
    Conversation before upsert; otherwise the model's default owner is kept."""
    source, conversations = load_conversations(path)
    changed = 0
    for conv in conversations:
        if owner_id is not None:
            conv.owner_id = owner_id
        if store.upsert(conv):
            changed += 1
    return IngestResult(source=source, total=len(conversations), new_or_changed=changed)
