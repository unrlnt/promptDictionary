"""Source adapters — seam #2.

Each export tool gets one adapter that turns its raw JSON into normalized
``Conversation`` objects. Adding a tool = write an adapter + register it; nothing
downstream changes because everything reads the normalized schema (`models.py`).

Two adapters ship:

  * ``ClaudeAdapter``  — Claude's export is *flat*: a list of conversations, each
                         with a ``chat_messages`` list in order.
  * ``ChatGPTAdapter`` — ChatGPT's export is a *tree*: each conversation has a
                         ``mapping`` of nodes; the real turn order is recovered by
                         walking parent -> children from the root.

``load_conversations(path)`` reads the file, picks the first adapter that matches
its shape, and returns ``(source, conversations)``.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod

from .models import Conversation, Message


class SourceAdapter(ABC):
    source: str

    @abstractmethod
    def matches(self, data: object) -> bool:
        """True if this adapter recognizes the export's shape."""

    @abstractmethod
    def parse(self, data: object) -> list[Conversation]:
        """Turn raw export data into normalized conversations."""


def _text_from_content(content: object) -> str:
    """Best-effort extraction of plain text from a message ``content`` field that
    may be a string, a list of parts, or a ChatGPT ``{parts: [...]}`` object."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        parts = content.get("parts")
        if isinstance(parts, list):
            return "\n".join(str(p) for p in parts if isinstance(p, (str, int, float)))
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                out.append(part["text"])
        return "\n".join(out)
    return ""


class ClaudeAdapter(SourceAdapter):
    source = "claude"

    def matches(self, data: object) -> bool:
        return (
            isinstance(data, list)
            and bool(data)
            and isinstance(data[0], dict)
            and "chat_messages" in data[0]
        )

    def parse(self, data: object) -> list[Conversation]:
        assert isinstance(data, list)
        conversations: list[Conversation] = []
        for conv in data:
            raw_msgs = conv.get("chat_messages") or []
            messages: list[Message] = []
            for i, m in enumerate(raw_msgs):
                sender = m.get("sender") or m.get("role") or "user"
                role = "user" if sender in ("human", "user") else "assistant"
                text = m.get("text")
                if not text:
                    text = _text_from_content(m.get("content"))
                messages.append(
                    Message(role=role, text=text or "", idx=i,
                            created_at=m.get("created_at"))
                )
            conversations.append(
                Conversation(
                    source=self.source,
                    external_id=str(conv.get("uuid") or conv.get("id") or ""),
                    title=conv.get("name") or conv.get("title"),
                    created_at=conv.get("created_at"),
                    updated_at=conv.get("updated_at"),
                    messages=messages,
                )
            )
        return conversations


class ChatGPTAdapter(SourceAdapter):
    source = "chatgpt"

    def matches(self, data: object) -> bool:
        return (
            isinstance(data, list)
            and bool(data)
            and isinstance(data[0], dict)
            and "mapping" in data[0]
        )

    def parse(self, data: object) -> list[Conversation]:
        assert isinstance(data, list)
        conversations: list[Conversation] = []
        for conv in data:
            mapping = conv.get("mapping") or {}
            ordered = self._walk(mapping)
            messages: list[Message] = []
            for node in ordered:
                msg = node.get("message")
                if not msg:
                    continue
                role = (msg.get("author") or {}).get("role", "user")
                if role not in ("user", "assistant", "system"):
                    continue
                text = _text_from_content(msg.get("content"))
                if not text.strip():
                    continue
                messages.append(
                    Message(role=role, text=text, idx=len(messages),
                            created_at=_fmt_time(msg.get("create_time")))
                )
            conversations.append(
                Conversation(
                    source=self.source,
                    external_id=str(conv.get("conversation_id") or conv.get("id") or ""),
                    title=conv.get("title"),
                    created_at=_fmt_time(conv.get("create_time")),
                    updated_at=_fmt_time(conv.get("update_time")),
                    messages=messages,
                )
            )
        return conversations

    @staticmethod
    def _walk(mapping: dict) -> list[dict]:
        """Recover turn order from the node tree: start at the root (no parent),
        then descend through children depth-first preserving order."""
        roots = [n for n in mapping.values() if not n.get("parent")]
        ordered: list[dict] = []
        seen: set[str] = set()
        stack = list(reversed(roots))
        while stack:
            node = stack.pop()
            nid = node.get("id") or id(node)
            if nid in seen:
                continue
            seen.add(nid)
            ordered.append(node)
            children = [mapping[c] for c in node.get("children", []) if c in mapping]
            stack.extend(reversed(children))
        return ordered


def _fmt_time(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    return str(value)


# Registry: ordered list of adapters tried against an export's shape.
ADAPTERS: list[SourceAdapter] = [ClaudeAdapter(), ChatGPTAdapter()]


def register(adapter: SourceAdapter) -> None:
    """Register a new source adapter (a new export tool = one call here)."""
    ADAPTERS.append(adapter)


def load_conversations(path: str) -> tuple[str, list[Conversation]]:
    """Load an export file and normalize it. Returns ``(source, conversations)``.

    Raises ``ValueError`` if no registered adapter recognizes the file's shape.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    for adapter in ADAPTERS:
        if adapter.matches(data):
            return adapter.source, adapter.parse(data)
    raise ValueError(
        f"No adapter recognized the export at {path!r}. "
        f"Registered sources: {[a.source for a in ADAPTERS]}."
    )
