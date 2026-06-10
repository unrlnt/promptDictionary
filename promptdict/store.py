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
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

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
    def get(self, owner_id: str, conversation_id: str) -> Conversation | None:
        """Return a single conversation (with its messages in idx order) owned by
        ``owner_id``, or None. MUST never return another owner's conversation."""

    @abstractmethod
    def iter_conversations(self, owner_id: str) -> list[Conversation]:
        """Return all conversations for ``owner_id`` (each with messages in idx
        order). MUST never include another owner's conversations."""

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

    def iter_conversations(self, owner_id: str = "local") -> list[Conversation]:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT id, source, external_id, title, model, project, created_at, updated_at "
            "FROM conversations WHERE owner_id = ? "
            "ORDER BY updated_at DESC NULLS LAST, id DESC",
            (owner_id,),
        )
        rows = cur.fetchall()
        out: list[Conversation] = []
        for r in rows:
            mcur = self._conn.cursor()
            mcur.execute(
                "SELECT role, text, idx, created_at FROM messages "
                "WHERE conversation_id = ? AND owner_id = ? ORDER BY idx",
                (r[0], owner_id),
            )
            messages = [Message(role=m[0], text=m[1], idx=m[2], created_at=m[3])
                        for m in mcur.fetchall()]
            out.append(Conversation(
                source=r[1], external_id=r[2], messages=messages, title=r[3],
                model=r[4], project=r[5], created_at=r[6], updated_at=r[7],
                owner_id=owner_id,
            ))
        return out

    def get(self, owner_id: str, conversation_id: str) -> Conversation | None:
        # SQLite uses an integer PK; the stable uuid5 isn't stored, so match on the
        # conversation_id property (owner-scoped via iter_conversations).
        for conv in self.iter_conversations(owner_id):
            if conv.conversation_id == conversation_id:
                return conv
        return None

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


def _ts(value: str | None):
    """Coerce an ISO-8601 string to a datetime (psycopg adapts it to timestamptz).
    Falls back to the raw value so a SQL ``::timestamptz`` cast can still parse it."""
    if not value:
        return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    return value


def _iso(value) -> str | None:
    """Render a DB timestamptz (a datetime) back to an ISO string for the model."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


class PostgresStore(Store):
    """Production store backed by Supabase Postgres (the live schema).

    SECURITY: this connection uses ``DATABASE_URL`` and BYPASSES row-level
    security. RLS protects app/anon connections; here it is not in effect, so
    every read and write filters by ``owner_id`` explicitly in SQL. ``get`` and
    ``iter_conversations`` can never return another owner's rows.

    The stable ``conversation_id`` (uuid5 of owner/source/external_id) is used as
    ``conversations.id``, so re-imports upsert the same row. Embeddings are left
    NULL here — that column is written by a later stage via the egress gateway.
    """

    def __init__(self, database_url: str | None = None):
        import psycopg  # optional dep (cloud extra); imported lazily

        if database_url is None:
            from .config import load_settings
            database_url = load_settings().require("database_url")

        # autocommit=True + explicit `with conn.transaction()` for write blocks.
        self._conn = psycopg.connect(database_url, autocommit=True)
        # Disable server-side prepared statements so this works under a
        # transaction/statement pooler (e.g. PgBouncer / Supabase pooler).
        self._conn.prepare_threshold = None

    def close(self) -> None:
        self._conn.close()

    def upsert(self, conversation: Conversation) -> bool:
        conv_id = uuid.UUID(conversation.conversation_id)
        owner_id = uuid.UUID(conversation.owner_id)
        new_hash = conversation.content_hash

        with self._conn.transaction():
            cur = self._conn.cursor()
            # ON CONFLICT (id): update only when the content hash actually differs.
            # The WHERE makes an unchanged re-import a true no-op, so RETURNING
            # yields no row and we report "not changed" — matching SQLiteStore.
            row = cur.execute(
                """
                INSERT INTO conversations
                    (id, owner_id, source, external_id, title, model, project,
                     created_at, updated_at, content_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s,
                        %s::timestamptz, %s::timestamptz, %s)
                ON CONFLICT (id) DO UPDATE SET
                    title        = EXCLUDED.title,
                    model        = EXCLUDED.model,
                    project      = EXCLUDED.project,
                    created_at   = EXCLUDED.created_at,
                    updated_at   = EXCLUDED.updated_at,
                    content_hash = EXCLUDED.content_hash
                WHERE conversations.owner_id = EXCLUDED.owner_id
                  AND conversations.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                RETURNING id
                """,
                (conv_id, owner_id, conversation.source, conversation.external_id,
                 conversation.title, conversation.model, conversation.project,
                 _ts(conversation.created_at), _ts(conversation.updated_at), new_hash),
            ).fetchone()

            if row is None:
                return False  # existed and unchanged — nothing to do

            # New or changed: replace the message set (owner-scoped delete + insert).
            cur.execute(
                "DELETE FROM messages WHERE conversation_id = %s AND owner_id = %s",
                (conv_id, owner_id),
            )
            cur.executemany(
                "INSERT INTO messages "
                "(conversation_id, owner_id, role, text, idx, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s::timestamptz)",
                [(conv_id, owner_id, m.role, m.text, m.idx, _ts(m.created_at))
                 for m in conversation.messages],
            )
            return True

    def get(self, owner_id: str, conversation_id: str) -> Conversation | None:
        from psycopg.rows import dict_row

        cur = self._conn.cursor(row_factory=dict_row)
        crow = cur.execute(
            "SELECT id, owner_id, source, external_id, title, model, project, "
            "created_at, updated_at FROM conversations "
            "WHERE id = %s AND owner_id = %s",
            (uuid.UUID(conversation_id), uuid.UUID(owner_id)),
        ).fetchone()
        if crow is None:
            return None
        return self._build(crow)

    def iter_conversations(self, owner_id: str) -> list[Conversation]:
        from psycopg.rows import dict_row

        cur = self._conn.cursor(row_factory=dict_row)
        rows = cur.execute(
            "SELECT id, owner_id, source, external_id, title, model, project, "
            "created_at, updated_at FROM conversations "
            "WHERE owner_id = %s ORDER BY updated_at DESC NULLS LAST, id",
            (uuid.UUID(owner_id),),
        ).fetchall()
        return [self._build(r) for r in rows]

    def list_conversations(self, owner_id: str = "local") -> list[ConversationSummary]:
        from psycopg.rows import dict_row

        cur = self._conn.cursor(row_factory=dict_row)
        rows = cur.execute(
            "SELECT c.source, c.external_id, c.title, c.updated_at, "
            "       (SELECT COUNT(*) FROM messages m "
            "        WHERE m.conversation_id = c.id AND m.owner_id = c.owner_id) AS n "
            "FROM conversations c WHERE c.owner_id = %s "
            "ORDER BY c.updated_at DESC NULLS LAST, c.id",
            (uuid.UUID(owner_id),),
        ).fetchall()
        return [
            ConversationSummary(source=r["source"], external_id=r["external_id"],
                                title=r["title"], updated_at=_iso(r["updated_at"]),
                                n_messages=r["n"])
            for r in rows
        ]

    def _build(self, crow: dict) -> Conversation:
        """Reconstruct a Conversation (owner-scoped messages, idx order) from a row."""
        from psycopg.rows import dict_row

        mcur = self._conn.cursor(row_factory=dict_row)
        mrows = mcur.execute(
            "SELECT role, text, idx, created_at FROM messages "
            "WHERE conversation_id = %s AND owner_id = %s ORDER BY idx",
            (crow["id"], crow["owner_id"]),
        ).fetchall()
        messages = [Message(role=m["role"], text=m["text"], idx=m["idx"],
                            created_at=_iso(m["created_at"])) for m in mrows]
        return Conversation(
            source=crow["source"], external_id=crow["external_id"], messages=messages,
            title=crow["title"], model=crow["model"], project=crow["project"],
            created_at=_iso(crow["created_at"]), updated_at=_iso(crow["updated_at"]),
            owner_id=str(crow["owner_id"]),
        )
