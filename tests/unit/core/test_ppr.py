"""Tests for the reverse-weighted Personalized PageRank traversal.

Covers seed handling, hop bounding, direction correctness,
deterministic ordering, and graceful behaviour under timeout.
"""

from __future__ import annotations

from codeguardian.core.skills.ppr import RankedNode, personalized_pagerank
from codeguardian.core.skills.types import EdgeKind, SkillEdge, compute_edge_id


def _prereq_edge(src: str, dst: str, weight: float = 1.0) -> SkillEdge:
    """Return a prerequisite edge from src to dst."""
    return SkillEdge(
        edge_id=compute_edge_id(src, dst, EdgeKind.PREREQUISITE),
        src=src,
        dst=dst,
        kind=EdgeKind.PREREQUISITE,
        weight=weight,
    )


def _semantic_edge(src: str, dst: str, weight: float = 1.0) -> SkillEdge:
    """Return a semantic edge from src to dst."""
    return SkillEdge(
        edge_id=compute_edge_id(src, dst, EdgeKind.SEMANTIC),
        src=src,
        dst=dst,
        kind=EdgeKind.SEMANTIC,
        weight=weight,
    )


def _names(result: list[RankedNode]) -> list[str]:
    return [node.node_id for node in result]


class TestEmptyInputs:
    def test_empty_seeds_returns_empty(self) -> None:
        assert personalized_pagerank([], []) == []

    def test_empty_graph_returns_seeds_with_uniform_score(self) -> None:
        result = personalized_pagerank([], ["a", "b"])
        assert sorted(_names(result)) == ["a", "b"]
        scores = {node.node_id: node.score for node in result}
        assert scores["a"] == scores["b"]
        assert scores["a"] > 0.0

    def test_duplicate_seeds_are_deduplicated(self) -> None:
        result = personalized_pagerank([], ["a", "a", "a"])
        assert _names(result) == ["a"]


class TestDirection:
    def test_prerequisite_of_seed_receives_score(self) -> None:
        edges = [_prereq_edge("dep", "root")]
        result = personalized_pagerank(edges, ["root"], max_hops=1)
        names = _names(result)
        assert "root" in names
        assert "dep" in names

    def test_dependent_of_seed_does_not_receive_score(self) -> None:
        edges = [_prereq_edge("seed", "dependent")]
        result = personalized_pagerank(edges, ["seed"], max_hops=1)
        names = _names(result)
        assert "seed" in names
        assert "dependent" not in names

    def test_seed_scores_higher_than_prerequisite(self) -> None:
        edges = [_prereq_edge("dep", "root")]
        result = personalized_pagerank(edges, ["root"], max_hops=1)
        by_name = {node.node_id: node.score for node in result}
        assert by_name["root"] > by_name["dep"]


class TestHopBounding:
    def test_zero_hops_returns_only_seeds(self) -> None:
        edges = [_prereq_edge("dep", "root")]
        result = personalized_pagerank(edges, ["root"], max_hops=0)
        assert _names(result) == ["root"]

    def test_one_hop_includes_direct_prerequisites(self) -> None:
        edges = [_prereq_edge("dep", "root")]
        result = personalized_pagerank(edges, ["root"], max_hops=1)
        assert set(_names(result)) == {"dep", "root"}

    def test_one_hop_excludes_transitive_prerequisites(self) -> None:
        edges = [
            _prereq_edge("ancestor", "dep"),
            _prereq_edge("dep", "root"),
        ]
        result = personalized_pagerank(edges, ["root"], max_hops=1)
        names = set(_names(result))
        assert "root" in names
        assert "dep" in names
        assert "ancestor" not in names

    def test_two_hops_includes_transitive_prerequisites(self) -> None:
        edges = [
            _prereq_edge("ancestor", "dep"),
            _prereq_edge("dep", "root"),
        ]
        result = personalized_pagerank(edges, ["root"], max_hops=2)
        names = set(_names(result))
        assert {"root", "dep", "ancestor"} <= names


class TestSkillNameFilter:
    def test_capability_nodes_excluded_when_filter_supplied(self) -> None:
        edges = [
            SkillEdge(
                edge_id=compute_edge_id("skill", "cap:thing", EdgeKind.PROVIDES),
                src="skill",
                dst="cap:thing",
                kind=EdgeKind.PROVIDES,
                weight=1.0,
            )
        ]
        result = personalized_pagerank(
            edges,
            ["cap:thing"],
            max_hops=1,
            skill_names={"skill"},
        )
        names = _names(result)
        assert "skill" in names
        assert "cap:thing" not in names

    def test_no_filter_returns_all_nodes(self) -> None:
        edges = [
            SkillEdge(
                edge_id=compute_edge_id("skill", "cap:thing", EdgeKind.PROVIDES),
                src="skill",
                dst="cap:thing",
                kind=EdgeKind.PROVIDES,
                weight=1.0,
            )
        ]
        result = personalized_pagerank(edges, ["cap:thing"], max_hops=1)
        names = _names(result)
        assert "skill" in names
        assert "cap:thing" in names


class TestTimeout:
    def test_zero_timeout_returns_promptly(self) -> None:
        edges = [_prereq_edge(f"n{i}", f"n{i + 1}") for i in range(50)]
        result = personalized_pagerank(edges, ["n50"], max_hops=5, timeout_ms=0)
        assert isinstance(result, list)

    def test_zero_timeout_does_not_raise(self) -> None:
        edges = [_prereq_edge("a", "b")]
        result = personalized_pagerank(edges, ["b"], max_hops=2, timeout_ms=0)
        assert isinstance(result, list)


class TestDeterminism:
    def test_output_is_sorted_by_score_then_name(self) -> None:
        edges = [
            _prereq_edge("x", "root"),
            _prereq_edge("y", "root"),
        ]
        result = personalized_pagerank(edges, ["root"], max_hops=1)
        for i in range(len(result) - 1):
            current = result[i]
            following = result[i + 1]
            if current.score == following.score:
                assert current.node_id < following.node_id
            else:
                assert current.score >= following.score

    def test_repeated_calls_produce_identical_scores(self) -> None:
        edges = [_prereq_edge("dep", "root")]
        first = personalized_pagerank(edges, ["root"], max_hops=1)
        second = personalized_pagerank(edges, ["root"], max_hops=1)
        first_scores = {n.node_id: round(n.score, 6) for n in first}
        second_scores = {n.node_id: round(n.score, 6) for n in second}
        assert first_scores == second_scores


class TestSemanticEdgesParticipate:
    def test_semantic_neighbour_of_seed_receives_score(self) -> None:
        edges = [_semantic_edge("similar", "root")]
        result = personalized_pagerank(edges, ["root"], max_hops=1)
        names = _names(result)
        assert "root" in names
        assert "similar" in names
