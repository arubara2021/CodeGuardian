"""Per-role output contracts.

Each role produces one of four output shapes:

- **Verdict** (the three verifiers). A JSON object with a ``verdict``
  field of ``pass``, ``fail``, ``uncertain``, or ``refused``. Machine
  actionable. ``fail`` requires at least one finding. ``refused``
  requires a reason and no confidence.
- **Source** (Tester, Writer). Raw text. If the first line starts with
  ``REFUSED:``, the rest is the refusal reason and the source is
  empty. Otherwise the whole text is source code the caller parses
  against the target language.
- **Context pack** (Analyst). A JSON object with a ``files`` array,
  each entry carrying ``path`` and ``reason``.
- **Skill proposal** (Skill Curator). A JSON object with
  ``skill_name``, ``skill_markdown``, and ``confidence``.

The validator raises ``CodeGuardianError(SYS_INTERNAL)`` on any
structural violation. The pipeline catches this and treats the role
as having produced the ``refused`` shape, with the validation message
as the reason. That keeps a malformed response from silently passing
through as success.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from codeguardian.core.errors import CodeGuardianError, ErrorCode

__all__ = [
    "ContextPackOutput",
    "FileSpec",
    "Finding",
    "SkillProposalOutput",
    "SourceOutput",
    "VerdictOutput",
    "validate_output",
]

Verdict = Literal["pass", "fail", "uncertain", "refused"]
Severity = Literal["info", "warning", "critical"]


@dataclass(frozen=True, slots=True)
class Finding:
    """One observation from a verifier."""

    severity: Severity
    message: str
    location: str | None = None


@dataclass(frozen=True, slots=True)
class VerdictOutput:
    """The result of a verifier role."""

    role: str
    verdict: Verdict
    confidence: float | None
    findings: tuple[Finding, ...] = ()
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SourceOutput:
    """The result of a Tester or Writer role."""

    role: str
    content: str
    is_refusal: bool = False
    refusal_reason: str | None = None


@dataclass(frozen=True, slots=True)
class FileSpec:
    """One file the Analyst wants included in the context pack."""

    path: str
    reason: str


@dataclass(frozen=True, slots=True)
class ContextPackOutput:
    """The result of the Analyst role."""

    role: str
    files: tuple[FileSpec, ...] = field(default_factory=tuple)
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SkillProposalOutput:
    """The result of the Skill Curator role."""

    role: str
    skill_name: str
    skill_markdown: str
    confidence: float


_VERIFIER_ROLES = frozenset({"correctness_verifier", "security_verifier", "contract_verifier"})
_SOURCE_ROLES = frozenset({"tester", "writer"})


def validate_output(role: str, data: object) -> object:
    """Validate ``data`` against the output contract for ``role``.

    Args:
        role: One of the seven role names.
        data: The raw output produced by the model.

    Returns:
        A typed output object whose shape depends on the role.

    Raises:
        CodeGuardianError: ``SYS_INTERNAL`` on any structural
            violation. The details name the failing field.
    """
    if role in _VERIFIER_ROLES:
        return _validate_verdict(role, data)
    if role in _SOURCE_ROLES:
        return _validate_source(role, data)
    if role == "analyst":
        return _validate_context_pack(role, data)
    if role == "skill_curator":
        return _validate_skill(role, data)
    raise CodeGuardianError(
        ErrorCode.SYS_INTERNAL,
        f"unknown role: {role!r}",
        details={"role": role},
    )


def _validate_verdict(role: str, data: object) -> VerdictOutput:
    if not isinstance(data, Mapping):
        raise _shape_error(role, "verdict must be a mapping")

    verdict = data.get("verdict")
    if verdict not in ("pass", "fail", "uncertain", "refused"):
        raise _shape_error(
            role,
            f"verdict must be one of pass/fail/uncertain/refused; got {verdict!r}",
        )

    if verdict == "refused":
        reason = data.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise _shape_error(role, "refused verdict requires a non-empty 'reason'")
        return VerdictOutput(
            role=role, verdict="refused", confidence=None, findings=(), reason=reason
        )

    confidence = data.get("confidence")
    if not isinstance(confidence, (int, float)) or not (0.0 <= float(confidence) <= 1.0):
        raise _shape_error(role, f"confidence must be a float in [0, 1]; got {confidence!r}")

    findings_raw = data.get("findings", [])
    if not isinstance(findings_raw, list):
        raise _shape_error(role, "'findings' must be a list")

    findings: list[Finding] = []
    for index, entry in enumerate(findings_raw):
        if not isinstance(entry, Mapping):
            raise _shape_error(role, f"findings[{index}] must be a mapping")
        severity = entry.get("severity")
        if severity not in ("info", "warning", "critical"):
            raise _shape_error(role, f"findings[{index}].severity must be info/warning/critical")
        message = entry.get("message")
        if not isinstance(message, str) or not message.strip():
            raise _shape_error(role, f"findings[{index}].message must be a non-empty string")
        location = entry.get("location")
        findings.append(
            Finding(
                severity=severity,
                message=message,
                location=location if isinstance(location, str) else None,
            )
        )

    if verdict == "fail" and not findings:
        raise _shape_error(role, "fail verdict requires at least one finding")

    return VerdictOutput(
        role=role,
        verdict=verdict,
        confidence=float(confidence),
        findings=tuple(findings),
        reason=None,
    )


def _validate_source(role: str, data: object) -> SourceOutput:
    if not isinstance(data, str):
        raise _shape_error(role, "source output must be a string")

    if data.startswith("REFUSED:"):
        reason = data[len("REFUSED:") :].strip()
        if not reason:
            raise _shape_error(role, "REFUSED: requires a non-empty reason")
        return SourceOutput(role=role, content="", is_refusal=True, refusal_reason=reason)

    return SourceOutput(role=role, content=data, is_refusal=False, refusal_reason=None)


def _validate_context_pack(role: str, data: object) -> ContextPackOutput:
    if not isinstance(data, Mapping):
        raise _shape_error(role, "context pack output must be a mapping")

    files_raw = data.get("files")
    if not isinstance(files_raw, list):
        raise _shape_error(role, "'files' must be a list")

    files: list[FileSpec] = []
    for index, entry in enumerate(files_raw):
        if not isinstance(entry, Mapping):
            raise _shape_error(role, f"files[{index}] must be a mapping")
        path = entry.get("path")
        reason = entry.get("reason")
        if not isinstance(path, str) or not path:
            raise _shape_error(role, f"files[{index}].path must be a non-empty string")
        if not isinstance(reason, str) or not reason:
            raise _shape_error(role, f"files[{index}].reason must be a non-empty string")
        files.append(FileSpec(path=path, reason=reason))

    reason_text = data.get("reason", "")
    if not isinstance(reason_text, str):
        raise _shape_error(role, "'reason' must be a string")

    return ContextPackOutput(role=role, files=tuple(files), reason=reason_text)


def _validate_skill(role: str, data: object) -> SkillProposalOutput:
    if not isinstance(data, Mapping):
        raise _shape_error(role, "skill proposal output must be a mapping")

    name = data.get("skill_name")
    markdown = data.get("skill_markdown")
    confidence = data.get("confidence")

    if not isinstance(name, str) or not name.strip():
        raise _shape_error(role, "'skill_name' must be a non-empty string")
    if not isinstance(markdown, str) or not markdown.strip():
        raise _shape_error(role, "'skill_markdown' must be a non-empty string")
    if not isinstance(confidence, (int, float)) or not (0.0 <= float(confidence) <= 1.0):
        raise _shape_error(role, f"'confidence' must be a float in [0, 1]; got {confidence!r}")

    return SkillProposalOutput(
        role=role,
        skill_name=name,
        skill_markdown=markdown,
        confidence=float(confidence),
    )


def _shape_error(role: str, message: str) -> CodeGuardianError:
    return CodeGuardianError(
        ErrorCode.SYS_INTERNAL,
        f"prompt output for role {role!r} is malformed: {message}",
        details={"role": role, "reason": message},
    )
