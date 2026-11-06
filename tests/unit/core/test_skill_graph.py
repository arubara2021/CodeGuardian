"""Tests for the deterministic skill graph builder.

Covers the four properties the builder is required to hold: edge
taxonomy from authored frontmatter, determinism of edge identifiers,
missing-target warnings, and semantic edge threshold behaviour.
"""

from __future__ import annotations

import math

from codeguardian.core.skills.graph import (
    CAPABILITY_PREFIX,
    build_edges,
    capability_node_id,
    detect_cycles,
)
from codeguardian.core.skills.types import EdgeKind, compute_edge_id


def _fm(name: str, **fields: object) -> dict:
    """Return a minimal frontmatter dictionary for a test skill."""
    base: dict = {
        "name": name,
        "description": f"description for {name}",
        "trigger": "when needed",
        "skill_type": "refactor_pattern",
        "language": "python",
        "state": "active",
    }
    base.update(fields)
    return base


def _unit_vector(index: int, dimension: int = 384) -> list[float]:
    """Return a unit vector with a one at the given index."""
    vector = [0.0] * dimension
    if 0 <= index < dimension:
        vector[index] = 1.0
    return vector


class TestCapabilityNode:
    def test_prefix_constant(self) -> None:
        assert CAPABILITY_PREFIX == "cap:"

    def test_node_id_format(self) -> None:
        assert capability_node_id("type-hints") == "cap:type-hints"


class TestAuthoredEdges:
    def test_empty_input_returns_empty(self) -> None:
        edges, warnings = build_edges({})
        assert edges == []
        assert warnings == []

    def test_prerequisite_edge_points_from_prereq_to_dependent(self) -> None:
        frontmatters = {
            "a": _fm("a"),
            "b": _fm("b", prerequisites=["a"]),
        }
        edges, warnings = build_edges(frontmatters)
        assert warnings == []
        prereq = [e for e in edges if e.kind == EdgeKind.PREREQUISITE]
        assert len(prereq) == 1
        assert prereq[0].src == "a"
        assert prereq[0].dst == "b"
        assert prereq[0].provenance == "frontmatter:prerequisites"

    def test_provides_creates_capability_target(self) -> None:
        frontmatters = {"a": _fm("a", provides=["type-hints"])}
        edges, _ = build_edges(frontmatters)
        provides = [e for e in edges if e.kind == EdgeKind.PROVIDES]
        assert len(provides) == 1
        assert provides[0].src == "a"
        assert provides[0].dst == "cap:type-hints"

    def test_consumes_creates_capability_target(self) -> None:
        frontmatters = {"a": _fm("a", consumes=["type-hints"])}
        edges, _ = build_edges(frontmatters)
        consumes = [e for e in edges if e.kind == EdgeKind.CONSUMES]
        assert len(consumes) == 1
        assert consumes[0].dst == "cap:type-hints"

    def test_supersedes_creates_edge_between_skills(self) -> None:
        frontmatters = {
            "old": _fm("old"),
            "new": _fm("new", supersedes=["old"]),
        }
        edges, warnings = build_edges(frontmatters)
        assert warnings == []
        supersedes = [e for e in edges if e.kind == EdgeKind.SUPERSEDES]
        assert len(supersedes) == 1
        assert supersedes[0].src == "new"
        assert supersedes[0].dst == "old"

    def test_missing_prerequisite_warns_and_skips(self) -> None:
        frontmatters = {"a": _fm("a", prerequisites=["does-not-exist"])}
        edges, warnings = build_edges(frontmatters)
        assert edges == []
        assert len(warnings) == 1
        assert "does-not-exist" in warnings[0]

    def test_missing_supersedes_warns_and_skips(self) -> None:
        frontmatters = {"a": _fm("a", supersedes=["ghost"])}
        edges, warnings = build_edges(frontmatters)
        assert edges == []
        assert any("ghost" in w for w in warnings)

    def test_non_list_field_ignored_with_warning(self) -> None:
        frontmatters = {"a": _fm("a", prerequisites="not-a-list")}
        edges, warnings = build_edges(frontmatters)
        assert edges == []
        assert any("not a list" in w for w in warnings)

    def test_empty_string_in_list_ignored(self) -> None:
        frontmatters = {
            "a": _fm("a"),
            "b": _fm("b", prerequisites=["a", ""]),
        }
        edges, _ = build_edges(frontmatters)
        prereq = [e for e in edges if e.kind == EdgeKind.PREREQUISITE]
        assert len(prereq) == 1


class TestDeterminism:
    def test_identical_inputs_produce_identical_edges(self) -> None:
        frontmatters = {
            "a": _fm("a"),
            "b": _fm("b", prerequisites=["a"], provides=["x"]),
            "c": _fm("c", prerequisites=["b"], consumes=["x"]),
        }
        edges1, warnings1 = build_edges(frontmatters)
        edges2, warnings2 = build_edges(frontmatters)
        assert warnings1 == warnings2
        ids1 = sorted(e.edge_id for e in edges1)
        ids2 = sorted(e.edge_id for e in edges2)
        assert ids1 == ids2

    def test_edge_id_matches_compute_edge_id(self) -> None:
        frontmatters = {
            "a": _fm("a"),
            "b": _fm("b", prerequisites=["a"]),
        }
        edges, _ = build_edges(frontmatters)
        prereq = next(e for e in edges if e.kind == EdgeKind.PREREQUISITE)
        expected = compute_edge_id("a", "b", EdgeKind.PREREQUISITE)
        assert prereq.edge_id == expected


class TestSemanticEdges:
    def test_no_embeddings_means_no_semantic_edges(self) -> None:
        frontmatters = {"a": _fm("a"), "b": _fm("b")}
        edges, _ = build_edges(frontmatters, None)
        assert not any(e.kind == EdgeKind.SEMANTIC for e in edges)

    def test_similar_embeddings_produce_bidirectional_edges(self) -> None:
        frontmatters = {"a": _fm("a"), "b": _fm("b")}
        embeddings = {"a": _unit_vector(0), "b": _unit_vector(0)}
        edges, _ = build_edges(frontmatters, embeddings, semantic_threshold=0.85)
        semantic = [e for e in edges if e.kind == EdgeKind.SEMANTIC]
        assert len(semantic) == 2
        pairs = {(e.src, e.dst) for e in semantic}
        assert ("a", "b") in pairs
        assert ("b", "a") in pairs

    def test_dissimilar_embeddings_produce_no_edges(self) -> None:
        frontmatters = {"a": _fm("a"), "b": _fm("b")}
        embeddings = {"a": _unit_vector(0), "b": _unit_vector(1)}
        edges, _ = build_edges(frontmatters, embeddings, semantic_threshold=0.5)
        assert not any(e.kind == EdgeKind.SEMANTIC for e in edges)

    def test_threshold_out_of_range_disables_semantic(self) -> None:
        frontmatters = {"a": _fm("a"), "b": _fm("b")}
        embeddings = {"a": _unit_vector(0), "b": _unit_vector(0)}
        edges, _ = build_edges(frontmatters, embeddings, semantic_threshold=1.5)
        assert not any(e.kind == EdgeKind.SEMANTIC for e in edges)

    def test_wrong_dimension_embeddings_yield_no_edge(self) -> None:
        frontmatters = {"a": _fm("a"), "b": _fm("b")}
        embeddings = {"a": [1.0, 0.0], "b": [0.0, 1.0]}
        edges, _ = build_edges(frontmatters, embeddings, semantic_threshold=0.5)
        assert not any(e.kind == EdgeKind.SEMANTIC for e in edges)

    def test_weight_equals_cosine_similarity(self) -> None:
        frontmatters = {"a": _fm("a"), "b": _fm("b")}
        embeddings = {"a": _unit_vector(0), "b": _unit_vector(0)}
        edges, _ = build_edges(frontmatters, embeddings, semantic_threshold=0.5)
        semantic = next(e for e in edges if e.kind == EdgeKind.SEMANTIC)
        assert math.isclose(semantic.weight, 1.0, abs_tol=1e-6)


class TestCycleDetection:
    def test_acyclic_graph_returns_empty(self) -> None:
        frontmatters = {
            "a": _fm("a"),
            "b": _fm("b", prerequisites=["a"]),
            "c": _fm("c", prerequisites=["b"]),
        }
        edges, _ = build_edges(frontmatters)
        assert detect_cycles(edges) == ()

    def test_two_node_cycle_detected(self) -> None:
        frontmatters = {
            "a": _fm("a", prerequisites=["b"]),
            "b": _fm("b", prerequisites=["a"]),
        }
        edges, warnings = build_edges(frontmatters)
        cycles = detect_cycles(edges)
        assert len(cycles) >= 1
        assert any("cycle" in w for w in warnings)

    def test_self_cycle_detected(self) -> None:
        frontmatters = {"a": _fm("a", prerequisites=["a"])}
        edges, warnings = build_edges(frontmatters)
        cycles = detect_cycles(edges)
        assert len(cycles) >= 1
        assert any("cycle" in w for w in warnings)

    def test_non_prerequisite_cycles_ignored(self) -> None:
        frontmatters = {
            "a": _fm("a", provides=["x"]),
            "b": _fm("b", provides=["x"]),
        }
        edges, _ = build_edges(frontmatters)
        assert detect_cycles(edges) == ()
