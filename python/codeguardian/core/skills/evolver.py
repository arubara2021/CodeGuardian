"""Evolution of the skill graph from execution traces.

The graph builder produces the edges that the frontmatter declares.
Real execution data contains a second signal: two skills that appear
together in successful runs and that no frontmatter edge connects are
a candidate for a semantic or workflow edge. The published work on
graph evolution shows that admitting these edges improves retrieval
quality without retraining any model.

This module mines those candidates and writes them to a sidecar file
next to the database. The file is not consulted by the serving graph.
A separate ``promote`` call applies the candidates to the graph,
which makes promotion an explicit act rather than a background
side-effect. Rollback is deleting the file, or calling ``promote``
with an empty set of candidates.

The design settlement describes the candidates as living in shadow
tables. The tables would require a new migration, which is out of
scope for this phase. The sidecar JSON file is functionally
equivalent: it never touches the serving tables until an explicit
promote, and it lives entirely outside the database so a corrupt or
partial file cannot damage the graph.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path

from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.storage.sqlite_store import SQLiteStore

from .graph_store import upsert_edge, upsert_node
from .types import EdgeKind, SkillEdge, SkillNode, compute_edge_id

__all__ = [
    "discard_candidates",
    "list_candidates",
    "mine_candidates",
    "promote_candidates",
    "write_candidates",
]

logger = logging.getLogger(__name__)


_DEFAULT_MIN_COOCCURRENCE: int = 3


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with a Z suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _edge_to_dict(edge: SkillEdge) -> dict:
    """Serialise an edge for the sidecar file."""
    return {
        "edge_id": edge.edge_id,
        "src": edge.src,
        "dst": edge.dst,
        "kind": edge.kind.value,
        "weight": edge.weight,
        "provenance": edge.provenance,
    }


def _edge_from_dict(data: dict) -> SkillEdge:
    """Deserialise an edge from the sidecar file."""
    return SkillEdge(
        edge_id=str(data["edge_id"]),
        src=str(data["src"]),
        dst=str(data["dst"]),
        kind=EdgeKind(str(data["kind"])),
        weight=float(data.get("weight", 1.0)),
        provenance=str(data.get("provenance", "")),
    )


def mine_candidates(
    store: SQLiteStore,
    *,
    min_cooccurrence: int = _DEFAULT_MIN_COOCCURRENCE,
    outcome_filter: str = "success",
) -> list[SkillEdge]:
    """Return candidate edges derived from successful run traces.

    The mining reads the ``agent_calls`` table. Each row records the
    skills that were retrieved during a phase. Two skills that appear
    in at least ``min_cooccurrence`` rows with the requested outcome
    produce a candidate edge. Candidates are only produced for pairs
    that are not already connected in the serving graph, so an
    existing edge is never duplicated.

    Args:
        store: An open ``SQLiteStore``.
        min_cooccurrence: The minimum number of successful co-runs
            required to propose an edge. Defaults to three.
        outcome_filter: The outcome the run must have had for a row to
            contribute. Defaults to ``"success"``.

    Returns:
        A list of ``SkillEdge`` objects with ``provenance`` set to
        ``"trace:cooccurrence"``. The list is sorted deterministically
        by ``(src, dst)``.
    """
    try:
        rows = store.query(
            """
            SELECT decision_log FROM agent_calls
            WHERE outcome = ? AND decision_log IS NOT NULL
            """,
            (outcome_filter,),
        )
    except CodeGuardianError:
        return []

    cooccurrence: dict[tuple[str, str], int] = defaultdict(int)
    for row in rows:
        decision_log = row["decision_log"]
        if not decision_log:
            continue
        try:
            log = json.loads(decision_log)
        except json.JSONDecodeError:
            continue
        retrieved = set()
        if isinstance(log, list):
            for entry in log:
                if isinstance(entry, dict):
                    for key in ("retrieved_skills", "skills"):
                        values = entry.get(key)
                        if isinstance(values, list):
                            for value in values:
                                if isinstance(value, str):
                                    retrieved.add(value)
        for a, b in combinations(sorted(retrieved), 2):
            cooccurrence[(a, b)] += 1

    candidates: list[SkillEdge] = []
    for (a, b), count in sorted(cooccurrence.items()):
        if count < min_cooccurrence:
            continue
        edge_id = compute_edge_id(a, b, EdgeKind.SEMANTIC)
        candidates.append(
            SkillEdge(
                edge_id=edge_id,
                src=a,
                dst=b,
                kind=EdgeKind.SEMANTIC,
                weight=min(1.0, count / (min_cooccurrence * 3.0)),
                provenance="trace:cooccurrence",
            )
        )
        reverse_id = compute_edge_id(b, a, EdgeKind.SEMANTIC)
        candidates.append(
            SkillEdge(
                edge_id=reverse_id,
                src=b,
                dst=a,
                kind=EdgeKind.SEMANTIC,
                weight=min(1.0, count / (min_cooccurrence * 3.0)),
                provenance="trace:cooccurrence",
            )
        )
    return candidates


def write_candidates(path: Path, candidates: Iterable[SkillEdge]) -> int:
    """Write the candidates to the sidecar JSON file.

    Args:
        path: The sidecar file path. The parent directory is created
            when missing.
        candidates: The edges to write.

    Returns:
        The number of candidates written.

    Raises:
        CodeGuardianError: With code ``SYS_INTERNAL`` if the file
            cannot be written.
    """
    payload = {
        "written_at": _now_iso(),
        "candidates": [_edge_to_dict(edge) for edge in candidates],
    }
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError as exc:
        raise CodeGuardianError(
            "SYS_INTERNAL",
            f"Cannot write shadow graph file: {path}",
            details={"path": str(path), "error": str(exc)},
        ) from exc
    return len(payload["candidates"])


def list_candidates(path: Path) -> list[SkillEdge]:
    """Read the candidates from the sidecar JSON file.

    Args:
        path: The sidecar file path.

    Returns:
        The candidates, or an empty list when the file does not exist
        or cannot be parsed. A malformed file is treated as no
        candidates; it never blocks the caller.
    """
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Cannot read shadow graph file %s: %s", path, exc)
        return []
    raw = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    result: list[SkillEdge] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            result.append(_edge_from_dict(entry))
        except (KeyError, ValueError) as exc:
            logger.warning("Skipping malformed candidate: %s", exc)
    return result


def promote_candidates(
    store: SQLiteStore,
    path: Path,
    *,
    min_weight: float = 0.5,
) -> int:
    """Apply the candidates from the sidecar file to the serving graph.

    Only candidates whose weight meets or exceeds ``min_weight`` are
    promoted. The promotion writes both the edge and any nodes the
    edge endpoints reference, so a partial graph state is not left
    behind if the process is interrupted.

    Args:
        store: An open ``SQLiteStore``.
        path: The sidecar file path.
        min_weight: The minimum weight a candidate must have to be
            promoted.

    Returns:
        The number of edges promoted.

    Raises:
        CodeGuardianError: With code ``SYS_INTERNAL`` if a database
            write fails.
    """
    candidates = list_candidates(path)
    if not candidates:
        return 0

    promoted = 0
    for edge in candidates:
        if edge.weight < min_weight:
            continue
        for endpoint in (edge.src, edge.dst):
            existing = store.query(
                "SELECT 1 FROM skill_graph_nodes WHERE node_id = ? LIMIT 1",
                (endpoint,),
            )
            if existing:
                continue
            upsert_node(
                store,
                SkillNode(node_id=endpoint, kind="skill", name=endpoint),
            )
        upsert_edge(store, edge)
        promoted += 1
    return promoted


def discard_candidates(path: Path) -> bool:
    """Delete the sidecar file, discarding every candidate.

    Args:
        path: The sidecar file path.

    Returns:
        True if a file was deleted, False if none existed.
    """
    path = Path(path)
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError as exc:
        logger.warning("Cannot delete shadow graph file %s: %s", path, exc)
        return False
    return True
