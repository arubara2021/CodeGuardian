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

Contradiction resolution. If the model omits the target file from its
context pack, the agent adds it and records a warning. Tool evidence
wins over model judgment; the warning records the override.
"""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    "RefactorChange",
    "RefactorSpec",
]


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


@dataclass(frozen=True, slots=True)
class RefactorChange:
    """One change the spec requires."""

    kind: str
    name: str | None = None
    inferred_type: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-shaped form."""
        result: dict[str, Any] = {"kind": self.kind}
        if self.name is not None:
            result["name"] = self.name
        if self.inferred_type is not None:
            result["inferred_type"] = self.inferred_type
        if self.reason is not None:
            result["reason"] = self.reason
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
            content, or None on refusal.
        is_refusal: True when the analyst is refusing the task.
        warnings: Non-fatal issues (contradiction overrides, etc.).
    """

    spec: RefactorSpec
    context_pack: ContextPackOutput
    context_pack_artifact: dict[str, Any] | None
    is_refusal: bool
    warnings: tuple[str, ...]


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
            An AnalystResult. On refusal, ``is_refusal`` is True and
            ``context_pack_artifact`` is None.

        Raises:
            CodeGuardianError: ``SYS_NOT_IMPLEMENTED`` when the kernel
                extension is unavailable.
        """
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

        body: str | None = None
        if _needs_body(input.directive):
            body = await _call_tool(
                input,
                "function-body",
                {"path": target_path, "qualified_name": target_symbol},
            )

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

        spec = _build_refactor_spec(
            directive=input.directive,
            target=f"{target_path}:{target_symbol}",
            body=body,
            blast=input.blast,
        )

        variables = {
            "target_symbol": target_symbol,
            "target_path": target_path,
            "directive": input.directive,
            "blast_report": json.dumps(input.blast.to_artifact(), indent=2),
            "skeleton": skeleton,
        }
        prompt_result = render_prompt("analyst", input.prompt_version, variables)

        response, latency_ms = await tracked_model_call(
            input.provider,
            prompt_result.rendered,
            input.model,
            ModelOptions(),
            "analyst",
        )

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
            )

        if not isinstance(context_pack, ContextPackOutput):
            return _refuse_invalid_output(
                input,
                spec,
                "unexpected output type",
                prompt_result,
                response,
                latency_ms,
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
                is_refusal=True,
                warnings=(),
            )

        warnings: list[str] = []
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
            is_refusal=False,
            warnings=tuple(warnings),
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


def _build_refactor_spec(
    *,
    directive: str,
    target: str,
    body: str | None,
    blast: BlastResult,
) -> RefactorSpec:
    """Build the RefactorSpec deterministically from tool evidence."""
    changes: list[RefactorChange] = [RefactorChange(kind="apply_directive", reason=directive)]

    if body is not None and _needs_body(directive):
        try:
            tree = ast.parse(body)
        except SyntaxError:
            tree = None
        if tree is not None and tree.body:
            func = tree.body[0]
            if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for arg in func.args.args:
                    if arg.arg == "self":
                        continue
                    if arg.annotation is None:
                        changes.append(
                            RefactorChange(
                                kind="parameter_annotation",
                                name=arg.arg,
                                inferred_type=None,
                            )
                        )
                if func.returns is None:
                    inferred = _infer_return_type(func)
                    changes.append(
                        RefactorChange(
                            kind="return_annotation",
                            inferred_type=inferred,
                        )
                    )

    constraints: list[str] = []
    if blast.direct_callers:
        constraints.append("keep positional parameters; callers pass positionally")
    for assertion in blast.contract_assertions:
        constraints.append(f"preserve behavior: {assertion.assumption}")
    if blast.recommendation_adjusted == "block":
        constraints.append("high blast risk; verify all callers before apply")

    return RefactorSpec(
        directive=directive,
        target=target,
        changes=tuple(changes),
        constraints=tuple(constraints),
    )


def _infer_return_type(func: ast.AST) -> str | None:
    """Infer the return type from return statements, or None if unknown.

    Returns the inferred type only when every return statement yields
    the same syntactic type. Mixed return types yield None.
    """
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
        is_refusal=True,
        warnings=(),
    )


def _refuse_invalid_output(
    input: AnalystInput,
    spec: RefactorSpec,
    reason: str,
    prompt_result: Any,
    response: Any,
    latency_ms: int,
) -> AnalystResult:
    """Return a structured refusal for a model output that failed schema."""
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
        is_refusal=True,
        warnings=(f"schema violation: {reason}",),
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
