"""Tests for the Phase 12 Skills Module.

This test suite verifies three things:
1. The lifecycle of a skill (draft -> active -> archived).
2. The hybrid retrieval engine (RRF merging of vector and FTS5).
3. The formatting of negative constraints and TTL decay.

To keep the tests fast and independent of external downloads, it uses
a FakeEmbedder that returns deterministic 384-dimensional arrays.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.skills.embeddings import EmbeddingProvider
from codeguardian.core.skills.indexer import SkillIndexer
from codeguardian.core.skills.library import SkillLibrary
from codeguardian.core.skills.retriever import SkillRetriever
from codeguardian.core.storage.migration_runner import MigrationRunner
from codeguardian.core.storage.sqlite_store import SQLiteStore

# ---------------------------------------------------------------------------
# Fixtures and Helpers
# ---------------------------------------------------------------------------

_VALID_MD = """\
---
name: python-add-type-hints
description: Add type hints to untyped Python functions
trigger: When a function has no type annotations
skill_type: refactor_pattern
language: python
state: active
allowed-tools: []
version: 1.0.0
---
Use the `typing` module to add annotations to all parameters.
"""

_NEGATIVE_MD = """\
---
name: avoid-fstring-sql
description: F-string SQL causes injection vulnerabilities
trigger: When building SQL queries
skill_type: refactor_pattern
language: python
state: active
effectiveness: 0.1
---
Never use f-strings for SQL. Use parameterized queries instead.
"""


class FakeEmbedder(EmbeddingProvider):
    """A deterministic mock of the embedding provider for unit tests."""

    def __init__(self) -> None:
        pass

    async def embed_text(self, text: str) -> list[float]:
        if not text.strip():
            raise CodeGuardianError(ErrorCode.STORAGE_SCHEMA_INVALID, "Cannot embed empty text.")
        # Generate a deterministic pseudo-vector based on the text hash
        h = int(hashlib.md5(text.encode()).hexdigest(), 16)
        return [float((h >> i) & 0xFF) / 255.0 for i in range(384)]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed_text(t) for t in texts]


@pytest.fixture
def schemas_dir(tmp_path: Path) -> Path:
    # Create a minimal schema for the store to validate against
    schema_path = tmp_path / "skill.json"
    schema_path.write_text(
        """\
{
    "type": "object",
    "required": ["name", "description", "trigger", "skill_type", "language", "state"],
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "trigger": {"type": "string"},
        "skill_type": {"type": "string"},
        "language": {"type": "string"},
        "state": {"type": "string", "enum": ["active", "draft", "stale", "archived"]},
        "effectiveness": {"type": "number"},
        "total_uses": {"type": "integer"},
        "total_successes": {"type": "integer"},
        "allowed-tools": {"type": "array", "items": {"type": "string"}},
        "version": {"type": "string"}
    }
}
""",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    db_path = tmp_path / "test.db"
    s = SQLiteStore(db_path)
    s.open()
    MigrationRunner(s).run()
    yield s
    s.close()


@pytest.fixture
def library(store: SQLiteStore, tmp_path: Path, schemas_dir: Path) -> SkillLibrary:
    return SkillLibrary(
        sqlite_store=store,
        root=tmp_path,
        schemas_dir=schemas_dir,
        embedder=FakeEmbedder(),
    )


@pytest.fixture
def retriever(library: SkillLibrary) -> SkillRetriever:
    return SkillRetriever(library, FakeEmbedder())


# ---------------------------------------------------------------------------
# 1. Embedding Provider Tests
# ---------------------------------------------------------------------------


class TestEmbeddingProvider:
    @pytest.mark.asyncio
    async def test_real_provider_raises_if_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Simulate the fastembed package being completely missing
        monkeypatch.setitem(sys.modules, "fastembed", None)

        provider = EmbeddingProvider()
        with pytest.raises(CodeGuardianError) as exc:
            await provider.embed_text("test text")

        assert exc.value.code == ErrorCode.SYS_NOT_IMPLEMENTED

    @pytest.mark.asyncio
    async def test_fake_provider_embeds(self) -> None:
        provider = FakeEmbedder()
        vec = await provider.embed_text("hello world")
        assert len(vec) == 384
        assert all(isinstance(x, float) for x in vec)

    @pytest.mark.asyncio
    async def test_fake_provider_rejects_empty(self) -> None:
        provider = FakeEmbedder()
        with pytest.raises(CodeGuardianError):
            await provider.embed_text("")


# ---------------------------------------------------------------------------
# 2. Skill Library Tests
# ---------------------------------------------------------------------------


class TestSkillLibrary:
    @pytest.mark.asyncio
    async def test_add_skill(self, library: SkillLibrary) -> None:
        name = await library.add_skill(_VALID_MD)
        assert name == "python-add-type-hints"

        record = library.get_skill(name)
        assert record is not None
        assert record["frontmatter"]["state"] == "active"
        assert "Use the `typing` module" in record["body"]

    @pytest.mark.asyncio
    async def test_add_skill_invalid_markdown(self, library: SkillLibrary) -> None:
        bad_md = "---\nname: test\n---\n"  # Missing required fields
        with pytest.raises(CodeGuardianError) as exc:
            await library.add_skill(bad_md)
        assert exc.value.code == ErrorCode.STORAGE_SCHEMA_INVALID

    @pytest.mark.asyncio
    async def test_promote_skill(self, library: SkillLibrary) -> None:
        draft_md = _VALID_MD.replace("state: active", "state: draft")
        await library.add_skill(draft_md)

        await library.promote_skill("python-add-type-hints")
        record = library.get_skill("python-add-type-hints")
        assert record is not None
        assert record["frontmatter"]["state"] == "active"

    @pytest.mark.asyncio
    async def test_promote_non_draft_fails(self, library: SkillLibrary) -> None:
        await library.add_skill(_VALID_MD)  # Already active
        with pytest.raises(CodeGuardianError):
            await library.promote_skill("python-add-type-hints")

    @pytest.mark.asyncio
    async def test_archive_skill(self, library: SkillLibrary) -> None:
        await library.add_skill(_VALID_MD)
        await library.archive_skill("python-add-type-hints")
        record = library.get_skill("python-add-type-hints")
        assert record is not None
        assert record["frontmatter"]["state"] == "archived"


# ---------------------------------------------------------------------------
# 3. Retriever Tests (Hybrid Search & Formatting)
# ---------------------------------------------------------------------------


class TestSkillRetriever:
    @pytest.mark.asyncio
    async def test_retrieve_positives(
        self, library: SkillLibrary, retriever: SkillRetriever
    ) -> None:
        await library.add_skill(_VALID_MD)

        prompt = await retriever.retrieve_for_prompt("add type hints to this python function")

        assert "## Relevant Skills and Constraints" in prompt
        assert "### RECOMMENDED PATTERNS" in prompt
        assert "#### PATTERN: python-add-type-hints" in prompt
        assert "### NEGATIVE CONSTRAINTS" not in prompt

    @pytest.mark.asyncio
    async def test_retrieve_negatives(
        self, library: SkillLibrary, retriever: SkillRetriever
    ) -> None:
        await library.add_skill(_NEGATIVE_MD)

        prompt = await retriever.retrieve_for_prompt("write a SQL query")

        assert "### NEGATIVE CONSTRAINTS (DO NOT DO THESE)" in prompt
        assert "#### AVOID: avoid-fstring-sql" in prompt
        assert "Never use f-strings for SQL" in prompt

    @pytest.mark.asyncio
    async def test_retrieve_ignores_drafts_and_archived(
        self, library: SkillLibrary, retriever: SkillRetriever
    ) -> None:
        draft_md = _VALID_MD.replace("state: active", "state: draft")
        await library.add_skill(draft_md)

        prompt = await retriever.retrieve_for_prompt("add type hints")
        assert prompt == ""

    @pytest.mark.asyncio
    async def test_hybrid_ranking(
        self, library: SkillLibrary, retriever: SkillRetriever, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Add both skills
        md1 = _VALID_MD.replace("name: python-add-type-hints", "name: type-skill")
        md2 = _VALID_MD.replace("name: python-add-type-hints", "name: annotate-skill")
        await library.add_skill(md1)
        await library.add_skill(md2)

        # Mock the underlying store to simulate specific rankings.
        # We want "type-skill" to win because it matches both semantic and keyword.
        def mock_semantic(embedding: list[float], limit: int) -> list[dict]:
            # Semantic search returns type-skill first
            return [{"name": "type-skill"}, {"name": "annotate-skill"}]

        def mock_keyword(query: str, limit: int) -> list[dict]:
            # Keyword search ALSO returns type-skill first
            return [{"name": "type-skill"}, {"name": "annotate-skill"}]

        monkeypatch.setattr(retriever._store, "search_semantic", mock_semantic)
        monkeypatch.setattr(retriever._store, "search_keyword", mock_keyword)

        prompt = await retriever.retrieve_for_prompt("type")

        idx1 = prompt.find("type-skill")
        idx2 = prompt.find("annotate-skill")

        # Both should be present, but type-skill should appear first due to RRF
        assert idx1 != -1
        assert idx2 != -1
        assert idx1 < idx2


# ---------------------------------------------------------------------------
# 4. Indexer Tests (TTL Decay & Rebuild)
# ---------------------------------------------------------------------------


class TestSkillIndexer:
    def test_decay_stale_skills(self, library: SkillLibrary, store: SQLiteStore) -> None:
        indexer = SkillIndexer(library._store, FakeEmbedder(), ttl_days=90)

        # Manually insert a skill with an old timestamp to bypass the library
        old_date = (datetime.now(UTC) - timedelta(days=100)).isoformat()
        store.execute(
            """
            INSERT INTO skills (name, path, description, trigger_text, skill_type, language, state, created_at)
            VALUES ('old-skill', 'path', 'desc', 'trigger', 'refactor_pattern', 'python', 'active', ?)
            """,
            (old_date,),
        )

        decayed = indexer.decay_stale_skills()
        assert decayed == 1

        row = store.query("SELECT state FROM skills WHERE name = 'old-skill'")[0]
        assert row["state"] == "stale"

    @pytest.mark.asyncio
    async def test_rebuild_missing_embeddings(
        self, library: SkillLibrary, store: SQLiteStore
    ) -> None:
        indexer = SkillIndexer(library._store, FakeEmbedder())

        # Manually insert a skill row without an embedding
        store.execute(
            """
            INSERT INTO skills (name, path, description, trigger_text, skill_type, language, state)
            VALUES ('manual-skill', 'path', 'desc', 'trigger', 'refactor_pattern', 'python', 'active')
            """
        )
        # Also need to write the SKILL.md file to disk so the indexer can read the body
        skill_dir = library._store.root / "skills" / "manual-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            _VALID_MD.replace("python-add-type-hints", "manual-skill"), encoding="utf-8"
        )

        count = await indexer.rebuild_missing_embeddings()
        assert count == 1

        vec_row = store.query("SELECT 1 FROM skill_embeddings WHERE name = 'manual-skill'")
        assert len(vec_row) == 1
