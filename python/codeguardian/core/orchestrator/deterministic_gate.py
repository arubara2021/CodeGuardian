"""Zero-token safety gate before any verifier model call.

The gate runs two checks on the writer's candidate before the verifier
stack spends any tokens:

1. **Diff applies.** ``git apply --check`` against the diff. This is
   the pre-apply equivalent of ``git diff --check``: it verifies the
   patch is well-formed, the context lines match the target file, and
   applying it will not fail. A diff that does not pass this check
   cannot be applied; running verifiers on it wastes tokens.
2. **Characterization test still passes.** Runs the test file the
   tester produced (or, when the tester failed, the tool's own
   characterization test) against the applied diff inside the sandbox.
   A nonzero pytest exit here means the diff broke behavior.

A failure at either step returns ``passed=False`` with a short
``stderr_tail`` the caller can pass to the writer as ``test_failure``.
The verifier stack bounces the candidate without running any LLM
verifier.

The gate does not apply the diff. It uses ``git apply --check`` to
verify apply-ability, then applies the diff, runs the test, and
rolls back. The apply and rollback are the caller's responsibility;
this module accepts callables for both so the caller controls the
mechanism.

The gate is best-effort. When the sandbox executor does not support
``write_file``, or the diff is empty, or the test path is missing,
the gate records a ``skip`` result rather than a failure. A skipped
gate means the caller should not treat the diff as verified; the
verifier stack's LLM layer is the only check that ran.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from codeguardian.core.agents._verifier_common import (
    ExecutionOutcome,
    SandboxExecutor,
)

__all__ = [
    "ApplyFn",
    "CheckOutcome",
    "DeterministicGateResult",
    "RollbackFn",
    "WritableSandboxExecutor",
    "deterministic_gate",
]


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_DEFAULT_TEST_TIMEOUT_S = 180.0
_DEFAULT_GIT_TIMEOUT_S = 30.0


class WritableSandboxExecutor(SandboxExecutor, Protocol):
    """A SandboxExecutor that can also write files into the sandbox.

    The deterministic gate needs to write the diff to disk before
    ``git apply --check`` can read it. The base SandboxExecutor
    protocol only runs commands; a writable extension is required for
    the git check. The characterization test still runs through the
    base protocol's ``run``, so an executor that only implements
    ``run`` still passes the test check but skips the git check.
    """

    def write_file(self, path: str, content: str) -> None:
        """Write ``content`` to ``path`` inside the sandbox."""
        ...


ApplyFn = Callable[[str], Any]
"""Apply a diff and return a rollback reference.

The gate calls this once. The return value is passed to ``rollback_fn``
when the test fails. Concrete implementations raise a
``CodeGuardianError`` on a failed apply; the gate converts the
exception into a failure result.
"""


RollbackFn = Callable[[Any], None]
"""Roll back a previously applied change, given its reference."""


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """One check inside the gate.

    Attributes:
        name: A short name, for the decision log.
        status: ``"pass"``, ``"fail"``, or ``"skip"``.
        detail: A short human-readable detail. For a fail this is the
            stderr tail.
    """

    name: str
    status: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DeterministicGateResult:
    """The gate's outcome.

    Attributes:
        passed: True when every check passed or was skipped. A skip is
            not a pass, but it does not block; the verifier stack's
            LLM layer will still run when the gate passed with skips.
        checks: One ``CheckOutcome`` per check, in execution order.
        stderr_tail: The last few hundred characters of stderr from
            the failing check, stripped of ANSI escapes. Empty when
            passed is True or when no check produced stderr.
        diff_hash: SHA-256 of the diff. Carried so the decision log
            can correlate the gate result with the diff.
    """

    passed: bool
    checks: tuple[CheckOutcome, ...] = ()
    stderr_tail: str = ""
    diff_hash: str = ""

    def skip_reason(self) -> str | None:
        """Return the first skip reason, or None when nothing skipped."""
        for check in self.checks:
            if check.status == "skip":
                return f"{check.name}: {check.detail}"
        return None

    def failure_reason(self) -> str | None:
        """Return the first failure reason, or None when nothing failed."""
        for check in self.checks:
            if check.status == "fail":
                return f"{check.name}: {check.detail}"
        return None


def _sha256(text: str) -> str:
    """Return the SHA-256 hex digest of ``text``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from ``text``."""
    return _ANSI_ESCAPE.sub("", text)


def _tail(text: str, limit: int = 400) -> str:
    """Return the last ``limit`` characters of ``text``, stripped of ANSI."""
    clean = _strip_ansi(text)
    return clean[-limit:] if len(clean) > limit else clean


def _run_git_apply_check(
    executor: SandboxExecutor,
    diff: str,
    *,
    diff_filename: str = "codeguardian_gate_diff.patch",
    timeout_s: float = _DEFAULT_GIT_TIMEOUT_S,
) -> CheckOutcome:
    """Write the diff into the sandbox and verify it applies.

    Returns a ``skip`` outcome when the executor does not support
    ``write_file``. ``git apply --check`` verifies that the diff is
    well-formed and matches the target file's current content without
    modifying anything.
    """
    if not hasattr(executor, "write_file"):
        return CheckOutcome(
            name="git apply --check",
            status="skip",
            detail="sandbox executor does not implement write_file",
        )
    try:
        executor.write_file(diff_filename, diff)  # type: ignore[attr-defined]
    except Exception as exc:
        return CheckOutcome(
            name="git apply --check",
            status="fail",
            detail=f"could not write diff into sandbox: {exc}",
        )
    try:
        outcome = executor.run(
            ["git", "apply", "--check", diff_filename],
            timeout_s=timeout_s,
        )
    except Exception as exc:
        return CheckOutcome(
            name="git apply --check",
            status="fail",
            detail=f"git apply --check could not start: {exc}",
        )
    if outcome.exit_code == 0:
        return CheckOutcome(
            name="git apply --check",
            status="pass",
            detail="diff applies cleanly",
        )
    return CheckOutcome(
        name="git apply --check",
        status="fail",
        detail=_tail(outcome.stderr or "git apply --check failed", 400),
    )


def _run_characterization_test(
    executor: SandboxExecutor,
    test_path: str | None,
    *,
    runner: str = "run_pytest.py",
    timeout_s: float = _DEFAULT_TEST_TIMEOUT_S,
) -> CheckOutcome:
    """Run the characterization test and record the outcome.

    Returns a ``skip`` outcome when no test path was supplied. A
    nonzero pytest exit is a failure; the gate treats exit code 1
    (test failures) and any other nonzero exit code the same way,
    because both indicate the candidate broke something.
    """
    if test_path is None:
        return CheckOutcome(
            name="characterization test",
            status="skip",
            detail="no test path supplied",
        )
    try:
        outcome: ExecutionOutcome = executor.run(
            ["python3", runner, test_path, "-v", "--tb=short"],
            timeout_s=timeout_s,
        )
    except Exception as exc:
        return CheckOutcome(
            name="characterization test",
            status="fail",
            detail=f"test run could not start: {exc}",
        )
    if outcome.exit_code == 0:
        return CheckOutcome(
            name="characterization test",
            status="pass",
            detail="pytest exit 0",
        )
    return CheckOutcome(
        name="characterization test",
        status="fail",
        detail=_tail(outcome.stderr or outcome.stdout or "pytest failed", 400),
    )


def deterministic_gate(
    *,
    executor: SandboxExecutor | None,
    diff: str,
    test_path: str | None,
    apply_fn: ApplyFn | None = None,
    rollback_fn: RollbackFn | None = None,
    test_timeout_s: float = _DEFAULT_TEST_TIMEOUT_S,
    git_timeout_s: float = _DEFAULT_GIT_TIMEOUT_S,
) -> DeterministicGateResult:
    """Run both checks and return the combined result.

    Execution order:

    1. If ``executor`` is supplied: ``git apply --check`` verifies the
       diff applies.
    2. If ``apply_fn`` and ``rollback_fn`` are supplied: apply the diff,
       run the characterization test, rollback. If either callable is
       missing the test check is skipped because it would run against
       an unapplied diff.
    3. A failure at any step short-circuits the rest. A skipped step
       does not short-circuit; subsequent steps still run.

    Args:
        executor: The sandbox executor. When None, both checks skip.
        diff: The unified diff to verify.
        test_path: The characterization test path relative to the
            clone root, or None when no test was produced.
        apply_fn: Applies ``diff`` and returns a rollback reference.
            When None, the test check skips.
        rollback_fn: Rolls back the reference ``apply_fn`` returned.
            When None, the test check skips.
        test_timeout_s: Wall-clock budget for the test run.
        git_timeout_s: Wall-clock budget for ``git apply --check``.

    Returns:
        A ``DeterministicGateResult``. ``passed`` is True when no
        check failed (skips do not fail the gate). ``stderr_tail``
        names the first failing check's stderr.
    """
    diff_hash = _sha256(diff or "")
    checks: list[CheckOutcome] = []

    if executor is None:
        checks.append(
            CheckOutcome(
                name="gate",
                status="skip",
                detail="no sandbox executor supplied",
            )
        )
        return DeterministicGateResult(
            passed=True,
            checks=tuple(checks),
            stderr_tail="",
            diff_hash=diff_hash,
        )

    if not diff:
        checks.append(
            CheckOutcome(
                name="gate",
                status="fail",
                detail="empty diff; nothing to verify",
            )
        )
        return DeterministicGateResult(
            passed=False,
            checks=tuple(checks),
            stderr_tail="empty diff",
            diff_hash=diff_hash,
        )

    apply_check = _run_git_apply_check(
        executor,
        diff,
        timeout_s=git_timeout_s,
    )
    checks.append(apply_check)
    if apply_check.status == "fail":
        return DeterministicGateResult(
            passed=False,
            checks=tuple(checks),
            stderr_tail=apply_check.detail,
            diff_hash=diff_hash,
        )

    if apply_fn is None or rollback_fn is None:
        checks.append(
            CheckOutcome(
                name="characterization test",
                status="skip",
                detail="apply_fn or rollback_fn not supplied",
            )
        )
        return DeterministicGateResult(
            passed=True,
            checks=tuple(checks),
            stderr_tail="",
            diff_hash=diff_hash,
        )

    try:
        rollback_ref = apply_fn(diff)
    except Exception as exc:
        checks.append(
            CheckOutcome(
                name="apply",
                status="fail",
                detail=f"apply failed: {exc}",
            )
        )
        return DeterministicGateResult(
            passed=False,
            checks=tuple(checks),
            stderr_tail=str(exc),
            diff_hash=diff_hash,
        )

    try:
        test_check = _run_characterization_test(
            executor,
            test_path,
            timeout_s=test_timeout_s,
        )
        checks.append(test_check)
    finally:
        with contextlib.suppress(Exception):
            rollback_fn(rollback_ref)

    if test_check.status == "fail":
        return DeterministicGateResult(
            passed=False,
            checks=tuple(checks),
            stderr_tail=test_check.detail,
            diff_hash=diff_hash,
        )

    return DeterministicGateResult(
        passed=True,
        checks=tuple(checks),
        stderr_tail="",
        diff_hash=diff_hash,
    )
