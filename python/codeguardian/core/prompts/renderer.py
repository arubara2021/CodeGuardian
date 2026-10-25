"""Strict template renderer for prompt files.

A prompt template contains ``{{variable_name}}`` placeholders. The
renderer substitutes them from a mapping and rejects three failure
modes:

- **Missing variable.** A placeholder has no matching key.
- **Unbalanced braces.** A ``{{`` or ``}}`` that is not part of a valid
  variable reference.
- **Injection.** A variable's value contains ``{{other_name}}`` where
  ``other_name`` is also a key in the same mapping. Substituting that
  value into the template produces text that, if rendered again,
  would substitute a variable the original template never declared.
  Rejecting is the only safe choice: the caller must escape or
  reshape the value.

Substitution is single-pass. ``re.sub`` does not re-scan inserted
text, so a value containing ``{{unknown}}`` (where ``unknown`` is not
a variable) passes through unchanged. Only values that could actually
trigger a substitution on a subsequent render are rejected.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from codeguardian.core.errors import CodeGuardianError, ErrorCode

__all__ = [
    "InjectionError",
    "MissingVariableError",
    "find_variables",
    "render",
]

_VAR_PATTERN = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")
_OPEN = "{{"
_CLOSE = "}}"


class MissingVariableError(CodeGuardianError):
    """A template references a variable the caller did not supply."""

    def __init__(self, missing: tuple[str, ...]) -> None:
        super().__init__(
            ErrorCode.SYS_INTERNAL,
            "prompt template missing required variables: "
            + ", ".join(repr(name) for name in missing),
            details={"missing": list(missing)},
        )


class InjectionError(CodeGuardianError):
    """A variable's value contains a template reference to another variable."""

    def __init__(self, variable: str, referenced: str) -> None:
        super().__init__(
            ErrorCode.SYS_INTERNAL,
            f"variable {variable!r} contains a template reference to "
            f"{referenced!r}; this is an injection risk",
            details={"variable": variable, "referenced": referenced},
        )


def find_variables(template: str) -> tuple[str, ...]:
    """Return every variable name referenced by ``template``, in order.

    Duplicates are collapsed to the first occurrence. The result is
    deterministic: the same template always returns the same tuple.
    """
    seen: dict[str, None] = {}
    for match in _VAR_PATTERN.finditer(template):
        seen.setdefault(match.group(1), None)
    return tuple(seen)


def render(template: str, variables: Mapping[str, str]) -> str:
    """Render ``template`` with ``variables``.

    Args:
        template: The template text, with ``{{name}}`` placeholders.
        variables: The mapping to substitute from. Extra keys are
            allowed and ignored.

    Returns:
        The rendered text.

    Raises:
        MissingVariableError: If the template references a name that
            is not in ``variables``.
        InjectionError: If a value in ``variables`` contains a
            template reference to another key that is also in
            ``variables``.
        CodeGuardianError: If the template contains an unbalanced
            ``{{`` or ``}}``.
    """
    _check_balanced(template)
    _check_injection(variables)

    required = find_variables(template)
    missing = tuple(name for name in required if name not in variables)
    if missing:
        raise MissingVariableError(missing)

    def _substitute(match: re.Match[str]) -> str:
        return variables[match.group(1)]

    return _VAR_PATTERN.sub(_substitute, template)


def _check_balanced(template: str) -> None:
    """Reject any ``{{`` or ``}}`` that is not part of a balanced pair.

    Valid ``{{name}}`` references are stripped first. What remains is
    either a balanced pair with an invalid variable name (like
    ``{{123}}``) or an unpaired marker. Only unpaired markers are
    rejected. A balanced pair with an invalid name passes through as
    literal text: the template author wrote it, the renderer leaves it
    alone, and the substitution step skips it because the regex does
    not match.

    Counting rather than substring-checking is deliberate. A substring
    check on ``{{`` rejects a template that merely *contains* the two
    characters, even when they are paired and harmless. Counting is the
    only check that distinguishes "stray marker" from "literal content
    the author intended to keep."
    """
    stripped = _VAR_PATTERN.sub("", template)
    opens = stripped.count(_OPEN)
    closes = stripped.count(_CLOSE)
    if opens != closes:
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"prompt template has unbalanced braces: {opens} '{{{{' and {closes} '}}}}'",
            details={
                "opens": opens,
                "closes": closes,
                "template_prefix": stripped[:200],
            },
        )


def _check_injection(variables: Mapping[str, str]) -> None:
    """Reject any value that references another key in the same mapping."""
    for name, value in variables.items():
        if not isinstance(value, str):
            continue
        for match in _VAR_PATTERN.finditer(value):
            referenced = match.group(1)
            if referenced in variables:
                raise InjectionError(name, referenced)
