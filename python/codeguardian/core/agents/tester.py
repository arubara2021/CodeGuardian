"""Tester agent.

Model-backed. Reads the RefactorSpec, writes a characterization test
that covers the target's branches, boundaries, and exception paths,
plus one test per ContractAssertion in the blast report.

The agent does not construct a Code Property Graph. Its tools read the
CPG from the ``cpg_available`` context that the caller establishes
before invoking the agent. This keeps the agent free of CPG lifetime
management.

Self-retry. Two attempts max. If attempt 1 fails to parse or has a
test function with no assertions, attempt 2 uses prompt v1.1.0 with
the specific deficiency named. Refusal from the model is not retried;
it is returned as the agent's verdict.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.plugins import BudgetTracker, PluginRegistry
from codeguardian.core.prompts import (
    SourceOutput,
    render_prompt,
    validate_output,
)
from codeguardian.interfaces.model import ModelOptions, ModelProvider
from codeguardian.interfaces.verifier import ContractAssertion

from ._runtime import fresh_barrier, tracked_model_call
from .analyst import RefactorSpec
from .blast import BlastResult
from .telemetry import record_call

__all__ = [
    "TesterAgent",
    "TesterInput",
    "TesterResult",
]


_NOT_FOUND_PREFIXES = (
    "(declaration ",
    "(file ",
    "(path ",
    "(graph ",
    "(cannot ",
)


@dataclass(frozen=True, slots=True)
class TesterInput:
    """Input to the tester agent."""

    __test__: ClassVar[bool] = False

    spec: RefactorSpec
    blast: BlastResult
    registry: PluginRegistry
    provider: ModelProvider
    model: str
    budget: BudgetTracker
    clone_root: Path
    target_path: str
    target_symbol: str
    language: str = "python"
    previous_test_attempt: str = ""
    parse_error: str = ""
    old_code_failure: str = ""


@dataclass(frozen=True, slots=True)
class TesterResult:
    """Output of the tester agent.

    Attributes:
        test_file: The test source. Empty on refusal.
        is_refusal: True when the agent refuses the task.
        is_valid: True when the test file parses and every test_*
            function has at least one assertion.
        refusal_code: One of the six typed refusal codes, or None.
        refusal_reason: The human-readable refusal reason, or None.
        failure_reason: The reason the last attempt is invalid, or
            None on success.
        attempts: Number of model calls made. 0 on pre-model refusal.
    """

    test_file: str
    is_refusal: bool
    is_valid: bool
    refusal_code: str | None = None
    refusal_reason: str | None = None
    failure_reason: str | None = None
    attempts: int = 0


class TesterAgent:
    """Model-backed tester agent."""

    name = "tester"
    phase = "tester"
    is_deterministic = False
    requires_tools = ("function-body", "code-skeleton")
    can_refuse = True
    prompt_role = "tester"
    prompt_version = "1.0.0"

    async def run(self, input: TesterInput) -> TesterResult:
        """Run the tester agent.

        Args:
            input: The tester input. The caller must have set up the
                ``cpg_available`` context before calling.

        Returns:
            A TesterResult. On refusal, ``is_refusal`` is True and
            ``test_file`` is empty.

        Raises:
            CodeGuardianError: ``SYS_NOT_IMPLEMENTED`` when the kernel
                extension is unavailable.
        """
        try:
            from codeguardian import _native  # noqa: F401
        except ImportError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_NOT_IMPLEMENTED,
                "tester requires the compiled kernel extension",
            ) from exc

        return await self._run(input)

    async def _run(self, input: TesterInput) -> TesterResult:
        body = await _call_tool(
            input,
            "function-body",
            {"path": input.target_path, "qualified_name": input.target_symbol},
        )
        if _is_not_found(body):
            return TesterResult(
                test_file="",
                is_refusal=True,
                is_valid=False,
                refusal_code="target_missing",
                refusal_reason=f"function-body returned: {body[:100]}",
                attempts=0,
            )

        skeleton = await _call_tool(input, "code-skeleton", {"path": input.target_path})

        base_vars = {
            "target_symbol": input.target_symbol,
            "target_path": input.target_path,
            "language": input.language,
            "directive": input.spec.directive,
            "skeleton": skeleton,
            "blast_report": json.dumps(input.blast.to_artifact(), indent=2),
        }

        is_external_retry = bool(
            input.previous_test_attempt or input.parse_error or input.old_code_failure
        )

        if is_external_retry:
            first_version = "1.1.0"
            first_vars = {
                **base_vars,
                "previous_test_attempt": input.previous_test_attempt,
                "parse_error": input.parse_error,
                "old_code_failure": input.old_code_failure,
            }
        else:
            first_version = "1.0.0"
            first_vars = base_vars

        first = await _call_model(input, first_version, first_vars)
        attempts = 1

        if first.is_refusal:
            return TesterResult(
                test_file="",
                is_refusal=True,
                is_valid=False,
                refusal_code="directive_ambiguous",
                refusal_reason=first.refusal_reason,
                attempts=attempts,
            )

        valid, reason = _validate_test_file(first.content, input.blast.contract_assertions)
        if valid:
            return TesterResult(
                test_file=first.content,
                is_refusal=False,
                is_valid=True,
                attempts=attempts,
            )

        retry_vars = {
            **base_vars,
            "previous_test_attempt": first.content,
            "parse_error": reason if reason and "SyntaxError" in reason else "",
            "old_code_failure": reason if reason and "no assertions" in reason else "",
        }
        second = await _call_model(input, "1.1.0", retry_vars)
        attempts = 2

        if second.is_refusal:
            return TesterResult(
                test_file="",
                is_refusal=True,
                is_valid=False,
                refusal_code="directive_ambiguous",
                refusal_reason=second.refusal_reason,
                attempts=attempts,
            )

        second_valid, second_reason = _validate_test_file(
            second.content, input.blast.contract_assertions
        )
        if second_valid:
            return TesterResult(
                test_file=second.content,
                is_refusal=False,
                is_valid=True,
                attempts=attempts,
            )

        return TesterResult(
            test_file=second.content,
            is_refusal=False,
            is_valid=False,
            failure_reason=second_reason,
            attempts=attempts,
        )


async def _call_tool(input: TesterInput, name: str, tool_input: dict[str, object]) -> str:
    with fresh_barrier(
        input.registry,
        name,
        mode="plan",
        budget=input.budget,
        allowed_tools=("provide",),
    ) as barrier:
        return await barrier.call_async("provide", tool_input)


async def _call_model(
    input: TesterInput,
    version: str,
    variables: dict[str, str],
) -> SourceOutput:
    prompt_result = render_prompt("tester", version, variables)
    response, latency_ms = await tracked_model_call(
        input.provider,
        prompt_result.rendered,
        input.model,
        ModelOptions(),
        "tester",
    )

    _record(
        input,
        version,
        prompt_result.rendered,
        response,
        latency_ms,
    )

    try:
        return validate_output("tester", response.text)  # type: ignore[return-value]
    except CodeGuardianError as exc:
        return SourceOutput(
            role="tester",
            content="",
            is_refusal=True,
            refusal_reason=f"schema: {exc}",
        )


def _is_not_found(tool_output: str) -> bool:
    return any(tool_output.startswith(p) for p in _NOT_FOUND_PREFIXES)


def _validate_test_file(
    source: str,
    contract_assertions: tuple[ContractAssertion, ...] = (),
) -> tuple[bool, str | None]:
    """Return (is_valid, failure_reason)."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return False, f"SyntaxError: {exc}"

    for _n in ast.walk(tree):
        if isinstance(_n, ast.Name) and _n.id in ("true", "false", "null"):
            return False, f"JS literal {_n.id} on line {_n.lineno}; use True/False/None"

    _js_lits = {"true", "false", "null"}
    for _n in ast.walk(tree):
        if isinstance(_n, ast.Name) and _n.id in _js_lits:
            return (
                False,
                f"JS literal {_n.id} on line {_n.lineno}; use True/False/None",
            )

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("test_"):
                continue
            if not _has_assertion(node):
                return (
                    False,
                    f"test function {node.name!r} has no assertions",
                )

    for assertion in contract_assertions:
        if assertion.test_expression:
            parts = assertion.test_expression.split(" ", 2)
            if len(parts) >= 3:
                operation = parts[2].strip()
                if operation and operation not in source:
                    return (
                        False,
                        f"missing contract assertion: {operation!r}",
                    )

    return True, None


def _has_assertion(func: ast.AST) -> bool:
    for child in ast.walk(func):
        if isinstance(child, ast.Assert):
            return True
        if isinstance(child, ast.Call):
            callee = child.func
            if (
                isinstance(callee, ast.Attribute)
                and callee.attr == "raises"
                and isinstance(callee.value, ast.Name)
                and callee.value.id == "pytest"
            ):
                return True
    return False


def _record(
    input: TesterInput,
    version: str,
    prompt: str,
    response: Any,
    latency_ms: int,
) -> None:
    from codeguardian.core.agents import _store_context

    store = _store_context.get_active_store()
    if store is None:
        return
    record_call(
        store,
        role="tester",
        outcome="success",
        target_symbol=input.target_symbol,
        prompt=prompt,
        output=response.text,
        tokens_in=response.tokens_in,
        tokens_out=response.tokens_out,
        latency_ms=latency_ms,
        model_id=response.model_id,
        prompt_role="tester",
        prompt_version=version,
        model_options={"temperature": 0.0},
    )
