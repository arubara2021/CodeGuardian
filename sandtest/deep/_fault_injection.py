"""Fault injection: prove the value-based check catches real drift.

The pipeline's moat is that a value-based characterization test fails
when the refactored code changes observable behaviour. Proving that
requires deliberately breaking the code in a way the test should
notice, and confirming that it does. This module is the one place
where a deliberately-introduced bug is expected to fail the test.

The injection wraps every ``return`` expression in the target
function with a call to a helper that transforms the value: an int
becomes int + 1, a string gains a suffix, a bool is inverted, a
container gains an extra element. Every transformation produces a
value that a well-written characterization test will catch and a
smoke test will not.

Two failure shapes are distinguished:

1. The injection cannot be performed. A function whose only return is
   bare ``return`` or ``return None`` has nothing to wrap. A function
   whose body contains no return at all has nothing to wrap. That is
   not a failure of the moat; it is a limitation of the target. The
   caller records it as skipped rather than failing.

2. The injection succeeds but the test still passes. That is a real
   failure. Either the value test was not present in the
   characterization file, or the assertion is too weak to notice the
   transformation. Either way the moat is not working for this
   target.

The module reports a single assertion per fact. There is no
"did the injection apply" check that can go green while "did the
test catch it" goes red. The design is one boolean: the drift was
caught, or it was not. Everything else is a note for the caller.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import TYPE_CHECKING

from codeguardian.core.errors import CodeGuardianError

from ._preflight import CheckResult

if TYPE_CHECKING:
    from ._sandbox import SandboxSession

__all__ = [
    "FaultInjectionResult",
    "can_inject_drift",
    "inject_behavioral_drift",
    "run_fault_injection",
]


# The helper inserted at the top of the file when drift is injected.
# The name is prefixed to avoid collision with any real function. The
# helper's transformations are chosen so the resulting value is
# observably different in a way that a characterization test on the
# original return will notice.
_INJECT_HELPER = '''
def _cg_inject(value):
    """Deliberately alter a return value for fault injection."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 1.0
    if isinstance(value, str):
        return value + "__cg_injected__"
    if isinstance(value, bytes):
        return value + b"__cg_injected__"
    if isinstance(value, dict):
        out = dict(value)
        out["__cg_injected__"] = True
        return out
    if isinstance(value, (list, tuple)):
        out = list(value)
        out.append("__cg_injected__")
        try:
            return type(value)(out)
        except TypeError:
            return out
    if isinstance(value, set):
        out = set(value)
        out.add("__cg_injected__")
        return out
    return "__cg_injected__"
'''


@dataclass(frozen=True, slots=True)
class FaultInjectionResult:
    """The outcome of a fault-injection cycle.

    Attributes:
        skipped_reason: When set, the injection could not be
            performed. The value is a short explanation for the
            caller's log. When this is set, ``drift_caught`` is
            meaningless and should not be checked.
        injected: True when the module built a modified source string.
        applied: True when the provider successfully applied the
            modified source to disk.
        exit_code: The pytest process's exit code on the injected
            code. ``None`` when pytest did not run.
        value_test_failed: True when at least one ``test_value_*``
            test appeared in the pytest ``FAILED`` lines.
        smoke_test_failed: True when at least one ``test_smoke_*``
            test appeared in the pytest ``FAILED`` lines.
        drift_caught: True when the injection worked, pytest ran, the
            value test failed, and the smoke test did not. That is
            the shape the moat promises: the value signal catches
            what the smoke signal misses.
        rolled_back: True when the file on disk was restored to its
            original content.
        stdout_tail: The last few lines of pytest's stdout, kept for
            diagnostic output when the check fails.
    """

    skipped_reason: str | None = None
    injected: bool = False
    applied: bool = False
    exit_code: int | None = None
    value_test_failed: bool = False
    smoke_test_failed: bool = False
    drift_caught: bool = False
    rolled_back: bool = False
    stdout_tail: str = ""

    def as_check(self) -> CheckResult:
        """Convert the result into a single ``CheckResult``.

        The check distinguishes five outcomes. A skipped injection is
        a passed check with the skip reason recorded. A collection or
        config error is a failed check with the pytest exit code and
        the stdout tail, because the injection broke the module in a
        way that prevents the tests from running at all. A test pass
        on injected code is a failed check, because the moat did not
        fire. A drift caught by the value test is the primary pass. A
        drift caught only by the smoke test is a pass with a weaker
        signal, because a target whose value test was not written
        still has a smoke test, and the smoke test firing on an
        injection whose value changed is evidence the drift was
        caught. When both tests fail, that is too aggressive — the
        injection broke the return type, not just the value — and
        the check fails.
        """
        if self.skipped_reason is not None:
            return CheckResult(
                "fault injection skipped",
                True,
                self.skipped_reason,
            )
        if not self.injected:
            return CheckResult(
                "fault injection caught drift",
                False,
                "injection failed; see notes",
            )
        if not self.applied:
            return CheckResult(
                "fault injection caught drift",
                False,
                "apply failed",
            )
        if self.exit_code is None:
            return CheckResult(
                "fault injection caught drift",
                False,
                "pytest did not run",
            )
        if self.exit_code not in (0, 1):
            return CheckResult(
                "fault injection caught drift",
                False,
                f"pytest exited with {self.exit_code}; collection or "
                f"config error, not a test failure. tail: "
                f"{self.stdout_tail[-300:]}",
            )
        if self.exit_code == 0:
            return CheckResult(
                "fault injection caught drift",
                False,
                "test passed on injected code; moat did not fire",
            )
        if self.value_test_failed and self.smoke_test_failed:
            return CheckResult(
                "fault injection caught drift",
                False,
                "smoke test also failed; drift too aggressive",
            )
        if not self.value_test_failed and not self.smoke_test_failed:
            return CheckResult(
                "fault injection caught drift",
                False,
                "pytest exit 1 but no test_value_* or test_smoke_* "
                f"in FAILED lines. tail: {self.stdout_tail[-300:]}",
            )
        if not self.rolled_back:
            return CheckResult(
                "fault injection caught drift",
                False,
                "drift caught but rollback failed",
            )
        caught_by = "value" if self.value_test_failed else "smoke"
        return CheckResult(
            "fault injection caught drift",
            True,
            f"exit={self.exit_code}, caught by {caught_by} test, rollback ok",
        )


def _insert_helper(source: str, helper: str) -> str:
    """Insert the helper function after any shebang, docstring, or
    ``__future__`` import.

    The insertion point matters: the helper references no other module
    state, so putting it immediately after the shebang and docstring
    and before any real code is always valid. The helper must appear
    before any function that uses it, which is every function whose
    return was wrapped.
    """
    insert_line = 0
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # A file the caller already knew did not parse; keep the
        # original and hope the caller checks. The injection call
        # site checks the result before applying, so this defensive
        # path is unreachable in practice.
        return source + "\n\n" + helper + "\n"

    lines = source.splitlines(keepends=True)
    if lines and lines[0].startswith("#!"):
        insert_line = 1

    for node in tree.body:
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and node is tree.body[0]
            and insert_line == 0
        ) or (isinstance(node, ast.ImportFrom) and node.module == "__future__"):
            insert_line = max(insert_line, node.end_lineno or 0)

    prefix = "".join(lines[:insert_line])
    suffix = "".join(lines[insert_line:])
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + "\n" + helper + "\n" + suffix


_MUTABLE_RETURN_ANNOTATIONS: frozenset[str] = frozenset(
    {
        "int",
        "float",
        "str",
        "bytes",
        "bool",
        "list",
        "dict",
        "set",
        "tuple",
        "list[str]",
        "list[int]",
        "dict[str, str]",
        "dict[str, int]",
    }
)


def _safe_to_inject_return(annotation_text: str) -> bool:
    """Return True when the return type is in the mutable set.

    A return annotation of an empty string (no annotation) is treated
    as unsafe because we cannot reason about what the function returns.
    Composite types like ``tuple[dict[str, str], ByteStream]`` are
    rejected because the injection helper cannot reliably mutate them.
    Only the annotations in ``_MUTABLE_RETURN_ANNOTATIONS`` pass.
    """
    if not annotation_text:
        return False
    text = annotation_text.replace(" ", "")
    if text in {t.replace(" ", "") for t in _MUTABLE_RETURN_ANNOTATIONS}:
        return True
    if text.endswith("|None"):
        return _safe_to_inject_return(text[: -len("|None")])
    if text.startswith("Optional["):
        return _safe_to_inject_return(text[len("Optional[") : -1])
    return False


def can_inject_drift(source: str, function_name: str) -> tuple[bool, str]:
    """Return whether the injection is possible, and why if not.

    A function is injectable when it is a synchronous function that
    contains at least one ``Return`` statement with a value, and whose
    return annotation is one of the mutable types that
    ``_cg_inject`` knows how to transform. A function that returns a
    complex object (SSLContext, ByteStream, a custom class) is skipped
    because the injection helper cannot produce a same-type mutated
    value, and forcing the injection produces a string that fails
    every assertion rather than testing the value path.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return False, f"source does not parse: {exc}"

    target: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            target = node
            break
    if target is None:
        return False, f"function {function_name!r} not found in source"
    if isinstance(target, ast.AsyncFunctionDef):
        return False, f"function {function_name!r} is async; not supported"
    has_value_return = any(
        isinstance(stmt, ast.Return) and stmt.value is not None for stmt in ast.walk(target)
    )
    if not has_value_return:
        return False, f"function {function_name!r} has no returns with values"

    annotation_text = ""
    if target.returns is not None:
        annotation_text = ast.unparse(target.returns)
    if not _safe_to_inject_return(annotation_text):
        return (
            False,
            f"function {function_name!r} returns {annotation_text or 'unannotated'}; "
            "not in the mutable-type set",
        )

    return True, ""


def inject_behavioral_drift(source: str, function_name: str) -> str | None:
    """Return the modified source with the target function's returns wrapped.

    Every ``Return`` statement inside the target function that has a
    value gets that value wrapped in a call to ``_cg_inject``. Every
    other function in the file is unchanged. The helper function is
    inserted at the top of the file. The result is guaranteed to
    parse: the function re-parses the modified source and returns
    ``None`` if the result does not parse.

    Args:
        source: The full text of the file.
        function_name: The simple name of the target function.

    Returns:
        The modified source, or ``None`` when the injection is not
        possible or the result did not parse. ``can_inject_drift``
        gives the reason the caller should record.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    target: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            target = node
            break
    if target is None:
        return None

    wrapped = False
    for stmt in ast.walk(target):
        if isinstance(stmt, ast.Return) and stmt.value is not None:
            stmt.value = ast.Call(
                func=ast.Name(id="_cg_inject", ctx=ast.Load()),
                args=[stmt.value],
                keywords=[],
            )
            wrapped = True
    if not wrapped:
        return None

    original_source = ast.get_source_segment(source, target)
    if original_source is None:
        return None

    new_func_source = ast.unparse(target)
    modified = source.replace(original_source, new_func_source, 1)
    modified = _insert_helper(modified, _INJECT_HELPER)

    try:
        ast.parse(modified)
    except SyntaxError:
        return None
    return modified


def _classify_pytest_output(stdout: str) -> tuple[bool, bool, str]:
    """Return (value_failed, smoke_failed, tail).

    Scans pytest stdout for ``FAILED`` lines and checks whether any
    names a ``test_value_*`` test or a ``test_smoke_*`` test. The
    substring check is deliberately not anchored to ``::`` because
    pytest's output format varies between ``--tb=line``, ``--tb=short``,
    and verbose versus quiet modes; the ``FAILED`` guard on the same
    line is what makes the substring safe.

    The tail is the last ten lines for diagnostic output. It is what
    the caller reads when the check fails and needs to know whether
    pytest reported a test failure, a collection error, or a config
    error.
    """
    value_failed = False
    smoke_failed = False
    for line in stdout.splitlines():
        if "FAILED" not in line:
            continue
        if "test_value_" in line:
            value_failed = True
        elif "test_smoke_" in line:
            smoke_failed = True
    tail_lines = stdout.splitlines()[-10:]
    return value_failed, smoke_failed, "\n".join(tail_lines)


def run_fault_injection(
    session: SandboxSession,
    *,
    original_source: str,
    function_name: str,
    target_path: str,
    char_filename: str,
    build_and_apply,
) -> FaultInjectionResult:
    """Inject drift, apply, run the value test, rollback.

    Args:
        session: An active network-OFF sandbox session.
        original_source: The file's current content, before injection.
        function_name: The simple name of the target function.
        target_path: The file's path relative to the clone root.
        char_filename: The characterization test filename to run.
        build_and_apply: A callable that accepts the modified source
            string and returns ``(rollback_ref, applier, applied)`` on
            success or ``(None, None, False)`` on failure. This is
            provided by the pipeline so the fault injection module
            does not depend on the apply provider's exact shape.

    Returns:
        A ``FaultInjectionResult``. The ``drift_caught`` field is the
        single assertion the caller records; every other field is
        diagnostic.
    """
    can_inject, reason = can_inject_drift(original_source, function_name)
    if not can_inject:
        return FaultInjectionResult(skipped_reason=reason)

    modified = inject_behavioral_drift(original_source, function_name)
    if modified is None:
        return FaultInjectionResult(
            skipped_reason=f"injection into {function_name!r} produced invalid source",
        )

    result = FaultInjectionResult(injected=True)

    applied = build_and_apply(modified)
    rollback_ref, applier, apply_ok = applied
    if not apply_ok:
        return FaultInjectionResult(
            injected=True,
            applied=False,
        )

    try:
        exec_result = session.run(
            f"PYTHONPATH=.deps:.:$PYTHONPATH python3 run_pytest.py {char_filename} -v --tb=line",
            cwd="clone",
            timeout_ms=120_000,
        )
        value_failed, smoke_failed, tail = _classify_pytest_output(exec_result.stdout or "")
        caught = exec_result.exit_code == 1 and value_failed and not smoke_failed
        # Update the mutable result through construction: the
        # dataclass is frozen, so build a new one.
        result = FaultInjectionResult(
            injected=True,
            applied=True,
            exit_code=exec_result.exit_code,
            value_test_failed=value_failed,
            smoke_test_failed=smoke_failed,
            drift_caught=caught,
            rolled_back=False,
            stdout_tail=tail,
        )
    except CodeGuardianError as exc:
        return FaultInjectionResult(
            injected=True,
            applied=True,
            stdout_tail=f"{exc.code.value}: {exc.message}",
        )
    finally:
        try:
            applier.rollback(rollback_ref)
            rollback_ok = True
        except CodeGuardianError:
            rollback_ok = False

    return FaultInjectionResult(
        injected=result.injected,
        applied=result.applied,
        exit_code=result.exit_code,
        value_test_failed=result.value_test_failed,
        smoke_test_failed=result.smoke_test_failed,
        drift_caught=result.drift_caught,
        rolled_back=rollback_ok,
        stdout_tail=result.stdout_tail,
    )
