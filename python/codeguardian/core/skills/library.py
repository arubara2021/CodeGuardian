"""High-level skill library operations.

This module wraps the Phase 3 ``SkillStore`` and the Phase 12
``EmbeddingProvider`` to provide a unified API for adding, retrieving,
and promoting skills. It enforces the lifecycle rules that the
``SkillStore`` does not handle on its own:

    draft -> trial -> active -> stable -> retired

Skills created by the Phase 11 Skill Curator enter as ``draft``.
Skills promoted by the admission gate enter ``trial``. The governor
advances a trial skill to active, active to stable, and retires any
skill whose effectiveness falls below the configured floor.

The v1 state names ``stale`` and ``archived`` remain valid on read
and are treated as aliases for ``retired`` by the governor.

In addition to the v1 surface, this module exposes accessors that
return the governance, negative-knowledge, graph, evolution, and
retrieval layers, each constructed lazily so that a caller who does
not use a layer pays no cost. The accessors are the recommended way
to reach those layers; the underlying modules remain importable for
callers that want them directly.

The module depends on Phase 1's error taxonomy. A rejected admission
raises ``STORAGE_SCHEMA_INVALID`` because the failure is a shape
problem with the input, not a runtime failure.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from codeguardian.core.config.schema import SkillsConfig
from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.storage.skill_store import SkillStore
from codeguardian.core.storage.sqlite_store import SQLiteStore

from .embeddings import EmbeddingProvider

if TYPE_CHECKING:
    from .governor import SkillGovernor
    from .retrieval import RetrievalPipeline
    from .types import NegativeRecord, SkillEdge, SkillNode

__all__ = [
    "NegativeKnowledgeStore",
    "SkillEvolver",
    "SkillGraph",
    "SkillLibrary",
]


class NegativeKnowledgeStore:
    """Store-bound accessor for the negative-knowledge module.

    The module functions take an explicit store argument. This class
    binds that argument once so callers that hold a library can reach
    the store through ``library.negative()`` without passing the store
    around.
    """

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def add(self, record: NegativeRecord) -> None:
        from .negative import add_record

        add_record(self._store, record)

    def find(
        self,
        context_signature: str,
        *,
        min_confidence: float = 0.1,
        limit: int = 5,
    ) -> tuple[NegativeRecord, ...]:
        from .negative import find_relevant

        return find_relevant(
            self._store,
            context_signature,
            min_confidence=min_confidence,
            limit=limit,
        )

    def adopt(self, failure_id: str) -> None:
        from .negative import adopt

        adopt(self._store, failure_id)

    def reject(self, failure_id: str) -> None:
        from .negative import reject

        reject(self._store, failure_id)

    def delete(self, failure_id: str) -> bool:
        from .negative import delete_record

        return delete_record(self._store, failure_id)

    def apply_forgetting_curve(
        self,
        *,
        decay_factor: float = 0.9,
        reference_days: int = 30,
        min_confidence: float = 0.1,
        max_per_skill: int = 20,
    ) -> int:
        from .negative import apply_forgetting_curve

        return apply_forgetting_curve(
            self._store,
            decay_factor=decay_factor,
            reference_days=reference_days,
            min_confidence=min_confidence,
            max_per_skill=max_per_skill,
        )


class SkillGraph:
    """Store-bound accessor for the procedural skill graph.

    Wraps the functions in ``graph_store`` so callers that hold a
    library can query and rebuild the graph without managing the store
    handle.
    """

    def __init__(self, store: SQLiteStore, skills_dir: Path) -> None:
        self._store = store
        self._skills_dir = skills_dir

    def nodes(self) -> tuple[SkillNode, ...]:
        from .graph_store import get_all_nodes

        return get_all_nodes(self._store)

    def edges(self) -> tuple[SkillEdge, ...]:
        from .graph_store import get_all_edges

        return get_all_edges(self._store)

    def neighbors(
        self,
        node_id: str,
        *,
        kind: Any = None,
        direction: str = "out",
    ) -> tuple[str, ...]:
        from .graph_store import neighbors

        return neighbors(self._store, node_id, kind=kind, direction=direction)

    def rebuild(
        self,
        embeddings: dict[str, list[float]] | None = None,
        *,
        semantic_threshold: float = 0.85,
    ) -> tuple[int, int]:
        from .graph_store import drop_and_rebuild

        return drop_and_rebuild(
            self._store,
            self._skills_dir,
            embeddings,
            semantic_threshold=semantic_threshold,
        )

    def node_count(self) -> int:
        from .graph_store import count_nodes

        return count_nodes(self._store)

    def edge_count(self) -> int:
        from .graph_store import count_edges

        return count_edges(self._store)


class SkillEvolver:
    """Store-bound accessor for the graph evolution helpers."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def mine(
        self,
        *,
        min_cooccurrence: int = 3,
        outcome_filter: str = "success",
    ) -> list[SkillEdge]:
        from .evolver import mine_candidates

        return mine_candidates(
            self._store,
            min_cooccurrence=min_cooccurrence,
            outcome_filter=outcome_filter,
        )

    def write(self, path: Path, candidates: list[SkillEdge]) -> int:
        from .evolver import write_candidates

        return write_candidates(path, candidates)

    def list(self, path: Path) -> list[SkillEdge]:
        from .evolver import list_candidates

        return list_candidates(path)

    def promote(self, path: Path, *, min_weight: float = 0.5) -> int:
        from .evolver import promote_candidates

        return promote_candidates(self._store, path, min_weight=min_weight)

    def discard(self, path: Path) -> bool:
        from .evolver import discard_candidates

        return discard_candidates(path)


class SkillLibrary:
    """The high-level facade for storing and managing skills.

    Args:
        sqlite_store: An open ``SQLiteStore``.
        root: The root of the on-disk storage tree.
        schemas_dir: The directory holding the JSON Schema files.
        embedder: An optional ``EmbeddingProvider``. If not supplied,
            a default instance is created.
        config: An optional ``SkillsConfig``. If not supplied, the
            defaults from ``config.defaults`` are used, which enables
            no feature flags and matches the v1 behavior.
    """

    def __init__(
        self,
        sqlite_store: SQLiteStore,
        root: Path,
        schemas_dir: Path,
        embedder: EmbeddingProvider | None = None,
        config: SkillsConfig | None = None,
    ) -> None:
        self._sqlite = sqlite_store
        self._root = Path(root)
        self._schemas_dir = Path(schemas_dir)
        self._store = SkillStore(
            sqlite_store=sqlite_store,
            root=root,
            schemas_dir=schemas_dir,
        )
        self._embedder = embedder or EmbeddingProvider()
        self._config = config if config is not None else SkillsConfig()
        self._governor_cache: SkillGovernor | None = None
        self._retrieval_cache: RetrievalPipeline | None = None
        self._negative_cache: NegativeKnowledgeStore | None = None
        self._graph_cache: SkillGraph | None = None
        self._evolver_cache: SkillEvolver | None = None

    @property
    def config(self) -> SkillsConfig:
        """Return the skills configuration this library was built with."""
        return self._config

    def governor(self) -> SkillGovernor:
        """Return the admission gate and lifecycle manager."""
        from .governor import SkillGovernor

        if self._governor_cache is None:
            self._governor_cache = SkillGovernor(
                store=self._sqlite,
                library=self,
                config=self._config,
            )
        return self._governor_cache

    def negative(self) -> NegativeKnowledgeStore:
        """Return the store-bound negative-knowledge accessor."""
        if self._negative_cache is None:
            self._negative_cache = NegativeKnowledgeStore(self._sqlite)
        return self._negative_cache

    def graph(self) -> SkillGraph:
        """Return the store-bound graph accessor."""
        if self._graph_cache is None:
            self._graph_cache = SkillGraph(self._sqlite, self._root / "skills")
        return self._graph_cache

    def evolver(self) -> SkillEvolver:
        """Return the store-bound evolver accessor."""
        if self._evolver_cache is None:
            self._evolver_cache = SkillEvolver(self._sqlite)
        return self._evolver_cache

    def retrieval(self) -> RetrievalPipeline:
        """Return the staged retrieval pipeline."""
        from .retrieval import RetrievalPipeline

        if self._retrieval_cache is None:
            self._retrieval_cache = RetrievalPipeline(
                store=self._sqlite,
                library=self,
                embedder=self._embedder,
                config=self._config,
            )
        return self._retrieval_cache

    async def add_skill(
        self,
        markdown_text: str,
        *,
        overwrite: bool = False,
    ) -> str:
        """Parse a raw SKILL.md string, generate its embedding, and store it.

        When ``config.admission_strict`` is ``True``, the skill is
        additionally run through the governor's admission gate. A
        rejection removes the just-written file and raises
        ``STORAGE_SCHEMA_INVALID`` with the gate's findings attached.
        When the flag is ``False``, the method behaves exactly as it
        did in Phase 12 v1.

        Args:
            markdown_text: The full contents of a SKILL.md file.
            overwrite: If True, replace an existing skill.

        Returns:
            The name of the skill that was added.

        Raises:
            CodeGuardianError: With code ``STORAGE_SCHEMA_INVALID`` if
                the markdown is malformed, fails schema validation, or
                is rejected by the admission gate.
        """
        opening = "---\n"
        closing = "\n---\n"
        if not markdown_text.startswith(opening):
            raise CodeGuardianError(
                ErrorCode.STORAGE_SCHEMA_INVALID,
                "Skill markdown must start with '---\\n'.",
            )
        end = markdown_text.find(closing, len(opening))
        if end == -1:
            raise CodeGuardianError(
                ErrorCode.STORAGE_SCHEMA_INVALID,
                "Skill markdown is missing the closing '---' delimiter.",
            )

        yaml_text = markdown_text[len(opening) : end]
        body = markdown_text[end + len(closing) :].lstrip("\n")

        try:
            frontmatter = yaml.safe_load(yaml_text)
        except yaml.YAMLError as exc:
            raise CodeGuardianError(
                ErrorCode.STORAGE_SCHEMA_INVALID,
                f"Skill frontmatter is not valid YAML: {exc}",
            ) from exc

        if not isinstance(frontmatter, dict) or "name" not in frontmatter:
            raise CodeGuardianError(
                ErrorCode.STORAGE_SCHEMA_INVALID,
                "Skill frontmatter must be a mapping and must contain a 'name'.",
            )

        name = str(frontmatter["name"])

        embedding = await self._embedder.embed_text(body)

        self._store.add(
            name=name,
            frontmatter=frontmatter,
            body=body,
            embedding=embedding,
            overwrite=overwrite,
        )

        if self._config.admission_strict:
            verdict = self.governor().admit(name, frontmatter, body)
            if not verdict.accepted:
                with contextlib.suppress(CodeGuardianError):
                    self._store.remove(name)
                raise CodeGuardianError(
                    ErrorCode.STORAGE_SCHEMA_INVALID,
                    f"Skill {name!r} rejected at admission: {verdict.reason}",
                    details={
                        "name": name,
                        "reason": verdict.reason,
                        "findings": list(verdict.blocking_findings),
                    },
                )

        return name

    async def promote_skill(self, name: str) -> None:
        """Transition a skill from 'draft' to 'active'.

        Args:
            name: The skill name.

        Raises:
            CodeGuardianError: With code ``STORAGE_SCHEMA_INVALID`` if
                the skill does not exist or is not in the 'draft' state.
        """
        record = self._store.get(name)
        if record is None:
            raise CodeGuardianError(
                ErrorCode.STORAGE_SCHEMA_INVALID,
                f"Skill not found: {name}",
            )

        frontmatter = record["frontmatter"]
        current_state = frontmatter.get("state", "draft")
        if current_state != "draft":
            raise CodeGuardianError(
                ErrorCode.STORAGE_SCHEMA_INVALID,
                f"Only 'draft' skills can be promoted. Current state: {current_state!r}",
            )

        frontmatter["state"] = "active"

        self._store.add(
            name=name,
            frontmatter=frontmatter,
            body=record["body"],
            embedding=None,
            overwrite=True,
        )

    async def archive_skill(self, name: str) -> None:
        """Transition a skill to 'archived'.

        Archived skills are removed from search results.

        Args:
            name: The skill name.
        """
        record = self._store.get(name)
        if record is None:
            return

        frontmatter = record["frontmatter"]
        frontmatter["state"] = "archived"

        self._store.add(
            name=name,
            frontmatter=frontmatter,
            body=record["body"],
            embedding=None,
            overwrite=True,
        )

    def set_skill_state(self, name: str, state: str) -> bool:
        """Set a skill's lifecycle state without changing anything else.

        The method is used by the governor for state transitions and by
        future CLI commands for explicit operator actions. It does not
        re-validate the target state against the transition map; the
        caller is expected to check the transition is legal when it
        matters.

        Args:
            name: The skill name.
            state: The desired state.

        Returns:
            True if the state was updated or was already correct,
            False if the skill does not exist.
        """
        record = self._store.get(name)
        if record is None:
            return False
        frontmatter = record["frontmatter"]
        if frontmatter.get("state") == state:
            return True
        frontmatter["state"] = state
        self._store.add(
            name=name,
            frontmatter=frontmatter,
            body=record["body"],
            embedding=None,
            overwrite=True,
        )
        return True

    def get_skill(self, name: str) -> dict[str, Any] | None:
        """Read a skill by name. Proxy to the underlying store."""
        return self._store.get(name)

    def list_skills(self) -> list[dict[str, Any]]:
        """List all skills. Proxy to the underlying store."""
        return self._store.list_all()

    def record_feedback(
        self,
        name: str,
        run_id: str | None,
        outcome: str,
        task_description: str | None = None,
    ) -> None:
        """Record the outcome of a skill use. Proxy to the underlying store."""
        self._store.record_feedback(
            name=name,
            run_id=run_id,
            outcome=outcome,
            task_description=task_description,
        )
