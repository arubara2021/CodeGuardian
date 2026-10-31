"""Blast agent.

Deterministic. Wraps ``_native.compute_blast`` and re-shapes the result
into a form downstream agents can act on:

- Callers ranked by their contribution to the blast score.
- ContractAssertions with a concrete ``test_expression`` the Tester can
  assert.
- An explicit ``caller_search_complete`` flag, computed by a text
  search when a clone root is available.
- A calibrated recommendation: when the search is incomplete, the
  recommendation shifts one band toward caution.

The blast output dict produced by the kernel conforms to the core
schema for direct callers, transitive callers, contract violations,
coverage gaps, score, and recommendation. The extended fields
(ranked callers, contract assertions, completeness signals) live on
the ``BlastResult`` object, not on the schema artifact.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.interfaces.verifier import ContractAssertion

from .telemetry import record_call

__all__ = ["BlastAgent", "BlastInput", "BlastResult", "RankedCaller"]


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
    """A caller with its contribution to the blast score."""

    qualified_name: str
    path: str
    line: int
    contribution: int
    is_direct: bool
    call_kind: str | None


@dataclass(frozen=True, slots=True)
class BlastResult:
    """Output of the blast agent.

    Schema-conforming fields match ``blast-radius.json``. Extended
    fields are not in the schema and live only on this object.
    """

    # Schema-conforming
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

    # Extended
    ranked_callers: tuple[RankedCaller, ...]
    contract_assertions: tuple[ContractAssertion, ...]
    unresolved_call_sites: int
    caller_search_complete: bool
    calibration_reason: str | None

    def to_artifact(self) -> dict[str, Any]:
        """Return the schema-conforming artifact for ``blast-radius.json``."""
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
            CodeGuardianError: With code BLAST_TARGET_NOT_FOUND as
                ``SYS_INTERNAL`` when the target does not exist in the
                CPG; SYS_NOT_IMPLEMENTED when the kernel is unavailable.
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

        assertions = _extract_contract_assertions(violations_raw)
        ranked = _rank_callers(direct, transitive, violations_raw, gaps_raw)
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
) -> tuple[ContractAssertion, ...]:
    """Turn raw violations into ContractAssertions with test expressions.

    The test expression is generated from the assumption text using a
    narrow heuristic: the first identifier appearing in the assumption
    is used as the subject of ``assert <name> is not None``. This
    matches the None-dereference pattern the kernel currently detects.
    Other patterns would need their own generator.
    """
    result: list[ContractAssertion] = []
    for violation in violations:
        caller = str(violation.get("caller", ""))
        assumption = str(violation.get("assumption", ""))
        severity = violation.get("risk", "warning")
        if severity not in ("info", "warning", "critical"):
            severity = "warning"
        subject = _extract_subject(assumption) or "result"
        result.append(
            ContractAssertion(
                caller=caller,
                assumption=assumption,
                severity=severity,
                test_expression=f"assert {subject} is not None",
                source=violation.get("evidence") or None,
            )
        )
    return tuple(result)


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
) -> tuple[RankedCaller, ...]:
    """Return callers ranked by their contribution to the blast score."""
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
        )
        for key, contribution in contributions.items()
    ]
    ranked.sort(key=lambda c: (-c.contribution, c.qualified_name))
    return tuple(ranked)


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

    When ``clone_root`` is None, the search cannot be performed and the
    caller is instructed to treat the search as incomplete. When a
    clone root is available, every non-caller source file in the CPG
    is scanned for call-position occurrences of the target's bare name.
    A file that contains such a reference but is not among the resolved
    callers contributes one unresolved site.
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
