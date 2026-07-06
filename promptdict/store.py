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

        # Map Python lists <-> pgvector's vector type. Optional (cloud extra):
        # store works without it for everything except embedding writes/reads.
        self._vector_ready = False
        try:
            from pgvector.psycopg import register_vector
            register_vector(self._conn)
            self._vector_ready = True
        except ImportError:
            pass

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
                    content_hash = EXCLUDED.content_hash,
                    -- Content changed (guarded by the WHERE below), so the derived
                    -- data is now stale: null it out so the incremental embed +
                    -- extract stages reprocess just this conversation next run.
                    embedding    = NULL,
                    cluster_id   = NULL,
                    refinements_extracted_at = NULL
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

    def iter_unembedded(self, owner_id: str) -> list[Conversation]:
        """Conversations for ``owner_id`` whose embedding is still NULL."""
        from psycopg.rows import dict_row

        cur = self._conn.cursor(row_factory=dict_row)
        rows = cur.execute(
            "SELECT id, owner_id, source, external_id, title, model, project, "
            "created_at, updated_at FROM conversations "
            "WHERE owner_id = %s AND embedding IS NULL "
            "ORDER BY updated_at DESC NULLS LAST, id",
            (uuid.UUID(owner_id),),
        ).fetchall()
        return [self._build(r) for r in rows]

    def set_embedding(self, owner_id: str, conversation_id: str,
                      vector: list[float]) -> None:
        """Write a 1024-dim embedding for one owner-scoped conversation."""
        if not self._vector_ready:
            raise RuntimeError(
                "pgvector not available — install the cloud extra "
                "(pip install -e \".[cloud]\") to write embeddings."
            )
        with self._conn.transaction():
            self._conn.execute(
                "UPDATE conversations SET embedding = %s "
                "WHERE id = %s AND owner_id = %s",
                (vector, uuid.UUID(conversation_id), uuid.UUID(owner_id)),
            )

    def iter_embedded(self, owner_id: str) -> list[tuple[str, object]]:
        """Return ``(conversation_id, embedding)`` for the owner's embedded
        conversations. The embedding comes back as a pgvector-mapped array."""
        if not self._vector_ready:
            raise RuntimeError(
                "pgvector not available — install the cloud extra "
                "(pip install -e \".[cloud]\") to read embeddings."
            )
        cur = self._conn.cursor()
        rows = cur.execute(
            "SELECT id, embedding FROM conversations "
            "WHERE owner_id = %s AND embedding IS NOT NULL ORDER BY id",
            (uuid.UUID(owner_id),),
        ).fetchall()
        return [(str(r[0]), r[1]) for r in rows]

    def replace_clusters(self, owner_id: str,
                         clusters: list[tuple[object, list[str]]]) -> None:
        """Idempotently rewrite this owner's clusters. ``clusters`` is a list of
        ``(centroid_vector, [member_conversation_id, ...])``. Clears the owner's
        existing clusters/assignments first, then writes the new ones. Other
        owners' rows are never touched."""
        with self._conn.transaction():
            # Detach this owner's conversations, then drop their old clusters.
            self._conn.execute(
                "UPDATE conversations SET cluster_id = NULL WHERE owner_id = %s",
                (uuid.UUID(owner_id),),
            )
            self._conn.execute(
                "DELETE FROM clusters WHERE owner_id = %s", (uuid.UUID(owner_id),)
            )
            for centroid, member_ids in clusters:
                row = self._conn.execute(
                    "INSERT INTO clusters (owner_id, centroid) VALUES (%s, %s) "
                    "RETURNING id",
                    (uuid.UUID(owner_id), centroid),
                ).fetchone()
                cluster_id = row[0]
                self._conn.execute(
                    "UPDATE conversations SET cluster_id = %s "
                    "WHERE owner_id = %s AND id = ANY(%s)",
                    (cluster_id, uuid.UUID(owner_id),
                     [uuid.UUID(m) for m in member_ids]),
                )

    def iter_unextracted(self, owner_id: str) -> list[tuple[Conversation, str | None]]:
        """Return ``(conversation, cluster_id)`` for the owner's conversations that
        have not had refinements extracted yet."""
        from psycopg.rows import dict_row

        cur = self._conn.cursor(row_factory=dict_row)
        rows = cur.execute(
            "SELECT id, owner_id, source, external_id, title, model, project, "
            "created_at, updated_at, cluster_id FROM conversations "
            "WHERE owner_id = %s AND refinements_extracted_at IS NULL ORDER BY id",
            (uuid.UUID(owner_id),),
        ).fetchall()
        out: list[tuple[Conversation, str | None]] = []
        for r in rows:
            cluster_id = None if r["cluster_id"] is None else str(r["cluster_id"])
            out.append((self._build(r), cluster_id))
        return out

    def replace_refinements(self, owner_id: str, conversation_id: str,
                            rows: list[tuple]) -> None:
        """Idempotently rewrite one conversation's refinements and stamp it as
        extracted. ``rows`` are ``(kind, turn_index, in_first_prompt, note,
        cluster_id)``. Owner-scoped; other owners untouched."""
        conv_uuid = uuid.UUID(conversation_id)
        owner_uuid = uuid.UUID(owner_id)
        with self._conn.transaction():
            self._conn.execute(
                "DELETE FROM refinements WHERE conversation_id = %s AND owner_id = %s",
                (conv_uuid, owner_uuid),
            )
            for kind, turn_index, in_first_prompt, note, cluster_id in rows:
                self._conn.execute(
                    "INSERT INTO refinements (owner_id, conversation_id, cluster_id, "
                    "kind, turn_index, in_first_prompt, note) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (owner_uuid, conv_uuid,
                     uuid.UUID(cluster_id) if cluster_id else None,
                     kind, turn_index, in_first_prompt, note),
                )
            self._conn.execute(
                "UPDATE conversations SET refinements_extracted_at = now() "
                "WHERE id = %s AND owner_id = %s",
                (conv_uuid, owner_uuid),
            )

    def iter_clusters_unlabelled(self, owner_id: str,
                                 sample_size: int = 5) -> list[tuple[str, list[str]]]:
        """Return ``(cluster_id, [first_user_message, ...])`` for this owner's
        clusters that have no label yet — up to ``sample_size`` samples each."""
        from psycopg.rows import dict_row

        cur = self._conn.cursor(row_factory=dict_row)
        clusters = cur.execute(
            "SELECT id FROM clusters WHERE owner_id = %s AND label IS NULL ORDER BY id",
            (uuid.UUID(owner_id),),
        ).fetchall()
        out: list[tuple[str, list[str]]] = []
        for c in clusters:
            members = self._conn.cursor(row_factory=dict_row).execute(
                "SELECT id FROM conversations "
                "WHERE owner_id = %s AND cluster_id = %s ORDER BY id LIMIT %s",
                (uuid.UUID(owner_id), c["id"], sample_size),
            ).fetchall()
            samples: list[str] = []
            for m in members:
                first = self._conn.cursor(row_factory=dict_row).execute(
                    "SELECT text FROM messages "
                    "WHERE conversation_id = %s AND owner_id = %s AND role = 'user' "
                    "ORDER BY idx LIMIT 1",
                    (m["id"], uuid.UUID(owner_id)),
                ).fetchone()
                if first and first["text"]:
                    samples.append(first["text"])
            out.append((str(c["id"]), samples))
        return out

    def set_cluster_label(self, owner_id: str, cluster_id: str, label: str) -> None:
        """Set a cluster's human-readable label, owner-scoped."""
        with self._conn.transaction():
            self._conn.execute(
                "UPDATE clusters SET label = %s WHERE id = %s AND owner_id = %s",
                (label, uuid.UUID(cluster_id), uuid.UUID(owner_id)),
            )

    def iter_refinements(self, owner_id: str) -> list[dict]:
        """All of the owner's refinement rows (both forgotten and upfront — the
        graduation trend needs the upfront ones), each joined to its conversation's
        created_at. Owner-scoped on both tables."""
        from psycopg.rows import dict_row

        cur = self._conn.cursor(row_factory=dict_row)
        rows = cur.execute(
            "SELECT r.kind, r.in_first_prompt, r.cluster_id, r.conversation_id, "
            "       r.note, c.created_at "
            "FROM refinements r "
            "JOIN conversations c "
            "  ON c.id = r.conversation_id AND c.owner_id = r.owner_id "
            "WHERE r.owner_id = %s "
            "ORDER BY c.created_at NULLS LAST, r.id",
            (uuid.UUID(owner_id),),
        ).fetchall()
        return [
            {"kind": r["kind"], "in_first_prompt": r["in_first_prompt"],
             "cluster_id": None if r["cluster_id"] is None else str(r["cluster_id"]),
             "conversation_id": str(r["conversation_id"]),
             "note": r["note"], "created_at": _iso(r["created_at"])}
            for r in rows
        ]

    def iter_clusters(self, owner_id: str) -> list[tuple[str, str | None]]:
        """``(cluster_id, label)`` for the owner's clusters."""
        from psycopg.rows import dict_row

        cur = self._conn.cursor(row_factory=dict_row)
        rows = cur.execute(
            "SELECT id, label FROM clusters WHERE owner_id = %s ORDER BY id",
            (uuid.UUID(owner_id),),
        ).fetchall()
        return [(str(r["id"]), r["label"]) for r in rows]

    def replace_checklists(self, owner_id: str, rows: list[tuple]) -> None:
        """Idempotently rewrite the owner's checklists. ``rows`` are
        ``(scope, cluster_id, kind, conversation_count, total_count, rank,
        sample_notes, graduation)``. Owner-scoped; other owners untouched."""
        owner = uuid.UUID(owner_id)
        with self._conn.transaction():
            self._conn.execute(
                "DELETE FROM checklists WHERE owner_id = %s", (owner,)
            )
            for (scope, cluster_id, kind, conv_count, total, rank,
                 sample_notes, graduation) in rows:
                self._conn.execute(
                    "INSERT INTO checklists (owner_id, scope, cluster_id, kind, "
                    "conversation_count, total_count, rank, sample_notes, graduation) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (owner, scope,
                     uuid.UUID(cluster_id) if cluster_id else None,
                     kind, conv_count, total, rank, list(sample_notes), graduation),
                )

    def iter_checklists(self, owner_id: str) -> list[dict]:
        """Read the owner's checklist rows for display."""
        from psycopg.rows import dict_row

        cur = self._conn.cursor(row_factory=dict_row)
        rows = cur.execute(
            "SELECT scope, cluster_id, kind, conversation_count, total_count, rank, "
            "       sample_notes, graduation FROM checklists "
            "WHERE owner_id = %s ORDER BY scope, cluster_id NULLS FIRST, rank",
            (uuid.UUID(owner_id),),
        ).fetchall()
        return [
            {"scope": r["scope"],
             "cluster_id": None if r["cluster_id"] is None else str(r["cluster_id"]),
             "kind": r["kind"], "conversation_count": r["conversation_count"],
             "total_count": r["total_count"], "rank": r["rank"],
             "sample_notes": list(r["sample_notes"] or []),
             "graduation": r["graduation"]}
            for r in rows
        ]

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

    # --- background jobs -----------------------------------------------------

    def create_job(self, owner_id: str, job_id: str) -> None:
        """Enqueue a pending job for ``owner_id`` with a caller-supplied id (which
        also names the upload file on the shared volume)."""
        with self._conn.transaction():
            self._conn.execute(
                "INSERT INTO jobs (id, owner_id, status) VALUES (%s, %s, 'pending')",
                (uuid.UUID(job_id), uuid.UUID(owner_id)),
            )

    def get_job(self, owner_id: str, job_id: str) -> dict | None:
        """Return a job's progress, but ONLY if it belongs to ``owner_id`` (else
        None — the API turns that into a 404, never leaking other owners' jobs)."""
        from psycopg.rows import dict_row

        cur = self._conn.cursor(row_factory=dict_row)
        row = cur.execute(
            "SELECT id, status, stage, total, processed, error, "
            "created_at, started_at, finished_at FROM jobs "
            "WHERE id = %s AND owner_id = %s",
            (uuid.UUID(job_id), uuid.UUID(owner_id)),
        ).fetchone()
        if row is None:
            return None
        return {
            "job_id": str(row["id"]),
            "status": row["status"],
            "stage": row["stage"],
            "total": row["total"],
            "processed": row["processed"],
            "error": row["error"],
            "created_at": _iso(row["created_at"]),
            "started_at": _iso(row["started_at"]),
            "finished_at": _iso(row["finished_at"]),
        }

    def claim_next_job(self) -> dict | None:
        """Atomically claim the oldest pending job and mark it running. Uses
        ``FOR UPDATE SKIP LOCKED`` so multiple workers never claim the same job.
        Returns ``{"id", "owner_id"}`` or None when the queue is empty. Worker-only
        (scoping to the owner happens when the worker processes the job)."""
        from psycopg.rows import dict_row

        with self._conn.transaction():
            cur = self._conn.cursor(row_factory=dict_row)
            row = cur.execute(
                "SELECT id, owner_id FROM jobs WHERE status = 'pending' "
                "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE jobs SET status = 'running', started_at = now() WHERE id = %s",
                (row["id"],),
            )
            return {"id": str(row["id"]), "owner_id": str(row["owner_id"])}

    def update_job(self, job_id: str, *, stage: str | None = None,
                   processed: int | None = None, total: int | None = None) -> None:
        """Update a running job's stage/progress counters."""
        sets, params = [], []
        if stage is not None:
            sets.append("stage = %s")
            params.append(stage)
        if processed is not None:
            sets.append("processed = %s")
            params.append(processed)
        if total is not None:
            sets.append("total = %s")
            params.append(total)
        if not sets:
            return
        params.append(uuid.UUID(job_id))
        with self._conn.transaction():
            self._conn.execute(
                f"UPDATE jobs SET {', '.join(sets)} WHERE id = %s", params
            )

    def finish_job(self, job_id: str, status: str, error: str | None = None) -> None:
        """Mark a job done/error with a finish timestamp. ``error`` must be a SAFE
        message (no raw data/PII)."""
        with self._conn.transaction():
            self._conn.execute(
                "UPDATE jobs SET status = %s, error = %s, finished_at = now() "
                "WHERE id = %s",
                (status, error, uuid.UUID(job_id)),
            )
