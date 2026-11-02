"""Skill Curator agent.

Reviews a completed refactor — successful or failed — and proposes a
reusable procedure worth saving to Phase 12's skill library.

Polarity is derived from the input, not the model's output. On success
the proposal is positive (what to do). On failure the proposal is
negative (what not to do, and why). The model is not asked to
distinguish; it is told what happened via labeled context injected into
its evidence fields, and the polarity tag is applied after the fact.

Every proposal carries state="draft". The Skill Curator never proposes
an active skill. Phase 12 quarantines drafts until evidence and review.

Mining gate. When ``recent_runs`` is supplied, the curator checks
whether the current directive's signature (outcome plus a sorted set of
significant directive words) has appeared at least ``MIN_RUNS_FOR_PROPOSAL``
times in the supplied history. If not, the curator returns no proposal
without calling the model. The source runs that opened the gate are
cited in the result's ``source_runs`` field. When ``recent_runs`` is
None, the gate is bypassed and the curator behaves as before this
block — a single run is enough.

Dedupe. Before emitting a proposal, the curator compares the proposed
skill_name (normalized for dashes, underscores, and case) against the
caller-supplied ``existing_skills``. A match returns no proposal with
``dedupe_status="duplicate"``.

SKILL.md frontmatter. The curator validates that the proposal's
markdown has SKILL.md-conformant frontmatter. The base fields (name,
description, trigger, skill_type) are required. Two optional fields,
allowed-tools and version, are validated when present and added with
safe defaults when absent. The result's markdown is normalized so
downstream consumers always see a portable SKILL.md document.

Eval gate. When ``eval_cases`` is supplied, the curator scores the
proposal's body against the case phrases. A score below
``eval_min_score`` returns no proposal with a warning. When
``eval_cases`` is empty, the gate is skipped and ``eval_score`` is
None.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from dataclasses import replace as _dc_replace
from pathlib import Path
from typing import Any, ClassVar, Literal

import yaml

from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.plugins import BudgetTracker
from codeguardian.core.prompts import (
    SkillProposalOutput,
    render_prompt,
    validate_output,
)
from codeguardian.interfaces.model import ModelOptions, ModelProvider

from ._runtime import tracked_model_call
from .telemetry import record_call

__all__ = [
    "MIN_RUNS_FOR_PROPOSAL",
    "RecentRun",
    "SkillCuratorAgent",
    "SkillCuratorInput",
    "SkillCuratorResult",
    "SkillEvalCase",
]


Outcome = Literal["success", "failure"]
Polarity = Literal["positive", "negative"]
DedupeStatus = Literal["unique", "duplicate"]


MIN_RUNS_FOR_PROPOSAL = 3

_SKILL_NAME_RE = re.compile(r"^[a-z0-9-]+$")

_VALID_SKILL_TYPES = frozenset(
    {
        "refactor_pattern",
        "test_pattern",
        "language_rule",
        "project_convention",
    }
)

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "to",
        "of",
        "for",
        "in",
        "on",
        "with",
        "and",
        "or",
        "is",
        "are",
        "be",
        "it",
    }
)

_DIRECTIVE_WORD_RE = re.compile(r"[a-z]+")


@dataclass(frozen=True, slots=True)
class RecentRun:
    """One recent run the curator can mine for patterns.

    Attributes:
        run_id: The run identifier, cited in the result's source_runs.
        directive: The directive the run applied.
        outcome: "success" or "failure". Callers derive this from the
            run's terminal state; the curator does not interpret
            low-level outcome categories from agent_calls.
        verdict_reason: The verifier's reason when the run failed, or
            None.
        target_symbol: The symbol the run targeted.
    """

    run_id: str
    directive: str
    outcome: str
    verdict_reason: str | None
    target_symbol: str


@dataclass(frozen=True, slots=True)
class SkillEvalCase:
    """One eval case for the skill promotion gate.

    Attributes:
        case_id: A stable identifier for reporting.
        expected_contains: Phrases the proposal's body must contain for
            the case to pass. A case passes when every phrase appears.
    """

    case_id: str
    expected_contains: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SkillCuratorInput:
    """Input to the skill curator agent.

    Attributes:
        recent_runs: When None, the mining gate is bypassed and the
            curator behaves as before this block. When a tuple, the
            gate is enforced: the current directive's signature must
            appear at least MIN_RUNS_FOR_PROPOSAL times for a proposal
            to be emitted.
        existing_skills: Existing skill names for dedupe. Empty means
            no existing skills; the dedupe check trivially passes.
        eval_cases: Eval cases for the promotion gate. Empty means the
            gate is skipped.
        eval_min_score: Threshold below which the eval gate fails.
            Defaults to 0.6.
    """

    __test__: ClassVar[bool] = False

    outcome: Outcome
    directive: str
    target_symbol: str
    target_path: str
    diff: str
    verifier_verdicts: str
    test_results: str
    failure_reason: str
    provider: ModelProvider
    model: str
    budget: BudgetTracker
    clone_root: Path
    prompt_version: str = "1.0.0"
    recent_runs: tuple[RecentRun, ...] | None = None
    existing_skills: tuple[str, ...] = ()
    eval_cases: tuple[SkillEvalCase, ...] = ()
    eval_min_score: float = 0.6


@dataclass(frozen=True, slots=True)
class SkillCuratorResult:
    """Output of the skill curator agent.

    Attributes:
        proposal: The validated SkillProposalOutput, or None when the
            model returned the (none) sentinel, a duplicate skill, an
            invalid proposal, or a failed eval gate.
        polarity: "positive" on success, "negative" on failure, None
            when there is no proposal.
        state: "draft" whenever there is a proposal, None otherwise.
        has_proposal: True iff ``proposal`` is not None.
        is_refusal: True when the model's response violated the schema
            or could not be parsed as JSON.
        refusal_reason: Human-readable reason for the refusal, or None.
        warnings: Non-fatal issues such as a malformed frontmatter or a
            failed eval gate.
        source_runs: Run identifiers from the mined pattern that
            opened the gate. Empty when mining was bypassed or failed.
        eval_score: Score from the eval gate in the range 0 through 1,
            or None when the gate was skipped.
        dedupe_status: "unique" when no existing skill matched,
            "duplicate" when the proposed name matched an existing one.
    """

    proposal: SkillProposalOutput | None
    polarity: Polarity | None
    state: Literal["draft"] | None
    has_proposal: bool
    is_refusal: bool
    refusal_reason: str | None
    warnings: tuple[str, ...]
    source_runs: tuple[str, ...] = ()
    eval_score: float | None = None
    dedupe_status: DedupeStatus = "unique"


class SkillCuratorAgent:
    """Model-backed skill curator agent."""

    name = "skill_curator"
    phase = "skill_curator"
    is_deterministic = False
    requires_tools: tuple[str, ...] = ()
    can_refuse = True
    prompt_role = "skill_curator"
    prompt_version = "1.0.0"

    async def run(self, input: SkillCuratorInput) -> SkillCuratorResult:
        """Run the skill curator agent."""
        source_runs: tuple[str, ...] = ()
        if input.recent_runs is not None:
            mined = _mine_patterns(input.recent_runs)
            current_key = (input.outcome, _directive_signature(input.directive))
            if current_key not in mined:
                return SkillCuratorResult(
                    proposal=None,
                    polarity=None,
                    state=None,
                    has_proposal=False,
                    is_refusal=False,
                    refusal_reason=None,
                    warnings=(
                        "no repeated pattern matches the current directive; "
                        f"need {MIN_RUNS_FOR_PROPOSAL} similar runs",
                    ),
                    source_runs=(),
                )
            source_runs = tuple(run.run_id for run in mined[current_key])

        verifier_verdicts = input.verifier_verdicts
        test_results = input.test_results
        if input.outcome == "failure" and input.failure_reason:
            injected = f"\n\nREFACTOR OUTCOME: FAILURE\nFailure reason: {input.failure_reason}"
            verifier_verdicts = verifier_verdicts + injected
            test_results = test_results + injected

        variables = {
            "directive": input.directive,
            "target_symbol": input.target_symbol,
            "target_path": input.target_path,
            "diff": input.diff,
            "verifier_verdicts": verifier_verdicts,
            "test_results": test_results,
        }
        prompt_result = render_prompt("skill_curator", input.prompt_version, variables)

        response, latency_ms = await tracked_model_call(
            input.provider,
            prompt_result.rendered,
            input.model,
            ModelOptions(),
            "skill_curator",
        )

        try:
            parsed = json.loads(response.text)
        except json.JSONDecodeError as exc:
            _record(
                input,
                prompt_result.rendered,
                response,
                latency_ms,
                outcome="schema_violation",
                failure_category="schema",
            )
            return _schema_refusal(f"invalid JSON: {exc}", source_runs=source_runs)

        try:
            validated = validate_output("skill_curator", parsed)
        except CodeGuardianError as exc:
            _record(
                input,
                prompt_result.rendered,
                response,
                latency_ms,
                outcome="schema_violation",
                failure_category="schema",
            )
            return _schema_refusal(f"schema: {exc}", source_runs=source_runs)

        if not isinstance(validated, SkillProposalOutput):
            _record(
                input,
                prompt_result.rendered,
                response,
                latency_ms,
                outcome="schema_violation",
                failure_category="schema",
            )
            return _schema_refusal("unexpected output type", source_runs=source_runs)

        if validated.skill_name == "(none)":
            _record(
                input,
                prompt_result.rendered,
                response,
                latency_ms,
                outcome="success",
                failure_category=None,
            )
            return SkillCuratorResult(
                proposal=None,
                polarity=None,
                state=None,
                has_proposal=False,
                is_refusal=False,
                refusal_reason=None,
                warnings=(),
                source_runs=source_runs,
                dedupe_status="unique",
            )

        if _is_duplicate(validated.skill_name, input.existing_skills):
            _record(
                input,
                prompt_result.rendered,
                response,
                latency_ms,
                outcome="success",
                failure_category=None,
            )
            return SkillCuratorResult(
                proposal=None,
                polarity=None,
                state=None,
                has_proposal=False,
                is_refusal=False,
                refusal_reason=None,
                warnings=(f"skill_name {validated.skill_name!r} duplicates an existing skill",),
                source_runs=source_runs,
                dedupe_status="duplicate",
            )

        markdown_valid, markdown_reason = _validate_skill_markdown(validated.skill_markdown)
        if not markdown_valid:
            _record(
                input,
                prompt_result.rendered,
                response,
                latency_ms,
                outcome="success",
                failure_category=None,
            )
            return SkillCuratorResult(
                proposal=None,
                polarity=None,
                state=None,
                has_proposal=False,
                is_refusal=False,
                refusal_reason=None,
                warnings=(f"invalid skill_markdown: {markdown_reason}",),
                source_runs=source_runs,
            )

        normalized_markdown = _normalize_frontmatter(validated.skill_markdown)

        if not _markdown_name_matches(normalized_markdown, validated.skill_name):
            _record(
                input,
                prompt_result.rendered,
                response,
                latency_ms,
                outcome="success",
                failure_category=None,
            )
            return SkillCuratorResult(
                proposal=None,
                polarity=None,
                state=None,
                has_proposal=False,
                is_refusal=False,
                refusal_reason=None,
                warnings=(
                    "skill_markdown frontmatter 'name' does not match "
                    f"skill_name {validated.skill_name!r}",
                ),
                source_runs=source_runs,
            )

        eval_score = _run_eval_gate(normalized_markdown, input.eval_cases)
        if eval_score is not None and eval_score < input.eval_min_score:
            _record(
                input,
                prompt_result.rendered,
                response,
                latency_ms,
                outcome="success",
                failure_category=None,
            )
            return SkillCuratorResult(
                proposal=None,
                polarity=None,
                state=None,
                has_proposal=False,
                is_refusal=False,
                refusal_reason=None,
                warnings=(
                    f"eval gate failed: score {eval_score:.2f} is below {input.eval_min_score:.2f}",
                ),
                source_runs=source_runs,
                eval_score=eval_score,
            )

        polarity: Polarity = "positive" if input.outcome == "success" else "negative"
        normalized_proposal = _dc_replace(
            validated,
            skill_markdown=normalized_markdown,
        )

        _record(
            input,
            prompt_result.rendered,
            response,
            latency_ms,
            outcome="success",
            failure_category=None,
        )

        return SkillCuratorResult(
            proposal=normalized_proposal,
            polarity=polarity,
            state="draft",
            has_proposal=True,
            is_refusal=False,
            refusal_reason=None,
            warnings=(),
            source_runs=source_runs,
            eval_score=eval_score,
            dedupe_status="unique",
        )


def _directive_signature(directive: str) -> tuple[str, ...]:
    """Return a stable signature for grouping similar directives.

    The signature is the sorted set of significant words in the
    directive. Significant means length >= 2 and not in the stopword
    set. Two directives with the same set of significant words share a
    signature and therefore cluster together during mining.
    """
    words = _DIRECTIVE_WORD_RE.findall(directive.lower())
    return tuple(sorted({w for w in words if w not in _STOPWORDS and len(w) >= 2}))


def _mine_patterns(
    runs: tuple[RecentRun, ...],
) -> dict[tuple[str, tuple[str, ...]], tuple[RecentRun, ...]]:
    """Group runs by (outcome, directive signature), keep groups of 3+."""
    groups: dict[tuple[str, tuple[str, ...]], list[RecentRun]] = defaultdict(list)
    for run in runs:
        key = (run.outcome, _directive_signature(run.directive))
        groups[key].append(run)
    return {
        key: tuple(items) for key, items in groups.items() if len(items) >= MIN_RUNS_FOR_PROPOSAL
    }


def _normalize_skill_name(name: str) -> str:
    """Normalize a skill name for comparison: dashes, underscores, case."""
    return re.sub(r"[-_\s]+", "-", name.strip().lower())


def _is_duplicate(name: str, existing: tuple[str, ...]) -> bool:
    """Return True when ``name`` matches an existing skill after normalization."""
    if not existing:
        return False
    normalized = _normalize_skill_name(name)
    return any(_normalize_skill_name(e) == normalized for e in existing)


def _extract_body(markdown: str) -> str:
    """Return the portion of a markdown document after the frontmatter."""
    end = markdown.find("\n---\n", 4)
    if end == -1:
        return markdown
    return markdown[end + len("\n---\n") :]


def _run_eval_gate(
    markdown: str,
    eval_cases: tuple[SkillEvalCase, ...],
) -> float | None:
    """Return the eval score, or None when no cases are supplied."""
    if not eval_cases:
        return None
    body = _extract_body(markdown)
    passed = 0
    for case in eval_cases:
        if all(phrase in body for phrase in case.expected_contains):
            passed += 1
    return passed / len(eval_cases)


def _normalize_frontmatter(markdown: str) -> str:
    """Add SKILL.md optional fields when the model omitted them.

    Preserves the original field order and values; only adds
    ``allowed-tools`` and ``version`` when absent. Returns the input
    unchanged when the frontmatter cannot be parsed.
    """
    end = markdown.find("\n---\n", 4)
    if end == -1:
        return markdown
    yaml_text = markdown[4:end]
    body = markdown[end + len("\n---\n") :]
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return markdown
    if not isinstance(data, dict):
        return markdown
    data.setdefault("allowed-tools", [])
    data.setdefault("version", "0.1.0")
    try:
        new_yaml = yaml.safe_dump(data, sort_keys=False).strip()
    except yaml.YAMLError:
        return markdown
    return f"---\n{new_yaml}\n---\n{body}"


def _validate_skill_markdown(text: str) -> tuple[bool, str | None]:
    """Return (is_valid, reason).

    A valid skill_markdown begins with ``---\\n``, contains a YAML
    mapping with the base required fields (name, description, trigger,
    skill_type), closes the frontmatter with ``\\n---\\n``, and has a
    non-empty body after the closing delimiter. The optional
    ``allowed-tools`` and ``version`` fields are validated when
    present.
    """
    if not text.startswith("---\n"):
        return False, "must begin with '---\\n' (YAML frontmatter)"
    end = text.find("\n---\n", 4)
    if end == -1:
        return False, "missing closing '---' line"
    yaml_text = text[4:end]
    body = text[end + len("\n---\n") :]
    if not body.strip():
        return False, "body is empty after frontmatter"

    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return False, f"frontmatter is not valid YAML: {exc}"

    if not isinstance(data, dict):
        return False, "frontmatter must be a YAML mapping"

    name = data.get("name")
    if not isinstance(name, str) or not _SKILL_NAME_RE.match(name):
        return False, f"frontmatter 'name' must match ^[a-z0-9-]+$; got {name!r}"

    for required in ("description", "trigger"):
        value = data.get(required)
        if not isinstance(value, str) or not value.strip():
            return False, f"frontmatter {required!r} must be a non-empty string"

    skill_type = data.get("skill_type")
    if skill_type not in _VALID_SKILL_TYPES:
        return False, (
            f"frontmatter 'skill_type' must be one of "
            f"{sorted(_VALID_SKILL_TYPES)}; got {skill_type!r}"
        )

    if "allowed-tools" in data:
        at = data["allowed-tools"]
        if not isinstance(at, list) or not all(isinstance(t, str) for t in at):
            return False, "frontmatter 'allowed-tools' must be a list of strings"

    if "version" in data:
        version = data["version"]
        if not isinstance(version, str) or not version or "." not in version:
            return False, (
                "frontmatter 'version' must be a non-empty string with at "
                "least one '.'; got " + repr(version)
            )

    return True, None


def _markdown_name_matches(markdown: str, skill_name: str) -> bool:
    """Return True if the frontmatter name equals ``skill_name``."""
    end = markdown.find("\n---\n", 4)
    if end == -1:
        return False
    try:
        data = yaml.safe_load(markdown[4:end])
    except yaml.YAMLError:
        return False
    if not isinstance(data, dict):
        return False
    return data.get("name") == skill_name


def _schema_refusal(
    reason: str,
    *,
    source_runs: tuple[str, ...] = (),
) -> SkillCuratorResult:
    """Return a schema-refusal result."""
    return SkillCuratorResult(
        proposal=None,
        polarity=None,
        state=None,
        has_proposal=False,
        is_refusal=True,
        refusal_reason=reason,
        warnings=(),
        source_runs=source_runs,
    )


def _record(
    input: SkillCuratorInput,
    prompt: str,
    response: Any,
    latency_ms: int,
    *,
    outcome: str,
    failure_category: Any,
) -> None:
    from codeguardian.core.agents import _store_context

    store = _store_context.get_active_store()
    if store is None:
        return
    record_call(
        store,
        role="skill_curator",
        outcome=outcome,
        failure_category=failure_category,
        target_symbol=input.target_symbol or None,
        prompt=prompt,
        output=response.text,
        tokens_in=response.tokens_in,
        tokens_out=response.tokens_out,
        latency_ms=latency_ms,
        model_id=response.model_id,
        prompt_role="skill_curator",
        prompt_version=input.prompt_version,
        model_options={"temperature": 0.0},
    )
