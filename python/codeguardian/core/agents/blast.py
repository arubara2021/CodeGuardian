"""Blast agent.

Deterministic. Wraps ``_native.compute_blast`` and re-shapes the result
into a form downstream agents can act on:

- Callers ranked by their contribution to the blast score, enriched
  with the call expression and argument map when the caller's source is
  available under the clone root.
- ContractAssertions with a concrete ``test_expression`` the Tester can
  assert. The expression is synthesized from how the caller uses the
  return value: subscript usage produces an index guard, attribute
  usage produces a hasattr check, and so on.
- An explicit ``caller_search_complete`` flag, computed by a text
  search when a clone root is available.
- A calibrated recommendation: when the search is incomplete, the
  recommendation shifts one band toward caution.
- A public-API flag on the target, derived from the module's ``__all__``
  when present, or from Python's underscore convention when not.

The blast output dict produced by the kernel conforms to the core
schema for direct callers, transitive callers, contract violations,
coverage gaps, score, and recommendation. The extended fields (ranked
callers, contract assertions, call-site details, public-API flag,
completeness signals) live on the ``BlastResult`` object, not on the
schema artifact.

Transitive path reconstruction from the target to each caller requires
per-edge graph access that the current kernel blast API does not
expose. The ``transitive_path`` field is reserved for a future Phase 13
addition to compute_blast. It is empty in this block.
"""

from __future__ import annotations

import ast
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.interfaces.verifier import ContractAssertion

from .telemetry import record_call

__all__ = [
    "BlastAgent",
    "BlastInput",
    "BlastResult",
    "RankedCaller",
]


_WEIGHT_DIRECT = 2
_WEIGHT_TRANSITIVE = 1
_WEIGHT_VIOLATION = 8
_WEIGHT_GAP = 3

_BAND_ORDER = ("proceed", "review", "block")


@dataclass(frozen=True, slots=True)
class BlastInput:
    """Input to the blast agent."""

    cpg_bytes: bytes = field(repr=False)
    target_path: str
    target_qualified_name: str
    clone_root: Path | None
    change_kind: str = "semantic"
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(frozen=True, slots=True)
class RankedCaller:
    """A caller with its contribution to the blast score.

    Attributes:
        qualified_name: The caller's qualified name.
        path: The caller's file path relative to the clone root.
        line: The 1-based line of the caller declaration.
        contribution: Its weight in the blast score.
        is_direct: True when the caller calls the target directly.
        call_kind: An optional classification from the kernel.
        call_expression: The unparsed text of the call to the target,
            truncated to 200 characters. Populated only when
            ``clone_root`` is set and the caller's source is readable.
        argument_map: A parameter-name to expression-text map for the
            call. Positional arguments map to the target's parameter
            names in order. Keyword arguments map to their own names.
            Truncated to 120 characters per value. Populated only under
            the same conditions as ``call_expression``.
        depth: One for direct callers; two or more for transitive.
        transitive_path: Reserved for path reconstruction from the
            target to this caller. Empty in this block; the kernel blast
            API does not currently expose per-edge graph access.
    """

    qualified_name: str
    path: str
    line: int
    contribution: int
    is_direct: bool
    call_kind: str | None
    call_expression: str | None = None
    argument_map: dict[str, Any] | None = None
    depth: int = 1
    transitive_path: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BlastResult:
    """Output of the blast agent.

    Schema-conforming fields match ``blast-radius.json``. Extended
    fields are not in the schema and live only on this object.
    """

    target_path: str
    target_qualified_name: str
    target_language: str | None
    change_kind: str
    direct_callers: tuple[dict[str, Any], ...]
    transitive_callers: tuple[dict[str, Any], ...]
    contract_violations: tuple[dict[str, Any], ...]
    coverage_gaps: tuple[dict[str, Any], ...]
    blast_score: int
    recommendation_raw: str
    recommendation_adjusted: str
    generated_at: str
    run_id: str

    ranked_callers: tuple[RankedCaller, ...]
    contract_assertions: tuple[ContractAssertion, ...]
    unresolved_call_sites: int
    caller_search_complete: bool
    calibration_reason: str | None
    target_public_api: bool = False

    def to_artifact(self) -> dict[str, Any]:
        """Return the schema-conforming artifact for ``blast-radius.json``.

        The extended fields on this result (ranked_callers,
        contract_assertions, call-site details, target_public_api) are
        not part of the schema artifact and are not included here.
        """
        return {
            "schema_version": "1.0",
            "run_id": self.run_id,
            "target": {
                "path": self.target_path,
                "qualified_name": self.target_qualified_name,
                **({"language": self.target_language} if self.target_language else {}),
            },
            "change_kind": self.change_kind,
            "direct_callers": [dict(c) for c in self.direct_callers],
            "transitive_callers": [dict(c) for c in self.transitive_callers],
            "contract_violations": [dict(v) for v in self.contract_violations],
            "coverage_gaps": [dict(g) for g in self.coverage_gaps],
            "blast_score": self.blast_score,
            "recommendation": self.recommendation_adjusted,
            "generated_at": self.generated_at,
        }


class BlastAgent:
    """Deterministic blast agent."""

    name = "blast"
    phase = "blast"
    is_deterministic = True
    requires_tools: tuple[str, ...] = ()
    can_refuse = True
    prompt_role: str | None = None
    prompt_version: str | None = None

    async def run(self, input: BlastInput) -> BlastResult:
        """Run blast radius analysis on the given target.

        Raises:
            CodeGuardianError: With code SYS_INTERNAL when the target
                does not exist in the CPG or is ambiguous;
                SYS_NOT_IMPLEMENTED when the kernel is unavailable.
        """
        try:
            from codeguardian import _native
        except ImportError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_NOT_IMPLEMENTED,
                "blast requires the compiled kernel extension",
            ) from exc

        try:
            raw = _native.compute_blast(
                input.cpg_bytes,
                input.target_path,
                input.target_qualified_name,
            )
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code == "BLAST_TARGET_NOT_FOUND":
                raise CodeGuardianError(
                    ErrorCode.SYS_INTERNAL,
                    f"target not found in CPG: {input.target_path}:{input.target_qualified_name}",
                    details={
                        "path": input.target_path,
                        "qualified_name": input.target_qualified_name,
                    },
                ) from exc
            if code == "BLAST_TARGET_AMBIGUOUS":
                raise CodeGuardianError(
                    ErrorCode.SYS_INTERNAL,
                    f"target is ambiguous: {input.target_path}:{input.target_qualified_name}",
                    details={
                        "path": input.target_path,
                        "qualified_name": input.target_qualified_name,
                    },
                ) from exc
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"blast computation failed: {exc}",
            ) from exc

        direct = tuple(_trim_caller(c) for c in raw.get("direct_callers", []))
        transitive = tuple(_trim_transitive(c) for c in raw.get("transitive_callers", []))
        violations_raw = tuple(raw.get("contract_violations", []))
        gaps_raw = tuple(raw.get("coverage_gaps", []))

        violations = tuple(_trim_violation(v) for v in violations_raw)
        gaps = tuple(_trim_gap(g) for g in gaps_raw)

        assertions = _extract_contract_assertions(
            violations_raw,
            clone_root=input.clone_root,
            target_qualified_name=input.target_qualified_name,
        )
        ranked = _rank_callers(
            direct,
            transitive,
            violations_raw,
            gaps_raw,
            clone_root=input.clone_root,
            target_path=input.target_path,
            target_qualified_name=input.target_qualified_name,
        )
        unresolved = _count_unresolved_call_sites(
            input.cpg_bytes,
            input.clone_root,
            input.target_path,
            input.target_qualified_name,
            direct,
        )
        caller_search_complete = input.clone_root is not None and unresolved == 0

        raw_recommendation = str(raw.get("recommendation", "review"))
        adjusted, reason = _calibrate_recommendation(raw_recommendation, caller_search_complete)
        target_public = _detect_public_api(
            input.clone_root,
            input.target_path,
            input.target_qualified_name,
        )

        result = BlastResult(
            target_path=input.target_path,
            target_qualified_name=input.target_qualified_name,
            target_language=None,
            change_kind=input.change_kind,
            direct_callers=direct,
            transitive_callers=transitive,
            contract_violations=violations,
            coverage_gaps=gaps,
            blast_score=int(raw.get("blast_score", 0)),
            recommendation_raw=raw_recommendation,
            recommendation_adjusted=adjusted,
            generated_at=_now_iso(),
            run_id=input.run_id,
            ranked_callers=ranked,
            contract_assertions=assertions,
            unresolved_call_sites=unresolved,
            caller_search_complete=caller_search_complete,
            calibration_reason=reason,
            target_public_api=target_public,
        )

        _record(role="blast", outcome="success", output=str(result.to_artifact()))
        return result


def _trim_caller(caller: dict[str, Any]) -> dict[str, Any]:
    """Return the schema-required subset of a direct caller entry."""
    path = caller.get("path") or caller.get("file_path") or ""
    trimmed: dict[str, Any] = {
        "qualified_name": caller.get("qualified_name", ""),
        "path": path,
        "line": int(caller.get("line", 1)),
    }
    if "call_kind" in caller:
        trimmed["call_kind"] = caller["call_kind"]
    return trimmed


def _trim_transitive(caller: dict[str, Any]) -> dict[str, Any]:
    """Return the schema-required subset of a transitive caller entry."""
    path = caller.get("path") or caller.get("file_path") or ""
    return {
        "qualified_name": caller.get("qualified_name", ""),
        "path": path,
        "depth": int(caller.get("depth", 1)),
    }


def _trim_violation(violation: dict[str, Any]) -> dict[str, Any]:
    """Return the schema-required subset of a contract violation entry."""
    trimmed: dict[str, Any] = {
        "caller": violation.get("caller", ""),
        "assumption": violation.get("assumption", ""),
        "risk": violation.get("risk", "warning"),
    }
    if violation.get("evidence"):
        trimmed["evidence"] = violation["evidence"]
    return trimmed


def _trim_gap(gap: dict[str, Any]) -> dict[str, Any]:
    """Return the schema-required subset of a coverage gap entry."""
    return {
        "caller": gap.get("caller", ""),
        "reason": gap.get("reason", ""),
    }


def _extract_contract_assertions(
    violations: tuple[dict[str, Any], ...],
    clone_root: Path | None = None,
    target_qualified_name: str = "",
) -> tuple[ContractAssertion, ...]:
    """Turn raw violations into ContractAssertions with test expressions.

    The test expression is synthesized from two pieces of evidence.
    First, the assumption text: the first identifier in the assumption
    becomes the subject of the assertion (``result`` when no identifier
    is found). Second, the caller's usage pattern, discovered by
    reading the caller's source and inspecting the AST node that
    immediately wraps the call to the target: a subscript produces a
    length guard, an attribute produces a hasattr check, a method
    access produces a hasattr check on the method name, and a call to
    ``len`` or ``iter`` produces the corresponding dunder check. When
    no usage pattern can be identified, the expression falls back to
    ``assert <subject> is not None``.

    Args:
        violations: The raw violation entries from the kernel.
        clone_root: The clone root, used to read caller source for
            usage analysis. When None, the fallback expression is used
            for every violation.
        target_qualified_name: The target's qualified name, used to
            locate the call site in the caller's source.

    Returns:
        A tuple of ContractAssertion objects, one per violation.
    """
    result: list[ContractAssertion] = []
    for violation in violations:
        caller = str(violation.get("caller", ""))
        assumption = str(violation.get("assumption", ""))
        severity = violation.get("risk", "warning")
        if severity not in ("info", "warning", "critical"):
            severity = "warning"

        subject = _extract_subject(assumption) or "result"
        usage = _analyze_violation_usage(violation, clone_root, target_qualified_name)
        test_expression = _synthesize_test_expression(usage, subject)

        result.append(
            ContractAssertion(
                caller=caller,
                assumption=assumption,
                severity=severity,
                test_expression=test_expression,
                source=violation.get("evidence") or None,
            )
        )
    return tuple(result)


def _analyze_violation_usage(
    violation: dict[str, Any],
    clone_root: Path | None,
    target_qualified_name: str,
) -> str | None:
    """Return the caller's return-value usage pattern, or None."""
    if clone_root is None:
        return None
    caller_path = violation.get("path") or violation.get("caller_path") or ""
    caller_line = int(violation.get("line", 0) or violation.get("caller_line", 0) or 0)
    if not caller_path or caller_line <= 0:
        return None
    caller_source = _read_source(clone_root, caller_path)
    if not caller_source:
        return None
    try:
        tree = ast.parse(caller_source)
    except SyntaxError:
        return None
    call_node = _extract_call_site(caller_source, target_qualified_name, caller_line)
    if call_node is None:
        return None
    target_bare = target_qualified_name.split(".")[-1]
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and _callee_name(node.func) == target_bare
            and node.lineno == call_node.lineno
            and node.col_offset == call_node.col_offset
        ):
            return _analyze_call_usage(node, tree)
    return None


def _synthesize_test_expression(usage: str | None, subject: str) -> str:
    """Return the assertion text for the given usage pattern."""
    if usage is None or usage == "return_value":
        return f"assert {subject} is not None"
    if usage == "subscript":
        return f"assert {subject} is not None and len({subject}) > 0"
    if usage.startswith("attribute:"):
        attr = usage.split(":", 1)[1]
        return f"assert {subject} is not None and hasattr({subject}, {attr!r})"
    if usage.startswith("method:"):
        method = usage.split(":", 1)[1]
        return f"assert {subject} is not None and hasattr({subject}, {method!r})"
    if usage == "len":
        return f"assert {subject} is not None and hasattr({subject}, '__len__')"
    if usage == "iter":
        return f"assert {subject} is not None and hasattr({subject}, '__iter__')"
    if usage == "comparison":
        return f"assert {subject} is not None"
    return f"assert {subject} is not None"


def _analyze_call_usage(call_node: ast.Call, tree: ast.Module) -> str | None:
    """Classify how the call's return value is used, or return None."""
    parent_map: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_map[id(child)] = parent
    parent = parent_map.get(id(call_node))
    return _classify_parent(parent, call_node)


def _classify_parent(parent: ast.AST | None, call_node: ast.Call) -> str | None:
    """Classify the usage based on the immediate parent of the call."""
    if parent is None:
        return None
    if isinstance(parent, ast.Subscript) and parent.value is call_node:
        return "subscript"
    if isinstance(parent, ast.Attribute) and parent.value is call_node:
        return f"attribute:{parent.attr}"
    if (
        isinstance(parent, ast.Call)
        and isinstance(parent.func, ast.Attribute)
        and parent.func.value is call_node
    ):
        return f"method:{parent.func.attr}"
    if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name):
        if parent.func.id == "len" and any(arg is call_node for arg in parent.args):
            return "len"
        if parent.func.id == "iter" and any(arg is call_node for arg in parent.args):
            return "iter"
    if isinstance(parent, ast.Compare) and (
        parent.left is call_node or any(c is call_node for c in parent.comparators)
    ):
        return "comparison"
    if isinstance(parent, ast.Return) and parent.value is call_node:
        return "return_value"
    return None


_IDENTIFIER_RE = re.compile(r"\b([a-z_][a-z0-9_]*)\b")


def _extract_subject(assumption: str) -> str | None:
    """Return the first identifier in ``assumption``, skipping keywords."""
    keywords = {
        "the",
        "a",
        "an",
        "is",
        "return",
        "returns",
        "value",
        "caller",
        "assumes",
        "assume",
        "not",
        "none",
        "none.",
        "non",
        "of",
    }
    for match in _IDENTIFIER_RE.finditer(assumption):
        word = match.group(1)
        if word not in keywords:
            return word
    return None


def _rank_callers(
    direct: tuple[dict[str, Any], ...],
    transitive: tuple[dict[str, Any], ...],
    violations: tuple[dict[str, Any], ...],
    gaps: tuple[dict[str, Any], ...],
    *,
    clone_root: Path | None = None,
    target_path: str = "",
    target_qualified_name: str = "",
) -> tuple[RankedCaller, ...]:
    """Return callers ranked by their contribution to the blast score.

    Direct callers are enriched with their call expression and
    argument map when ``clone_root`` is available and the caller's
    source is readable. Transitive callers are not enriched; reading
    every transitive caller would read most of the repository for a
    small gain, and the primary use of the enrichment is downstream
    verification of the immediate call contract.
    """
    contributions: dict[str, int] = {}
    details: dict[str, RankedCaller] = {}

    for caller in direct:
        key = _caller_key(caller)
        contributions[key] = contributions.get(key, 0) + _WEIGHT_DIRECT
        details.setdefault(
            key,
            RankedCaller(
                qualified_name=caller.get("qualified_name", ""),
                path=caller.get("path", ""),
                line=int(caller.get("line", 1)),
                contribution=0,
                is_direct=True,
                call_kind=caller.get("call_kind"),
                depth=1,
            ),
        )

    for caller in transitive:
        key = _caller_key(caller)
        contributions[key] = contributions.get(key, 0) + _WEIGHT_TRANSITIVE
        details.setdefault(
            key,
            RankedCaller(
                qualified_name=caller.get("qualified_name", ""),
                path=caller.get("path", ""),
                line=1,
                contribution=0,
                is_direct=False,
                call_kind=None,
                depth=int(caller.get("depth", 2)),
            ),
        )

    for violation in violations:
        key = f"violation::{violation.get('caller', '')}"
        contributions[key] = contributions.get(key, 0) + _WEIGHT_VIOLATION
        details.setdefault(
            key,
            RankedCaller(
                qualified_name=violation.get("caller", ""),
                path="",
                line=1,
                contribution=0,
                is_direct=True,
                call_kind=None,
                depth=1,
            ),
        )

    for gap in gaps:
        key = f"gap::{gap.get('caller', '')}"
        contributions[key] = contributions.get(key, 0) + _WEIGHT_GAP
        details.setdefault(
            key,
            RankedCaller(
                qualified_name=gap.get("caller", ""),
                path="",
                line=1,
                contribution=0,
                is_direct=False,
                call_kind=None,
                depth=2,
            ),
        )

    ranked = [
        RankedCaller(
            qualified_name=details[key].qualified_name,
            path=details[key].path,
            line=details[key].line,
            contribution=contribution,
            is_direct=details[key].is_direct,
            call_kind=details[key].call_kind,
            depth=details[key].depth,
        )
        for key, contribution in contributions.items()
    ]
    ranked.sort(key=lambda c: (-c.contribution, c.qualified_name))

    if clone_root is None or not target_path:
        return tuple(ranked)

    param_names = _read_target_param_names(clone_root, target_path, target_qualified_name)
    enriched: list[RankedCaller] = []
    for caller in ranked:
        if not caller.is_direct or not caller.path:
            enriched.append(caller)
            continue
        source = _read_source(clone_root, caller.path)
        if not source:
            enriched.append(caller)
            continue
        call_node = _extract_call_site(source, target_qualified_name, caller.line)
        if call_node is None:
            enriched.append(caller)
            continue
        try:
            call_expression = ast.unparse(call_node)[:200]
        except Exception:
            call_expression = None
        argument_map = (
            _build_argument_map(call_node, param_names) if param_names is not None else None
        )
        enriched.append(
            RankedCaller(
                qualified_name=caller.qualified_name,
                path=caller.path,
                line=caller.line,
                contribution=caller.contribution,
                is_direct=caller.is_direct,
                call_kind=caller.call_kind,
                call_expression=call_expression,
                argument_map=argument_map,
                depth=caller.depth,
            )
        )
    return tuple(enriched)


def _extract_call_site(
    caller_source: str,
    target_qualified_name: str,
    line: int,
) -> ast.Call | None:
    """Find the Call node in ``caller_source`` that invokes the target.

    The target's bare name is compared against the callee name of each
    Call node. The nearest match to ``line`` wins. A tolerance of two
    lines is applied so that multi-line call expressions still resolve.
    """
    try:
        tree = ast.parse(caller_source)
    except SyntaxError:
        return None
    target_bare = target_qualified_name.split(".")[-1]
    candidates: list[tuple[int, ast.Call]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _callee_name(node.func) != target_bare:
            continue
        start = node.lineno
        end = getattr(node, "end_lineno", start) or start
        if start <= line <= end or abs(start - line) <= 2:
            candidates.append((abs(start - line), node))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    return candidates[0][1]


def _callee_name(func: ast.expr) -> str | None:
    """Return the bare name of the callee, or None for other expressions."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _read_target_param_names(
    clone_root: Path,
    target_path: str,
    target_qualified_name: str,
) -> list[str] | None:
    """Return the target's parameter names in order, excluding self/cls.

    Returns None when the target source cannot be read or the function
    definition cannot be found.
    """
    source = _read_source(clone_root, target_path)
    if not source:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    func = _find_function_definition(tree, target_qualified_name)
    if func is None:
        return None
    params = [a.arg for a in func.args.args]
    if params and params[0] in ("self", "cls"):
        params = params[1:]
    return params


def _find_function_definition(
    tree: ast.Module,
    qualified_name: str,
) -> ast.AST | None:
    """Walk the qualified name to the function definition, or None."""
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
    if isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return target
    return None


def _build_argument_map(
    call_node: ast.Call,
    param_names: list[str] | None,
) -> dict[str, Any]:
    """Return a parameter-name to expression-text map for the call.

    Positional arguments map to the target's parameter names in order.
    Positional arguments beyond the target's declared parameters map to
    ``arg_N``. ``*args`` unmapping maps to ``*args_N``. ``**kwargs``
    unmapping maps to ``**kwargs``. Keyword arguments map to their own
    names.
    """
    result: dict[str, Any] = {}
    params = param_names or []
    for i, arg in enumerate(call_node.args):
        try:
            text = ast.unparse(arg)[:120]
        except Exception:
            text = "<unparseable>"
        if isinstance(arg, ast.Starred):
            result[f"*args_{i}"] = text
            continue
        if i < len(params):
            result[params[i]] = text
        else:
            result[f"arg_{i}"] = text
    for kw in call_node.keywords:
        try:
            text = ast.unparse(kw.value)[:120]
        except Exception:
            text = "<unparseable>"
        if kw.arg is None:
            result["**kwargs"] = text
        else:
            result[kw.arg] = text
    return result


def _detect_public_api(
    clone_root: Path | None,
    target_path: str,
    target_qualified_name: str,
) -> bool:
    """Return True when the target appears to be part of the public API.

    A name is public when it is declared in the module's ``__all__`` if
    ``__all__`` exists, or when no part of its qualified name starts
    with an underscore (Python convention). Private names, and names
    declared inside private classes or modules, return False.
    """
    if clone_root is None or not target_path:
        return False
    source = _read_source(clone_root, target_path)
    if not source:
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False

    parts = target_qualified_name.split(".")
    if any(p.startswith("_") for p in parts):
        return False

    target_bare = parts[-1]
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "__all__":
                    names: list[str] = []
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        names = [
                            elt.value
                            for elt in node.value.elts
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                        ]
                    return target_bare in names
    return True


def _read_source(clone_root: Path, relative_path: str) -> str:
    """Read a file under the clone root, returning empty on failure."""
    try:
        return (clone_root / relative_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _caller_key(caller: dict[str, Any]) -> str:
    """Return a stable key for a caller across direct and transitive lists."""
    return f"{caller.get('qualified_name', '')}::{caller.get('path', '')}"


def _count_unresolved_call_sites(
    cpg_bytes: bytes,
    clone_root: Path | None,
    target_path: str,
    target_qualified_name: str,
    direct_callers: tuple[dict[str, Any], ...],
) -> int:
    """Return an approximate count of unresolved call sites.

    When ``clone_root`` is None, the search cannot be performed and
    the caller treats the search as incomplete. When a clone root is
    available, every non-caller source file in the CPG is scanned for
    call-position occurrences of the target's bare name. A file that
    contains such a reference but is not among the resolved callers
    contributes one unresolved site.
    """
    if clone_root is None or not clone_root.is_dir():
        return 0

    try:
        from codeguardian import _native
    except ImportError:
        return 0

    bare_name = target_qualified_name.split(".")[-1]
    if not bare_name:
        return 0

    try:
        handle = _native.PyCPG(cpg_bytes)
        all_files = list(handle.files)
    except Exception:
        return 0

    caller_files = {c.get("path", "") for c in direct_callers if c.get("path")}
    caller_files.discard("")

    pattern = re.compile(rf"(?<![\w.]){re.escape(bare_name)}\s*\(")
    unresolved = 0
    for rel in all_files:
        if rel == target_path or rel in caller_files:
            continue
        full = clone_root / rel
        try:
            text = full.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.lstrip()
            if stripped.startswith(("def ", "async def ", "class ")):
                continue
            if pattern.search(line):
                unresolved += 1
                break
    return unresolved


def _calibrate_recommendation(raw: str, caller_search_complete: bool) -> tuple[str, str | None]:
    """Return the adjusted recommendation and a calibration reason.

    Incomplete caller search shifts the recommendation one band toward
    caution. Complete search returns the raw recommendation unchanged.
    """
    if raw not in _BAND_ORDER:
        raw = "review"
    if caller_search_complete:
        return raw, None
    index = _BAND_ORDER.index(raw)
    shifted = _BAND_ORDER[min(index + 1, len(_BAND_ORDER) - 1)]
    reason = (
        "caller search is incomplete; some callers may be unresolved, "
        "so the recommendation is shifted one band toward caution"
    )
    return shifted, reason


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def _record(*, role: str, outcome: str, output: str | None) -> None:
    """Best-effort telemetry write."""
    from codeguardian.core.agents import _store_context

    store = _store_context.get_active_store()
    if store is None:
        return
    record_call(store, role=role, outcome=outcome, output=output)
