"""Tests for the staged retrieval pipeline.

Covers the flag-off delegation guarantee, the empty-library path, the
stage recording on the returned bundle, and the fallback when the
reranker is unavailable.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

from codeguardian.core.config.schema import validate as validate_config
from codeguardian.core.skills.embeddings import EmbeddingProvider
from codeguardian.core.skills.library import SkillLibrary
from codeguardian.core.skills.retrieval import RetrievalPipeline
from codeguardian.core.skills.retriever import SkillRetriever
from codeguardian.core.skills.types import ProjectCard, RetrievalItem
from codeguardian.core.storage.migration_runner import MigrationRunner
from codeguardian.core.storage.sqlite_store import SQLiteStore

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SCHEMAS_DIR = _REPO_ROOT / "schemas"


class FakeEmbedder(EmbeddingProvider):
    """A deterministic embedder that never downloads a model."""

    def __init__(self) -> None:
        pass

    async def embed_text(self, text: str) -> list[float]:
        digest = int(hashlib.md5(text.encode()).hexdigest(), 16)
        return [float((digest >> (i % 64)) & 0xFF) / 255.0 for i in range(384)]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed_text(t) for t in texts]


class PassthroughReranker:
    """A reranker that returns its input unchanged."""

    async def rerank(
        self,
        query: str,
        items: tuple[RetrievalItem, ...],
    ) -> tuple[RetrievalItem, ...]:
        return items


def _skill_md(name: str, description: str = "") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description or f'description for {name}'}\n"
        "trigger: when needed\n"
        "skill_type: refactor_pattern\n"
        "language: python\n"
        "state: active\n"
        "---\n\n"
        f"Body of {name}.\n"
    )


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    instance = SQLiteStore(tmp_path / "test.db")
    instance.open()
    MigrationRunner(instance).run()
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def config_all_off():
    return validate_config({}).skills


@pytest.fixture
def config_all_on():
    return validate_config(
        {
            "skills": {
                "graph_enabled": True,
                "ppr_enabled": True,
                "reranker_enabled": True,
                "recon_blast_filter_enabled": True,
            }
        }
    ).skills


@pytest.fixture
def library(
    store: SQLiteStore,
    tmp_path: Path,
    embedder: FakeEmbedder,
    config_all_off,
) -> SkillLibrary:
    return SkillLibrary(
        sqlite_store=store,
        root=tmp_path,
        schemas_dir=_SCHEMAS_DIR,
        embedder=embedder,
        config=config_all_off,
    )


class TestFlagOffDelegation:
    async def test_all_flags_off_delegates_to_v1_retriever(
        self,
        store: SQLiteStore,
        library: SkillLibrary,
        embedder: FakeEmbedder,
        config_all_off,
    ) -> None:
        await library.add_skill(_skill_md("test-skill", "add type hints"))
        pipeline = RetrievalPipeline(
            store=store,
            library=library,
            embedder=embedder,
            config=config_all_off,
        )
        v1 = SkillRetriever(library, embedder)

        async def delegate(query: str, limit: int) -> str:
            return await v1.retrieve_for_prompt(query, limit)

        pipeline_output = await pipeline.retrieve_for_prompt(
            "add type hints", seed_retriever=delegate
        )
        v1_output = await v1.retrieve_for_prompt("add type hints")
        assert pipeline_output == v1_output

    async def test_all_flags_off_without_seed_retriever_still_returns(
        self,
        store: SQLiteStore,
        library: SkillLibrary,
        embedder: FakeEmbedder,
        config_all_off,
    ) -> None:
        pipeline = RetrievalPipeline(
            store=store,
            library=library,
            embedder=embedder,
            config=config_all_off,
        )
        output = await pipeline.retrieve_for_prompt("anything")
        assert isinstance(output, str)


class TestEmptyLibrary:
    async def test_empty_library_returns_empty_bundle(
        self,
        store: SQLiteStore,
        library: SkillLibrary,
        embedder: FakeEmbedder,
        config_all_off,
    ) -> None:
        pipeline = RetrievalPipeline(
            store=store,
            library=library,
            embedder=embedder,
            config=config_all_off,
        )
        bundle = await pipeline.retrieve("anything")
        assert bundle.items == ()
        assert "seed" in bundle.stages_run
        assert "threshold_empty" in bundle.stages_run


class TestSeededRetrieval:
    async def test_skill_matching_query_is_returned(
        self,
        store: SQLiteStore,
        library: SkillLibrary,
        embedder: FakeEmbedder,
        config_all_off,
    ) -> None:
        await library.add_skill(_skill_md("add-types", "add type hints to python functions"))
        pipeline = RetrievalPipeline(
            store=store,
            library=library,
            embedder=embedder,
            config=config_all_off,
        )
        bundle = await pipeline.retrieve("type hints", limit=3)
        names = [item.name for item in bundle.items]
        assert "add-types" in names

    async def test_selected_item_carries_hydrated_body(
        self,
        store: SQLiteStore,
        library: SkillLibrary,
        embedder: FakeEmbedder,
        config_all_off,
    ) -> None:
        await library.add_skill(_skill_md("add-types", "add type hints to python functions"))
        pipeline = RetrievalPipeline(
            store=store,
            library=library,
            embedder=embedder,
            config=config_all_off,
        )
        bundle = await pipeline.retrieve("type hints", limit=3)
        assert bundle.items
        for item in bundle.items:
            if not item.is_negative:
                assert item.body is not None
                assert item.token_count > 0

    async def test_draft_state_filtered_from_results(
        self,
        store: SQLiteStore,
        library: SkillLibrary,
        embedder: FakeEmbedder,
        config_all_off,
    ) -> None:
        draft_md = _skill_md("draft-skill", "add type hints").replace(
            "state: active", "state: draft"
        )
        await library.add_skill(draft_md)
        pipeline = RetrievalPipeline(
            store=store,
            library=library,
            embedder=embedder,
            config=config_all_off,
        )
        bundle = await pipeline.retrieve("type hints", limit=3)
        names = [item.name for item in bundle.items]
        assert "draft-skill" not in names


class TestStageRecording:
    async def test_seed_stage_always_recorded(
        self,
        store: SQLiteStore,
        library: SkillLibrary,
        embedder: FakeEmbedder,
        config_all_off,
    ) -> None:
        pipeline = RetrievalPipeline(
            store=store,
            library=library,
            embedder=embedder,
            config=config_all_off,
        )
        bundle = await pipeline.retrieve("anything")
        assert "seed" in bundle.stages_run

    async def test_ppr_stage_skipped_when_disabled(
        self,
        store: SQLiteStore,
        library: SkillLibrary,
        embedder: FakeEmbedder,
        config_all_off,
    ) -> None:
        await library.add_skill(_skill_md("add-types", "add type hints"))
        pipeline = RetrievalPipeline(
            store=store,
            library=library,
            embedder=embedder,
            config=config_all_off,
        )
        bundle = await pipeline.retrieve("type hints")
        assert "ppr" not in bundle.stages_run

    async def test_rerank_stage_skipped_when_disabled(
        self,
        store: SQLiteStore,
        library: SkillLibrary,
        embedder: FakeEmbedder,
        config_all_off,
    ) -> None:
        await library.add_skill(_skill_md("add-types", "add type hints"))
        pipeline = RetrievalPipeline(
            store=store,
            library=library,
            embedder=embedder,
            config=config_all_off,
        )
        bundle = await pipeline.retrieve("type hints")
        assert "rerank" not in bundle.stages_run


class TestRerankerFailure:
    async def test_reranker_passthrough_does_not_break_pipeline(
        self,
        store: SQLiteStore,
        library: SkillLibrary,
        embedder: FakeEmbedder,
        config_all_off,
    ) -> None:
        await library.add_skill(_skill_md("add-types", "add type hints"))
        reranker = PassthroughReranker()
        pipeline = RetrievalPipeline(
            store=store,
            library=library,
            embedder=embedder,
            config=config_all_off,
            reranker=reranker,
        )
        bundle = await pipeline.retrieve("type hints")
        assert isinstance(bundle.items, tuple)


class TestLatencyBound:
    async def test_simple_query_completes_quickly(
        self,
        store: SQLiteStore,
        library: SkillLibrary,
        embedder: FakeEmbedder,
        config_all_off,
    ) -> None:
        await library.add_skill(_skill_md("add-types", "add type hints"))
        pipeline = RetrievalPipeline(
            store=store,
            library=library,
            embedder=embedder,
            config=config_all_off,
        )
        start = time.monotonic()
        await pipeline.retrieve("type hints")
        elapsed = time.monotonic() - start
        assert elapsed < 2.0


class TestProjectContextFilter:
    async def test_filter_disabled_by_default(
        self,
        store: SQLiteStore,
        library: SkillLibrary,
        embedder: FakeEmbedder,
        config_all_off,
    ) -> None:
        await library.add_skill(_skill_md("py-skill", "python type hints"))
        pipeline = RetrievalPipeline(
            store=store,
            library=library,
            embedder=embedder,
            config=config_all_off,
        )
        card = ProjectCard(
            languages=("rust",),
            confidence={"languages": "observed"},
        )
        bundle = await pipeline.retrieve("python type hints", project_context=card)
        assert "project_filter" not in bundle.stages_run

    async def test_filter_enabled_narrows_by_language(
        self,
        store: SQLiteStore,
        library: SkillLibrary,
        embedder: FakeEmbedder,
    ) -> None:
        config = validate_config({"skills": {"recon_blast_filter_enabled": True}}).skills
        py_lib = SkillLibrary(
            sqlite_store=store,
            root=library._root,
            schemas_dir=_SCHEMAS_DIR,
            embedder=embedder,
            config=config,
        )
        await py_lib.add_skill(_skill_md("py-skill", "python type hints for functions"))
        pipeline = RetrievalPipeline(
            store=store,
            library=py_lib,
            embedder=embedder,
            config=config,
        )
        card = ProjectCard(
            languages=("rust",),
            confidence={"languages": "observed"},
        )
        bundle = await pipeline.retrieve("python type hints for functions", project_context=card)
        names = [item.name for item in bundle.items]
        assert "py-skill" not in names
        assert "project_filter" in bundle.stages_run
