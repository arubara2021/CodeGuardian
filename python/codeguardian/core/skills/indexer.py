"""Background maintenance for the skill library.

Three maintenance passes live here. Two were added in Phase 12 v2:

1. ``rebuild_missing_embeddings`` (v1). Scans every skill and
   generates the embedding for any that lack one in the vector index.

2. ``decay_stale_skills`` (v1). Marks skills whose last use is older
   than the TTL as stale.

3. ``rebuild_graph`` (v2). Rebuilds the procedural graph from the
   Markdown skill files. Never touches the vectors, never touches the
   skill rows.

4. ``apply_forgetting_curve`` (v2). Decays negative records and
   optionally retires low-fitness skills. The negative decay is
   independent of the skill TTL: a failure record and a positive skill
   do not age on the same schedule.

The two v2 methods are wrapped so that they degrade rather than raise
when the graph store or the negative store is unavailable. The
maintenance layer runs on a timer or a background thread; a failing
pass should not stop the rest of the process.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.storage.skill_store import SkillStore

from .embeddings import EmbeddingProvider

if TYPE_CHECKING:
    from .governor import SkillGovernor

__all__ = ["SkillIndexer"]

logger = logging.getLogger(__name__)


class SkillIndexer:
    """Maintains the SQLite index, the graph, and the negative store.

    Args:
        store: The Phase 3 ``SkillStore``.
        embedder: The embedding provider.
        ttl_days: Number of days before a skill is considered stale.
            Defaults to 90.
    """

    def __init__(
        self,
        store: SkillStore,
        embedder: EmbeddingProvider,
        ttl_days: int = 90,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._ttl_days = ttl_days

    async def rebuild_missing_embeddings(self) -> int:
        """Scan all skills and generate embeddings for any that are missing.

        This is called at startup. If a user manually drops a new
        SKILL.md file into the skills directory, it will not have an
        embedding in SQLite until this runs.

        Returns:
            The number of embeddings generated.
        """
        all_skills = self._store.list_all()
        missing: list[dict] = []

        for skill in all_skills:
            name = skill["name"]
            rows = self._store._sqlite.query(
                "SELECT 1 FROM skill_embeddings WHERE name = ? LIMIT 1",
                (name,),
            )
            if not rows:
                record = self._store.get(name)
                if record:
                    missing.append({"name": name, "body": record["body"]})

        if not missing:
            return 0

        logger.info("Generating embeddings for %d missing skills...", len(missing))

        bodies = [m["body"] for m in missing]
        try:
            vectors = await self._embedder.embed_batch(bodies)
        except CodeGuardianError as exc:
            logger.error("Failed to generate missing embeddings: %s", exc.message)
            return 0

        count = 0
        for item, vector in zip(missing, vectors, strict=False):
            try:
                import json as _json

                self._store._sqlite.execute(
                    "INSERT OR REPLACE INTO skill_embeddings(name, embedding) VALUES (?, ?)",
                    (item["name"], _json.dumps(vector)),
                )
                count += 1
            except Exception as exc:
                logger.warning("Failed to insert embedding for %s: %s", item["name"], exc)

        return count

    def decay_stale_skills(self) -> int:
        """Mark skills as 'stale' if they have not been used recently.

        Returns:
            The number of skills marked as stale.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=self._ttl_days)).isoformat()
        cutoff = cutoff.replace("+00:00", "Z")

        query = """
            SELECT name FROM skills
            WHERE state IN ('active', 'draft')
            AND (
                (last_used_at IS NOT NULL AND last_used_at < ?)
                OR
                (last_used_at IS NULL AND created_at < ?)
            )
        """
        rows = self._store._sqlite.query(query, (cutoff, cutoff))
        if not rows:
            return 0

        names = [row["name"] for row in rows]
        placeholders = ",".join(["?"] * len(names))

        self._store._sqlite.execute(
            f"UPDATE skills SET state = 'stale' WHERE name IN ({placeholders})",
            tuple(names),
        )

        logger.info("Decayed %d skills to 'stale' status.", len(names))
        return len(names)

    def rebuild_graph(
        self,
        skills_dir: Path | None = None,
        embeddings: dict[str, list[float]] | None = None,
        *,
        semantic_threshold: float = 0.85,
    ) -> tuple[int, int]:
        """Rebuild the procedural skill graph from the Markdown files.

        Args:
            skills_dir: The directory holding one subdirectory per
                skill. Defaults to ``<store root>/skills``.
            embeddings: An optional mapping from skill name to
                embedding vector. Skills without an embedding still
                participate in authored edges but not in semantic edges.
            semantic_threshold: The cosine similarity threshold above
                which two skills receive a semantic edge.

        Returns:
            A tuple of ``(node_count, edge_count)`` describing the
            graph after the rebuild. Returns ``(0, 0)`` if the rebuild
            failed; the previous graph is left untouched on failure
            because ``drop_and_rebuild`` deletes only after it has read
            every file it can parse.
        """
        from .graph_store import drop_and_rebuild

        target_dir = (
            Path(skills_dir) if skills_dir is not None else Path(self._store.root) / "skills"
        )
        try:
            return drop_and_rebuild(
                self._store._sqlite,
                target_dir,
                embeddings,
                semantic_threshold=semantic_threshold,
            )
        except CodeGuardianError as exc:
            logger.warning("Graph rebuild failed: %s", exc.message)
            return (0, 0)

    def apply_forgetting_curve(
        self,
        governor: SkillGovernor | None = None,
        *,
        decay_factor: float = 0.9,
        reference_days: int = 30,
        min_confidence: float = 0.1,
        max_per_skill: int = 20,
    ) -> tuple[int, int]:
        """Decay negative records and optionally retire low-fitness skills.

        Args:
            governor: When supplied, the governor's retirement pass
                also runs and the second return value counts the
                retired skills. When ``None``, only the negative store
                is decayed and the second value is zero.
            decay_factor: The per-period multiplier for record
                confidence. Between zero and one.
            reference_days: The age at which one decay step is applied.
            min_confidence: The floor below which a record is deleted.
            max_per_skill: The maximum number of records retained for
                any one skill.

        Returns:
            A tuple of ``(negatives_deleted, skills_retired)``.
        """
        from .negative import apply_forgetting_curve as _neg_decay

        try:
            deleted = _neg_decay(
                self._store._sqlite,
                decay_factor=decay_factor,
                reference_days=reference_days,
                min_confidence=min_confidence,
                max_per_skill=max_per_skill,
            )
        except Exception as exc:
            logger.warning("Negative knowledge decay failed: %s", exc)
            deleted = 0

        retired: list[str] = []
        if governor is not None:
            try:
                retired = governor.apply_retirement()
            except Exception as exc:
                logger.warning("Governance retirement failed: %s", exc)

        return deleted, len(retired)
