"""Writer agent.

Model-backed. Reads the RefactorSpec. Generates N candidates with
different seeds. Deterministic pre-check per candidate: signature
drift (Python only), ast.parse on the candidate, directive compliance
score. Prunes candidates whose compliance score is below 0.5. Ranks
survivors by (compliance_score desc, diff_size asc, creation order).

The prompt is prefixed with a preamble telling the model it has no
API-level tool calling. Without it, tool-use-trained models attempt
tool calls even when the API request defines no tools.

On retry: when the input carries ``previous_attempt``,
``test_failure``, or ``previous_diff``, the agent uses prompt v1.1.0
with those variables populated.
"""

from __future__ import annotations

import ast
import difflib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.plugins import BudgetTracker, PluginRegistry
from codeguardian.core.prompts import (
    SourceOutput,
    render_prompt,
    validate_output,
)
from codeguardian.interfaces.model import ModelOptions, ModelProvider

from ._runtime import fresh_barrier, tracked_model_call
from .analyst import RefactorSpec
from .blast import BlastResult
from .telemetry import record_call

__all__ = [
    "WriterAgent",
    "WriterCandidate",
    "WriterInput",
    "WriterResult",
]


_MIN_COMPLIANCE = 0.5
_CANDIDATE_TEMPERATURE = 0.2

_NO_TOOL_PREAMBLE = (
    "IMPORTANT: You do not have API-level tool calling. "
    "All context has been pre-gathered by the pipeline and appears below. "
    "Do not emit tool call JSON. "
    "Return only the refactored function as raw source code. "
    "If the context is insufficient, respond starting with REFUSED:."
    + chr(10) * 3
    + "---"
    + chr(10) * 2
)


@dataclass(frozen=True, slots=True)
class WriterInput:
    """Input to the writer agent."""

    spec: RefactorSpec
    blast: BlastResult
    skeleton: str
    characterization_test: str
    registry: PluginRegistry
    provider: ModelProvider
    model: str
    budget: BudgetTracker
    clone_root: Path
    target_path: str
    target_symbol: str
    language: str = "python"
    n_candidates: int = 3
    previous_attempt: str = ""
    test_failure: str = ""
    previous_diff: str = ""


@dataclass(frozen=True, slots=True)
class WriterCandidate:
    """One refactor candidate that passed all deterministic checks."""

    source: str
    compliance_score: float
    diff_size: int


@dataclass(frozen=True, slots=True)
class WriterResult:
    """Output of the writer agent."""

    candidates: tuple[WriterCandidate, ...]
    is_refusal: bool
    refusal_reason: str | None
    total_attempted: int


class WriterAgent:
    """Model-backed writer agent."""

    name = "writer"
    phase = "writer"
    is_deterministic = False
    requires_tools = ("function-body", "call-graph-slice")
    can_refuse = True
    prompt_role = "writer"
    prompt_version = "1.0.0"

    async def run(self, input: WriterInput) -> WriterResult:
        """Run the writer agent.

        Raises:
            CodeGuardianError: ``SYS_NOT_IMPLEMENTED`` when the kernel
                extension is unavailable.
        """
        try:
            from codeguardian import _native  # noqa: F401
        except ImportError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_NOT_IMPLEMENTED,
                "writer requires the compiled kernel extension",
            ) from exc

        body = await _call_tool(
            input,
            "function-body",
            {"path": input.target_path, "qualified_name": input.target_symbol},
        )
        if _is_not_found(body):
            return WriterResult(
                candidates=(),
                is_refusal=True,
                refusal_reason=f"target body not found: {body[:100]}",
                total_attempted=0,
            )
        original_source = body

        if input.blast.contract_violations:
            await _call_tool(
                input,
                "call-graph-slice",
                {
                    "path": input.target_path,
                    "qualified_name": input.target_symbol,
                    "direction": "callers",
                },
            )

        is_retry = bool(input.previous_attempt or input.test_failure or input.previous_diff)
        version = "1.1.0" if is_retry else "1.0.0"

        base_vars = {
            "target_symbol": input.target_symbol,
            "target_path": input.target_path,
            "language": input.language,
            "directive": input.spec.directive,
            "blast_report": json.dumps(input.blast.to_artifact(), indent=2),
            "skeleton": input.skeleton,
            "characterization_test": input.characterization_test,
        }
        if is_retry:
            variables = {
                **base_vars,
                "previous_attempt": input.previous_attempt,
                "test_failure": input.test_failure,
                "previous_diff": input.previous_diff,
            }
        else:
            variables = base_vars

        prompt_result = render_prompt("writer", version, variables)
        augmented_prompt = _NO_TOOL_PREAMBLE + prompt_result.rendered

        survivors: list[WriterCandidate] = []
        attempted = 0
        model_call_failures = 0
        schema_failures = 0
        refusals = 0
        pre_check_failures = 0
        last_error: CodeGuardianError | None = None
        last_refusal_reason: str | None = None

        for seed_index in range(input.n_candidates):
            attempted += 1
            options = ModelOptions(
                temperature=_CANDIDATE_TEMPERATURE,
                max_tokens=4096,
            )
            try:
                response, latency_ms = await tracked_model_call(
                    input.provider,
                    augmented_prompt,
                    input.model,
                    options,
                    "writer",
                )
            except CodeGuardianError as exc:
                model_call_failures += 1
                last_error = exc
                continue

            _record(
                input,
                version,
                augmented_prompt,
                response,
                latency_ms,
                seed_index,
            )

            try:
                parsed = validate_output("writer", response.text)
            except CodeGuardianError:
                schema_failures += 1
                continue
            if not isinstance(parsed, SourceOutput) or parsed.is_refusal:
                refusals += 1
                if isinstance(parsed, SourceOutput) and parsed.is_refusal:
                    last_refusal_reason = parsed.refusal_reason
                continue

            candidate = _pre_check(
                original_source=original_source,
                candidate_source=parsed.content,
                spec=input.spec,
            )
            if candidate is None:
                pre_check_failures += 1
                continue
            survivors.append(candidate)

        if not survivors:
            reason = (
                f"all {attempted} candidates failed: "
                f"model={model_call_failures}, "
                f"schema={schema_failures}, "
                f"refusal={refusals}, "
                f"pre_check={pre_check_failures}"
            )
            if last_error is not None:
                reason += f"; last error: {last_error.code.value}: {last_error.message[:200]}"
            elif last_refusal_reason is not None:
                reason += f"; refusal text: {last_refusal_reason[:300]}"
            return WriterResult(
                candidates=(),
                is_refusal=True,
                refusal_reason=reason,
                total_attempted=attempted,
            )

        survivors.sort(key=lambda c: (-c.compliance_score, c.diff_size))
        return WriterResult(
            candidates=tuple(survivors),
            is_refusal=False,
            refusal_reason=None,
            total_attempted=attempted,
        )


async def _call_tool(input: WriterInput, name: str, tool_input: dict[str, object]) -> str:
    with fresh_barrier(
        input.registry,
        name,
        mode="plan",
        budget=input.budget,
        allowed_tools=("provide",),
    ) as barrier:
        return await barrier.call_async("provide", tool_input)


def _is_not_found(tool_output: str) -> bool:
    return any(
        tool_output.startswith(p)
        for p in (
            "(declaration ",
            "(file ",
            "(path ",
            "(graph ",
            "(cannot ",
        )
    )


def _pre_check(
    *,
    original_source: str,
    candidate_source: str,
    spec: RefactorSpec,
) -> WriterCandidate | None:
    """Run deterministic pre-checks; return None if any fail."""
    try:
        ast.parse(candidate_source)
    except SyntaxError:
        return None

    drift, _reason = _check_signature_drift(original_source, candidate_source)
    if not drift:
        return None

    compliance = _check_directive_compliance(candidate_source, spec)
    if compliance < _MIN_COMPLIANCE:
        return None

    diff_size = _compute_diff_size(original_source, candidate_source)
    return WriterCandidate(
        source=candidate_source,
        compliance_score=compliance,
        diff_size=diff_size,
    )


def _check_signature_drift(original_source: str, candidate_source: str) -> tuple[bool, str | None]:
    """Return (is_clean, reason). is_clean=False means drift."""
    try:
        orig_tree = ast.parse(original_source)
        cand_tree = ast.parse(candidate_source)
    except SyntaxError:
        return True, None

    orig_func = _first_function(orig_tree)
    if orig_func is None:
        return True, None

    cand_func = _first_function(cand_tree)
    if cand_func is None:
        return False, "candidate contains no function definition"

    orig_params = [a.arg for a in orig_func.args.args]
    cand_params = [a.arg for a in cand_func.args.args]
    if orig_params != cand_params:
        return (
            False,
            f"parameter names/order changed: {orig_params} -> {cand_params}",
        )

    orig_ret = ast.unparse(orig_func.returns) if orig_func.returns else None
    cand_ret = ast.unparse(cand_func.returns) if cand_func.returns else None
    if orig_ret is not None and cand_ret is not None and orig_ret != cand_ret:
        return False, f"return type changed: {orig_ret} -> {cand_ret}"

    return True, None


def _first_function(tree: ast.Module) -> ast.AST | None:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def _check_directive_compliance(candidate_source: str, spec: RefactorSpec) -> float:
    """Return compliance score [0.0, 1.0] for spec.changes."""
    try:
        tree = ast.parse(candidate_source)
    except SyntaxError:
        return 0.0

    func = _first_function(tree)
    if func is None:
        return 0.0

    specific = [c for c in spec.changes if c.kind != "apply_directive"]
    if not specific:
        return 1.0

    satisfied = 0
    for change in specific:
        if change.kind == "parameter_annotation":
            if any(a.arg == change.name and a.annotation is not None for a in func.args.args):
                satisfied += 1
        elif change.kind == "return_annotation":
            if func.returns is not None:
                satisfied += 1
        elif change.kind == "keep_signature_shape":
            satisfied += 1
    return satisfied / len(specific)


def _compute_diff_size(original: str, candidate: str) -> int:
    """Return the number of changed lines between the two sources."""
    orig = original.splitlines()
    cand = candidate.splitlines()
    matcher = difflib.SequenceMatcher(None, orig, cand)
    size = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            size += max(i2 - i1, j2 - j1)
    return size


def _record(
    input: WriterInput,
    version: str,
    prompt: str,
    response: Any,
    latency_ms: int,
    seed_index: int,
) -> None:
    from codeguardian.core.agents import _store_context

    store = _store_context.get_active_store()
    if store is None:
        return
    record_call(
        store,
        role="writer",
        outcome="success",
        target_symbol=input.target_symbol,
        prompt=prompt,
        output=response.text,
        tokens_in=response.tokens_in,
        tokens_out=response.tokens_out,
        latency_ms=latency_ms,
        model_id=response.model_id,
        prompt_role="writer",
        prompt_version=version,
        model_options={
            "temperature": _CANDIDATE_TEMPERATURE,
            "seed_index": seed_index,
        },
    )
