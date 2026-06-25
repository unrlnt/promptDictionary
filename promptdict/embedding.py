"""Embed conversations through the gateway.

Builds one representative text per conversation, gets a vector via the egress
gateway (which sanitizes first — never the provider directly), and stores it on
``conversations.embedding``. This is the only place embeddings are written.

Owner-scoped and idempotent: it only touches conversations belonging to
``owner_id`` and skips any that already have an embedding.
"""
from __future__ import annotations

from collections.abc import Callable

from .cloud import SanitizingGateway
from .models import Conversation
from .store import PostgresStore

# Optional per-stage progress hook: progress(processed, total).
Progress = Callable[[int, int], None]

# Cap the representative text so a long conversation can't blow up token cost.
DEFAULT_CHAR_LIMIT = 8000


def representative_text(conversation: Conversation, char_limit: int = DEFAULT_CHAR_LIMIT) -> str:
    """The text we embed for a conversation: its user turns concatenated (the user's
    own phrasing is what defines the task), capped at ``char_limit`` characters."""
    user_turns = [m.text for m in conversation.messages if m.role == "user" and m.text]
    return "\n\n".join(user_turns).strip()[:char_limit]


def embed_conversations(store: PostgresStore, gateway: SanitizingGateway,
                        owner_id: str, char_limit: int = DEFAULT_CHAR_LIMIT,
                        limit: int | None = None,
                        progress: Progress | None = None) -> int:
    """Embed every not-yet-embedded conversation for ``owner_id``. Returns the count
    embedded. Sanitization happens inside ``gateway.embed`` — no provider is called
    directly here. ``limit`` caps how many are embedded (None = all, unchanged).
    ``progress(done, total)`` is called as work proceeds, if given."""
    convs = store.iter_unembedded(owner_id)
    total = len(convs) if limit is None else min(limit, len(convs))
    if progress:
        progress(0, total)
    embedded = 0
    done = 0
    for conv in convs:
        if limit is not None and embedded >= limit:
            break
        done += 1
        text = representative_text(conv, char_limit)
        if text:
            vector = gateway.embed([text])[0]
            store.set_embedding(owner_id, conv.conversation_id, vector)
            embedded += 1
        if progress:
            progress(min(done, total), total)
    return embedded
