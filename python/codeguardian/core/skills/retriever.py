"""Hybrid skill retrieval for agent prompts.

This module answers the question: "Given the current task, which skills
from the library should we put in the LLM's context window?"

It implements a three-tier progressive disclosure model:
1. Tier 1 (Metadata): Only the name and a short trigger are matched
   during the search phase.
2. Tier 2 (Body): Only the top-ranked skills have their full text
   loaded and formatted for the prompt.
3. Tier 3 (Constraints): Negative constraints (failed skills) are
   prefixed with a warning and injected into the prompt.

Search is hybrid. It combines a semantic vector search with an exact
keyword FTS5 search. The two ranked lists are merged using Reciprocal
Rank Fusion (RRF), ensuring that a skill that matches both semantically
and lexically outranks a skill that only matches one.
"""

from __future__ import annotations

from dataclasses import dataclass

from .embeddings import EmbeddingProvider
from .library import SkillLibrary

__all__ = ["RetrievedSkill", "SkillRetriever"]


@dataclass(frozen=True, slots=True)
class RetrievedSkill:
    """A skill that has been selected for the prompt context.

    Attributes:
        name: The skill name.
        body: The full Markdown body of the skill.
        is_negative: True if this skill represents a failure to avoid.
        score: The final RRF score.
    """

    name: str
    body: str
    is_negative: bool
    score: float


class SkillRetriever:
    """Retrieves and formats skills for the Analyst and Writer agents.

    Args:
        library: The initialized SkillLibrary.
        embedder: The embedding provider.
    """

    def __init__(self, library: SkillLibrary, embedder: EmbeddingProvider) -> None:
        self._library = library
        self._store = library._store  # Accessing the underlying SQLite store
        self._embedder = embedder
        # RRF constant, usually 60 per the original paper.
        self._rrf_k = 60

    async def retrieve_for_prompt(
        self,
        query: str,
        limit: int = 3,
    ) -> str:
        """Retrieve the top skills and format them for an LLM prompt.

        Args:
            query: The directive or task description from the user.
            limit: The maximum number of positive skills to include.

        Returns:
            A formatted string containing the skills, or an empty string
            if no relevant skills were found.
        """
        # 1. Generate the semantic embedding for the query
        query_embedding = await self._embedder.embed_text(query)

        # 2. Fetch candidates from both search methods
        # We fetch more than the limit so we have enough to merge.
        fetch_limit = limit * 3
        semantic_results = self._store.search_semantic(query_embedding, limit=fetch_limit)
        keyword_results = self._store.search_keyword(query, limit=fetch_limit)

        # 3. Merge using Reciprocal Rank Fusion (RRF)
        scores: dict[str, float] = {}
        for rank, row in enumerate(semantic_results):
            name = row["name"]
            scores[name] = scores.get(name, 0.0) + (1.0 / (self._rrf_k + rank + 1))

        for rank, row in enumerate(keyword_results):
            name = row["name"]
            scores[name] = scores.get(name, 0.0) + (1.0 / (self._rrf_k + rank + 1))

        # Sort merged results by RRF score
        sorted_names = sorted(scores.keys(), key=lambda n: scores[n], reverse=True)

        # 4. Fetch full bodies and filter out drafts/archived
        selected_positives: list[RetrievedSkill] = []
        selected_negatives: list[RetrievedSkill] = []

        for name in sorted_names:
            if len(selected_positives) >= limit and len(selected_negatives) >= 2:
                break

            record = self._library.get_skill(name)
            if record is None:
                continue

            frontmatter = record["frontmatter"]
            state = frontmatter.get("state", "active")
            if state in ("draft", "archived"):
                continue

            # A skill is negative if its effectiveness is very low OR if
            # its name explicitly indicates it's a negative constraint.
            effectiveness = float(frontmatter.get("effectiveness", 0.5))
            is_negative = effectiveness < 0.3 or "avoid" in name or "failure" in name

            skill = RetrievedSkill(
                name=name,
                body=record["body"],
                is_negative=is_negative,
                score=scores[name],
            )

            if is_negative:
                if len(selected_negatives) < 2:
                    selected_negatives.append(skill)
            else:
                if len(selected_positives) < limit:
                    selected_positives.append(skill)

        # 5. Format for the prompt
        return self._format_prompt(selected_positives, selected_negatives)

    def _format_prompt(
        self,
        positives: list[RetrievedSkill],
        negatives: list[RetrievedSkill],
    ) -> str:
        """Format retrieved skills into a prompt string."""
        if not positives and not negatives:
            return ""

        parts: list[str] = ["## Relevant Skills and Constraints\n"]

        if negatives:
            parts.append("### NEGATIVE CONSTRAINTS (DO NOT DO THESE)")
            parts.append(
                "The following patterns failed in previous runs. Avoid using these approaches.\n"
            )
            for neg in negatives:
                parts.append(f"#### AVOID: {neg.name}")
                parts.append(neg.body.strip())
                parts.append("")

        if positives:
            parts.append("### RECOMMENDED PATTERNS")
            for pos in positives:
                parts.append(f"#### PATTERN: {pos.name}")
                parts.append(pos.body.strip())
                parts.append("")

        return "\n".join(parts)
