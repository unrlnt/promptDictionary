"""Store — seam #3.

The contract for persisting normalized conversations. ``SQLiteStore`` is the
local-dev implementation and stays working forever (CLAUDE.md). ``PostgresStore``
(Supabase, with RLS) is the production implementation — to build.

Raw text is stored here, privately. Sanitization is NOT done at ingest; it happens
only at the cloud egress boundary (`cloud.SanitizingGateway`). ``owner_id`` /
``source`` / ``external_id`` exist from row one so accounts are additive.
"""
from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .models import Conversation, Message


@dataclass
class ConversationSummary:
    """Lightweight row for listing without loading full message text."""

    source: str
    external_id: str
    title: str | None
    n_messages: int
    updated_at: str | None


class Store(ABC):
    @abstractmethod
    def upsert(self, conversation: Conversation) -> bool:
        """Insert or update a conversation. Returns True if it was new or its
        content changed (so downstream re-analysis is warranted), False if an
        identical version was already stored."""

    @abstractmethod
    def list_conversations(self, owner_id: str = "local") -> list[ConversationSummary]:
        """List stored conversations for an owner, newest activity first."""


class SQLiteStore(Store):
    """Local-dev store. Mirrors the owner/source/external_id contract and the
    conversations+messages split of the Postgres schema."""

    def __init__(self, path: str = "promptdict.db"):
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id      TEXT NOT NULL,
                source        TEXT NOT NULL,
                external_id   TEXT NOT NULL,
                title         TEXT,
                model         TEXT,
                project       TEXT,
                created_at    TEXT,
                updated_at    TEXT,
                content_hash  TEXT NOT NULL,
                UNIQUE (owner_id, source, external_id)
            );
            CREATE TABLE IF NOT EXISTS messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                owner_id        TEXT NOT NULL,
                role            TEXT NOT NULL,
                text            TEXT NOT NULL,
                idx             INTEGER NOT NULL,
                created_at      TEXT
            );
            CREATE INDEX IF NOT EXISTS messages_conv_idx ON messages (conversation_id, idx);
            """
        )
        self._conn.commit()

    def upsert(self, conversation: Conversation) -> bool:
        cur = self._conn.cursor()
        key = (conversation.owner_id, conversation.source, conversation.external_id)
        cur.execute(
            "SELECT id, content_hash FROM conversations "
            "WHERE owner_id = ? AND source = ? AND external_id = ?",
            key,
        )
        row = cur.fetchone()
        new_hash = conversation.content_hash

        if row and row[1] == new_hash:
            return False  # unchanged — nothing to do

        if row:
            conv_id = row[0]
            cur.execute(
                "UPDATE conversations SET title=?, model=?, project=?, "
                "created_at=?, updated_at=?, content_hash=? WHERE id=?",
                (conversation.title, conversation.model, conversation.project,
                 conversation.created_at, conversation.updated_at, new_hash, conv_id),
            )
            cur.execute("DELETE FROM messages WHERE conversation_id=?", (conv_id,))
        else:
            cur.execute(
                "INSERT INTO conversations (owner_id, source, external_id, title, "
                "model, project, created_at, updated_at, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (conversation.owner_id, conversation.source, conversation.external_id,
                 conversation.title, conversation.model, conversation.project,
                 conversation.created_at, conversation.updated_at, new_hash),
            )
            conv_id = cur.lastrowid

        self._insert_messages(cur, conv_id, conversation)
        self._conn.commit()
        return True

    @staticmethod
    def _insert_messages(cur: sqlite3.Cursor, conv_id: int, conv: Conversation) -> None:
        cur.executemany(
            "INSERT INTO messages (conversation_id, owner_id, role, text, idx, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(conv_id, conv.owner_id, m.role, m.text, m.idx, m.created_at)
             for m in conv.messages],
        )

    def list_conversations(self, owner_id: str = "local") -> list[ConversationSummary]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT c.source, c.external_id, c.title, c.updated_at, "
            "       (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) "
            "FROM conversations c WHERE c.owner_id = ? "
            "ORDER BY c.updated_at DESC NULLS LAST, c.id DESC",
            (owner_id,),
        )
        return [
            ConversationSummary(source=r[0], external_id=r[1], title=r[2],
                                updated_at=r[3], n_messages=r[4])
            for r in cur.fetchall()
        ]

    def close(self) -> None:
        self._conn.close()
