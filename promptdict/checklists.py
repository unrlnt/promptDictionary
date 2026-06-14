"""Aggregate refinements into checklists — pure aggregation over existing rows.

No LLM, no provider, no gateway. For one owner this rolls their structural
refinement rows up into:

  * a GLOBAL checklist (over ALL forgotten refinements, including those on noise
    conversations with no cluster), and
  * a PER-CLUSTER checklist (over each cluster's forgotten refinements),

each ranked by how many distinct conversations forgot a given kind. It also tags a
GLOBAL graduation trend per kind (is the user learning to state this up front?).

The aggregation core is plain functions over ``RefinementRecord`` values, so it is
fully testable without a database. ``build_checklists`` is the thin store-bound
wrapper: read rows, aggregate, write rows.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .store import PostgresStore

# Graduation thresholds.
GRAD_MIN_PER_HALF = 3      # need this many occurrences in BOTH halves to classify
GRAD_RISE_MARGIN = 0.2     # absolute rise in upfront-share that counts as "clear"
GRAD_HIGH_SHARE = 0.5      # upfront-share in the later half that counts as "high"

MAX_SAMPLE_NOTES = 3


@dataclass(frozen=True)
class RefinementRecord:
    """One refinement row, with the fields aggregation needs."""

    kind: str
    in_first_prompt: bool
    cluster_id: str | None
    conversation_id: str
    created_at: str | None       # the conversation's created_at (ISO-8601)
    note: str | None = None


@dataclass
class ChecklistRow:
    scope: str                   # 'global' | 'cluster'
    cluster_id: str | None
    kind: str
    conversation_count: int
    total_count: int
    rank: int
    sample_notes: list[str] = field(default_factory=list)
    graduation: str | None = None

    def as_tuple(self) -> tuple:
        return (self.scope, self.cluster_id, self.kind, self.conversation_count,
                self.total_count, self.rank, self.sample_notes, self.graduation)


@dataclass
class ChecklistBuildResult:
    global_kinds: int
    cluster_rows: int
    clusters: int


def _sample_notes(items: list[RefinementRecord], n: int = MAX_SAMPLE_NOTES) -> list[str]:
    """Up to ``n`` representative generalized notes — most common first."""
    counts = Counter(i.note for i in items if i.note)
    return [note for note, _ in counts.most_common(n)]


def _aggregate(records: list[RefinementRecord], scope: str,
               cluster_id: str | None) -> list[ChecklistRow]:
    """Per-kind counts over a set of forgotten records, ranked by distinct
    conversation_count desc, then total_count desc, then kind for stability."""
    by_kind: dict[str, list[RefinementRecord]] = defaultdict(list)
    for r in records:
        by_kind[r.kind].append(r)

    rows = [
        ChecklistRow(
            scope=scope,
            cluster_id=cluster_id,
            kind=kind,
            conversation_count=len({i.conversation_id for i in items}),
            total_count=len(items),
            rank=0,
            sample_notes=_sample_notes(items),
        )
        for kind, items in by_kind.items()
    ]
    rows.sort(key=lambda x: (-x.conversation_count, -x.total_count, x.kind))
    for i, row in enumerate(rows, start=1):
        row.rank = i
    return rows


def compute_graduation(records: list[RefinementRecord]) -> dict[str, str]:
    """Per kind, compare the upfront-share (in_first_prompt=true over true+false) in
    the earlier vs later half of the owner's refinements, ordered by conversation
    date. Only kinds with enough data in BOTH halves are classified."""
    timed = sorted((r for r in records if r.created_at), key=lambda r: r.created_at)
    if len(timed) < 2 * GRAD_MIN_PER_HALF:
        # Still try per-kind below; the per-half minimums are the real gate.
        pass
    mid = len(timed) // 2
    early, late = timed[:mid], timed[mid:]

    tags: dict[str, str] = {}
    for kind in {r.kind for r in timed}:
        e = [r for r in early if r.kind == kind]
        l = [r for r in late if r.kind == kind]
        if len(e) < GRAD_MIN_PER_HALF or len(l) < GRAD_MIN_PER_HALF:
            continue
        e_share = sum(1 for r in e if r.in_first_prompt) / len(e)
        l_share = sum(1 for r in l if r.in_first_prompt) / len(l)
        rose = (l_share - e_share) >= GRAD_RISE_MARGIN
        if rose and l_share >= GRAD_HIGH_SHARE:
            tags[kind] = "graduated"
        elif rose:
            tags[kind] = "improving"
        else:
            tags[kind] = "persistent"
    return tags


def build_checklist_rows(records: list[RefinementRecord]) -> list[ChecklistRow]:
    """Pure core: produce all checklist rows (global + per-cluster) from refinement
    records. Global includes noise (cluster_id is None) conversations."""
    forgotten = [r for r in records if not r.in_first_prompt]

    # GLOBAL — over all forgotten, including noise.
    rows = _aggregate(forgotten, scope="global", cluster_id=None)
    grad = compute_graduation(records)  # uses all rows (true + false)
    for row in rows:
        row.graduation = grad.get(row.kind)

    # PER-CLUSTER — each cluster's forgotten only; skip clusters with none.
    cluster_ids = sorted({r.cluster_id for r in forgotten if r.cluster_id is not None})
    for cid in cluster_ids:
        members = [r for r in forgotten if r.cluster_id == cid]
        rows.extend(_aggregate(members, scope="cluster", cluster_id=cid))

    return rows


def build_checklists(store: PostgresStore, owner_id: str) -> ChecklistBuildResult:
    """Read the owner's refinements, aggregate, and replace their checklists
    (idempotent, owner-scoped). No egress."""
    records = [
        RefinementRecord(
            kind=r["kind"],
            in_first_prompt=r["in_first_prompt"],
            cluster_id=r["cluster_id"],
            conversation_id=r["conversation_id"],
            created_at=r["created_at"],
            note=r["note"],
        )
        for r in store.iter_refinements(owner_id)
    ]
    rows = build_checklist_rows(records)
    store.replace_checklists(owner_id, [row.as_tuple() for row in rows])

    n_global = sum(1 for r in rows if r.scope == "global")
    cluster_rows = [r for r in rows if r.scope == "cluster"]
    return ChecklistBuildResult(
        global_kinds=n_global,
        cluster_rows=len(cluster_rows),
        clusters=len({r.cluster_id for r in cluster_rows}),
    )
