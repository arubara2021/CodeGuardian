"""Tests for the Markdown-backed skill library.

Each test exercises a public method of ``SkillStore`` against a fresh
temporary store. Tests cover the round-trip, schema enforcement, FTS
keyword search, vector nearest-neighbor search, feedback tracking, and
the index rebuild from Markdown.

The ``skill_store`` fixture opens a fresh SQLite database and applies
every migration before constructing the SkillStore. Without this step
the schema tables (``skills``, ``skills_fts``, ``skill_embeddings``)
would not exist and every operation on the store would fail with
``no such table``. Applying migrations in the fixture mirrors what the
orchestrator does at startup in production.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.storage import MigrationRunner, SkillStore, SQLiteStore

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SCHEMAS_DIR = _REPO_ROOT / "schemas"
_EMBEDDING_DIM = 384


def _frontmatter(name: str = "test-skill") -> dict:
    return {
        "name": name,
        "description": "Add type hints to untyped Python functions",
        "trigger": "When refactoring Python code that lacks annotations",
        "skill_type": "refactor_pattern",
        "language": "python",
        "state": "active",
    }


def _embedding(seed: float = 0.1) -> list[float]:
    return [seed * (i + 1) for i in range(_EMBEDDING_DIM)]


@pytest.fixture
def skill_store(tmp_path: Path):
    with SQLiteStore(tmp_path / "test.db") as store:
        MigrationRunner(store).run()
        s = SkillStore(store, tmp_path / "cg-home", _SCHEMAS_DIR)
        yield s


class TestAddAndGet:
    def test_add_creates_skill_file(self, skill_store: SkillStore) -> None:
        path = skill_store.add("test-skill", _frontmatter(), "# Body\n\nSteps here.\n")
        assert path.exists()
        assert path.name == "SKILL.md"
        assert path.parent.name == "test-skill"

    def test_add_returns_absolute_path(self, skill_store: SkillStore) -> None:
        path = skill_store.add("test-skill", _frontmatter(), "body")
        assert path.is_absolute()

    def test_get_returns_frontmatter_and_body(self, skill_store: SkillStore) -> None:
        skill_store.add("test-skill", _frontmatter(), "# Heading\n\nContent.\n")
        loaded = skill_store.get("test-skill")
        assert loaded is not None
        assert loaded["frontmatter"]["name"] == "test-skill"
        assert loaded["frontmatter"]["skill_type"] == "refactor_pattern"
        assert "# Heading" in loaded["body"]

    def test_get_missing_returns_none(self, skill_store: SkillStore) -> None:
        assert skill_store.get("does-not-exist") is None

    def test_add_rejects_missing_required_field(self, skill_store: SkillStore) -> None:
        bad = _frontmatter()
        del bad["description"]
        with pytest.raises(CodeGuardianError) as exc_info:
            skill_store.add("test-skill", bad, "body")
        assert exc_info.value.code is ErrorCode.STORAGE_SCHEMA_INVALID

    def test_add_rejects_unknown_field(self, skill_store: SkillStore) -> None:
        bad = _frontmatter()
        bad["unexpected_field"] = "nope"
        with pytest.raises(CodeGuardianError) as exc_info:
            skill_store.add("test-skill", bad, "body")
        assert exc_info.value.code is ErrorCode.STORAGE_SCHEMA_INVALID

    def test_add_rejects_invalid_enum_value(self, skill_store: SkillStore) -> None:
        bad = _frontmatter()
        bad["skill_type"] = "not_a_real_type"
        with pytest.raises(CodeGuardianError) as exc_info:
            skill_store.add("test-skill", bad, "body")
        assert exc_info.value.code is ErrorCode.STORAGE_SCHEMA_INVALID

    def test_add_rejects_mismatched_name(self, skill_store: SkillStore) -> None:
        bad = _frontmatter(name="other-name")
        with pytest.raises(CodeGuardianError) as exc_info:
            skill_store.add("test-skill", bad, "body")
        assert exc_info.value.code is ErrorCode.STORAGE_SCHEMA_INVALID

    def test_add_refuses_overwrite_by_default(self, skill_store: SkillStore) -> None:
        skill_store.add("test-skill", _frontmatter(), "body 1")
        with pytest.raises(CodeGuardianError) as exc_info:
            skill_store.add("test-skill", _frontmatter(), "body 2")
        assert exc_info.value.code is ErrorCode.STORAGE_SCHEMA_INVALID

    def test_add_with_overwrite_replaces(self, skill_store: SkillStore) -> None:
        skill_store.add("test-skill", _frontmatter(), "body 1")
        skill_store.add("test-skill", _frontmatter(), "body 2", overwrite=True)
        loaded = skill_store.get("test-skill")
        assert loaded is not None
        assert "body 2" in loaded["body"]

    def test_add_rejects_wrong_embedding_dimension(self, skill_store: SkillStore) -> None:
        with pytest.raises(CodeGuardianError) as exc_info:
            skill_store.add("test-skill", _frontmatter(), "body", embedding=[0.0] * 10)
        assert exc_info.value.code is ErrorCode.STORAGE_SCHEMA_INVALID


class TestListAndRemove:
    def test_list_all_empty(self, skill_store: SkillStore) -> None:
        assert skill_store.list_all() == []

    def test_list_all_returns_skills(self, skill_store: SkillStore) -> None:
        skill_store.add("a-skill", _frontmatter("a-skill"), "body a")
        skill_store.add("b-skill", _frontmatter("b-skill"), "body b")
        skills = skill_store.list_all()
        assert len(skills) == 2
        assert skills[0]["name"] == "a-skill"
        assert skills[1]["name"] == "b-skill"

    def test_count(self, skill_store: SkillStore) -> None:
        assert skill_store.count() == 0
        skill_store.add("a-skill", _frontmatter("a-skill"), "body")
        assert skill_store.count() == 1

    def test_remove_deletes_file_and_rows(self, skill_store: SkillStore) -> None:
        path = skill_store.add("test-skill", _frontmatter(), "body")
        assert path.exists()
        removed = skill_store.remove("test-skill")
        assert removed is True
        assert not path.exists()
        assert skill_store.count() == 0
        assert skill_store.get("test-skill") is None

    def test_remove_missing_returns_false(self, skill_store: SkillStore) -> None:
        assert skill_store.remove("does-not-exist") is False

    def test_remove_also_removes_embedding(self, skill_store: SkillStore) -> None:
        skill_store.add("test-skill", _frontmatter(), "body", embedding=_embedding())
        results = skill_store.search_semantic(_embedding(), limit=10)
        assert len(results) == 1
        skill_store.remove("test-skill")
        results = skill_store.search_semantic(_embedding(), limit=10)
        assert results == []


class TestSearch:
    def test_keyword_search_matches_description(self, skill_store: SkillStore) -> None:
        skill_store.add("a-skill", _frontmatter("a-skill"), "body")
        results = skill_store.search_keyword("type hints")
        assert len(results) == 1
        assert results[0]["name"] == "a-skill"

    def test_keyword_search_matches_trigger(self, skill_store: SkillStore) -> None:
        skill_store.add("a-skill", _frontmatter("a-skill"), "body")
        results = skill_store.search_keyword("refactoring")
        assert len(results) == 1

    def test_keyword_search_no_match(self, skill_store: SkillStore) -> None:
        skill_store.add("a-skill", _frontmatter("a-skill"), "body")
        results = skill_store.search_keyword("nonexistentterm")
        assert results == []

    def test_keyword_search_empty_query(self, skill_store: SkillStore) -> None:
        skill_store.add("a-skill", _frontmatter("a-skill"), "body")
        assert skill_store.search_keyword("") == []
        assert skill_store.search_keyword("   ") == []

    def test_keyword_search_special_characters(self, skill_store: SkillStore) -> None:
        skill_store.add("a-skill", _frontmatter("a-skill"), "body")
        results = skill_store.search_keyword('a "quoted" term')
        assert isinstance(results, list)

    def test_semantic_search_finds_nearest(self, skill_store: SkillStore) -> None:
        skill_store.add("near", _frontmatter("near"), "body", embedding=_embedding(0.1))
        skill_store.add("far", _frontmatter("far"), "body", embedding=_embedding(10.0))
        results = skill_store.search_semantic(_embedding(0.1), limit=1)
        assert len(results) == 1
        assert results[0]["name"] == "near"

    def test_semantic_search_no_embeddings(self, skill_store: SkillStore) -> None:
        skill_store.add("a-skill", _frontmatter("a-skill"), "body")
        assert skill_store.search_semantic(_embedding()) == []

    def test_semantic_search_rejects_wrong_dimension(self, skill_store: SkillStore) -> None:
        with pytest.raises(CodeGuardianError) as exc_info:
            skill_store.search_semantic([0.0] * 10)
        assert exc_info.value.code is ErrorCode.STORAGE_SCHEMA_INVALID


class TestFeedbackAndRebuild:
    def test_record_feedback_updates_totals(self, skill_store: SkillStore) -> None:
        skill_store.add("test-skill", _frontmatter(), "body")
        skill_store.record_feedback("test-skill", "run-1", "success")
        skill_store.record_feedback("test-skill", "run-2", "failure")
        rows = skill_store.list_all()
        assert rows[0]["total_uses"] == 2
        assert rows[0]["total_successes"] == 1
        assert rows[0]["effectiveness"] == pytest.approx(0.5)

    def test_record_feedback_rejects_bad_outcome(self, skill_store: SkillStore) -> None:
        skill_store.add("test-skill", _frontmatter(), "body")
        with pytest.raises(CodeGuardianError) as exc_info:
            skill_store.record_feedback("test-skill", "run-1", "not-a-real-outcome")
        assert exc_info.value.code is ErrorCode.STORAGE_SCHEMA_INVALID

    def test_record_feedback_rejects_unknown_skill(self, skill_store: SkillStore) -> None:
        with pytest.raises(CodeGuardianError) as exc_info:
            skill_store.record_feedback("does-not-exist", "run-1", "success")
        assert exc_info.value.code is ErrorCode.STORAGE_SCHEMA_INVALID

    def test_rebuild_index_from_markdown(self, skill_store: SkillStore) -> None:
        skill_store.add("test-skill", _frontmatter(), "body")
        skill_store._sqlite.execute("DELETE FROM skills")
        skill_store._sqlite.execute("DELETE FROM skills_fts")
        assert skill_store.count() == 0

        inserted = skill_store.rebuild_index()
        assert inserted == 1
        assert skill_store.count() == 1

        results = skill_store.search_keyword("type hints")
        assert len(results) == 1
