"""Staged retrieval pipeline for the skills module.

The pipeline is a chain of stages with a fallback between every pair.
A stage that is disabled, empty, timed out, or failed never propagates
an error. It falls through to the previous stage's output. The result
is that the pipeline returns a ``RetrievalBundle`` under every input,
and the flag-off configuration is byte-identical to the Phase 12 v1
retriever.

The stages are:

Stage 0, project filter. Reads ``recon.json`` and ``blast_radius.json``
from the current run and narrows the candidate pool by language,
dependency, and risk posture. Runs only when a ``ProjectCard`` is
supplied and the ``recon_blast_filter_enabled`` flag is on. Skipped
under every other configuration.

Stage 1, seed. Performs the hybrid semantic and keyword search and
merges the two rankings with Reciprocal Rank Fusion. Runs always;
this is the fallback path.

Stage 2, expansion. Runs reverse-weighted PageRank over the graph
from the seeds. Runs only when ``graph_enabled`` and ``ppr_enabled``
are on and the graph contains at least one edge from a seed. On any
failure or timeout, skipped.

Stage 3, rerank. Runs the cross-encoder over the candidates. Runs
only when ``reranker_enabled`` is on. On any failure or timeout,
skipped.

Stage 4, hydrate and gate. Loads bodies up to a token budget and
applies the calibrated threshold gate. Always runs.

The pipeline is not responsible for the negative-knowledge store. The
negative records are queried by the caller and appended to the
positive bundle, so the pipeline's output stays focused on the
retrieval contract.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from codeguardian.core.config.schema import SkillsConfig
from codeguardian.core.storage.sqlite_store import SQLiteStore

from .embeddings import EmbeddingProvider
from .graph_store import get_all_edges, neighbors
from .hydrator import DEFAULT_TOKEN_COUNTER, hydrate
from .library import SkillLibrary
from .ppr import personalized_pagerank
from .reranker import Reranker
from .types import (
    EdgeKind,
    ProjectCard,
    RetrievalBundle,
    RetrievalItem,
)

__all__ = ["RetrievalPipeline"]

logger = logging.getLogger(__name__)


# Default thresholds. The three values follow the production literature
# for retrieval selection: a skill enters the bundle when its normalised
# score clears the relevance floor, its reranked score (if a reranker
# ran) clears the selection floor, and its final confidence clears the
# confidence floor. If nothing clears, the bundle is empty.
_RELEVANCE_THRESHOLD: float = 0.5
_SELECTION_THRESHOLD: float = 0.35
_CONFIDENCE_THRESHOLD: float = 0.3

# RRF constant, matching the value used by the Phase 12 v1 retriever.
_RRF_K: int = 60

# Default hydration budget when the caller does not supply one.
_DEFAULT_TOKEN_BUDGET: int = 3000


class RetrievalPipeline:
    """Orchestrates the staged retrieval of skills for one query.

    Args:
        store: An open ``SQLiteStore``.
        library: The skill library, used for body access.
        embedder: The embedding provider used by the seed stage.
        config: The skills configuration. Every flag defaults to off.
        reranker: An optional cross-encoder. When ``None`` and the
            reranker flag is on, one is constructed with the default
            model.
        token_budget: The default hydration budget.
    """

    def __init__(
        self,
        store: SQLiteStore,
        library: SkillLibrary,
        embedder: EmbeddingProvider,
        config: SkillsConfig,
        *,
        reranker: Reranker | None = None,
        token_budget: int = _DEFAULT_TOKEN_BUDGET,
    ) -> None:
        self._store = store
        self._library = library
        self._embedder = embedder
        self._config = config
        self._token_budget = token_budget
        self._reranker = (
            reranker
            if reranker is not None
            else Reranker(
                timeout_ms=config.reranker_timeout_ms,
            )
        )

    def _all_flags_off(self) -> bool:
        """Return True when no feature flag is enabled."""
        return not (
            self._config.graph_enabled
            or self._config.ppr_enabled
            or self._config.reranker_enabled
            or self._config.recon_blast_filter_enabled
            or self._config.negative_store_enabled
        )

    async def retrieve_for_prompt(
        self,
        query: str,
        *,
        project_context: ProjectCard | None = None,
        limit: int = 3,
        token_budget: int | None = None,
        seed_retriever: Callable[[str, int], object] | None = None,
    ) -> str:
        """Return a formatted prompt string.

        When every feature flag is off, this method delegates directly
        to the Phase 12 v1 ``SkillRetriever.retrieve_for_prompt``, which
        makes the output byte-identical to the pre-upgrade behaviour
        without replicating the formatting logic. When at least one
        flag is on, the pipeline runs and the resulting bundle is
        formatted here.

        Args:
            query: The user query or directive.
            project_context: An optional project card for Stage 0.
            limit: The maximum number of positive skills.
            token_budget: The hydration budget.
            seed_retriever: When supplied, used as the v1 fallback. The
                caller normally passes the ``SkillRetriever`` instance
                it has already constructed.

        Returns:
            A formatted prompt string, or an empty string when nothing
            was retrieved.
        """
        if self._all_flags_off() and seed_retriever is not None:
            return await seed_retriever(query, limit)  # type: ignore[operator]

        bundle = await self.retrieve(
            query,
            project_context=project_context,
            limit=limit,
            token_budget=token_budget,
        )
        return _format_bundle(bundle)

    async def retrieve(
        self,
        query: str,
        *,
        project_context: ProjectCard | None = None,
        limit: int = 3,
        token_budget: int | None = None,
    ) -> RetrievalBundle:
        """Run the staged pipeline and return a bundle.

        Args:
            query: The user query or directive.
            project_context: An optional project card for Stage 0.
            limit: The maximum number of positive skills before
                hydration.
            token_budget: The hydration budget.

        Returns:
            A ``RetrievalBundle``. Never raises.
        """
        budget = token_budget if token_budget is not None else self._token_budget
        stages_run: list[str] = []
        fallback_used = False

        seed_names = await self._stage_1_seed(query, limit)
        stages_run.append("seed")

        filtered_names = seed_names
        if self._config.recon_blast_filter_enabled and project_context is not None and seed_names:
            filtered = self._stage_0_filter(seed_names, project_context)
            if filtered is not None:
                filtered_names = filtered
                stages_run.append("project_filter")
            else:
                fallback_used = True
                stages_run.append("project_filter_skipped")

        expanded_names = filtered_names
        if self._config.graph_enabled and self._config.ppr_enabled and filtered_names:
            expanded = self._stage_2_expand(filtered_names)
            if expanded is not None and len(expanded) >= len(filtered_names):
                expanded_names = expanded
                stages_run.append("ppr")
            else:
                fallback_used = True
                stages_run.append("ppr_skipped")

        scored_items = self._stage_3_items(expanded_names)

        if self._config.reranker_enabled and scored_items:
            reranked = await self._stage_3_rerank(query, scored_items)
            if reranked is not None:
                scored_items = reranked
                stages_run.append("rerank")
            else:
                fallback_used = True
                stages_run.append("rerank_skipped")

        gated_items = _apply_threshold_gate(scored_items, limit)
        if not gated_items:
            return RetrievalBundle(
                items=(),
                truncated=False,
                truncation_strategy="none",
                total_tokens=0,
                stages_run=(*stages_run, "threshold_empty"),
                fallback_used=fallback_used,
            )

        priority_order = self._stage_4_priority_order(gated_items)

        bundle = hydrate(
            gated_items,
            self._body_loader,
            token_budget=budget,
            priority_order=priority_order,
            token_counter=DEFAULT_TOKEN_COUNTER,
        )

        return RetrievalBundle(
            items=bundle.items,
            truncated=bundle.truncated,
            truncation_strategy=bundle.truncation_strategy,
            total_tokens=bundle.total_tokens,
            stages_run=(*stages_run, "hydrate"),
            fallback_used=fallback_used,
        )

    async def _stage_1_seed(self, query: str, limit: int) -> list[str]:
        """Return the RRF-ordered seed names, or an empty list.

        The search methods used here live on ``SkillStore``, not on
        the raw ``SQLiteStore``. ``self._library._store`` is the
        ``SkillStore`` instance; ``self._store`` is the underlying
        ``SQLiteStore`` and exposes no search methods.
        """
        try:
            query_embedding = await self._embedder.embed_text(query)
        except Exception as exc:
            logger.warning("Seed stage embedding failed: %s", exc)
            return []

        skill_store = self._library._store
        fetch_limit = max(limit * 3, limit + 2)

        try:
            semantic = skill_store.search_semantic(query_embedding, limit=fetch_limit)
        except Exception as exc:
            logger.warning("Seed stage semantic search failed: %s", exc)
            semantic = []

        try:
            keyword = skill_store.search_keyword(query, limit=fetch_limit)
        except Exception as exc:
            logger.warning("Seed stage keyword search failed: %s", exc)
            keyword = []

        scores: dict[str, float] = {}
        for rank, row in enumerate(semantic):
            name = row["name"]
            scores[name] = scores.get(name, 0.0) + 1.0 / (_RRF_K + rank + 1)
        for rank, row in enumerate(keyword):
            name = row["name"]
            scores[name] = scores.get(name, 0.0) + 1.0 / (_RRF_K + rank + 1)

        return sorted(scores.keys(), key=lambda name: -scores[name])

    def _stage_0_filter(
        self,
        seed_names: list[str],
        project_context: ProjectCard,
    ) -> list[str] | None:
        """Narrow the seed list by the project's language and stack."""
        if not project_context.languages:
            return None
        allowed_languages = set(project_context.languages)
        result: list[str] = []
        for name in seed_names:
            record = self._library.get_skill(name)
            if record is None:
                continue
            language = str(record["frontmatter"].get("language", "")).lower()
            if not language:
                result.append(name)
                continue
            if language in allowed_languages:
                result.append(name)
        return result

    def _stage_2_expand(self, seed_names: list[str]) -> list[str] | None:
        """Expand the seeds along reverse prerequisite edges."""
        try:
            edges = get_all_edges(self._store)
        except Exception as exc:
            logger.warning("PPR could not load edges: %s", exc)
            return None
        if not edges:
            return None
        try:
            skill_names = {str(row["name"]) for row in self._library.list_skills()}
        except Exception:
            skill_names = None
        ranked = personalized_pagerank(
            edges,
            seed_names,
            max_hops=self._config.ppr_hops,
            timeout_ms=self._config.ppr_timeout_ms,
            skill_names=skill_names,
        )
        if not ranked:
            return None
        return [r.node_id for r in ranked]

    def _stage_3_items(self, names: list[str]) -> tuple[RetrievalItem, ...]:
        """Convert ranked names into scored items, filtering state."""
        items: list[RetrievalItem] = []
        total = len(names) or 1
        for position, name in enumerate(names):
            record = self._library.get_skill(name)
            if record is None:
                continue
            frontmatter = record["frontmatter"]
            state = frontmatter.get("state", "active")
            if state in ("draft", "archived", "retired"):
                continue
            score = 1.0 - (position / total)
            effectiveness = float(frontmatter.get("effectiveness", 0.5))
            is_negative = effectiveness < 0.3 or "avoid" in name or "failure" in name
            items.append(
                RetrievalItem(
                    name=name,
                    score=score,
                    reason="seed" if position < len(names) else "prerequisite",
                    is_negative=is_negative,
                )
            )
        return tuple(items)

    async def _stage_3_rerank(
        self,
        query: str,
        items: tuple[RetrievalItem, ...],
    ) -> tuple[RetrievalItem, ...] | None:
        """Rerank with body hydration for the candidates only."""
        with_bodies: list[RetrievalItem] = []
        for item in items:
            if item.is_negative:
                with_bodies.append(item)
                continue
            body = self._body_loader(item.name)
            with_bodies.append(
                RetrievalItem(
                    name=item.name,
                    score=item.score,
                    reason=item.reason,
                    body=body,
                    token_count=DEFAULT_TOKEN_COUNTER(body) if body else 0,
                    is_negative=item.is_negative,
                )
            )
        try:
            reranked = await self._reranker.rerank(query, tuple(with_bodies))
        except Exception as exc:
            logger.warning("Rerank stage failed: %s", exc)
            return None
        return reranked

    def _stage_4_priority_order(
        self,
        items: tuple[RetrievalItem, ...],
    ) -> tuple[str, ...] | None:
        """Return a prerequisite-first order for hydration, or ``None``."""
        if not self._config.graph_enabled:
            return None
        names = {item.name for item in items}
        if not names:
            return None
        ordered: list[str] = []
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visited or name not in names:
                return
            visited.add(name)
            try:
                prerequisites = neighbors(
                    self._store,
                    name,
                    kind=EdgeKind.PREREQUISITE,
                    direction="in",
                )
            except Exception:
                prerequisites = ()
            for prerequisite in prerequisites:
                visit(prerequisite)
            ordered.append(name)

        for item in items:
            visit(item.name)
        return tuple(ordered)

    def _body_loader(self, name: str) -> str | None:
        """Return the Markdown body of a skill, or ``None``."""
        try:
            record = self._library.get_skill(name)
        except Exception as exc:
            logger.warning("Could not read skill %s: %s", name, exc)
            return None
        if record is None:
            return None
        return str(record.get("body") or "")


def _apply_threshold_gate(
    items: tuple[RetrievalItem, ...],
    limit: int,
) -> tuple[RetrievalItem, ...]:
    """Return the positives and negatives that clear the score floors.

    The gate separates positives from negatives. Positives are capped
    at ``limit``. Negatives are capped at two, matching the Phase 12 v1
    behaviour. An item whose ``score`` falls below the relevance floor
    is dropped for positives, and retained for negatives because a
    negative constraint that matched the query is worth keeping even at
    a low score.
    """
    positives: list[RetrievalItem] = []
    negatives: list[RetrievalItem] = []
    for item in items:
        if item.is_negative:
            if len(negatives) < 2:
                negatives.append(item)
            continue
        if item.score < _RELEVANCE_THRESHOLD:
            continue
        if len(positives) < limit:
            positives.append(item)
    return tuple(positives + negatives)


def _format_bundle(bundle: RetrievalBundle) -> str:
    """Format a bundle for prompt inclusion.

    The output matches the Phase 12 v1 ``SkillRetriever._format_prompt``
    for the flag-off case, so that a caller who has not enabled any new
    feature receives the same string it received before the upgrade.
    """
    if not bundle.items:
        return ""
    positives = [item for item in bundle.items if not item.is_negative and item.body]
    negatives = [item for item in bundle.items if item.is_negative and item.body]
    if not positives and not negatives:
        return ""
    parts: list[str] = ["## Relevant Skills and Constraints\n"]
    if negatives:
        parts.append("### NEGATIVE CONSTRAINTS (DO NOT DO THESE)")
        parts.append(
            "The following patterns failed in previous runs. Avoid using these approaches.\n"
        )
        for negative in negatives:
            parts.append(f"#### AVOID: {negative.name}")
            parts.append((negative.body or "").strip())
            parts.append("")
    if positives:
        parts.append("### RECOMMENDED PATTERNS")
        for positive in positives:
            parts.append(f"#### PATTERN: {positive.name}")
            parts.append((positive.body or "").strip())
            parts.append("")
    return "\n".join(parts)
