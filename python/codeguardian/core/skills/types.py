"""Data types shared across the Phase 12 skills module.

This module defines every structured value that crosses a boundary
inside the skills package. It contains frozen dataclasses and one
enum, and no executable logic beyond two trivial factories. Nothing
here imports from another CodeGuardian module, and nothing here
performs I/O.

The types divide into three groups:

Graph types. ``SkillNode``, ``SkillEdge``, and ``EdgeKind`` model the
procedural dependency graph. Nodes are either skills or capability
labels. Edges are typed and carry a deterministic identifier so that
rebuilding the graph from the same inputs produces byte-identical
tables.

Retrieval types. ``RetrievalItem`` and ``RetrievalBundle`` are the
output of the staged retrieval pipeline. A bundle records not only
what was retrieved but which stages executed, whether a fallback was
taken, and how truncation was handled. That information lets the
caller distinguish an empty result from a degraded result.

Governance and provenance types. ``ProvenancePin``, ``AdmissionVerdict``,
and ``NegativeRecord`` describe where a skill came from, whether it was
allowed into the active pool, and why a specific skill attempt failed
in a specific context.

Frozen instances protect against accidental mutation. Mapping-typed
fields are wrapped in ``MappingProxyType`` in ``__post_init__`` so a
caller cannot mutate the underlying dictionary through the returned
dataclass.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

__all__ = [
    "AdmissionVerdict",
    "EdgeKind",
    "NegativeRecord",
    "ProjectCard",
    "ProvenancePin",
    "RetrievalBundle",
    "RetrievalItem",
    "SkillEdge",
    "SkillNode",
    "compute_edge_id",
    "empty_bundle",
]


class EdgeKind(str, Enum):
    """The kind of relationship a ``SkillEdge`` represents.

    The four authored kinds come from frontmatter declarations. The
    two derived kinds are produced by the graph builder from embedding
    similarity or execution traces.
    """

    PREREQUISITE = "prerequisite"
    PROVIDES = "provides"
    CONSUMES = "consumes"
    SUPERSEDES = "supersedes"
    SEMANTIC = "semantic"
    CONFLICTS = "conflicts"


def compute_edge_id(src: str, dst: str, kind: EdgeKind | str) -> str:
    """Return a deterministic identifier for a typed edge.

    The identifier is derived from the source, destination, and kind
    only. Two calls with the same inputs return the same string, which
    is what allows the graph to be rebuilt from scratch and compared
    byte for byte against a stored graph.

    Args:
        src: The source node identifier.
        dst: The destination node identifier.
        kind: The edge kind, as an ``EdgeKind`` or its string value.

    Returns:
        A string of the form ``edge:<24 hex chars>``.
    """
    kind_value = kind.value if isinstance(kind, EdgeKind) else str(kind)
    digest = hashlib.sha256(f"{src}|{dst}|{kind_value}".encode()).hexdigest()
    return f"edge:{digest[:24]}"


@dataclass(frozen=True, slots=True)
class SkillNode:
    """One node in the procedural skill graph.

    A node is either a skill (``kind="skill"``) or a capability label
    (``kind="capability"``). Capability nodes are shared across skills
    that provide or consume the same capability, which is what lets
    the graph connect a skill to a task requirement without naming a
    specific skill.

    Attributes:
        node_id: A stable identifier. Skill nodes use the skill name
            directly. Capability nodes use ``cap:<capability name>``.
        kind: Either ``"skill"`` or ``"capability"``.
        name: The human-readable name of the node.
        metadata: Arbitrary key-value metadata used by the graph
            builder and by future consumers. Frozen after construction.
    """

    node_id: str
    kind: str
    name: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class SkillEdge:
    """One typed edge between two nodes in the skill graph.

    Attributes:
        edge_id: Deterministic identifier, normally produced by
            ``compute_edge_id``.
        src: The source node identifier.
        dst: The destination node identifier.
        kind: The typed relationship.
        weight: A relative weight used by the PageRank traversal. One
            is the default. Semantic edges carry a weight derived from
            cosine similarity. Execution-derived edges may carry a
            weight derived from observed co-occurrence frequency.
        provenance: A short string identifying where the edge came
            from. Values include ``"frontmatter:prerequisites"``,
            ``"embedding:semantic"``, and ``"trace:cooccurrence"``.
    """

    edge_id: str
    src: str
    dst: str
    kind: EdgeKind
    weight: float = 1.0
    provenance: str = ""


@dataclass(frozen=True, slots=True)
class RetrievalItem:
    """One skill selected by the retrieval pipeline.

    Attributes:
        name: The skill name.
        score: The final score after all stages that ran. A higher
            score means higher relevance to the query.
        reason: A short string describing why the item was included.
            Values include ``"seed"``, ``"prerequisite_of:<name>"``,
            ``"reranked"``, and ``"negative_adopted"``.
        body: The hydrated Markdown body, or ``None`` if the item was
            selected but the caller asked for metadata only.
        token_count: The token count of the body, or zero when not
            hydrated.
        is_negative: True when the item represents a negative
            constraint record rather than a positive skill.
    """

    name: str
    score: float
    reason: str
    body: str | None = None
    token_count: int = 0
    is_negative: bool = False


@dataclass(frozen=True, slots=True)
class RetrievalBundle:
    """The result of one retrieval call.

    A bundle is always returned, even when nothing was retrieved. The
    ``stages_run`` field records which pipeline stages executed, which
    lets a caller distinguish an empty library from a stage that was
    skipped because its feature flag was off, and from a stage that
    degraded because of a timeout.

    Attributes:
        items: The selected items in priority order.
        truncated: True when hydration stopped before loading every
            selected item because the token budget was reached.
        truncation_strategy: The name of the strategy applied when
            truncation happened. One of ``"none"``, ``"prerequisite_priority"``,
            or ``"score_order"``.
        total_tokens: The sum of ``token_count`` across hydrated items.
        stages_run: The names of the pipeline stages that executed.
        fallback_used: True when at least one stage degraded to a
            fallback path.
    """

    items: tuple[RetrievalItem, ...] = ()
    truncated: bool = False
    truncation_strategy: str = "none"
    total_tokens: int = 0
    stages_run: tuple[str, ...] = ()
    fallback_used: bool = False


def empty_bundle(reason: str = "empty") -> RetrievalBundle:
    """Return an empty bundle tagged with the given reason.

    Args:
        reason: A short label recorded in ``stages_run``. Callers use
            this to distinguish "no skills match" from "feature disabled"
            from "stage failed and no fallback was available".

    Returns:
        A ``RetrievalBundle`` with no items and one entry in
        ``stages_run``.
    """
    return RetrievalBundle(stages_run=(reason,))


@dataclass(frozen=True, slots=True)
class ProvenancePin:
    """A record of where a skill came from and how it was admitted.

    Provenance is written at admission time and read during security
    investigations and rollback. The subset of this record that is
    persisted inside ``SKILL.md`` frontmatter is smaller: only
    ``source_runs``, ``authored_by``, ``commit_hash``, and ``license``.
    The admission evidence and reviewer fields live only in the
    ``skill_provenance`` table.

    Attributes:
        source_runs: Run identifiers where the skill was proposed.
        authored_by: ``"skill_curator"`` or ``"human"``.
        commit_hash: The repository HEAD at proposal time.
        license: The declared or inherited license identifier.
        schema_hash: The hash of the ``SKILL.md`` schema the skill
            validated against.
        scan_result: The admission scanner's verdict, ``"clean"`` or
            ``"warn"`` or ``"block"``.
        eval_score: The eval harness score, or ``None`` when the gate
            was skipped.
        reviewer: ``"auto"`` or ``"human"``.
        reviewer_id: A human reviewer identifier, or ``None``.
        kernel_version: The kernel version active at admission.
        admission_evidence: Additional evidence captured by the
            admission gate. Frozen after construction.
    """

    source_runs: tuple[str, ...] = ()
    authored_by: str = "auto"
    commit_hash: str = ""
    license: str = ""
    schema_hash: str = ""
    scan_result: str = ""
    eval_score: float | None = None
    reviewer: str = "auto"
    reviewer_id: str | None = None
    kernel_version: str = ""
    admission_evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "admission_evidence",
            MappingProxyType(dict(self.admission_evidence)),
        )


@dataclass(frozen=True, slots=True)
class NegativeRecord:
    """A typed failure record kept in the negative-knowledge store.

    A negative record is not a low-effectiveness skill. It is a
    distinct object describing a specific failure in a specific
    context. The agent that receives it explicitly adopts or rejects
    it before the next attempt; the adoption counters on the record
    track those decisions over time.

    Attributes:
        failure_id: A UUID.
        skill_name: The skill that was attempted.
        failure_category: One of ``"localization"``, ``"generation"``,
            ``"schema"``, ``"contract"``, ``"timeout"``,
            ``"context_overflow"``, ``"model_unavailable"``.
        context_signature: A deterministic hash of the directive verb,
            stack, and target shape at the time of failure.
        attempted: Free text describing what the skill attempted.
        observed: Free text describing what actually happened.
        run_id: The run in which the failure was observed.
        confidence: The confidence of the failure classification in
            the range 0 to 1.
        first_seen: ISO 8601 timestamp of the first occurrence.
        last_seen: ISO 8601 timestamp of the most recent occurrence.
        provenance: The provenance of the failing run, when available.
        adopted_count: The number of times an agent adopted this
            record as guidance for the next attempt.
        rejected_count: The number of times an agent rejected this
            record.
    """

    failure_id: str
    skill_name: str
    failure_category: str
    context_signature: str
    attempted: str
    observed: str
    run_id: str
    confidence: float
    first_seen: str
    last_seen: str
    provenance: ProvenancePin | None = None
    adopted_count: int = 0
    rejected_count: int = 0


@dataclass(frozen=True, slots=True)
class AdmissionVerdict:
    """The outcome of the admission gate for one candidate skill.

    Attributes:
        accepted: True when the skill may enter the trial state.
        reason: A short human-readable reason for the outcome.
        blocking_findings: The specific findings that caused a
            rejection. Empty when accepted.
        warning_findings: Non-blocking findings. Present whether the
            skill was accepted or rejected.
        scan_result: The admission scanner's overall result. One of
            ``"clean"``, ``"warn"``, or ``"block"``.
        dedupe_status: One of ``"unique"``, ``"near_duplicate"``, or
            ``"duplicate"``.
        near_duplicate_of: The name of the similar existing skill when
            ``dedupe_status`` is not ``"unique"``.
        eval_score: The eval harness score, or ``None`` when the gate
            was skipped.
    """

    accepted: bool
    reason: str
    blocking_findings: tuple[str, ...] = ()
    warning_findings: tuple[str, ...] = ()
    scan_result: str = "clean"
    dedupe_status: str = "unique"
    near_duplicate_of: str | None = None
    eval_score: float | None = None


@dataclass(frozen=True, slots=True)
class ProjectCard:
    """A compact description of the project a retrieval call is for.

    The project card is assembled by the retrieval pipeline from the
    current run's ``recon.json`` and ``blast_radius.json``. It is
    passed to the cross-encoder reranker so the reranker can prefer
    skills that match the project's stack and risk posture.

    Fields whose value in the ``confidence`` mapping is
    ``"inferred"`` must not be treated as hard filters by any
    consumer. Only fields with confidence ``"observed"`` may be used
    to eliminate candidates outright.

    Attributes:
        repo_id: The repository identifier.
        commit_hash: The commit the card describes.
        languages: Languages present in the repository.
        frameworks: Frameworks detected during reconnaissance.
        test_frameworks: Test frameworks detected.
        test_command: The command that runs the project's tests, or
            ``None`` when reconnaissance could not determine one.
        build_system: The build system detected, or ``None``.
        blast_recommendation: The recommendation from the blast
            report for the current target. One of ``"proceed"``,
            ``"review"``, or ``"block"``.
        caller_search_complete: The honesty flag from the blast
            report. ``False`` means callers may be under-reported.
        confidence: A mapping from field name to ``"observed"`` or
            ``"inferred"``. Frozen after construction.
    """

    repo_id: str = ""
    commit_hash: str = ""
    languages: tuple[str, ...] = ()
    frameworks: tuple[str, ...] = ()
    test_frameworks: tuple[str, ...] = ()
    test_command: str | None = None
    build_system: str | None = None
    blast_recommendation: str | None = None
    caller_search_complete: bool | None = None
    confidence: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "confidence", MappingProxyType(dict(self.confidence)))
