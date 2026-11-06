"""Tests for the negative-knowledge store.

Covers CRUD, the adopt/reject protocol, the forgetting curve, and
provenance round-tripping. Every test uses a fresh ``tmp_path`` with
migrations applied.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codeguardian.core.skills.negative import (
    add_record,
    adopt,
    apply_forgetting_curve,
    delete_record,
    find_relevant,
    get_record,
    list_all,
    reject,
)
from codeguardian.core.skills.types import NegativeRecord, ProvenancePin
from codeguardian.core.storage.migration_runner import MigrationRunner
from codeguardian.core.storage.sqlite_store import SQLiteStore


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _iso_days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _record(
    failure_id: str = "f1",
    skill_name: str = "test-skill",
    category: str = "contract",
    context: str = "sig-1",
    confidence: float = 0.8,
    last_seen: str | None = None,
    provenance: ProvenancePin | None = None,
) -> NegativeRecord:
    now = _now_iso()
    return NegativeRecord(
        failure_id=failure_id,
        skill_name=skill_name,
        failure_category=category,
        context_signature=context,
        attempted="tried something",
        observed="something failed",
        run_id="run-1",
        confidence=confidence,
        first_seen=now,
        last_seen=last_seen if last_seen is not None else now,
        provenance=provenance,
    )


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d, SQLiteStore(Path(d) / "t.db") as s:
        MigrationRunner(s).run()
        yield s


class TestCrud:
    def test_add_and_get_round_trip(self, store) -> None:
        add_record(store, _record(failure_id="abc"))
        loaded = get_record(store, "abc")
        assert loaded is not None
        assert loaded.failure_id == "abc"
        assert loaded.skill_name == "test-skill"
        assert loaded.attempted == "tried something"

    def test_get_missing_returns_none(self, store) -> None:
        assert get_record(store, "does-not-exist") is None

    def test_add_replaces_existing(self, store) -> None:
        add_record(store, _record(failure_id="abc", confidence=0.5))
        add_record(store, _record(failure_id="abc", confidence=0.9))
        loaded = get_record(store, "abc")
        assert loaded is not None
        assert loaded.confidence == 0.9

    def test_list_all_returns_every_record(self, store) -> None:
        add_record(store, _record(failure_id="a"))
        add_record(store, _record(failure_id="b"))
        add_record(store, _record(failure_id="c"))
        all_records = list_all(store)
        assert len(all_records) == 3
        assert {r.failure_id for r in all_records} == {"a", "b", "c"}

    def test_delete_existing_returns_true(self, store) -> None:
        add_record(store, _record(failure_id="abc"))
        assert delete_record(store, "abc") is True
        assert get_record(store, "abc") is None

    def test_delete_missing_returns_false(self, store) -> None:
        assert delete_record(store, "does-not-exist") is False


class TestFindRelevant:
    def test_matching_signature_returned(self, store) -> None:
        add_record(store, _record(failure_id="f1", context="sig-A"))
        add_record(store, _record(failure_id="f2", context="sig-B"))
        found = find_relevant(store, "sig-A")
        assert len(found) == 1
        assert found[0].failure_id == "f1"

    def test_no_match_returns_empty(self, store) -> None:
        add_record(store, _record(failure_id="f1", context="sig-A"))
        assert find_relevant(store, "sig-Z") == ()

    def test_confidence_filter_applied(self, store) -> None:
        add_record(store, _record(failure_id="hi", context="sig-A", confidence=0.9))
        add_record(store, _record(failure_id="lo", context="sig-A", confidence=0.05))
        found = find_relevant(store, "sig-A", min_confidence=0.1)
        assert len(found) == 1
        assert found[0].failure_id == "hi"

    def test_limit_respected(self, store) -> None:
        for i in range(5):
            add_record(store, _record(failure_id=f"f{i}", context="sig-A"))
        found = find_relevant(store, "sig-A", limit=2)
        assert len(found) == 2

    def test_ordered_by_confidence_descending(self, store) -> None:
        add_record(store, _record(failure_id="lo", context="sig-A", confidence=0.3))
        add_record(store, _record(failure_id="hi", context="sig-A", confidence=0.9))
        add_record(store, _record(failure_id="mid", context="sig-A", confidence=0.6))
        found = find_relevant(store, "sig-A")
        assert [r.failure_id for r in found] == ["hi", "mid", "lo"]

    def test_empty_store_returns_empty(self, store) -> None:
        assert find_relevant(store, "anything") == ()


class TestAdoptReject:
    def test_adopt_increments_counter(self, store) -> None:
        add_record(store, _record(failure_id="f1"))
        adopt(store, "f1")
        adopt(store, "f1")
        loaded = get_record(store, "f1")
        assert loaded is not None
        assert loaded.adopted_count == 2
        assert loaded.rejected_count == 0

    def test_reject_increments_counter(self, store) -> None:
        add_record(store, _record(failure_id="f1"))
        reject(store, "f1")
        loaded = get_record(store, "f1")
        assert loaded is not None
        assert loaded.rejected_count == 1
        assert loaded.adopted_count == 0

    def test_adopt_and_reject_independent(self, store) -> None:
        add_record(store, _record(failure_id="f1"))
        adopt(store, "f1")
        reject(store, "f1")
        adopt(store, "f1")
        loaded = get_record(store, "f1")
        assert loaded is not None
        assert loaded.adopted_count == 2
        assert loaded.rejected_count == 1


class TestForgettingCurve:
    def test_old_records_decay(self, store) -> None:
        add_record(
            store,
            _record(
                failure_id="old",
                confidence=0.9,
                last_seen=_iso_days_ago(90),
            ),
        )
        apply_forgetting_curve(store, decay_factor=0.9, reference_days=30, min_confidence=0.01)
        loaded = get_record(store, "old")
        assert loaded is not None
        assert loaded.confidence < 0.9

    def test_fresh_records_unchanged(self, store) -> None:
        add_record(
            store,
            _record(
                failure_id="fresh",
                confidence=0.9,
                last_seen=_iso_days_ago(0),
            ),
        )
        apply_forgetting_curve(store, decay_factor=0.9, reference_days=30, min_confidence=0.01)
        loaded = get_record(store, "fresh")
        assert loaded is not None
        assert loaded.confidence == pytest.approx(0.9)

    def test_below_floor_records_deleted(self, store) -> None:
        add_record(
            store,
            _record(
                failure_id="ancient",
                confidence=0.05,
                last_seen=_iso_days_ago(365),
            ),
        )
        deleted = apply_forgetting_curve(
            store, decay_factor=0.5, reference_days=30, min_confidence=0.1
        )
        assert deleted >= 1
        assert get_record(store, "ancient") is None

    def test_per_skill_cap_enforced(self, store) -> None:
        for i in range(5):
            add_record(
                store,
                _record(
                    failure_id=f"f{i}",
                    skill_name="same-skill",
                    confidence=0.5 + i * 0.1,
                    last_seen=_iso_days_ago(0),
                ),
            )
        apply_forgetting_curve(
            store,
            decay_factor=1.0,
            reference_days=30,
            min_confidence=0.01,
            max_per_skill=2,
        )
        remaining = list_all(store)
        assert len(remaining) == 2
        # Highest confidence retained
        confidences = sorted(r.confidence for r in remaining)
        assert confidences == pytest.approx([0.8, 0.9])

    def test_invalid_decay_factor_returns_zero(self, store) -> None:
        add_record(store, _record(failure_id="f1"))
        deleted = apply_forgetting_curve(store, decay_factor=0.0)
        assert deleted == 0
        assert get_record(store, "f1") is not None


class TestProvenance:
    def test_provenance_round_trip(self, store) -> None:
        pin = ProvenancePin(
            source_runs=("r1", "r2"),
            authored_by="skill_curator",
            commit_hash="abc123",
            license="MIT",
            schema_hash="sha256:test",
            scan_result="clean",
            eval_score=0.75,
            reviewer="auto",
            reviewer_id=None,
            kernel_version="1.0.0",
            admission_evidence={"note": "test"},
        )
        add_record(store, _record(failure_id="f1", provenance=pin))
        loaded = get_record(store, "f1")
        assert loaded is not None
        assert loaded.provenance is not None
        assert loaded.provenance.source_runs == ("r1", "r2")
        assert loaded.provenance.authored_by == "skill_curator"
        assert loaded.provenance.commit_hash == "abc123"
        assert loaded.provenance.eval_score == 0.75

    def test_record_without_provenance(self, store) -> None:
        add_record(store, _record(failure_id="f1", provenance=None))
        loaded = get_record(store, "f1")
        assert loaded is not None
        assert loaded.provenance is None
