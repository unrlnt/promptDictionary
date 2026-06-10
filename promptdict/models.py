"""Normalized schema — the stable contract every stage reads.

This is seam #1. Downstream stages (store, clustering, extraction) read ONLY
``Conversation`` / ``Message`` — never a raw export. Adding a new source tool
means writing an adapter that produces these types; nothing downstream changes.

Field names mirror the Supabase schema (`supabase/0001_init.sql`) so the local
SQLite store and the production Postgres store share one mental model.
``owner_id`` / ``source`` / ``external_id`` exist from row one so multi-account
support is additive, not a migration.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Message:
    """One turn of a conversation. ``text`` is RAW and stays private."""

    role: str                       # 'user' | 'assistant' | 'system' | ...
    text: str                       # RAW content; private to the owner
    idx: int                        # position within the conversation (0-based)
    created_at: str | None = None   # ISO-8601 if the source provides it


@dataclass
class Conversation:
    """A normalized conversation. The unit every stage operates on."""

    source: str                          # 'chatgpt' | 'claude' | ...
    external_id: str                     # stable id from the source export
    messages: list[Message] = field(default_factory=list)
    title: str | None = None
    model: str | None = None
    project: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    owner_id: str = "local"              # auth user id in prod; 'local' for dev

    @property
    def content_hash(self) -> str:
        """Stable hash over message content. Lets the store skip re-analysis when
        a re-imported conversation hasn't changed (mirrors `content_hash` in the
        schema)."""
        h = hashlib.sha256()
        for m in self.messages:
            h.update(f"{m.idx}\x1f{m.role}\x1f{m.text}\x1e".encode("utf-8"))
        return h.hexdigest()
