"""Analyst agent.

Model-backed. Turns reconnaissance evidence into a RefactorSpec (built
deterministically from tool output) plus a context pack (selected by
the model, validated against the schema).

Progressive disclosure. The agent starts with the cheapest tool and
escalates on need:

1. list-symbols — cheapest. Called first. If the file has no
   declarations, code-skeleton is skipped because a skeleton of
   nothing is nothing.
2. code-skeleton — medium. Called when list-symbols finds at least
   one declaration. Produces the "skeleton" prompt variable.
3. function-body — high. Called only when the directive implies type
   work, because only type inference needs the body.
4. call-graph-slice — medium. Called only when the blast report
   contains contract violations.

Change taxonomy. The RefactorSpec's changes carry one of thirteen
kinds. Three were present from the start (apply_directive,
parameter_annotation, return_annotation). Ten are added: rename_symbol,
extract_function, inline_function, add_parameter, remove_parameter,
change_return_type, add_guard, change_exception, reorder_statements,
add_docstring. Each change carries acceptance criteria, a risk level,
and a reversibility rating. Kinds are detected from directive keywords
with a conservative phrase-matching rule; when no specific kind is
detected, apply_directive is the fallback.

Clarification. When the directive is a single ambiguous word or one of
a small set of vague phrases, the analyst short-circuits before any
model call and returns status "needs_input" with a Clarification
carrying a question, options, and a default. This is honest asking
rather than a silent refusal.

Contradiction resolution. If the model omits the target file from its
context pack, the agent adds it and records a warning. Tool evidence
wins over model judgment; the warning records the override.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.plugins import BudgetTracker, PluginRegistry
from codeguardian.core.plugins.context_runtime import cpg_available
from codeguardian.core.prompts import (
    ContextPackOutput,
    FileSpec,
    render_prompt,
    validate_output,
)
from codeguardian.interfaces.model import ModelOptions, ModelProvider

from ._runtime import fresh_barrier, tracked_model_call
from .blast import BlastResult
from .recon import ReconResult
from .telemetry import record_call

__all__ = [
    "AnalystAgent",
    "AnalystInput",
    "AnalystResult",
    "AnalystStatus",
    "Clarification",
    "RefactorChange",
    "RefactorSpec",
]


AnalystStatus = Literal["success", "refused", "needs_input"]

_DIRECTIVE_BODY_KEYWORDS = (
    "type",
    "hint",
    "annotat",
    "signature",
    "return type",
    "param",
)

_SCHEMA_REASONS = frozenset(
    {
        "target",
        "direct_import",
        "caller",
        "callee",
        "test",
        "config",
        "contract",
    }
)

_AMBIGUOUS_DIRECTIVES = frozenset(
    {
        "improve",
        "improve it",
        "optimize",
        "optimise",
        "make it better",
        "clean up",
        "cleanup",
        "refactor",
        "simplify",
        "fix",
        "fix it",
        "update",
    }
)

_KIND_DEFAULTS: dict[str, dict[str, Any]] = {
    "apply_directive": {
        "acceptance": ("the directive's stated outcome is achieved",),
        "risk": "medium",
        "reversibility": "medium",
    },
    "parameter_annotation": {
        "acceptance": ("the parameter is annotated with the correct type",),
        "risk": "low",
        "reversibility": "easy",
    },
    "return_annotation": {
        "acceptance": ("the return type is annotated with the correct type",),
        "risk": "low",
        "reversibility": "easy",
    },
    "rename_symbol": {
        "acceptance": (
            "every reference to the old name is updated",
            "the function's behavior is unchanged",
        ),
        "risk": "medium",
        "reversibility": "easy",
    },
    "extract_function": {
        "acceptance": (
            "the extracted function preserves the original body's semantics",
            "the caller's behavior is unchanged",
        ),
        "risk": "medium",
        "reversibility": "medium",
    },
    "inline_function": {
        "acceptance": (
            "the inlined code preserves the callee's semantics",
            "the caller's behavior is unchanged",
        ),
        "risk": "high",
        "reversibility": "hard",
    },
    "add_parameter": {
        "acceptance": (
            "the new parameter is keyword-only with a default",
            "existing callers continue to work",
        ),
        "risk": "medium",
        "reversibility": "easy",
    },
    "remove_parameter": {
        "acceptance": (
            "every caller is updated",
            "the parameter is not referenced in the body",
        ),
        "risk": "high",
        "reversibility": "hard",
    },
    "change_return_type": {
        "acceptance": (
            "every caller accepts the new type",
            "the characterization test still passes",
        ),
        "risk": "high",
        "reversibility": "medium",
    },
    "add_guard": {
        "acceptance": (
            "the guarded condition does not change unguarded behavior",
            "the guard raises on the expected edge case",
        ),
        "risk": "medium",
        "reversibility": "easy",
    },
    "change_exception": {
        "acceptance": (
            "every caller catches the new exception",
            "the old exception is no longer raised in the changed path",
        ),
        "risk": "high",
        "reversibility": "medium",
    },
    "reorder_statements": {
        "acceptance": (
            "no observable side-effect order changes",
            "the characterization test still passes",
        ),
        "risk": "high",
        "reversibility": "hard",
    },
    "add_docstring": {
        "acceptance": (
            "the docstring describes the current behavior accurately",
            "the function body is unchanged",
        ),
        "risk": "low",
        "reversibility": "easy",
    },
}

_ADD_PARAMETER_RE = re.compile(r"\badd\s+(?:a\s+)?(?:new\s+)?(?:param|arg(?:ument)?)\b")
_REMOVE_PARAMETER_RE = re.compile(
    r"\b(?:remove|drop|delete)\s+(?:a\s+)?(?:the\s+)?(?:param|arg(?:ument)?)\b"
)


@dataclass(frozen=True, slots=True)
class Clarification:
    """A question the analyst asks instead of refusing.

    Attributes:
        question: The question the caller should answer.
        options: Concrete answers the caller can pick from.
        default: The analyst's suggested answer. None when no
            suggestion is reasonable.
    """

    question: str
    options: tuple[str, ...]
    default: str | None = None


@dataclass(frozen=True, slots=True)
class RefactorChange:
    """One change the spec requires.

    Attributes:
        kind: One of the thirteen change kinds.
        name: The symbol affected by this change, when relevant.
        inferred_type: The type inferred for annotation changes, when
            the analyst can determine it from the body.
        reason: The directive text or rationale this change addresses.
        acceptance: The criteria that must hold for the change to be
            considered complete.
        risk: "low", "medium", or "high". Reflects how much caller
            breakage the change can cause.
        reversibility: "easy", "medium", or "hard". Reflects how
            cleanly the change can be rolled back.
    """

    kind: str
    name: str | None = None
    inferred_type: str | None = None
    reason: str | None = None
    acceptance: tuple[str, ...] = ()
    risk: str = "medium"
    reversibility: str = "easy"

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-shaped form."""
        result: dict[str, Any] = {"kind": self.kind}
        if self.name is not None:
            result["name"] = self.name
        if self.inferred_type is not None:
            result["inferred_type"] = self.inferred_type
        if self.reason is not None:
            result["reason"] = self.reason
        if self.acceptance:
            result["acceptance"] = list(self.acceptance)
        result["risk"] = self.risk
        result["reversibility"] = self.reversibility
        return result


@dataclass(frozen=True, slots=True)
class RefactorSpec:
    """Structured refactor specification produced by the Analyst."""

    directive: str
    target: str
    changes: tuple[RefactorChange, ...]
    constraints: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-shaped form for storage in extensions."""
        return {
            "directive": self.directive,
            "target": self.target,
            "changes": [c.to_dict() for c in self.changes],
            "constraints": list(self.constraints),
        }


@dataclass(frozen=True, slots=True)
class AnalystInput:
    """Input to the analyst agent."""

    blast: BlastResult
    recon: ReconResult
    directive: str
    registry: PluginRegistry
    provider: ModelProvider
    model: str
    budget: BudgetTracker
    clone_root: Path
    token_budget: int = 12_000
    prompt_version: str = "1.0.0"


@dataclass(frozen=True, slots=True)
class AnalystResult:
    """Output of the analyst agent.

    Attributes:
        spec: The structured RefactorSpec, always present.
        context_pack: The validated model output, always present.
        context_pack_artifact: Schema-conforming context-pack.json
            content, or None on refusal or needs_input.
        status: "success", "refused", or "needs_input".
        clarification: The question to ask the caller, populated only
            when status is "needs_input".
        alternatives: Alternative specs the caller supplied for
            consideration. Empty in this block; Phase 13 populates
            from a critic pass.
        warnings: Non-fatal issues (contradiction overrides, etc.).
    """

    spec: RefactorSpec
    context_pack: ContextPackOutput
    context_pack_artifact: dict[str, Any] | None
    status: AnalystStatus
    clarification: Clarification | None = None
    alternatives: tuple[RefactorSpec, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def is_refusal(self) -> bool:
        """True when the analyst refused to produce a spec."""
        return self.status == "refused"

    @property
    def needs_input(self) -> bool:
        """True when the analyst is asking for clarification."""
        return self.status == "needs_input"

    @property
    def is_usable(self) -> bool:
        """True when the spec and context pack are ready for the next agent."""
        return self.status == "success"


class AnalystAgent:
    """Model-backed analyst agent."""

    name = "analyst"
    phase = "analyst"
    is_deterministic = False
    requires_tools = (
        "list-symbols",
        "code-skeleton",
        "function-body",
        "call-graph-slice",
    )
    can_refuse = True
    prompt_role = "analyst"
    prompt_version = "1.0.0"

    async def run(self, input: AnalystInput) -> AnalystResult:
        """Run the analyst agent.

        Args:
            input: The analyst input.

        Returns:
            An AnalystResult with status set.

        Raises:
            CodeGuardianError: SYS_NOT_IMPLEMENTED when the kernel
                extension is unavailable.
        """
        clarification = _detect_ambiguity(input.directive)
        if clarification is not None:
            return _needs_input_result(input, clarification)

        try:
            from codeguardian import _native
        except ImportError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_NOT_IMPLEMENTED,
                "analyst requires the compiled kernel extension",
            ) from exc

        cpg = _native.PyCPG(input.recon.cpg_bytes)
        target_path = input.blast.target_path
        target_symbol = input.blast.target_qualified_name

        with cpg_available(cpg, input.clone_root):
            try:
                return await self._run(input, target_path, target_symbol)
            except CodeGuardianError as exc:
                if exc.code is ErrorCode.PLUGIN_TIMEOUT and exc.details.get("budget"):
                    return _budget_refusal(input, exc)
                raise

    async def _run(
        self,
        input: AnalystInput,
        target_path: str,
        target_symbol: str,
    ) -> AnalystResult:
        """Execute the analyst body with the CPG context active."""
        symbols_text = await _call_tool(input, "list-symbols", {"path": target_path})
        if "(no declarations)" in symbols_text:
            skeleton = symbols_text
        else:
            skeleton = await _call_tool(input, "code-skeleton", {"path": target_path})

        pre_warnings: list[str] = []
        body: str | None = None
        if _needs_body(input.directive):
            raw_body = await _call_tool(
                input,
                "function-body",
                {"path": target_path, "qualified_name": target_symbol},
            )
            if _is_not_found(raw_body):
                _record_tool_failure(
                    input,
                    "function-body",
                    {"path": target_path, "qualified_name": target_symbol},
                    raw_body,
                )
                pre_warnings.append(
                    f"function-body could not resolve {target_symbol!r} "
                    f"in {target_path!r}: {raw_body[:100]}"
                )
                body = None
            else:
                body = raw_body

        if input.blast.contract_violations:
            await _call_tool(
                input,
                "call-graph-slice",
                {
                    "path": target_path,
                    "qualified_name": target_symbol,
                    "direction": "callers",
                },
            )

        spec, spec_warnings = _build_refactor_spec(
            directive=input.directive,
            target=f"{target_path}:{target_symbol}",
            body=body,
            blast=input.blast,
        )
        pre_warnings.extend(spec_warnings)

        variables = {
            "target_symbol": target_symbol,
            "target_path": target_path,
            "directive": input.directive,
            "blast_report": json.dumps(input.blast.to_artifact(), indent=2),
            "skeleton": skeleton,
        }
        prompt_result = render_prompt("analyst", input.prompt_version, variables)

        try:
            response, latency_ms = await tracked_model_call(
                input.provider,
                prompt_result.rendered,
                input.model,
                ModelOptions(),
                "analyst",
            )
        except CodeGuardianError as exc:
            if exc.code in (
                ErrorCode.MODEL_CONTEXT_EXCEEDED,
                ErrorCode.MODEL_RATE_LIMITED,
                ErrorCode.MODEL_UNAVAILABLE,
                ErrorCode.PROVIDER_UNAVAILABLE,
            ):
                return _model_refusal(
                    input,
                    spec,
                    exc,
                    pre_warnings=tuple(pre_warnings),
                )
            raise

        try:
            parsed = json.loads(response.text)
        except json.JSONDecodeError as exc:
            return _refuse_invalid_output(
                input,
                spec,
                f"invalid JSON: {exc}",
                prompt_result,
                response,
                latency_ms,
                pre_warnings=tuple(pre_warnings),
            )

        try:
            context_pack = validate_output("analyst", parsed)
        except CodeGuardianError as exc:
            return _refuse_invalid_output(
                input,
                spec,
                f"schema: {exc}",
                prompt_result,
                response,
                latency_ms,
                pre_warnings=tuple(pre_warnings),
            )

        if not isinstance(context_pack, ContextPackOutput):
            return _refuse_invalid_output(
                input,
                spec,
                "unexpected output type",
                prompt_result,
                response,
                latency_ms,
                pre_warnings=tuple(pre_warnings),
            )

        if not context_pack.files or context_pack.reason.startswith("refused:"):
            _record_call(
                input,
                spec,
                context_pack,
                prompt_result,
                response,
                latency_ms,
                is_refusal=True,
            )
            return AnalystResult(
                spec=spec,
                context_pack=context_pack,
                context_pack_artifact=None,
                status="refused",
                warnings=tuple(pre_warnings),
            )

        warnings: list[str] = list(pre_warnings)
        files_set = {f.path for f in context_pack.files}
        if target_path not in files_set:
            warnings.append(f"model omitted target file {target_path!r}; added by tool evidence")
            context_pack = ContextPackOutput(
                role="analyst",
                files=(FileSpec(path=target_path, reason="target"), *context_pack.files),
                reason=context_pack.reason,
            )

        artifact = _build_context_pack_artifact(
            input=input,
            target_path=target_path,
            target_symbol=target_symbol,
            context_pack=context_pack,
            spec=spec,
        )

        _record_call(
            input,
            spec,
            context_pack,
            prompt_result,
            response,
            latency_ms,
            is_refusal=False,
        )

        return AnalystResult(
            spec=spec,
            context_pack=context_pack,
            context_pack_artifact=artifact,
            status="success",
            warnings=tuple(warnings),
        )


def _detect_change_kinds(directive: str) -> list[str]:
    """Return the change kinds inferred from the directive text.

    Detection is conservative: only explicit phrasings trigger a
    specific kind. "add type hints to every parameter and the return
    type" describes what to annotate, not what to add or change, so it
    does not trigger add_parameter or change_return_type.
    """
    lowered = directive.lower()
    kinds: list[str] = []

    if "rename" in lowered:
        kinds.append("rename_symbol")
    if "extract" in lowered and any(word in lowered for word in ("function", "helper", "method")):
        kinds.append("extract_function")
    if "inline" in lowered:
        kinds.append("inline_function")
    if _ADD_PARAMETER_RE.search(lowered):
        kinds.append("add_parameter")
    if _REMOVE_PARAMETER_RE.search(lowered):
        kinds.append("remove_parameter")
    if "change the return type" in lowered or "change return type" in lowered:
        kinds.append("change_return_type")
    if "add a guard" in lowered or "add guard" in lowered:
        kinds.append("add_guard")
    if "change the exception" in lowered or "change exception" in lowered:
        kinds.append("change_exception")
    if "reorder" in lowered:
        kinds.append("reorder_statements")
    if "docstring" in lowered or "add documentation" in lowered:
        kinds.append("add_docstring")

    return kinds


def _detect_ambiguity(directive: str) -> Clarification | None:
    """Return a Clarification when the directive is too vague to plan."""
    lowered = directive.strip().lower()

    if lowered in _AMBIGUOUS_DIRECTIVES:
        return Clarification(
            question=(
                f"The directive {directive!r} is too ambiguous to plan. "
                "Which specific change should be made to the target?"
            ),
            options=(
                "add type hints",
                "add a docstring",
                "extract a helper function",
                "add a guard clause",
                "rename a symbol",
            ),
            default="add type hints",
        )

    words = lowered.split()
    if len(words) < 2 and not _detect_change_kinds(lowered):
        return Clarification(
            question=(
                f"The directive {directive!r} is too short to plan. "
                "What change should be made to the target?"
            ),
            options=(
                "add type hints",
                "add a docstring",
                "add a guard clause",
            ),
            default="add type hints",
        )

    return None


def _needs_input_result(
    input: AnalystInput,
    clarification: Clarification,
) -> AnalystResult:
    """Return a needs_input result with a stub spec and context pack."""
    target = f"{input.blast.target_path}:{input.blast.target_qualified_name}"
    spec = RefactorSpec(
        directive=input.directive,
        target=target,
        changes=(),
        constraints=(),
    )
    context_pack = ContextPackOutput(
        role="analyst",
        files=(),
        reason=f"needs_input: {clarification.question}",
    )
    return AnalystResult(
        spec=spec,
        context_pack=context_pack,
        context_pack_artifact=None,
        status="needs_input",
        clarification=clarification,
        warnings=(),
    )


async def _call_tool(input: AnalystInput, name: str, tool_input: dict[str, object]) -> str:
    """Call one context provider under the agent's budget and mode."""
    with fresh_barrier(
        input.registry,
        name,
        mode="plan",
        budget=input.budget,
        allowed_tools=("provide",),
    ) as barrier:
        return await barrier.call_async("provide", tool_input)


def _needs_body(directive: str) -> bool:
    """Return True if the directive implies the target body is needed."""
    lowered = directive.lower()
    return any(keyword in lowered for keyword in _DIRECTIVE_BODY_KEYWORDS)


_NOT_FOUND_PREFIXES = (
    "(declaration ",
    "(file ",
    "(path ",
    "(graph ",
    "(cannot ",
)


def _is_not_found(tool_output: str) -> bool:
    """Return True when a tool response is a not-found marker.

    Context provider plugins return a parenthesized message beginning
    with one of several prefixes when they cannot resolve the request.
    The prefixes match those the ``function_body`` plugin emits for its
    four error cases (declaration not found, file not found, path
    escapes root, stale offsets) plus the ``(cannot ...)`` form the
    bundled providers use for read failures. Detecting them lets the
    analyst surface a warning and a decision-log entry instead of
    handing the string to ``ast.parse`` and letting the resulting
    SyntaxError silently degrade the spec.
    """
    return any(tool_output.startswith(prefix) for prefix in _NOT_FOUND_PREFIXES)


def _build_refactor_spec(
    *,
    directive: str,
    target: str,
    body: str | None,
    blast: BlastResult,
) -> tuple[RefactorSpec, tuple[str, ...]]:
    """Build the RefactorSpec deterministically from tool evidence.

    Returns a two-tuple of ``(spec, warnings)``. The warnings list is
    populated when the body was required by the directive but could
    not be used for annotation extraction: parse failure, empty body,
    or a top-level statement that is not a function definition. Each
    warning names the specific failure so a caller reading
    ``AnalystResult.warnings`` knows the spec may be degraded rather
    than complete.

    The tool-not-found case is handled by the caller (``_run``) before
    this function is called; when the body was not found, the caller
    passes ``body=None`` and this function does not attempt annotation
    extraction at all.
    """
    warnings: list[str] = []
    changes: list[RefactorChange] = []

    for kind in _detect_change_kinds(directive):
        defaults = _KIND_DEFAULTS.get(kind, _KIND_DEFAULTS["apply_directive"])
        changes.append(
            RefactorChange(
                kind=kind,
                reason=directive,
                acceptance=defaults["acceptance"],
                risk=defaults["risk"],
                reversibility=defaults["reversibility"],
            )
        )

    if not changes:
        defaults = _KIND_DEFAULTS["apply_directive"]
        changes.append(
            RefactorChange(
                kind="apply_directive",
                reason=directive,
                acceptance=defaults["acceptance"],
                risk=defaults["risk"],
                reversibility=defaults["reversibility"],
            )
        )

    if body is not None and _needs_body(directive):
        try:
            tree = ast.parse(body)
        except SyntaxError as exc:
            warnings.append(
                f"function body did not parse ({exc.msg} at line "
                f"{exc.lineno}); annotation extraction skipped"
            )
            tree = None

        if tree is not None:
            if not tree.body:
                warnings.append("function body is empty; annotation extraction skipped")
            else:
                func = tree.body[0]
                if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for arg in func.args.args:
                        if arg.arg == "self":
                            continue
                        if arg.annotation is None:
                            defaults = _KIND_DEFAULTS["parameter_annotation"]
                            changes.append(
                                RefactorChange(
                                    kind="parameter_annotation",
                                    name=arg.arg,
                                    inferred_type=None,
                                    acceptance=defaults["acceptance"],
                                    risk=defaults["risk"],
                                    reversibility=defaults["reversibility"],
                                )
                            )
                    if func.returns is None:
                        inferred = _infer_return_type(func)
                        defaults = _KIND_DEFAULTS["return_annotation"]
                        changes.append(
                            RefactorChange(
                                kind="return_annotation",
                                inferred_type=inferred,
                                acceptance=defaults["acceptance"],
                                risk=defaults["risk"],
                                reversibility=defaults["reversibility"],
                            )
                        )
                else:
                    warnings.append(
                        f"function body's first statement is "
                        f"{type(func).__name__}, not a function definition; "
                        "annotation extraction skipped"
                    )

    constraints: list[str] = []
    if blast.direct_callers:
        constraints.append("keep positional parameters; callers pass positionally")
    for assertion in blast.contract_assertions:
        constraints.append(f"preserve behavior: {assertion.assumption}")
    if blast.recommendation_adjusted == "block":
        constraints.append("high blast risk; verify all callers before apply")

    return (
        RefactorSpec(
            directive=directive,
            target=target,
            changes=tuple(changes),
            constraints=tuple(constraints),
        ),
        tuple(warnings),
    )


def _infer_return_type(func: ast.AST) -> str | None:
    """Infer the return type from return statements, or None if unknown."""
    returns = [node for node in ast.walk(func) if isinstance(node, ast.Return)]
    if not returns:
        return "None"
    types: set[str] = set()
    for ret in returns:
        inferred = _type_of_expression(ret.value)
        if inferred is None:
            return None
        types.add(inferred)
    return types.pop() if len(types) == 1 else None


def _type_of_expression(node: ast.AST | None) -> str | None:
    """Infer a type from a simple expression node, or None if unknown."""
    if node is None:
        return "None"
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool):
            return "bool"
        if isinstance(value, int):
            return "int"
        if isinstance(value, float):
            return "float"
        if isinstance(value, str):
            return "str"
        if value is None:
            return "None"
        return None
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr in (
            "strip",
            "lower",
            "upper",
            "title",
            "capitalize",
        ):
            return "str"
        if isinstance(node.func, ast.Name):
            if node.func.id == "len":
                return "int"
            if node.func.id == "str":
                return "str"
            if node.func.id == "int":
                return "int"
            if node.func.id == "float":
                return "float"
            if node.func.id == "bool":
                return "bool"
        return None
    if isinstance(node, ast.List):
        return "list"
    if isinstance(node, ast.Dict):
        return "dict"
    if isinstance(node, ast.Set):
        return "set"
    return None


def _build_context_pack_artifact(
    *,
    input: AnalystInput,
    target_path: str,
    target_symbol: str,
    context_pack: ContextPackOutput,
    spec: RefactorSpec,
) -> dict[str, Any]:
    """Build the schema-conforming context-pack.json artifact."""
    files_artifact: list[dict[str, Any]] = []
    token_count = 0
    for entry in context_pack.files:
        full = input.clone_root / entry.path
        try:
            content_bytes = full.read_bytes()
            content_hash = hashlib.sha256(content_bytes).hexdigest()
            token_count += len(content_bytes) // 4
        except OSError:
            content_hash = ""
        files_artifact.append(
            {
                "path": entry.path,
                "content_hash": content_hash,
                "reason": _map_reason(entry.reason),
            }
        )

    truncated = token_count > input.token_budget
    return {
        "schema_version": "1.1",
        "target": {
            "path": target_path,
            "language": "python",
            "symbol": target_symbol,
        },
        "blast_aware": True,
        "files": files_artifact,
        "contract_assertions": [
            {
                "caller": a.caller,
                "assumption": a.assumption,
                "severity": a.severity,
            }
            for a in input.blast.contract_assertions
        ],
        "token_budget": input.token_budget,
        "token_count": token_count,
        "truncated": truncated,
        "truncation_strategy": "drop_oldest" if truncated else "none",
        "extensions": {"refactor_spec": spec.to_dict()},
    }


def _map_reason(reason: str) -> str:
    """Map a free-form reason to the schema's enumerated set."""
    lowered = reason.lower()
    if lowered in _SCHEMA_REASONS:
        return lowered
    if "target" in lowered:
        return "target"
    if "caller" in lowered:
        return "caller"
    if "callee" in lowered:
        return "callee"
    if "test" in lowered:
        return "test"
    if "contract" in lowered or "assert" in lowered:
        return "contract"
    if "import" in lowered:
        return "direct_import"
    return "config"


def _budget_refusal(input: AnalystInput, exc: CodeGuardianError) -> AnalystResult:
    """Return a structured refusal for budget exhaustion."""
    target = f"{input.blast.target_path}:{input.blast.target_qualified_name}"
    spec = RefactorSpec(
        directive=input.directive,
        target=target,
        changes=(),
        constraints=(),
    )
    context_pack = ContextPackOutput(
        role="analyst",
        files=(),
        reason=f"refused: budget_exhausted ({exc.message})",
    )
    return AnalystResult(
        spec=spec,
        context_pack=context_pack,
        context_pack_artifact=None,
        status="refused",
        warnings=(),
    )


def _model_refusal(
    input: AnalystInput,
    spec: RefactorSpec,
    exc: CodeGuardianError,
    *,
    pre_warnings: tuple[str, ...] = (),
) -> AnalystResult:
    """Return a structured refusal for an unserviceable model request.

    A request the model cannot service is a refusal, not a phase
    failure. The three cases this covers are context-window overflow
    (the target's context pack is larger than the model's input
    window), rate limiting, and provider unavailability. In each case
    the analyst cannot produce a spec and the pipeline should continue
    with a refusal recorded at the analyst slot rather than aborting.

    No telemetry row is written. The Phase 11 ``record_call`` helper
    requires a ``ModelResponse``, which does not exist when the call
    raised. The refusal is recorded on the ``AnalystResult`` and the
    loop writes the phase-level audit entry.
    """
    context_pack = ContextPackOutput(
        role="analyst",
        files=(),
        reason=(f"refused: {exc.code.value} ({exc.message[:150]})"),
    )
    return AnalystResult(
        spec=spec,
        context_pack=context_pack,
        context_pack_artifact=None,
        status="refused",
        warnings=(
            *pre_warnings,
            f"model request refused: {exc.code.value}: {exc.message[:200]}",
        ),
    )


def _refuse_invalid_output(
    input: AnalystInput,
    spec: RefactorSpec,
    reason: str,
    prompt_result: Any,
    response: Any,
    latency_ms: int,
    *,
    pre_warnings: tuple[str, ...] = (),
) -> AnalystResult:
    """Return a structured refusal for a model output that failed schema.

    The ``pre_warnings`` argument carries non-fatal findings recorded
    before the model call, for example a function-body not-found
    response. Merging them here means the caller's
    ``AnalystResult.warnings`` contains the full picture even when the
    model output was rejected.
    """
    context_pack = ContextPackOutput(
        role="analyst",
        files=(),
        reason=f"refused: {reason}",
    )
    _record_call(
        input,
        spec,
        context_pack,
        prompt_result,
        response,
        latency_ms,
        is_refusal=True,
        outcome_override="schema_violation",
        failure_category="schema",
    )
    return AnalystResult(
        spec=spec,
        context_pack=context_pack,
        context_pack_artifact=None,
        status="refused",
        warnings=(*pre_warnings, f"schema violation: {reason}"),
    )


def _record_call(
    input: AnalystInput,
    spec: RefactorSpec,
    context_pack: ContextPackOutput,
    prompt_result: Any,
    response: Any,
    latency_ms: int,
    *,
    is_refusal: bool,
    outcome_override: str | None = None,
    failure_category: Any = None,
) -> None:
    """Record the analyst call when a store is available."""
    from codeguardian.core.agents import _store_context

    store = _store_context.get_active_store()
    if store is None:
        return
    record_call(
        store,
        role="analyst",
        outcome=outcome_override or ("refused" if is_refusal else "success"),
        failure_category=failure_category,
        target_symbol=input.blast.target_qualified_name,
        prompt=prompt_result.rendered,
        output=response.text,
        tokens_in=response.tokens_in,
        tokens_out=response.tokens_out,
        latency_ms=latency_ms,
        model_id=response.model_id,
        prompt_role="analyst",
        prompt_version=input.prompt_version,
        model_options={"temperature": 0.0},
    )


def _record_tool_failure(
    input: AnalystInput,
    tool_name: str,
    tool_input: dict[str, object],
    tool_output: str,
) -> None:
    """Record a failed tool call as a telemetry row.

    Called when ``function-body`` cannot resolve the target. Records a
    decision_log entry naming the specific failure and a tool_chain
    entry naming the tool that produced it. Uses the same active-store
    check as ``_record_call`` so absence of a store (unit tests,
    isolated evals) is a silent skip rather than an error.

    This is the Phase 11 section 2.3 fix for the analyst: a
    function-body lookup that fails is now visible in the decision log
    rather than only in the spec-warnings list.
    """
    from codeguardian.core.agents import _store_context

    store = _store_context.get_active_store()
    if store is None:
        return
    record_call(
        store,
        role="analyst",
        outcome="refused",
        refusal_code="target_missing",
        target_symbol=input.blast.target_qualified_name,
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
