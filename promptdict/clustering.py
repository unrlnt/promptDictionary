"""Cluster a user's conversations into task types.

Reads the owner's embedded conversations, groups the 1024-dim vectors with
HDBSCAN over cosine distance (vectors are L2-normalized so Euclidean distance
ranks identically to cosine), and writes the result back: one row per cluster in
``clusters`` (centroid = mean vector; ``label`` left NULL — naming clusters is the
LLM's job in step 6), plus ``conversations.cluster_id`` for clustered members.

No LLM is involved here. Noise points (HDBSCAN label -1) keep ``cluster_id`` NULL.
The whole thing is owner-scoped and idempotent: re-running recomputes only this
owner's clusters via ``store.replace_clusters`` and never touches other owners.

``numpy`` and ``hdbscan`` are lazy-imported so the stdlib-only core stays
importable offline.
"""
from __future__ import annotations

from dataclasses import dataclass

from .store import PostgresStore

# Small default so clusters form on modest histories; tune up for big accounts.
DEFAULT_MIN_CLUSTER_SIZE = 2


@dataclass
class ClusterResult:
    n_conversations: int
    n_clusters: int
    n_noise: int


def cluster_conversations(store: PostgresStore, owner_id: str,
                          min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE) -> ClusterResult:
    """Cluster ``owner_id``'s embedded conversations and persist the assignments."""
    import numpy as np

    rows = store.iter_embedded(owner_id)
    ids = [r[0] for r in rows]

    # Too few to cluster: clear any stale clusters and report all as noise.
    if len(rows) < max(2, min_cluster_size):
        store.replace_clusters(owner_id, [])
        return ClusterResult(n_conversations=len(rows), n_clusters=0, n_noise=len(rows))

    vectors = np.asarray([np.asarray(r[1], dtype=float) for r in rows])
    normed = _l2_normalize(vectors)

    import hdbscan

    labels = hdbscan.HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=1,
        metric="euclidean",  # on L2-normalized vectors this ranks like cosine
    ).fit_predict(normed)

    # Build (centroid, member_ids) per real cluster; -1 is noise (left unassigned).
    clusters: list[tuple[list[float], list[str]]] = []
    for label in sorted({int(x) for x in labels if x != -1}):
        members = [i for i, x in enumerate(labels) if int(x) == label]
        centroid = vectors[members].mean(axis=0)  # mean of original (un-normalized) vectors
        clusters.append(([float(v) for v in centroid], [ids[i] for i in members]))

    store.replace_clusters(owner_id, clusters)

    n_noise = int((labels == -1).sum())
    return ClusterResult(n_conversations=len(rows), n_clusters=len(clusters), n_noise=n_noise)


def _l2_normalize(vectors):
    import numpy as np

    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0  # avoid divide-by-zero for any all-zero vector
    return vectors / norms
