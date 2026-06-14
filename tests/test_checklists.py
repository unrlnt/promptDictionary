"""Offline unit tests for checklist aggregation — pure functions, no DB, no network."""
from __future__ import annotations

from promptdict.checklists import RefinementRecord, build_checklist_rows

# Synthetic refinements. "format" is forgotten on three early conversations (one of
# them noise / no cluster) and then specified up front on three late ones — it should
# graduate. "audience" appears once. Clusters A and B keep their own rows.
RECORDS = [
    # --- early: format forgotten (in_first_prompt=False) ---
    RefinementRecord("format", False, "A", "c1", "2026-01-01", "make a table"),
    RefinementRecord("format", False, None, "c2", "2026-01-02", "make a table"),   # noise
    RefinementRecord("format", False, "A", "c3", "2026-01-03", "as a list"),
    RefinementRecord("format", False, "B", "c4", "2026-01-04", "as a table"),
    RefinementRecord("audience", False, "A", "c1", "2026-01-05", "target audience"),
    # --- late: format specified up front (in_first_prompt=True) ---
    RefinementRecord("format", True, "A", "c5", "2026-06-01", None),
    RefinementRecord("format", True, "A", "c6", "2026-06-02", None),
    RefinementRecord("format", True, "B", "c7", "2026-06-03", None),
    RefinementRecord("format", True, None, "c8", "2026-06-04", None),
]


def _row(rows, scope, kind, cluster_id=None):
    for r in rows:
        if r.scope == scope and r.kind == kind and r.cluster_id == cluster_id:
            return r
    return None


def test_global_includes_noise_and_ranks_by_distinct_conversations():
    rows = build_checklist_rows(RECORDS)

    g_format = _row(rows, "global", "format")
    g_audience = _row(rows, "global", "audience")

    # format forgotten on c1, c2 (noise), c3, c4 -> distinct conversation_count = 4,
    # proving the noise conversation (c2) is counted in global.
    assert g_format.conversation_count == 4
    assert g_format.total_count == 4
    assert g_audience.conversation_count == 1

    # Ranked by distinct-conversation count: format (4) outranks audience (1).
    assert g_format.rank == 1
    assert g_audience.rank == 2

    # Representative note carried through.
    assert "make a table" in g_format.sample_notes


def test_per_cluster_excludes_other_clusters_and_noise():
    rows = build_checklist_rows(RECORDS)

    a_format = _row(rows, "cluster", "format", "A")
    b_format = _row(rows, "cluster", "format", "B")

    # Cluster A's format forgotten only on c1, c3 — excludes B's c4 and noise c2.
    assert a_format.conversation_count == 2
    assert b_format.conversation_count == 1

    # No cluster-scoped rows for the noise conversation.
    cluster_ids = {r.cluster_id for r in rows if r.scope == "cluster"}
    assert cluster_ids == {"A", "B"}
    assert None not in cluster_ids


def test_graduating_kind_is_tagged_graduated():
    rows = build_checklist_rows(RECORDS)

    g_format = _row(rows, "global", "format")
    g_audience = _row(rows, "global", "audience")

    # format: early upfront-share 0/4, late 4/4 -> rose and high -> graduated.
    assert g_format.graduation == "graduated"
    # audience: too little data in both halves -> left unclassified.
    assert g_audience.graduation is None
    # graduation tags are global-only.
    assert all(r.graduation is None for r in rows if r.scope == "cluster")


def test_empty_records_produce_no_rows():
    assert build_checklist_rows([]) == []
