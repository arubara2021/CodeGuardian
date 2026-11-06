"""Deterministic skill graph builder.

A pure function from frontmatter dictionaries and embedding vectors to
a list of typed edges. It reads no files, opens no database, and calls
no model. The only dependency beyond the standard library is the shared
type module.

Two kinds of edges are produced:

1. Authored edges from the ``prerequisites``, ``provides``,
   ``consumes``, and ``supersedes`` frontmatter fields. Every such
   edge has a provenance string identifying the field.
2. Semantic edges computed from cosine similarity between skill
   embeddings. A pair above the threshold produces a pair of edges,
   one in each direction, so retrieval through the graph is
   symmetric.

Cycle detection runs over the prerequisite edges only, because a
prerequisite cycle is a design error and the other edge kinds are
acyclic by construction. A detected cycle produces a warning but not
an exception. The PageRank traversal used by the retrieval pipeline
converges on graphs that contain cycles; the warning is enough for a
maintainer to know the graph is not a DAG.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping
from typing import Any

from .types import EdgeKind, SkillEdge, compute_edge_id

__all__ = [
    "CAPABILITY_PREFIX",
    "build_edges",
    "capability_node_id",
    "detect_cycles",
]

logger = logging.getLogger(__name__)


CAPABILITY_PREFIX = "cap:"


def capability_node_id(name: str) -> str:
    """Return the canonical node identifier for a capability label."""
    return f"{CAPABILITY_PREFIX}{name}"


def _make_edge(
    src: str,
    dst: str,
    kind: EdgeKind,
    *,
    weight: float = 1.0,
    provenance: str,
) -> SkillEdge:
    """Construct a ``SkillEdge`` with a deterministic identifier."""
    return SkillEdge(
        edge_id=compute_edge_id(src, dst, kind),
        src=src,
        dst=dst,
        kind=kind,
        weight=weight,
        provenance=provenance,
    )


def _frontmatter_string_list(
    frontmatter: Mapping[str, Any],
    key: str,
    name: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (values, warnings) for a frontmatter list field.

    A missing field returns an empty tuple and no warning. A field that
    is present but is not a list returns an empty tuple and one warning
    naming the skill and the offending field. A field that is a list is
    filtered to its non-empty string items; non-string items are
    dropped silently because their presence is malformed enough that
    the caller already has the skill in hand.
    """
    raw = frontmatter.get(key)
    if raw is None:
        return (), ()
    if not isinstance(raw, list):
        return (), (f"{name}: frontmatter field {key!r} is not a list; ignoring",)
    result: list[str] = []
    for item in raw:
        if isinstance(item, str) and item:
            result.append(item)
    return tuple(result), ()


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Return the cosine similarity of two equal-length vectors.

    Returns zero when either vector is empty, has zero magnitude, or the
    lengths differ. A wrong-length pair is a bug in the caller, not a
    reason to fail a rebuild.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def _semantic_edges(
    embeddings: Mapping[str, list[float]],
    threshold: float,
) -> list[SkillEdge]:
    """Return bidirectional semantic edges for pairs above the threshold."""
    edges: list[SkillEdge] = []
    names = sorted(embeddings.keys())
    for i, a in enumerate(names):
        vec_a = embeddings[a]
        for b in names[i + 1 :]:
            vec_b = embeddings[b]
            similarity = _cosine_similarity(vec_a, vec_b)
            if similarity >= threshold:
                edges.append(
                    _make_edge(
                        a,
                        b,
                        EdgeKind.SEMANTIC,
                        weight=similarity,
                        provenance=f"embedding:cosine>={threshold:.2f}",
                    )
                )
                edges.append(
                    _make_edge(
                        b,
                        a,
                        EdgeKind.SEMANTIC,
                        weight=similarity,
                        provenance=f"embedding:cosine>={threshold:.2f}",
                    )
                )
    return edges


def detect_cycles(edges: Iterable[SkillEdge]) -> tuple[tuple[str, ...], ...]:
    """Return every prerequisite cycle found in the given edges.

    A cycle is reported as a tuple of node identifiers where the first
    and last elements are the same node. The traversal is iterative to
    avoid the recursion limit on large graphs.

    Args:
        edges: The edges to examine. Only ``PREREQUISITE`` edges are
            considered.

    Returns:
        A tuple of cycles. Empty when the prerequisite graph is a DAG.
    """
    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        if edge.kind == EdgeKind.PREREQUISITE:
            adjacency.setdefault(edge.src, []).append(edge.dst)

    cycles: list[tuple[str, ...]] = []
    visited: set[str] = set()
    on_stack: set[str] = set()
    stack_path: list[str] = []

    def _dfs(node: str) -> None:
        if node in on_stack:
            try:
                index = stack_path.index(node)
            except ValueError:
                return
            cycles.append((*tuple(stack_path[index:]), node))
            return
        if node in visited:
            return
        visited.add(node)
        on_stack.add(node)
        stack_path.append(node)
        for neighbor in adjacency.get(node, ()):
            _dfs(neighbor)
        stack_path.pop()
        on_stack.discard(node)

    for start in list(adjacency.keys()):
        if start not in visited:
            _dfs(start)

    return tuple(cycles)


def build_edges(
    frontmatters: Mapping[str, Mapping[str, Any]],
    embeddings: Mapping[str, list[float]] | None = None,
    *,
    semantic_threshold: float = 0.85,
) -> tuple[list[SkillEdge], list[str]]:
    """Build the full edge list for a set of skills.

    Args:
        frontmatters: A mapping from skill name to its parsed
            frontmatter dictionary.
        embeddings: A mapping from skill name to its embedding vector.
            Skills without an embedding are excluded from semantic
            edges but still participate in authored edges.
        semantic_threshold: The cosine similarity above which two
            skills receive a semantic edge. Values outside ``(0, 1)``
            disable semantic edges entirely.

    Returns:
        A tuple of ``(edges, warnings)``. The edges list is
        deterministic: two calls with the same inputs produce the same
        edges in the same order. The warnings list names any anomaly
        detected during the build, including prerequisite cycles,
        prerequisite or supersedes targets that do not exist in the
        supplied set, and frontmatter fields whose type is wrong.
    """
    skill_names = set(frontmatters.keys())
    edges: list[SkillEdge] = []
    warnings: list[str] = []

    for name in sorted(frontmatters.keys()):
        frontmatter = frontmatters[name]

        prereqs, prereq_warnings = _frontmatter_string_list(frontmatter, "prerequisites", name)
        warnings.extend(prereq_warnings)
        for prereq in prereqs:
            if prereq not in skill_names:
                warnings.append(f"{name}: prerequisite {prereq!r} is not in the scanned set")
                continue
            edges.append(
                _make_edge(
                    prereq,
                    name,
                    EdgeKind.PREREQUISITE,
                    provenance="frontmatter:prerequisites",
                )
            )

        provides, provides_warnings = _frontmatter_string_list(frontmatter, "provides", name)
        warnings.extend(provides_warnings)
        for provided in provides:
            edges.append(
                _make_edge(
                    name,
                    capability_node_id(provided),
                    EdgeKind.PROVIDES,
                    provenance="frontmatter:provides",
                )
            )

        consumes, consumes_warnings = _frontmatter_string_list(frontmatter, "consumes", name)
        warnings.extend(consumes_warnings)
        for consumed in consumes:
            edges.append(
                _make_edge(
                    name,
                    capability_node_id(consumed),
                    EdgeKind.CONSUMES,
                    provenance="frontmatter:consumes",
                )
            )

        supersedes, supersedes_warnings = _frontmatter_string_list(frontmatter, "supersedes", name)
        warnings.extend(supersedes_warnings)
        for superseded in supersedes:
            if superseded not in skill_names:
                warnings.append(
                    f"{name}: supersedes {superseded!r}, which is not in the scanned set"
                )
                continue
            edges.append(
                _make_edge(
                    name,
                    superseded,
                    EdgeKind.SUPERSEDES,
                    provenance="frontmatter:supersedes",
                )
            )

    if embeddings and 0.0 < semantic_threshold < 1.0:
        filtered = {name: vec for name, vec in embeddings.items() if name in skill_names}
        edges.extend(_semantic_edges(filtered, semantic_threshold))

    for cycle in detect_cycles(edges):
        warnings.append("prerequisite cycle: " + " -> ".join(cycle))

    return edges, warnings
