"""Refinement extraction + cluster labelling — through the gateway.

This is step 2's mining stage: for each conversation, ask the LLM what REQUIREMENTS
the user introduced in follow-ups (things they "forgot" to say up front) and which
dimensions the first message already specified. Results are stored structurally in
``refinements`` (kind + position + a generalized note) — never raw text.

The LLM is reached ONLY via ``gateway.extract`` (sanitized egress); no provider is
called directly. ``numpy``/SDKs are not needed here — this module is stdlib-only.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable

from .cloud import SanitizingGateway
from .store import PostgresStore

log = logging.getLogger(__name__)

# Optional per-stage progress hook: progress(processed, total).
Progress = Callable[[int, int], None]

# The 13 canonical refinement dimensions. Anything the LLM returns outside this set
# is dropped.
TAXONOMY = frozenset({
    "audience", "format", "length", "tone", "structure", "depth", "language",
    "sources", "examples", "scope", "constraints", "role", "context",
})

# Cap the user-turn text we send (sanitization happens at egress regardless).
DEFAULT_CHAR_LIMIT = 8000

_PROMPT_TEMPLATE = """You analyze how a user refined a request across a conversation, to learn what they
could have put in their first prompt. You're given the user's messages in order
(assistant replies omitted). Message 1 is the initial request; the rest are follow-ups.
Personal data is replaced with placeholders like [PERSON] — ignore them.

Identify REQUIREMENTS the user introduced in follow-ups that they could have stated in
the first prompt to get a better initial result — things they effectively "forgot." A
requirement is a constraint or specification about the desired output.

IGNORE follow-ups that are: natural continuations ("continue", "now do the next part"),
corrections of the assistant's mistakes ("that's wrong", "you misunderstood"), or brand-
new unrelated tasks.

Also note which requirement dimensions the FIRST message already specified.

Classify every requirement into exactly one of these dimensions:
- audience: who the output is for
- format: output medium (slides, table, email, list, JSON, code)
- length: size or amount (word count, number of items or slides, brevity)
- tone: register or style (formal, academic, persuasive, casual)
- structure: organization, sections, ordering, headings
- depth: level of detail or complexity (simplify, go deeper, beginner vs expert)
- language: output language or regional/terminology variant
- sources: citations, references, evidence, fact-checking
- examples: include examples, analogies, or sample data
- scope: what to include or exclude, focus, specific points to cover
- constraints: explicit rules, must-haves, or things to avoid
- role: persona or perspective the assistant should adopt
- context: background the user had to supply that was missing up front

Here are the user's messages, in order:
{user_turns}

Return ONLY valid JSON, no markdown, no commentary, exactly this shape:
{
  "forgotten": [{"kind": "<dimension>", "turn": <1-based index of the user message that introduced it>, "note": "<actionable prompt instruction the user should add, based on what they actually forgot, aim for max 12 words, imperative mood>"}],
  "specified_upfront": [{"kind": "<dimension>", "note": "<actionable prompt instruction the user should add, based on what they actually forgot, aim for max 12 words, imperative mood>"}]
}
Write each note as a specific, actionable prompt instruction in imperative mood, grounded in what the user actually forgot in this conversation (e.g. "Always require citations for every factual claim.", "Specify output as a numbered list."). Never copy sensitive content or personal data.
If nothing qualifies, return empty arrays."""


def build_extraction_prompt(user_turns: list[str]) -> str:
    """Render the extraction prompt with the user's messages numbered 1..N. Uses
    ``str.replace`` (not ``.format``) so the literal JSON braces stay intact."""
    numbered = "\n".join(f"{i}. {turn}" for i, turn in enumerate(user_turns, start=1))
    return _PROMPT_TEMPLATE.replace("{user_turns}", numbered)


def _strip_code_fences(text: str) -> str:
    t = text.strip()
    fence = re.match(r"^```[a-zA-Z0-9]*\s*(.*?)\s*```$", t, re.DOTALL)
    return fence.group(1).strip() if fence else t


def parse_extraction_response(text: str) -> tuple[list, list]:
    """Defensively parse the LLM response into ``(forgotten, specified_upfront)``
    lists. Strips code fences; falls back to the outermost ``{...}`` object. Raises
    ``ValueError`` if no JSON object can be recovered."""
    t = _strip_code_fences(text)
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object found in extraction response")
        data = json.loads(t[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("extraction response was not a JSON object")
    forgotten = data.get("forgotten") or []
    specified = data.get("specified_upfront") or []
    if not isinstance(forgotten, list) or not isinstance(specified, list):
        raise ValueError("forgotten/specified_upfront were not arrays")
    return forgotten, specified


def _note(item: dict) -> str | None:
    note = item.get("note")
    if not isinstance(note, str):
        return None
    return note.strip()[:200] or None


def refinement_rows(response_text: str, cluster_id: str | None) -> list[tuple]:
    """Turn a raw LLM response into refinement rows
    ``(kind, turn_index, in_first_prompt, note, cluster_id)``.

    Drops any kind not in TAXONOMY and any forgotten item with a non-integer turn.
    ``forgotten`` -> in_first_prompt=False, turn_index=item["turn"] (1-based, passed
    straight through). ``specified_upfront`` -> in_first_prompt=True, turn_index=1.
    """
    forgotten, specified = parse_extraction_response(response_text)
    rows: list[tuple] = []

    for item in forgotten:
        if not isinstance(item, dict) or item.get("kind") not in TAXONOMY:
            continue
        try:
            turn_index = int(item.get("turn"))
        except (TypeError, ValueError):
            continue
        rows.append((item["kind"], turn_index, False, _note(item), cluster_id))

    for item in specified:
        if not isinstance(item, dict) or item.get("kind") not in TAXONOMY:
            continue
        rows.append((item["kind"], 1, True, _note(item), cluster_id))

    return rows


def _capped_turns(user_turns: list[str], char_limit: int) -> list[str]:
    """Keep every turn (so 1-based indices stay valid) but bound total length by
    truncating each turn to share the budget."""
    if not user_turns:
        return []
    per_turn = max(200, char_limit // len(user_turns))
    return [turn[:per_turn] for turn in user_turns]


def extract_refinements(store: PostgresStore, gateway: SanitizingGateway,
                        owner_id: str, char_limit: int = DEFAULT_CHAR_LIMIT,
                        limit: int | None = None,
                        progress: Progress | None = None) -> int:
    """Extract refinements for the owner's not-yet-extracted conversations. Returns
    the count processed. Parse/LLM failures are logged and skipped (the conversation
    stays unextracted for a future retry); the batch never crashes. ``limit`` caps
    how many conversations are processed (None = all, unchanged). ``progress(done,
    total)`` is called as work proceeds, if given."""
    items = store.iter_unextracted(owner_id)
    total = len(items) if limit is None else min(limit, len(items))
    if progress:
        progress(0, total)
    processed = 0
    for conv, cluster_id in items:
        if limit is not None and processed >= limit:
            break
        user_turns = [m.text for m in conv.messages if m.role == "user" and m.text]
        prompt = build_extraction_prompt(_capped_turns(user_turns, char_limit))
        try:
            raw = gateway.extract(prompt)          # sanitized egress — never the provider
            rows = refinement_rows(raw, cluster_id)
        except Exception as exc:  # noqa: BLE001 — one bad conversation must not kill the batch
            log.warning("refinement extraction failed for %s: %s",
                        conv.conversation_id, exc)
            continue
        store.replace_refinements(owner_id, conv.conversation_id, rows)
        processed += 1
        if progress:
            progress(processed, total)
    return processed


_LABEL_PROMPT = """You are labelling a cluster of one user's AI chats that share a task type.
Below are the first messages from up to five example conversations. Personal data is
replaced with placeholders like [PERSON] — ignore them.

Give a short, human-readable label naming the shared task type: at most 5 words, plain
text only, no quotes, no trailing punctuation, no explanation.

Example conversations:
{samples}

Label:"""


def _clean_label(text: str) -> str:
    label = text.strip().splitlines()[0] if text.strip() else ""
    label = label.strip().strip('"').strip("'").strip().rstrip(".")
    return " ".join(label.split()[:5])


def label_clusters(store: PostgresStore, gateway: SanitizingGateway,
                   owner_id: str, sample_chars: int = 300,
                   limit: int | None = None,
                   progress: Progress | None = None) -> int:
    """Give each of the owner's unlabelled clusters a short task-type label via the
    gateway. Returns the count labelled. Idempotent: already-labelled clusters are
    skipped (they aren't returned by ``iter_clusters_unlabelled``). ``limit`` caps
    how many clusters are labelled (None = all, unchanged). ``progress(done, total)``
    is called as work proceeds, if given."""
    clusters = store.iter_clusters_unlabelled(owner_id)
    total = len(clusters) if limit is None else min(limit, len(clusters))
    if progress:
        progress(0, total)
    labelled = 0
    done = 0
    for cluster_id, samples in clusters:
        if limit is not None and labelled >= limit:
            break
        done += 1
        if not samples:
            if progress:
                progress(min(done, total), total)
            continue
        numbered = "\n".join(f"{i}. {s[:sample_chars]}"
                             for i, s in enumerate(samples, start=1))
        prompt = _LABEL_PROMPT.replace("{samples}", numbered)
        try:
            label = _clean_label(gateway.extract(prompt))  # sanitized egress
        except Exception as exc:  # noqa: BLE001
            log.warning("cluster labelling failed for %s: %s", cluster_id, exc)
            continue
        if label:
            store.set_cluster_label(owner_id, cluster_id, label)
            labelled += 1
        if progress:
            progress(min(done, total), total)
    return labelled
