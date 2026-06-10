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


def ingest(path: str, store: Store) -> IngestResult:
    source, conversations = load_conversations(path)
    changed = sum(1 for conv in conversations if store.upsert(conv))
    return IngestResult(source=source, total=len(conversations), new_or_changed=changed)
