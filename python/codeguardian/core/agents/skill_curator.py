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

Two negative paths do not raise:

- Invalid skill_markdown frontmatter yields has_proposal=False with a
  warning. The main run is not compromised by a malformed proposal.
- Model schema violations yield is_refusal=True. The curator could not
  complete its job, but nothing downstream depends on the outcome.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
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
    "SkillCuratorAgent",
    "SkillCuratorInput",
    "SkillCuratorResult",
]


Outcome = Literal["success", "failure"]
Polarity = Literal["positive", "negative"]


_SKILL_NAME_RE = re.compile(r"^[a-z0-9-]+$")

_VALID_SKILL_TYPES = frozenset(
    {
        "refactor_pattern",
        "test_pattern",
        "language_rule",
        "project_convention",
    }
)


@dataclass(frozen=True, slots=True)
class SkillCuratorInput:
    """Input to the skill curator agent."""

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


@dataclass(frozen=True, slots=True)
class SkillCuratorResult:
    """Output of the skill curator agent.

    Attributes:
        proposal: The validated SkillProposalOutput, or None when the
            model returned the (none) sentinel or produced an invalid
            skill_markdown.
        polarity: "positive" on success, "negative" on failure, None
            when there is no proposal.
        state: "draft" whenever there is a proposal, None otherwise.
        has_proposal: True iff ``proposal`` is not None.
        is_refusal: True when the model's response violated the
            schema or the response could not be parsed as JSON.
        refusal_reason: Human-readable reason for the refusal, or None.
        warnings: Non-fatal issues such as a malformed frontmatter in
            an otherwise valid proposal.
    """

    proposal: SkillProposalOutput | None
    polarity: Polarity | None
    state: Literal["draft"] | None
    has_proposal: bool
    is_refusal: bool
    refusal_reason: str | None
    warnings: tuple[str, ...]


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
            return _schema_refusal(f"invalid JSON: {exc}", is_refusal=True)

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
            return _schema_refusal(f"schema: {exc}", is_refusal=True)

        if not isinstance(validated, SkillProposalOutput):
            _record(
                input,
                prompt_result.rendered,
                response,
                latency_ms,
                outcome="schema_violation",
                failure_category="schema",
            )
            return _schema_refusal("unexpected output type", is_refusal=True)

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
            )

        if not _markdown_name_matches(validated.skill_markdown, validated.skill_name):
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
            )

        polarity: Polarity = "positive" if input.outcome == "success" else "negative"

        _record(
            input,
            prompt_result.rendered,
            response,
            latency_ms,
            outcome="success",
            failure_category=None,
        )

        return SkillCuratorResult(
            proposal=validated,
            polarity=polarity,
            state="draft",
            has_proposal=True,
            is_refusal=False,
            refusal_reason=None,
            warnings=(),
        )


def _validate_skill_markdown(text: str) -> tuple[bool, str | None]:
    """Return (is_valid, reason).

    A valid skill_markdown begins with ``---\\n``, contains a YAML
    mapping with the required fields (name, description, trigger,
    skill_type), closes the frontmatter with ``\\n---\\n``, and has a
    non-empty body after the closing delimiter.
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
        return False, (f"frontmatter 'name' must match ^[a-z0-9-]+$; got {name!r}")

    for required in ("description", "trigger"):
        value = data.get(required)
        if not isinstance(value, str) or not value.strip():
            return False, (f"frontmatter {required!r} must be a non-empty string")

    skill_type = data.get("skill_type")
    if skill_type not in _VALID_SKILL_TYPES:
        return False, (
            f"frontmatter 'skill_type' must be one of "
            f"{sorted(_VALID_SKILL_TYPES)}; got {skill_type!r}"
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


def _schema_refusal(reason: str, *, is_refusal: bool) -> SkillCuratorResult:
    """Return a schema-refusal result."""
    return SkillCuratorResult(
        proposal=None,
        polarity=None,
        state=None,
        has_proposal=False,
        is_refusal=is_refusal,
        refusal_reason=reason,
        warnings=(),
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
