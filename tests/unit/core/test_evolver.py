"""Tests for the graph evolver and the shadow-promotion path.

Covers the sidecar file lifecycle, shadow isolation (nothing touches
the serving graph until ``promote``), the weight threshold, and the
mine-on-empty path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeguardian.core.skills.evolver import (
    discard_candidates,
    list_candidates,
    mine_candidates,
    promote_candidates,
    write_candidates,
)
from codeguardian.core.skills.graph_store import (
    count_edges,
    count_nodes,
    get_all_edges,
    get_node,
)
from codeguardian.core.skills.types import (
    EdgeKind,
    SkillEdge,
    compute_edge_id,
)
from codeguardian.core.storage.migration_runner import MigrationRunner
from codeguardian.core.storage.sqlite_store import SQLiteStore


def _edge(src: str, dst: str, weight: float = 0.8) -> SkillEdge:
    return SkillEdge(
        edge_id=compute_edge_id(src, dst, EdgeKind.SEMANTIC),
        src=src,
        dst=dst,
        kind=EdgeKind.SEMANTIC,
        weight=weight,
        provenance="trace:cooccurrence",
    )


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d, SQLiteStore(Path(d) / "t.db") as s:
        MigrationRunner(s).run()
        yield s


@pytest.fixture
def shadow_path(tmp_path: Path) -> Path:
    return tmp_path / "shadow.json"


class TestWriteRead:
    def test_write_empty_list(self, shadow_path: Path) -> None:
        assert write_candidates(shadow_path, []) == 0
        assert shadow_path.exists()
        assert list_candidates(shadow_path) == []

    def test_write_and_read_round_trip(self, shadow_path: Path) -> None:
        edges = [_edge("a", "b"), _edge("c", "d")]
        assert write_candidates(shadow_path, edges) == 2
        loaded = list_candidates(shadow_path)
        assert len(loaded) == 2
        assert {(e.src, e.dst) for e in loaded} == {("a", "b"), ("c", "d")}

    def test_read_missing_returns_empty(self, tmp_path: Path) -> None:
        assert list_candidates(tmp_path / "does-not-exist.json") == []

    def test_read_corrupt_returns_empty(self, shadow_path: Path) -> None:
        shadow_path.write_text("not valid json {{", encoding="utf-8")
        assert list_candidates(shadow_path) == []

    def test_read_wrong_shape_returns_empty(self, shadow_path: Path) -> None:
        shadow_path.write_text(json.dumps({"candidates": "not-a-list"}), encoding="utf-8")
        assert list_candidates(shadow_path) == []


class TestDiscard:
    def test_discard_existing(self, shadow_path: Path) -> None:
        write_candidates(shadow_path, [_edge("a", "b")])
        assert shadow_path.exists()
        assert discard_candidates(shadow_path) is True
        assert not shadow_path.exists()

    def test_discard_missing(self, shadow_path: Path) -> None:
        assert discard_candidates(shadow_path) is False


class TestMineCandidates:
    def test_empty_db_returns_empty(self, store: SQLiteStore) -> None:
        assert mine_candidates(store) == []


class TestShadowIsolation:
    def test_candidates_do_not_touch_db_until_promote(
        self, store: SQLiteStore, shadow_path: Path
    ) -> None:
        edges = [_edge("a", "b", weight=0.8)]
        write_candidates(shadow_path, edges)
        assert count_nodes(store) == 0
        assert count_edges(store) == 0

    def test_discard_leaves_db_untouched(self, store: SQLiteStore, shadow_path: Path) -> None:
        write_candidates(shadow_path, [_edge("a", "b")])
        discard_candidates(shadow_path)
        assert count_nodes(store) == 0
        assert count_edges(store) == 0


class TestPromotion:
    def test_empty_file_promotes_nothing(self, store: SQLiteStore, shadow_path: Path) -> None:
        write_candidates(shadow_path, [])
        assert promote_candidates(store, shadow_path) == 0
        assert count_nodes(store) == 0

    def test_missing_file_promotes_nothing(self, store: SQLiteStore, shadow_path: Path) -> None:
        assert promote_candidates(store, shadow_path) == 0

    def test_valid_edge_promoted(self, store: SQLiteStore, shadow_path: Path) -> None:
        write_candidates(shadow_path, [_edge("a", "b", weight=0.8)])
        promoted = promote_candidates(store, shadow_path, min_weight=0.5)
        assert promoted == 1
        # Nodes created for both endpoints
        assert get_node(store, "a") is not None
        assert get_node(store, "b") is not None
        # Edge present in serving graph
        edges = get_all_edges(store)
        assert any(e.src == "a" and e.dst == "b" for e in edges)

    def test_low_weight_not_promoted(self, store: SQLiteStore, shadow_path: Path) -> None:
        write_candidates(shadow_path, [_edge("a", "b", weight=0.3)])
        promoted = promote_candidates(store, shadow_path, min_weight=0.5)
        assert promoted == 0
        assert count_nodes(store) == 0
        assert count_edges(store) == 0

    def test_mixed_weights_partial_promotion(self, store: SQLiteStore, shadow_path: Path) -> None:
        write_candidates(
            shadow_path,
            [
                _edge("strong-a", "strong-b", weight=0.9),
                _edge("weak-a", "weak-b", weight=0.2),
            ],
        )
        promoted = promote_candidates(store, shadow_path, min_weight=0.5)
        assert promoted == 1
        assert get_node(store, "strong-a") is not None
        assert get_node(store, "weak-a") is None

    def test_existing_node_not_duplicated(self, store: SQLiteStore, shadow_path: Path) -> None:
        from codeguardian.core.skills.graph_store import upsert_node
        from codeguardian.core.skills.types import SkillNode

        upsert_node(store, SkillNode(node_id="a", kind="skill", name="a"))
        write_candidates(shadow_path, [_edge("a", "b")])
        promote_candidates(store, shadow_path, min_weight=0.5)
        # No duplication: node "a" still present exactly once
        nodes_a = store.query(
            "SELECT COUNT(*) AS c FROM skill_graph_nodes WHERE node_id = ?",
            ("a",),
        )
        assert nodes_a[0]["c"] == 1


# Standard-library import needed by the fixture above.
import tempfile  # noqa: E402
