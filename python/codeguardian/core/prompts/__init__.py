"""Prompt rendering, versioning, output validation, and golden-case eval.

Every role's prompt is a pair of files: a shared ``_base`` markdown and
a role-specific markdown. The loader reads both, the renderer
substitutes variables, and the schema validator checks the model's
output against the role's contract.

The eval harness runs curated golden cases against a verifier prompt to
measure whether a change to the prompt improves or regresses its
agreement with human-labeled verdicts. It is a manual tool, not run in
CI, because it requires real model calls.

# Public surface

- ``render_prompt(role, version, variables)`` — full rendered text plus
  frontmatter, ready to send to a model.
- ``load_prompt(role, version)`` — the raw bundle, for inspection or
  custom concatenation.
- ``validate_output(role, data)`` — typed output object from raw model
  output. Raises on structural violation.
- ``load_golden(role)`` / ``run_golden(...)`` — the eval harness.
- ``PromptResult``, ``PromptBundle``, ``GoldenCase``, ``CaseResult``,
  ``EvalReport`` — the dataclasses those functions return.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .eval_harness import (
    CaseResult,
    EvalReport,
    GoldenCase,
    load_golden,
    run_golden,
)
from .renderer import (
    InjectionError,
    MissingVariableError,
    find_variables,
    render,
)
from .schema import (
    ContextPackOutput,
    FileSpec,
    Finding,
    SkillProposalOutput,
    SourceOutput,
    VerdictOutput,
    validate_output,
)
from .versioning import (
    PromptBundle,
    load_prompt,
    set_prompts_root,
)

__all__ = [
    "CaseResult",
    "ContextPackOutput",
    "EvalReport",
    "FileSpec",
    "Finding",
    "GoldenCase",
    "InjectionError",
    "MissingVariableError",
    "PromptBundle",
    "PromptResult",
    "SkillProposalOutput",
    "SourceOutput",
    "VerdictOutput",
    "find_variables",
    "load_golden",
    "load_prompt",
    "render",
    "render_prompt",
    "run_golden",
    "set_prompts_root",
    "validate_output",
]


@dataclass(frozen=True, slots=True)
class PromptResult:
    """A rendered prompt, ready to send to a model.

    Attributes:
        role: The role name.
        version: The prompt version.
        rendered: The full prompt text: base body followed by role body,
            with variables substituted in both.
        frontmatter: The parsed frontmatter from the role file. Contains
            at least ``mode``; may contain role-specific metadata.
        variables_used: Every variable name referenced by either body,
            in order of first appearance.
    """

    role: str
    version: str
    rendered: str
    frontmatter: dict[str, Any]
    variables_used: tuple[str, ...]


def render_prompt(
    role: str,
    version: str,
    variables: dict[str, str],
    prompts_root: Path | None = None,
) -> PromptResult:
    """Load, render, and combine the base and role prompts.

    Args:
        role: One of the seven role names.
        version: A MAJOR.MINOR.PATCH version string.
        variables: The mapping to substitute into both prompt bodies.
        prompts_root: Optional override for the prompts directory.

    Returns:
        A ``PromptResult`` with the combined rendered text.

    Raises:
        CodeGuardianError: If loading fails, or if rendering raises
            ``MissingVariableError`` or ``InjectionError``.
    """
    bundle = load_prompt(role, version, prompts_root)

    base_rendered = render(bundle.base_text, variables).rstrip()
    role_rendered = render(bundle.role_text, variables).lstrip()
    rendered = base_rendered + "\n\n" + role_rendered if base_rendered else role_rendered

    required = tuple(
        dict.fromkeys(
            list(find_variables(bundle.base_text)) + list(find_variables(bundle.role_text))
        )
    )

    return PromptResult(
        role=role,
        version=version,
        rendered=rendered,
        frontmatter=dict(bundle.frontmatter),
        variables_used=required,
    )
