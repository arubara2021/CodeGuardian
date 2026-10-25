"""Golden-case evaluation harness for verifier prompts.

Loads a curated set of test cases from ``prompts/<role>/eval/golden.yaml``,
renders the prompt for each, calls a model, validates the model's
output against the schema, and compares the verdict against the
expected verdict for that case.

The harness is a manual tool. It is not run in CI, because it requires
real model calls. The intended use is:

1. Change a verifier prompt.
2. Run the harness against the golden cases.
3. Compare the pass rate before and after the change.
4. If the rate went down, revert the prompt change.

The model callable is injected so the tests can pass a deterministic
fake. Real usage passes a callable that calls ``ModelProvider.complete``
with the appropriate role.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from codeguardian.core.errors import CodeGuardianError, ErrorCode

from .renderer import render
from .schema import validate_output
from .versioning import load_prompt

__all__ = [
    "CaseResult",
    "EvalReport",
    "GoldenCase",
    "load_golden",
    "run_golden",
]

_VERIFIER_ROLES = frozenset({"correctness_verifier", "security_verifier", "contract_verifier"})


@dataclass(frozen=True, slots=True)
class GoldenCase:
    """One golden case loaded from a golden.yaml file."""

    id: str
    description: str
    variables: dict[str, str]
    expected_verdict: str


@dataclass(frozen=True, slots=True)
class CaseResult:
    """The outcome of running one golden case."""

    case_id: str
    expected_verdict: str
    actual_verdict: str | None
    passed: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class EvalReport:
    """The outcome of running every golden case for one role."""

    role: str
    total: int
    passed: int
    failed: int
    results: tuple[CaseResult, ...] = field(default_factory=tuple)


ModelCallable = Callable[[str], Awaitable[str]]


def load_golden(
    role: str,
    prompts_root: Path | None = None,
) -> list[GoldenCase]:
    """Load every golden case for ``role``.

    Args:
        role: One of the three verifier role names.
        prompts_root: Optional override for the prompts directory.

    Returns:
        A list of ``GoldenCase`` objects. Empty if the golden file has
        no cases.

    Raises:
        CodeGuardianError: ``SYS_INTERNAL`` if the role is not a
            verifier, the golden file is missing, or the YAML is
            malformed or lacks required fields.
    """
    if role not in _VERIFIER_ROLES:
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"golden cases exist only for verifier roles; got {role!r}",
            details={"role": role},
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

    cases: list[GoldenCase] = []
    for index, entry in enumerate(raw_cases):
        cases.append(_parse_case(entry, index, path))
    return cases


async def run_golden(
    cases: Sequence[GoldenCase],
    model_callable: ModelCallable,
    role: str,
    version: str,
    prompts_root: Path | None = None,
) -> EvalReport:
    """Run every case through the prompt and the model.

    Args:
        cases: The golden cases to run.
        model_callable: An async callable that takes a rendered prompt
            and returns the model's response as a string.
        role: The verifier role.
        version: The prompt version.
        prompts_root: Optional override for the prompts directory.

    Returns:
        An ``EvalReport`` summarizing the pass rate and every case
        result. Every case is attempted; a failure on one case does not
        abort the run.
    """
    results: list[CaseResult] = []

    for case in cases:
        try:
            bundle = load_prompt(role, version, prompts_root)
        except CodeGuardianError as exc:
            results.append(
                CaseResult(
                    case.id,
                    case.expected_verdict,
                    None,
                    False,
                    f"load failed: {exc}",
                )
            )
            continue

        try:
            base_rendered = render(bundle.base_text, case.variables).rstrip()
            role_rendered = render(bundle.role_text, case.variables).lstrip()
            prompt = base_rendered + "\n\n" + role_rendered
        except CodeGuardianError as exc:
            results.append(
                CaseResult(
                    case.id,
                    case.expected_verdict,
                    None,
                    False,
                    f"render failed: {exc}",
                )
            )
            continue

        try:
            raw = await model_callable(prompt)
        except Exception as exc:
            results.append(
                CaseResult(
                    case.id,
                    case.expected_verdict,
                    None,
                    False,
                    f"model call failed: {type(exc).__name__}: {exc}",
                )
            )
            continue

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            results.append(
                CaseResult(
                    case.id,
                    case.expected_verdict,
                    None,
                    False,
                    f"invalid JSON: {exc}",
                )
            )
            continue

        try:
            validated = validate_output(role, parsed)
        except CodeGuardianError as exc:
            results.append(
                CaseResult(
                    case.id,
                    case.expected_verdict,
                    None,
                    False,
                    f"schema: {exc}",
                )
            )
            continue

        actual_verdict = getattr(validated, "verdict", None)
        passed = actual_verdict == case.expected_verdict
        results.append(
            CaseResult(
                case.id,
                case.expected_verdict,
                actual_verdict,
                passed,
                None if passed else f"expected {case.expected_verdict}, got {actual_verdict}",
            )
        )

    passed = sum(1 for result in results if result.passed)
    return EvalReport(
        role=role,
        total=len(results),
        passed=passed,
        failed=len(results) - passed,
        results=tuple(results),
    )


def _golden_path(role: str, prompts_root: Path | None) -> Path:
    if prompts_root is None:
        root = Path(__file__).resolve().parents[4] / "prompts"
    else:
        root = Path(prompts_root)
    return root / role / "eval" / "golden.yaml"


def _parse_case(entry: object, index: int, path: Path) -> GoldenCase:
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
            f"golden case {index} is missing 'id': {path}",
            details={"path": str(path), "index": index},
        )

    description = entry.get("description", "")
    if not isinstance(description, str):
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"golden case {case_id!r} has non-string 'description': {path}",
            details={"path": str(path), "case_id": case_id},
        )

    expected = entry.get("expected_verdict")
    if expected not in ("pass", "fail", "uncertain", "refused"):
        raise CodeGuardianError(
            ErrorCode.SYS_INTERNAL,
            f"golden case {case_id!r} has invalid 'expected_verdict': {path}",
            details={
                "path": str(path),
                "case_id": case_id,
                "expected": expected,
            },
        )

    variables_raw = {
        "diff": entry.get("diff", ""),
        "change_kind": entry.get("change_kind", "semantic"),
        "blast_score": str(entry.get("blast_score", 0)),
        "contract_assertions": entry.get("contract_assertions", ""),
    }
    variables: dict[str, str] = {}
    for key, value in variables_raw.items():
        variables[key] = value if isinstance(value, str) else str(value)

    return GoldenCase(
        id=case_id,
        description=description,
        variables=variables,
        expected_verdict=expected,
    )
