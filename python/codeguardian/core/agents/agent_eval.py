"""Golden-case evaluation harness for non-verifier agent roles.

The three verifier roles use ``codeguardian.core.prompts.eval_harness``,
which loads JSON-verdict cases from ``prompts/<role>/eval/golden.yaml``.
This module covers the four roles whose output contract differs from
the verifier contract:

- ``analyst`` and ``skill_curator`` emit JSON but with role-specific
  keys, so their expected checks differ from a verdict comparison.
- ``tester`` and ``writer`` emit raw source. The verifier's JSON
  comparison does not apply.

Each role's golden case carries a ``variables`` mapping (fed to the
prompt renderer) and an ``expected`` mapping (role-specific checks
applied to the validated output). The loader dispatches on role, so a
case written for one role cannot be silently loaded as another.

The harness is a manual tool. It runs against curated cases and an
injected model callable. Real model calls are non-deterministic and
expensive, so the harness is not run in CI. The intended use is to
compare two prompt versions by pass rate.

# Expected fields by role

analyst:

- ``expects_refusal`` (bool, default ``False``)
- ``expected_files_min`` (int, default ``0``)
- ``expected_files_max`` (int, default ``999``)

tester and writer:

- ``expects_refusal`` (bool, default ``False``)
- ``expected_ast_valid`` (bool, default ``True``; only checked when
  the case's ``variables["language"]`` is ``"python"``)
- ``expected_contains`` (list of strings, default ``[]``)

skill_curator:

- ``expected_skill_name_or_none`` (``"auto"`` or ``"(none)"``,
  default ``"auto"``)
- ``expected_confidence_min`` (float, default ``0.0``)
"""

from __future__ import annotations

import ast
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.prompts.renderer import render
from codeguardian.core.prompts.schema import (
    ContextPackOutput,
    SkillProposalOutput,
    SourceOutput,
    validate_output,
)
from codeguardian.core.prompts.versioning import PromptBundle, load_prompt

__all__ = [
    "AgentEvalReport",
    "CaseResult",
    "GoldenCase",
    "load_golden",
    "run_golden",
]

_ANALYST_ROLES = frozenset({"analyst"})
_SOURCE_ROLES = frozenset({"tester", "writer"})
_SKILL_ROLES = frozenset({"skill_curator"})
_SUPPORTED_ROLES = _ANALYST_ROLES | _SOURCE_ROLES | _SKILL_ROLES

_APPROX_CHARS_PER_TOKEN = 4

ModelCallable = Callable[[str], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class GoldenCase:
    """One golden case loaded from a golden.yaml file."""

    id: str
    description: str
    variables: dict[str, str]
    expected: dict[str, object]


@dataclass(frozen=True, slots=True)
class CaseResult:
    """The outcome of running one golden case."""

    case_id: str
    passed: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AgentEvalReport:
    """The outcome of running every golden case for one role.

    ``avg_tokens_per_case`` is estimated from character count using a
    fixed characters-per-token ratio. It is sufficient to compare two
    prompt versions but is not a substitute for a provider-reported
    count. ``avg_latency_ms`` is measured wall-clock time around the
    model call, in milliseconds.
    """

    role: str
    total: int
    passed: int
    failed: int
    pass_rate: float
    avg_tokens_per_case: float
    avg_latency_ms: float
    results: tuple[CaseResult, ...] = field(default_factory=tuple)


def load_golden(
    role: str,
    prompts_root: Path | None = None,
) -> list[GoldenCase]:
    """Load every golden case for ``role``.

    Args:
        role: One of ``analyst``, ``tester``, ``writer``,
            ``skill_curator``. Verifier roles use
            ``codeguardian.core.prompts.load_golden`` instead.
        prompts_root: Optional override for the prompts directory.

    Returns:
        A list of ``GoldenCase`` objects. Empty if the golden file
        has no cases.

    Raises:
        CodeGuardianError: If the role is not supported by this
            harness, the golden file is missing, or the YAML is
            malformed or lacks required fields.
    """
    if role not in _SUPPORTED_ROLES:
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"agent_eval handles {sorted(_SUPPORTED_ROLES)}; "
            f"role {role!r} uses codeguardian.core.prompts.load_golden",
            details={"role": role, "valid": sorted(_SUPPORTED_ROLES)},
        )

    path = _golden_path(role, prompts_root)
    if not path.is_file():
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"golden case file not found: {path}",
            details={"path": str(path), "role": role},
        )

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"golden case file is not valid YAML: {path}: {exc}",
            details={"path": str(path)},
        ) from exc

    if not isinstance(data, dict):
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"golden case file must be a mapping with a 'cases' key: {path}",
            details={"path": str(path)},
        )

    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list):
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"golden case file must have a 'cases' list: {path}",
            details={"path": str(path)},
        )

    return [_parse_case(role, entry, index, path) for index, entry in enumerate(raw_cases)]


async def run_golden(
    cases: Sequence[GoldenCase],
    model_callable: ModelCallable,
    role: str,
    version: str,
    prompts_root: Path | None = None,
) -> AgentEvalReport:
    """Run every case through the prompt and the model.

    Args:
        cases: The golden cases to run.
        model_callable: An async callable that takes a rendered prompt
            and returns the model's response as a string.
        role: One of ``analyst``, ``tester``, ``writer``,
            ``skill_curator``.
        version: The prompt version.
        prompts_root: Optional override for the prompts directory.

    Returns:
        An ``AgentEvalReport`` summarizing the pass rate, average
        latency, and every case result. Every case is attempted; a
        failure on one case does not abort the run.
    """
    if role not in _SUPPORTED_ROLES:
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"agent_eval handles {sorted(_SUPPORTED_ROLES)}; role {role!r} is not supported",
            details={"role": role},
        )

    try:
        bundle = load_prompt(role, version, prompts_root)
    except CodeGuardianError as exc:
        return _report_from_load_failure(role, cases, exc)

    results: list[CaseResult] = []
    total_tokens = 0
    total_latency_ms = 0.0

    for case in cases:
        try:
            prompt = _render_bundle(bundle, case.variables)
        except CodeGuardianError as exc:
            results.append(CaseResult(case.id, False, f"render failed: {exc}"))
            continue

        start = time.perf_counter()
        try:
            raw = await model_callable(prompt)
        except Exception as exc:
            total_latency_ms += (time.perf_counter() - start) * 1000
            results.append(
                CaseResult(case.id, False, f"model call failed: {type(exc).__name__}: {exc}")
            )
            continue

        latency_ms = (time.perf_counter() - start) * 1000
        total_latency_ms += latency_ms
        total_tokens += (len(prompt) + len(raw)) // _APPROX_CHARS_PER_TOKEN

        try:
            validated = _parse_and_validate(role, raw)
        except CodeGuardianError as exc:
            results.append(CaseResult(case.id, False, f"schema: {exc}"))
            continue

        passed, reason = _check_expectation(role, case, validated)
        results.append(CaseResult(case.id, passed, reason))

    total = len(results)
    passed_count = sum(1 for result in results if result.passed)
    return AgentEvalReport(
        role=role,
        total=total,
        passed=passed_count,
        failed=total - passed_count,
        pass_rate=(passed_count / total) if total else 0.0,
        avg_tokens_per_case=(total_tokens / total) if total else 0.0,
        avg_latency_ms=(total_latency_ms / total) if total else 0.0,
        results=tuple(results),
    )


def _report_from_load_failure(
    role: str,
    cases: Sequence[GoldenCase],
    exc: CodeGuardianError,
) -> AgentEvalReport:
    """Return an all-failed report when the prompt itself cannot be loaded."""
    results = tuple(CaseResult(case.id, False, f"load failed: {exc}") for case in cases)
    return AgentEvalReport(
        role=role,
        total=len(results),
        passed=0,
        failed=len(results),
        pass_rate=0.0,
        avg_tokens_per_case=0.0,
        avg_latency_ms=0.0,
        results=results,
    )


def _golden_path(role: str, prompts_root: Path | None) -> Path:
    if prompts_root is None:
        root = Path(__file__).resolve().parents[4] / "prompts"
    else:
        root = Path(prompts_root)
    return root / role / "eval" / "golden.yaml"


def _render_bundle(bundle: PromptBundle, variables: dict[str, str]) -> str:
    base_rendered = render(bundle.base_text, variables).rstrip()
    role_rendered = render(bundle.role_text, variables).lstrip()
    if not base_rendered:
        return role_rendered
    return base_rendered + "\n\n" + role_rendered


def _parse_case(
    role: str,
    entry: object,
    index: int,
    path: Path,
) -> GoldenCase:
    if not isinstance(entry, dict):
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"golden case {index} is not a mapping: {path}",
            details={"path": str(path), "index": index},
        )

    case_id = entry.get("id")
    if not isinstance(case_id, str) or not case_id:
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"golden case {index} missing 'id': {path}",
            details={"path": str(path), "index": index},
        )

    description = entry.get("description", "")
    if not isinstance(description, str):
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"golden case {case_id!r} has non-string 'description': {path}",
            details={"path": str(path), "case_id": case_id},
        )

    variables_raw = entry.get("variables")
    if not isinstance(variables_raw, dict):
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"golden case {case_id!r} 'variables' must be a mapping: {path}",
            details={"path": str(path), "case_id": case_id},
        )
    variables: dict[str, str] = {}
    for key, value in variables_raw.items():
        if not isinstance(key, str):
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"golden case {case_id!r} variable key must be a string: {path}",
                details={"path": str(path), "case_id": case_id},
            )
        variables[key] = value if isinstance(value, str) else str(value)

    expected = entry.get("expected")
    if not isinstance(expected, dict):
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"golden case {case_id!r} 'expected' must be a mapping: {path}",
            details={"path": str(path), "case_id": case_id},
        )

    return GoldenCase(
        id=case_id,
        description=description,
        variables=variables,
        expected=dict(expected),
    )


def _parse_and_validate(role: str, raw: str) -> object:
    if role in _SOURCE_ROLES:
        return validate_output(role, raw)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"role {role!r} output is not valid JSON: {exc}",
            details={"role": role},
        ) from exc

    return validate_output(role, parsed)


def _check_expectation(
    role: str,
    case: GoldenCase,
    validated: object,
) -> tuple[bool, str | None]:
    if role in _ANALYST_ROLES:
        return _check_analyst_output(validated, case.expected)
    if role in _SOURCE_ROLES:
        return _check_source_output(case.variables, validated, case.expected)
    if role in _SKILL_ROLES:
        return _check_skill_output(validated, case.expected)
    return False, f"unknown role {role!r}"


def _check_analyst_output(
    validated: object,
    expected: dict[str, object],
) -> tuple[bool, str | None]:
    if not isinstance(validated, ContextPackOutput):
        return False, f"unexpected output type: {type(validated).__name__}"

    expects_refusal = bool(expected.get("expects_refusal", False))
    file_count = len(validated.files)
    is_refusal = file_count == 0 and validated.reason.startswith("refused:")

    if expects_refusal and not is_refusal:
        return False, "expected refusal but received files"
    if not expects_refusal and is_refusal:
        return False, "unexpected refusal"

    if not expects_refusal:
        min_files = int(expected.get("expected_files_min", 0))
        max_files = int(expected.get("expected_files_max", 999))
        if not (min_files <= file_count <= max_files):
            return False, f"file count {file_count} outside [{min_files}, {max_files}]"

    return True, None


def _check_source_output(
    variables: dict[str, str],
    validated: object,
    expected: dict[str, object],
) -> tuple[bool, str | None]:
    if not isinstance(validated, SourceOutput):
        return False, f"unexpected output type: {type(validated).__name__}"

    expects_refusal = bool(expected.get("expects_refusal", False))
    if expects_refusal != validated.is_refusal:
        return (
            False,
            f"refusal mismatch: expected {expects_refusal}, received {validated.is_refusal}",
        )
    if validated.is_refusal:
        return True, None

    language = variables.get("language", "python")
    if language == "python" and bool(expected.get("expected_ast_valid", True)):
        try:
            ast.parse(validated.content)
        except SyntaxError as exc:
            return False, f"ast.parse failed: {exc}"

    for needle in expected.get("expected_contains", []):
        if not isinstance(needle, str):
            return False, f"invalid expected_contains entry {needle!r}"
        if needle not in validated.content:
            return False, f"output missing {needle!r}"

    return True, None


def _check_skill_output(
    validated: object,
    expected: dict[str, object],
) -> tuple[bool, str | None]:
    if not isinstance(validated, SkillProposalOutput):
        return False, f"unexpected output type: {type(validated).__name__}"

    sentinel = str(expected.get("expected_skill_name_or_none", "auto"))
    if sentinel == "(none)":
        if validated.skill_name != "(none)":
            return False, f"expected '(none)' sentinel, received {validated.skill_name!r}"
        if validated.confidence != 0.0:
            return False, f"expected confidence 0.0 for sentinel, received {validated.confidence}"
        return True, None

    if validated.skill_name == "(none)":
        return False, "received '(none)' sentinel but a real skill was expected"

    min_confidence = float(expected.get("expected_confidence_min", 0.0))
    if validated.confidence < min_confidence:
        return False, f"confidence {validated.confidence} below minimum {min_confidence}"

    return True, None
