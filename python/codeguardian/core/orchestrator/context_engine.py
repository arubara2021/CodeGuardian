"""Run-tree context compilation.

The orchestrator's context is a tree: one node per completed phase,
each carrying a one-line summary and the typed output the phase
produced. Before a phase's agent runs, the loop compiles a context
block from the tree, bounded by the run's ``ContextBudget``. The
agent's input factory appends this block to whatever prompt the agent
already builds.

Three rules govern the compilation:

1. **Completed phases contribute their summary plus their typed
   output.** The summary is a short string a human can read; the
   typed output is the serializable artifact the phase produced,
   which the next agent's tools can consult.
2. **The current phase contributes only its input.** The current
   phase has no history yet, so nothing from inside the phase appears
   in the compiled block. The input factory supplies the input.
3. **Pending phases contribute nothing.** A phase the run has not
   reached yet is invisible to the compilation. This is what keeps
   the block small even on a ten-slot graph.

Compilation is bounded by the remaining budget. Completed phases are
included newest-first; when a phase would push the block past the
per-compilation share, it is dropped and recorded in
``CompiledContext.phases_dropped``. The share defaults to thirty
percent of the remaining budget; the caller can raise or lower it.

The engine owns the run's ``ContextBudget``. The budget fires
compaction at 60 percent and refusal at 85 percent, matching the
``ContextBudget`` defaults. ``should_compact`` and ``should_refuse``
are the loop's reads of that state; a compacting phase calls
``reset_with_summary`` to bring the fill back below the compact
threshold while keeping the summary tokens counted.

The engine does not call a model. It does not write to the store. It
does not import an agent. Its only dependency is
``ContextBudget``.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from codeguardian.core.agents.context_budget import (
    BudgetSignal,
    ContextBudget,
)

__all__ = [
    "CompiledContext",
    "ContextEngine",
    "PhaseSummary",
    "Summarizer",
]


Summarizer = Callable[[str, Any], tuple[str, Mapping[str, Any]]]
"""Return ``(one_line_summary, typed_output)`` for a phase output.

A caller can supply a custom summarizer to control what a specific
phase contributes. The default summarizer handles the shapes the
Phase 11 and Phase 12 v2 agents produce.
"""


_APPROX_CHARS_PER_TOKEN = 4
_DEFAULT_SHARE = 0.30


def _approx_tokens(text: str) -> int:
    """Return an approximate token count for ``text``."""
    return max(1, len(text) // _APPROX_CHARS_PER_TOKEN)


@dataclass(frozen=True, slots=True)
class PhaseSummary:
    """One completed phase's contribution to the compiled context.

    Attributes:
        phase: The slot name.
        summary: A one-line human-readable summary.
        typed_output: The serializable output the phase produced.
            Empty when the summarizer could not produce one.
        token_cost: The approximate token cost of ``summary`` plus
            ``typed_output`` when serialized to JSON.
    """

    phase: str
    summary: str
    typed_output: Mapping[str, Any] = field(default_factory=dict)
    token_cost: int = 0


@dataclass(frozen=True, slots=True)
class CompiledContext:
    """The compiled context block for the current phase.

    Attributes:
        block: The text block. Empty when nothing was compiled.
        phases_included: Slot names of the phases whose summaries are
            in the block, in inclusion order.
        phases_dropped: Slot names dropped because the per-compilation
            share was exhausted.
        budget_signal: The budget's signal at the end of compilation.
            ``"ok"``, ``"compact"``, or ``"refuse"``.
        budget_fill: The budget's fill fraction at the end of
            compilation, in the range 0 through 1+.
        token_cost: The approximate token cost of ``block``.
    """

    block: str
    phases_included: tuple[str, ...] = ()
    phases_dropped: tuple[str, ...] = ()
    budget_signal: str = "ok"
    budget_fill: float = 0.0
    token_cost: int = 0


def _default_summarizer(
    slot_name: str,
    output: Any,
) -> tuple[str, Mapping[str, Any]]:
    """Return a one-line summary and a JSON-safe typed output.

    Handles the concrete result types the pipeline produces. Falls
    back to a class-name summary plus an empty typed output for
    anything else, so a future agent's result still contributes a
    line.
    """
    if output is None:
        return (f"{slot_name}: <no output>", {})

    # ReconResult: cpg_bytes + artifact
    cpg_bytes = getattr(output, "cpg_bytes", None)
    artifact = getattr(output, "artifact", None)
    if isinstance(cpg_bytes, (bytes, bytearray)) and isinstance(artifact, dict):
        summary = (
            f"recon: {len(artifact.get('languages', []))} languages, {len(cpg_bytes)} bytes CPG"
        )
        return (summary, {"artifact_keys": sorted(artifact.keys())})

    # BlastResult: score + recommendation
    blast_score = getattr(output, "blast_score", None)
    recommendation = getattr(output, "recommendation_adjusted", None)
    if isinstance(blast_score, int) and isinstance(recommendation, str):
        ranked = getattr(output, "ranked_callers", ())
        summary = (
            f"blast: score={blast_score}, "
            f"recommendation={recommendation}, "
            f"{len(ranked)} ranked callers"
        )
        return (
            summary,
            {
                "score": blast_score,
                "recommendation": recommendation,
                "ranked_callers": len(ranked),
            },
        )

    # AnalystResult: spec changes + context pack
    spec = getattr(output, "spec", None)
    if spec is not None:
        changes = getattr(spec, "changes", ())
        status = getattr(output, "status", None)
        summary = f"analyst: status={status}, {len(changes)} spec change(s)"
        return (summary, {"status": status, "changes": len(changes)})

    # TesterResult: valid flag
    is_valid = getattr(output, "is_valid", None)
    if isinstance(is_valid, bool):
        refusal = getattr(output, "is_refusal", False)
        summary = (
            f"tester: valid={is_valid}, refusal={refusal}, "
            f"attempts={getattr(output, 'attempts', 0)}"
        )
        return (summary, {"valid": is_valid, "refusal": refusal})

    # WriterResult: candidates + status
    candidates = getattr(output, "candidates", None)
    status = getattr(output, "status", None)
    if isinstance(candidates, (tuple, list)) and isinstance(status, str):
        summary = f"writer: status={status}, {len(candidates)} candidate(s)"
        return (summary, {"status": status, "candidates": len(candidates)})

    # Verifier results: verdict + reason
    verdict = getattr(output, "verdict", None)
    if verdict is not None:
        value = getattr(verdict, "verdict", None)
        reason = getattr(output, "verdict_reason", None)
        summary = f"{slot_name}: verdict={value}" + (f", reason={reason}" if reason else "")
        return (summary, {"verdict": value, "reason": reason})

    # SkillCuratorResult: proposal presence
    has_proposal = getattr(output, "has_proposal", None)
    if isinstance(has_proposal, bool):
        summary = (
            f"skill_curator: has_proposal={has_proposal}, "
            f"polarity={getattr(output, 'polarity', None)}"
        )
        return (
            summary,
            {"has_proposal": has_proposal},
        )

    # Fallback
    return (f"{slot_name}: {type(output).__name__}", {})


def _json_safe(mapping: Mapping[str, Any]) -> str:
    """Serialize ``mapping`` to compact JSON, empty on failure."""
    try:
        return json.dumps(dict(mapping), sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return "{}"


class ContextEngine:
    """The run's context tree plus its budget.

    Constructed once per run. ``record_phase`` appends a summary after
    each successful phase; ``compile_for`` builds the block for the
    next phase; the budget methods answer the loop's pressure queries.

    Args:
        budget: The run's ``ContextBudget``. The engine owns the
            instance for the rest of the run.
        summarizer: Optional override for the default summarizer.
        share: The fraction of the remaining budget a single
            compilation may use. Defaults to 0.30.
    """

    def __init__(
        self,
        *,
        budget: ContextBudget,
        summarizer: Summarizer | None = None,
        share: float = _DEFAULT_SHARE,
    ) -> None:
        if not (0.0 < share <= 1.0):
            raise ValueError(f"share must be in (0, 1]; got {share}")
        self._budget = budget
        self._summarizer = summarizer or _default_summarizer
        self._share = share
        self._phases: list[PhaseSummary] = []

    @property
    def budget(self) -> ContextBudget:
        """The run's context budget."""
        return self._budget

    @property
    def completed_phases(self) -> tuple[str, ...]:
        """Every phase recorded so far, in completion order."""
        return tuple(p.phase for p in self._phases)

    def record_phase(
        self,
        slot_name: str,
        output: Any,
        *,
        extra_tokens: int = 0,
    ) -> PhaseSummary:
        """Summarize a phase and add it to the tree.

        Args:
            slot_name: The slot that completed.
            output: The value the slot's agent returned.
            extra_tokens: Additional tokens to charge the budget for
                this phase beyond the summary's own cost, for example
                the tokens the agent consumed on its own model call.

        Returns:
            The ``PhaseSummary`` that was added.
        """
        summary_text, typed_output = self._summarizer(slot_name, output)
        serialized = _json_safe(typed_output)
        token_cost = _approx_tokens(summary_text) + _approx_tokens(serialized)

        record = PhaseSummary(
            phase=slot_name,
            summary=summary_text,
            typed_output=typed_output,
            token_cost=token_cost,
        )
        self._phases.append(record)
        with contextlib.suppress(Exception):
            self._budget.record(
                tokens_in=token_cost + max(0, extra_tokens),
                tokens_out=0,
            )
        return record

    def compile_for(self, slot_name: str) -> CompiledContext:
        """Compile the context block for the phase ``slot_name``.

        Completed phases contribute newest-first until the per-
        compilation share of remaining budget is exhausted. Phases
        beyond that share are recorded in ``phases_dropped``.

        Args:
            slot_name: The slot the compilation is for. Used only for
                logging; the compiled block does not name the current
                phase.

        Returns:
            A ``CompiledContext``. ``block`` is empty when no phases
            have completed, when the budget is at or above the
            compaction threshold, or when every phase's summary was
            dropped.
        """
        del slot_name  # reserved for future per-slot filtering

        signal = self._safe_signal()
        if signal == BudgetSignal.REFUSE:
            return CompiledContext(
                block="",
                budget_signal=signal.value,
                budget_fill=self._safe_fill(),
            )
        if signal == BudgetSignal.COMPACT:
            # The compaction is the caller's job. Until it runs, do
            # not push more context in; the block is empty.
            return CompiledContext(
                block="",
                budget_signal=signal.value,
                budget_fill=self._safe_fill(),
            )

        remaining_tokens = max(1, int((1.0 - self._safe_fill()) * self._budget.window))
        per_compilation_budget = max(1, int(remaining_tokens * self._share))

        lines: list[str] = []
        included: list[str] = []
        dropped: list[str] = []
        used = 0

        for phase in reversed(self._phases):
            cost = phase.token_cost
            if used + cost > per_compilation_budget and included:
                dropped.append(phase.phase)
                continue
            lines.append(f"- {phase.summary}")
            included.append(phase.phase)
            used += cost

        included.reverse()
        dropped.reverse()

        block = ""
        if lines:
            block = "## prior phases\n" + "\n".join(lines) + "\n"

        return CompiledContext(
            block=block,
            phases_included=tuple(included),
            phases_dropped=tuple(dropped),
            budget_signal=self._safe_signal().value,
            budget_fill=self._safe_fill(),
            token_cost=_approx_tokens(block) if block else 0,
        )

    def should_compact(self) -> bool:
        """True when the budget asks for compaction."""
        return self._safe_signal() == BudgetSignal.COMPACT

    def should_refuse(self) -> bool:
        """True when the budget asks the loop to refuse further calls."""
        return self._safe_signal() == BudgetSignal.REFUSE

    def reset_with_summary(self, *, summary_tokens: int) -> None:
        """Compact the budget, keeping only the summary cost counted.

        Called by the loop after a compacting phase has replaced its
        accumulated context with a short summary. ``summary_tokens``
        is the summary's own token cost; the budget starts from there
        rather than from zero so a phase cannot hide the summary's
        cost from the next compilation's fill calculation.
        """
        with contextlib.suppress(Exception):
            self._budget.reset(summary_tokens=summary_tokens)

    def snapshot(self, *, role: str = "orchestrator") -> dict[str, Any]:
        """Return a JSON-safe budget snapshot for the decision log."""
        try:
            return self._budget.snapshot(role=role)
        except Exception:
            return {"role": role, "fill_percent": 0.0}

    def _safe_signal(self) -> BudgetSignal:
        try:
            return self._budget.check()
        except Exception:
            return BudgetSignal.OK

    def _safe_fill(self) -> float:
        try:
            return float(self._budget.fill)
        except Exception:
            return 0.0
