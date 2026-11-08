"""Writer agent.

Model-backed. Reads the RefactorSpec. Generates N candidates with
different seeds and different strategy hints. Deterministic pre-check
per candidate: signature drift (Python only), ast.parse on the
candidate, directive compliance score. Candidates that survive the
pre-check are then verified in a sandbox when the caller supplies one:
the candidate is spliced into the target file and the characterization
test is run against the spliced file. Survivors are ranked by
verification (test pass first, verifier score next, compliance next,
diff size last).

The Writer's final status reflects both candidate production and blast
context. Status is "success" when candidates were produced without a
block recommendation, "partial" when candidates were produced under a
block recommendation (the caller must attach human approval), and
"refused" when no candidate survived the pre-check.

On retry: when the input carries ``previous_attempt``, ``test_failure``,
or ``previous_diff``, the agent uses prompt v1.1.0 with those variables
populated.
"""

from __future__ import annotations

import ast
import contextlib
import difflib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.plugins import BudgetTracker, PluginRegistry
from codeguardian.core.prompts import (
    SourceOutput,
    render_prompt,
    validate_output,
)
from codeguardian.interfaces.model import ModelOptions, ModelProvider

from ._runtime import fresh_barrier, tracked_model_call
from ._verifier_common import SandboxExecutor
from .analyst import RefactorSpec
from .blast import BlastResult
from .telemetry import record_call

__all__ = [
    "WriterAgent",
    "WriterCandidate",
    "WriterInput",
    "WriterResult",
]


WriterStatus = Literal["success", "partial", "refused"]

_MIN_COMPLIANCE = 0.5
_CANDIDATE_TEMPERATURE = 0.2
_DEFAULT_VERIFIER_TIMEOUT_S = 120.0

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

_STRATEGY_HINTS: tuple[str, ...] = (
    "Prefer the most minimal change that satisfies the directive.",
    "If the directive allows, extract a helper function to reduce complexity.",
    "If the directive allows, add a guard clause for edge cases.",
    "If the directive allows, add a docstring explaining the function's contract.",
    "Preserve every existing line; make only the changes the directive requires.",
)


@dataclass(frozen=True, slots=True)
class WriterInput:
    """Input to the writer agent.

    Attributes:
        sandbox_executor: Optional sandbox for splice-and-verify. When
            None, pre-check is the only gate and verification fields
            on candidates remain None.
        characterization_test_path: Path to the characterization test
            inside the sandbox. Required when sandbox_executor is set
            for the sandbox test run to fire.
        verifier_score_fn: Optional callback that takes a candidate
            source string and returns a score in the range 0 through
            100. Used by the ranking. Exceptions from the callback are
            caught and the score is left as None for that candidate.
    """

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
    sandbox_executor: SandboxExecutor | None = None
    characterization_test_path: str | None = None
    verifier_score_fn: Callable[[str], float] | None = None


@dataclass(frozen=True, slots=True)
class WriterCandidate:
    """One refactor candidate that passed the pre-check.

    Attributes:
        source: The candidate function source.
        compliance_score: Fraction of the spec changes present, 0.0 to
            1.0.
        diff_size: Number of changed lines against the original.
        verification: A dict populated when a sandbox was supplied.
            Keys: ``splices`` (bool), ``parses`` (bool), ``tests_pass``
            (True, False, or None when the test did not run),
            ``verifier_score`` (float or None). All four are the
            default (True, True, None, None) when no sandbox was used.
    """

    source: str
    compliance_score: float
    diff_size: int
    verification: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class WriterResult:
    """Output of the writer agent.

    Attributes:
        candidates: Survivors ranked by verification, then compliance,
            then diff size. Empty when status is "refused".
        status: One of "success", "partial", "refused".
            "success" means candidates were produced without a block
            recommendation. "partial" means candidates were produced
            under a block recommendation and require human approval
            before apply. "refused" means no candidate survived
            pre-check.
        requires: Pre-apply requirements. Set to
            ``("human_approval",)`` when status is "partial". Empty
            otherwise.
        refusal_reason: Human-readable reason when status is
            "refused", None otherwise.
        total_attempted: Number of model calls made.
        warnings: Non-fatal findings recorded during the run. Surfaced
            when the agent could not do its job cleanly but did not
            fail outright, for example when the ``function-body`` tool
            returned a not-found response. Empty by default.
    """

    candidates: tuple[WriterCandidate, ...]
    status: WriterStatus
    requires: tuple[str, ...] = ()
    refusal_reason: str | None = None
    total_attempted: int = 0
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in ("success", "partial", "refused"):
            raise ValueError(f"invalid status: {self.status!r}")

    @property
    def is_refusal(self) -> bool:
        """True when the agent refused to produce candidates."""
        return self.status == "refused"


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
        """Run the writer agent."""
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
            _record_tool_failure(
                input,
                "function-body",
                {"path": input.target_path, "qualified_name": input.target_symbol},
                body,
            )
            return WriterResult(
                candidates=(),
                status="refused",
                refusal_reason=f"target body not found: {body[:100]}",
                total_attempted=0,
                warnings=(
                    f"function-body did not find {input.target_symbol!r} in {input.target_path!r}",
                ),
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
            "function_body": original_source,
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

        is_block = input.blast.recommendation_adjusted == "block"

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
            strategy_prompt = _augment_with_strategy(augmented_prompt, seed_index)
            options = ModelOptions(
                temperature=_CANDIDATE_TEMPERATURE,
                max_tokens=4096,
            )
            try:
                response, latency_ms = await tracked_model_call(
                    input.provider,
                    strategy_prompt,
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
                strategy_prompt,
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

            verification = _verify_candidate(input, parsed.content)
            candidate_with_verification = WriterCandidate(
                source=candidate.source,
                compliance_score=candidate.compliance_score,
                diff_size=candidate.diff_size,
                verification=verification,
            )
            survivors.append(candidate_with_verification)

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
                status="refused",
                refusal_reason=reason,
                total_attempted=attempted,
            )

        ranked = _rank_candidates(survivors)

        if is_block:
            return WriterResult(
                candidates=tuple(ranked),
                status="partial",
                requires=("human_approval",),
                refusal_reason=None,
                total_attempted=attempted,
            )

        return WriterResult(
            candidates=tuple(ranked),
            status="success",
            requires=(),
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


def _augment_with_strategy(prompt: str, seed_index: int) -> str:
    """Append a strategy hint that differs per candidate."""
    hint = _STRATEGY_HINTS[seed_index % len(_STRATEGY_HINTS)]
    return f"{prompt}\n\n# Strategy for this attempt\n\n{hint}\n"


def _rank_candidates(candidates: list[WriterCandidate]) -> list[WriterCandidate]:
    """Rank candidates by verification, then compliance, then diff size.

    Primary sort: tests_pass descending (True before False before None).
    Secondary sort: verifier_score descending.
    Tertiary sort: compliance_score descending.
    Quaternary sort: diff_size ascending.
    """

    def key(c: WriterCandidate) -> tuple[int, float, float, int]:
        v = c.verification or {}
        tests_pass = 1 if v.get("tests_pass") is True else 0
        verifier_score = float(v.get("verifier_score") or 0.0)
        return (-tests_pass, -verifier_score, -c.compliance_score, c.diff_size)

    return sorted(candidates, key=key)


def _verify_candidate(
    input: WriterInput,
    candidate_source: str,
) -> dict[str, Any]:
    """Splice the candidate into the target file and run the test.

    Returns a verification dict with keys ``splices``, ``parses``,
    ``tests_pass``, and ``verifier_score``. When no sandbox is
    available, tests_pass remains None and the other flags reflect the
    in-memory splice check only.
    """
    result: dict[str, Any] = {
        "splices": True,
        "parses": True,
        "tests_pass": None,
        "verifier_score": None,
    }

    if input.verifier_score_fn is not None:
        with contextlib.suppress(Exception):
            result["verifier_score"] = float(input.verifier_score_fn(candidate_source))

    executor = input.sandbox_executor
    if executor is None or not hasattr(executor, "write_file"):
        return result

    target_file = input.clone_root / input.target_path
    try:
        file_source = target_file.read_text(encoding="utf-8")
    except OSError:
        return result

    spliced = _splice_into_file(file_source, input.target_symbol, candidate_source)
    if spliced is None:
        result["splices"] = False
        result["parses"] = False
        return result

    try:
        ast.parse(spliced)
    except SyntaxError:
        result["parses"] = False
        return result

    try:
        executor.write_file(input.target_path, spliced)
    except Exception:
        return result

    if input.characterization_test_path:
        try:
            outcome = executor.run(
                [
                    "python3",
                    "run_pytest.py",
                    input.characterization_test_path,
                    "-q",
                    "--tb=no",
                ],
                timeout_s=_DEFAULT_VERIFIER_TIMEOUT_S,
            )
            result["tests_pass"] = outcome.exit_code == 0
        except Exception:
            pass

    with contextlib.suppress(Exception):
        executor.write_file(input.target_path, file_source)

    return result


def _splice_into_file(
    file_source: str,
    qualified_name: str,
    candidate: str,
) -> str | None:
    """Replace the target function in file_source with candidate.

    Walks the qualified name through the AST. Returns the spliced
    source when the replacement parses; None otherwise.
    """
    try:
        tree = ast.parse(file_source)
    except SyntaxError:
        return None

    parts = qualified_name.split(".")
    current_scope: list[ast.stmt] = list(tree.body)
    target: ast.AST | None = None

    for part in parts:
        found: ast.AST | None = None
        for node in current_scope:
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == part
            ):
                found = node
                break
        if found is None:
            return None
        target = found
        current_scope = list(found.body) if isinstance(found, ast.ClassDef) else []

    if target is None:
        return None

    segment = ast.get_source_segment(file_source, target)
    if segment is None:
        return None

    spliced = file_source.replace(segment, candidate, 1)
    try:
        ast.parse(spliced)
    except SyntaxError:
        return None
    return spliced


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
    if diff_size == 0:
        return None

    return WriterCandidate(
        source=candidate_source,
        compliance_score=compliance,
        diff_size=diff_size,
        verification=None,
    )


def _check_signature_drift(original_source: str, candidate_source: str) -> tuple[bool, str | None]:
    """Return (is_clean, reason). is_clean=False means drift.

    A syntax error in either the original or the candidate is treated
    as a rejection. When the original does not parse we cannot verify
    that the candidate preserves its signature, and the safe choice is
    to refuse the candidate rather than bypass the check. When the
    candidate does not parse the pre-check would normally reject it
    first; a direct caller that bypasses the pre-check still gets a
    clear rejection rather than a silent pass.
    """
    try:
        orig_tree = ast.parse(original_source)
    except SyntaxError as exc:
        return False, f"original source did not parse: {exc}"

    try:
        cand_tree = ast.parse(candidate_source)
    except SyntaxError as exc:
        return False, f"candidate source did not parse: {exc}"

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


def _record_tool_failure(
    input: WriterInput,
    tool_name: str,
    tool_input: dict[str, object],
    tool_output: str,
) -> None:
    """Record a failed tool call as a telemetry row.

    Called when ``function-body`` cannot resolve the target. Records a
    decision_log entry naming the specific failure and a tool_chain
    entry naming the tool that produced it. Uses the same active-store
    check as ``_record`` so absence of a store (unit tests, isolated
    evals) is a silent skip rather than an error.

    This is the Phase 11 section 2.3 fix for the writer: a tool lookup
    that fails is now visible in the decision log rather than only in
    the refusal text.
    """
    from codeguardian.core.agents import _store_context

    store = _store_context.get_active_store()
    if store is None:
        return
    record_call(
        store,
        role="writer",
        outcome="refused",
        refusal_code="target_missing",
        target_symbol=input.target_symbol or None,
        decision_log=[
            {
                "step": "function_body_lookup",
                "candidate": None,
                "verdict": "not_found",
                "reason": tool_output[:100],
            }
        ],
        tool_chain=[
            {
                "tool": tool_name,
                "input": tool_input,
                "output_bytes": len(tool_output),
            }
        ],
    )
