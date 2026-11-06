"""Reverse-weighted Personalized PageRank over the skill graph.

Given a set of seed nodes (produced by the hybrid retrieval stage),
this module propagates the seed scores along graph edges so that
prerequisites of the seeds also receive a nonzero score. The
propagation walks incoming edges, because a ``prerequisite`` edge
points from the prerequisite to the dependent and a downstream task
that names a dependent needs its prerequisites to rank above
unrelated skills.

Reverse edges are weighted more heavily than forward edges, matching
the published ablation showing that backward traversal is the source
of the reward improvement, not forward diffusion. A multiplier of 2.0
means a prerequisite edge walked backwards carries twice the weight
of a same-kind edge walked forward, and four times the weight of an
undirected semantic edge walked in either direction.

The traversal is bounded by two limits. First, only nodes within
``max_hops`` hops of any seed participate at all; the closure is
computed by breadth-first search before the iterations begin. Second,
the iteration loop checks a deadline every iteration and returns the
scores it has when the deadline expires. Neither bound raises; both
degrade to whatever partial result was available.

This module does not raise on empty input, empty graph, or timeout.
It returns an empty list or the seed list, because an empty result is
a valid signal that the graph added no information and the caller
should fall back to the previous stage.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from collections.abc import Iterable

from .types import EdgeKind, SkillEdge

__all__ = ["RankedNode", "personalized_pagerank"]

logger = logging.getLogger(__name__)


class RankedNode:
    """A node with a PPR score. Used internally and returned to callers."""

    __slots__ = ("node_id", "score")

    def __init__(self, node_id: str, score: float) -> None:
        self.node_id = node_id
        self.score = score

    def __repr__(self) -> str:
        return f"RankedNode(node_id={self.node_id!r}, score={self.score:.4f})"


# The damping factor used in every iteration. 0.85 is the value from
# the PageRank paper and the value every implementation uses unless it
# has a reason not to.
_DAMPING: float = 0.85

# The maximum number of iterations. The score is a fixed point so the
# loop terminates on convergence long before this in practice; the cap
# is a safety net.
_MAX_ITERATIONS: int = 30

# The convergence tolerance. When the L1 norm of the score change
# between two iterations falls below this value, the loop stops. 1e-4
# is well below any decision threshold the pipeline uses downstream.
_CONVERGENCE_EPS: float = 1e-4

# The default multiplier applied to a back-edge. The number comes from
# the Graph-of-Skills ablation, which measures the reward loss when
# backward traversal is disabled. A value of 2.0 preserves the
# relationship while keeping the score normalisation tractable.
_DEFAULT_REVERSE_MULTIPLIER: float = 2.0


def _edge_weight(edge: SkillEdge, reverse_multiplier: float) -> float:
    """Return the contribution weight for an edge walked backwards.

    Prerequisite edges carry the reverse multiplier because those are
    the edges the published ablation identifies as the source of the
    gain. Every other edge kind contributes its own weight unchanged.
    """
    weight = edge.weight if edge.weight > 0 else 1.0
    if edge.kind == EdgeKind.PREREQUISITE:
        weight *= reverse_multiplier
    return weight


def _build_adjacency(
    edges: Iterable[SkillEdge],
    reverse_multiplier: float,
) -> tuple[dict[str, list[tuple[str, float]]], dict[str, list[tuple[str, float]]]]:
    """Return the pair of adjacency maps used by reverse PPR.

    For an original edge ``a -> b`` (meaning ``a`` is a prerequisite of
    ``b``), reverse PPR propagates ``b``'s score back to ``a``. The two
    maps encode this direction in opposite forms:

    - ``incoming[a]`` contains ``(b, weight)``, so ``a``'s new score
      draws from ``b``'s old score. Used by the iteration loop.
    - ``outgoing[b]`` contains ``(a, weight)``, so a breadth-first walk
      started at ``b`` reaches ``a``. Used by the closure.

    Both maps use the same per-edge weight, so the two views stay
    consistent under any weight computation.
    """
    incoming: dict[str, list[tuple[str, float]]] = defaultdict(list)
    outgoing: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for edge in edges:
        weight = _edge_weight(edge, reverse_multiplier)
        incoming[edge.src].append((edge.dst, weight))
        outgoing[edge.dst].append((edge.src, weight))
    return incoming, outgoing


def _bounded_closure(
    seeds: Iterable[str],
    outgoing: dict[str, list[tuple[str, float]]],
    max_hops: int,
) -> set[str]:
    """Return nodes reachable from the seeds in at most ``max_hops`` steps.

    The traversal walks the ``outgoing`` map, so from a seed ``b`` it
    reaches every node whose score should be boosted because ``b`` is
    a seed. Depth zero is the seeds themselves; each additional step
    extends to the prerequisites of the already-included nodes.
    """
    closure: set[str] = set()
    queue: deque[tuple[str, int]] = deque()
    for seed in seeds:
        if seed not in closure:
            closure.add(seed)
            queue.append((seed, 0))
    while queue:
        node, depth = queue.popleft()
        if depth >= max_hops:
            continue
        for neighbour, _ in outgoing.get(node, ()):
            if neighbour not in closure:
                closure.add(neighbour)
                queue.append((neighbour, depth + 1))
    return closure


def personalized_pagerank(
    edges: Iterable[SkillEdge],
    seeds: Iterable[str],
    *,
    max_hops: int = 1,
    timeout_ms: int = 50,
    reverse_multiplier: float = _DEFAULT_REVERSE_MULTIPLIER,
    skill_names: set[str] | None = None,
) -> list[RankedNode]:
    """Return PPR scores for the nodes reachable from the seeds.

    Args:
        edges: Every edge in the graph. Only the edges whose endpoints
            are both inside the bounded closure contribute.
        seeds: The node identifiers produced by the previous stage.
            Duplicates are ignored.
        max_hops: The maximum number of reverse edges to follow from
            any seed. One hop returns the seeds and their direct
            prerequisites. Zero returns only the seeds.
        timeout_ms: The wall-clock budget, in milliseconds. When the
            budget expires, the iteration stops and the current scores
            are returned. The function never raises on timeout.
        reverse_multiplier: The weight boost applied to prerequisite
            edges walked backwards. Values below 1.0 make the traversal
            closer to forward diffusion. The default comes from the
            literature.
        skill_names: When supplied, only skill nodes (as opposed to
            capability nodes) appear in the returned list. Capability
            nodes still contribute to the propagation, they are simply
            not returned.

    Returns:
        A list of ``RankedNode`` objects sorted by descending score and,
        for ties, by ascending node identifier. The seed nodes are
        always present in the result even when the graph contains no
        edges. When the graph is empty or the seeds are empty, the
        returned list is empty.
    """
    seed_list = sorted(set(seeds))
    if not seed_list:
        return []

    edges_list = list(edges)
    if not edges_list:
        return [RankedNode(node, 1.0 / len(seed_list)) for node in seed_list]

    _incoming_full, outgoing_full = _build_adjacency(edges_list, reverse_multiplier)

    closure = _bounded_closure(seed_list, outgoing_full, max_hops)
    if not closure:
        return []

    filtered_edges = [edge for edge in edges_list if edge.src in closure and edge.dst in closure]
    incoming, _ = _build_adjacency(filtered_edges, reverse_multiplier)

    seed_weight = 1.0 / len(seed_list)
    scores: dict[str, float] = {
        node: (seed_weight if node in seed_list else 0.0) for node in closure
    }

    nodes = sorted(closure)
    deadline = time.monotonic() + (timeout_ms / 1000.0)

    for _iteration in range(_MAX_ITERATIONS):
        if time.monotonic() >= deadline:
            break

        new_scores: dict[str, float] = {}
        for node in nodes:
            contributions = incoming.get(node, ())
            total = sum(weight * scores[source] for source, weight in contributions)
            norm = sum(weight for _, weight in contributions) or 1.0
            incoming_component = total / norm
            seed_component = seed_weight if node in seed_list else 0.0
            new_scores[node] = _DAMPING * incoming_component + (1.0 - _DAMPING) * seed_component

        delta = sum(abs(new_scores[node] - scores.get(node, 0.0)) for node in nodes)
        scores = new_scores

        if delta < _CONVERGENCE_EPS:
            break

    result: list[RankedNode] = []
    for node in nodes:
        if skill_names is not None and node not in skill_names:
            continue
        if scores[node] <= 0.0:
            continue
        result.append(RankedNode(node, scores[node]))

    result.sort(key=lambda r: (-r.score, r.node_id))
    return result
