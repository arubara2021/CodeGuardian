"""The three-layer verification stack.

Layer one is the deterministic gate: ``git apply --check`` and the
characterization test. It costs zero tokens and catches structurally
broken candidates before any model runs. A gate failure bounces the
candidate back to the writer.

Layer two is the deterministic pre-scan. It runs inside each verifier
agent when the agent produces its verdict; the agent's own pre-scan
logic is the layer. This module does not duplicate that work. It
consumes the pre-scan results the verifiers return.

Layer three is the LLM verifier loop. The three verifiers run in
parallel; their verdicts are combined into a single pass/fail.

The K bound scales with the blast band:

- ``proceed`` runs K=1. A single round; a verifier failure returns.
- ``review`` runs K=2. One retry with the verifier reason as
  ``previous_attempt`` feedback.
- ``block`` runs K=2 and flags the run for human escalation on a
  second failure.

Rollback happens between rounds. When a round fails, the diff is
rolled back so the next round's writer starts from the original file.
The stack owns the sequencing; the loop owns the K bound via the
``VerifierLoop`` primitive.

The stack is a single async function. It accepts callables for
generate, gate, apply, verify, and rollback. It does not import an
agent, does not call a model directly, does not touch storage. Every
side effect goes through a caller-supplied callable, so the stack is
trivially testable with fakes and trivially wireable to the real
pipeline.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from codeguardian.core.agents._loop import (
    GenerationResult,
    LoopOutcome,
    VerificationResult,
    VerifierLoop,
)

from .deterministic_gate import DeterministicGateResult

__all__ = [
    "DEFAULT_K_BY_BAND",
    "VerifierStackResult",
    "run_verifier_stack",
]


BlastBand = Literal["proceed", "review", "block"]


DEFAULT_K_BY_BAND: Mapping[BlastBand, int] = {
    "proceed": 1,
    "review": 2,
    "block": 2,
}


GenerateFn = Callable[
    [VerificationResult | None],
    Awaitable[GenerationResult[Any]],
]
"""Produce one writer candidate. Receives the previous round's failure
as feedback (None on round 1)."""


GateFn = Callable[[Any], DeterministicGateResult]
"""Run the deterministic gate against a generation. Synchronous."""


ApplyFn = Callable[[Any], Awaitable[Any]]
"""Apply the generation's diff and return a rollback reference."""


VerifyFn = Callable[[Any], Awaitable[tuple[Any, ...]]]
"""Run the three verifiers and return their results as a tuple."""


RollbackFn = Callable[[Any], Awaitable[None]]
"""Roll back a rollback reference."""


OnAttemptFn = Callable[[int, GenerationResult[Any], VerificationResult], None]
"""Called after each verification."""


@dataclass(frozen=True, slots=True)
class VerifierPanelConfig:
    """Policy applied to a round of verifier verdicts.

    The defaults preserve pre-fix behaviour. Production wiring
    supplies ``min_verifiers=3, require_distinct_types=True,
    confidence_floor=0.6`` to enforce the panel policy the settlement
    describes.
    """

    min_verifiers: int = 1
    require_distinct_types: bool = False
    confidence_floor: float = 0.0


@dataclass(frozen=True, slots=True)
class VerifierPanelResult:
    """Outcome of the panel check for one verification round.

    Attributes:
        passed: True when every panel requirement is satisfied.
        verifier_count: Number of verdicts supplied.
        distinct_types: Number of distinct verifier_type values seen.
        min_confidence: Lowest declared confidence, or 0.0 when none
            declared.
        panel_agreement: Fraction of verdicts whose value is "pass".
            Recorded for observability; it does not affect pass/fail.
        rejected_reason: Human-readable reason the panel was rejected,
            or None on success.
    """

    passed: bool
    verifier_count: int
    distinct_types: int
    min_confidence: float
    panel_agreement: float
    rejected_reason: str | None = None


_DEFAULT_PANEL_CONFIG = VerifierPanelConfig()


@dataclass(frozen=True, slots=True)
class VerifierStackResult:
    """The complete outcome of one verification stack run.

    Attributes:
        passed: True when a round's verifiers all returned pass.
        k: The maximum number of rounds the stack was configured to
            run.
        attempts: How many generation attempts actually ran.
        gate_result: The gate result of the final round. ``None`` when
            the writer refused before producing a candidate.
        verdicts: The three verifier results of the final round.
            ``None`` when the gate failed or the writer refused.
        halted_reason: Why the stack stopped. ``None`` on success.
        escalation_required: True when band is ``block`` and the stack
            exhausted its K rounds without passing. The caller must
            surface this to a human before any apply.
        final_generation: The generation the last attempt produced,
            or None.
    """

    passed: bool
    k: int
    attempts: int
    gate_result: DeterministicGateResult | None = None
    verdicts: tuple[Any, ...] | None = None
    halted_reason: str | None = None
    escalation_required: bool = False
    final_generation: GenerationResult[Any] | None = None
    panel: VerifierPanelResult | None = None


def _k_for_band(band: BlastBand) -> int:
    """Return the K bound for a blast band."""
    return DEFAULT_K_BY_BAND.get(band, 2)


def _verifier_label(verifier_result: Any) -> str:
    """Return the verifier's name from its result.

    Handles the three concrete types the Phase 11 verifiers return.
    Falls back to the class name when the type is unfamiliar so a
    future verifier plugin is still labelled.
    """
    verdict = getattr(verifier_result, "verdict", None)
    if verdict is not None:
        verifier_type = getattr(verdict, "verifier_type", None)
        if isinstance(verifier_type, str):
            return verifier_type
    return type(verifier_result).__name__


def _verdict_is_pass(verifier_result: Any) -> bool:
    """Return True when this single verdict's value is ``pass``."""
    verdict = getattr(verifier_result, "verdict", None)
    if verdict is None:
        return False
    return getattr(verdict, "verdict", None) == "pass"


def _verdict_confidence(verifier_result: Any) -> float | None:
    """Return the verdict's declared confidence, or None."""
    verdict = getattr(verifier_result, "verdict", None)
    if verdict is None:
        return None
    value = getattr(verdict, "confidence", None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _panel_passes(
    verdicts: tuple[Any, ...],
    config: VerifierPanelConfig,
) -> VerifierPanelResult:
    """Apply the panel policy to one verification round.

    Returns a structured result carrying the reason a panel was
    rejected, so the caller can log it without re-inspecting the
    verdicts.
    """
    count = len(verdicts)
    agreeing = sum(1 for v in verdicts if _verdict_is_pass(v))
    agreement = (agreeing / count) if count else 0.0

    if count < config.min_verifiers:
        return VerifierPanelResult(
            passed=False,
            verifier_count=count,
            distinct_types=0,
            min_confidence=0.0,
            panel_agreement=agreement,
            rejected_reason=(f"panel has {count} verifier(s); minimum is {config.min_verifiers}"),
        )

    types = {getattr(getattr(v, "verdict", None), "verifier_type", None) for v in verdicts}
    types.discard(None)
    distinct_types = len(types)

    if config.require_distinct_types and distinct_types < config.min_verifiers:
        return VerifierPanelResult(
            passed=False,
            verifier_count=count,
            distinct_types=distinct_types,
            min_confidence=0.0,
            panel_agreement=agreement,
            rejected_reason=(
                f"panel has {distinct_types} distinct verifier type(s); "
                f"minimum is {config.min_verifiers}"
            ),
        )

    confidences = [_verdict_confidence(v) for v in verdicts]
    provided = [c for c in confidences if c is not None]
    min_confidence = min(provided) if provided else 0.0

    if provided and min_confidence < config.confidence_floor:
        return VerifierPanelResult(
            passed=False,
            verifier_count=count,
            distinct_types=distinct_types,
            min_confidence=min_confidence,
            panel_agreement=agreement,
            rejected_reason=(
                f"minimum confidence {min_confidence:.2f} is below "
                f"floor {config.confidence_floor:.2f}"
            ),
        )

    if agreeing != count:
        return VerifierPanelResult(
            passed=False,
            verifier_count=count,
            distinct_types=distinct_types,
            min_confidence=min_confidence,
            panel_agreement=agreement,
            rejected_reason="one or more verifiers did not return pass",
        )

    return VerifierPanelResult(
        passed=True,
        verifier_count=count,
        distinct_types=distinct_types,
        min_confidence=min_confidence,
        panel_agreement=agreement,
    )


def _verdict_reason(verifier_result: Any) -> str:
    """Return a short reason string for the verifier's verdict."""
    reason = getattr(verifier_result, "verdict_reason", None)
    if reason:
        return str(reason)
    verdict = getattr(verifier_result, "verdict", None)
    if verdict is not None:
        model_reason = getattr(verdict, "reason", None)
        if model_reason:
            return str(model_reason)
    return "no reason"


def _summarize_verdicts(verdicts: tuple[Any, ...]) -> str:
    """Return a one-line summary of the failing verifiers.

    Example: ``"security: vulnerability_introduced; contract: contract_broken"``.
    Returns an empty string when every verifier passed.
    """
    parts: list[str] = []
    for v in verdicts:
        if _verdict_is_pass(v):
            continue
        parts.append(f"{_verifier_label(v)}: {_verdict_reason(v)}")
    return "; ".join(parts)


async def run_verifier_stack(
    *,
    band: BlastBand,
    generate: GenerateFn,
    gate: GateFn,
    apply: ApplyFn,
    verify: VerifyFn,
    rollback: RollbackFn,
    on_attempt: OnAttemptFn | None = None,
    k_override: int | None = None,
    panel_config: VerifierPanelConfig = _DEFAULT_PANEL_CONFIG,
) -> VerifierStackResult:
    """Run the three-layer verification stack.

    Args:
        band: The blast band. Sets the K bound and, for ``block``,
            flags escalation.
        generate: Produces one writer candidate. Receives the previous
            round's failure as feedback on retries.
        gate: Runs the deterministic gate against a generation. Called
            synchronously. When the gate fails, the stack skips the
            apply and the LLM verifiers, and the writer retries with
            the gate failure as feedback.
        apply: Applies the generation's diff, returns a rollback ref.
        verify: Runs the three verifiers and returns their results.
        rollback: Rolls back a reference ``apply`` returned. Called
            between rounds and on final failure.
        on_attempt: Called after each verification. Optional.
        k_override: Override the K from the band. Useful for tests.

    Returns:
        A ``VerifierStackResult``. The stack never raises on a
        verifier failure; the failure is recorded on the result. It
        raises only when a caller-supplied callable raises in a way the
        stack cannot classify — for example, ``apply`` raising a
        non-recoverable error before any round produced output.
    """
    k = k_override if k_override is not None else _k_for_band(band)
    loop = VerifierLoop(max_attempts=k)

    state: dict[str, Any] = {
        "pending_rollback": None,
        "last_gate": None,
        "last_verdicts": None,
        "last_panel": None,
    }

    async def _generate_with_rollback(
        previous: VerificationResult | None,
    ) -> GenerationResult[Any]:
        # Roll back the previous round's apply before generating the
        # next candidate. This runs even on the first round (no-op
        # because pending_rollback is None).
        pending = state["pending_rollback"]
        if pending is not None:
            with contextlib.suppress(Exception):
                await rollback(pending)
            state["pending_rollback"] = None
        # Reset the last gate and verdicts; the new round will set them.
        state["last_gate"] = None
        state["last_verdicts"] = None
        return await generate(previous)

    async def _verify_and_record(
        generation: GenerationResult[Any],
    ) -> VerificationResult:
        # Layer 1: deterministic gate.
        gate_result = gate(generation.payload)
        state["last_gate"] = gate_result
        if not gate_result.passed:
            return VerificationResult(
                all_pass=False,
                failure_summary=(
                    f"deterministic gate failed: {(gate_result.failure_reason() or '')[:300]}"
                ),
                detail={
                    "gate": gate_result,
                    "verdicts": None,
                    "rollback_ref": None,
                },
            )

        # Layer 2: apply the diff. The apply itself is deterministic;
        # a failure here means the diff could not be written despite
        # applying cleanly in the gate, which is an infrastructure
        # failure rather than a candidate failure.
        try:
            rollback_ref = await apply(generation.payload)
            state["pending_rollback"] = rollback_ref
        except Exception as exc:
            return VerificationResult(
                all_pass=False,
                failure_summary=(f"apply failed after passing the gate: {exc}"),
                detail={
                    "gate": gate_result,
                    "verdicts": None,
                    "rollback_ref": None,
                },
            )

        # Layer 3: the three LLM verifiers.
        try:
            verdicts = await verify(generation.payload)
        except Exception as exc:
            return VerificationResult(
                all_pass=False,
                failure_summary=f"verifier raised: {exc}",
                detail={
                    "gate": gate_result,
                    "verdicts": None,
                    "rollback_ref": rollback_ref,
                },
            )

        state["last_verdicts"] = verdicts
        panel = _panel_passes(verdicts, panel_config)
        state["last_panel"] = panel
        if panel.passed:
            return VerificationResult(
                all_pass=True,
                failure_summary=None,
                detail={
                    "gate": gate_result,
                    "verdicts": verdicts,
                    "panel": panel,
                    "rollback_ref": rollback_ref,
                },
            )
        parts: list[str] = []
        if panel.rejected_reason:
            parts.append(panel.rejected_reason)
        verifier_summary = _summarize_verdicts(verdicts)
        if verifier_summary:
            parts.append(verifier_summary)
        return VerificationResult(
            all_pass=False,
            failure_summary="; ".join(parts) if parts else "panel rejected",
            detail={
                "gate": gate_result,
                "verdicts": verdicts,
                "panel": panel,
                "rollback_ref": rollback_ref,
            },
        )

    outcome: LoopOutcome[Any] = await loop.run(
        generate=_generate_with_rollback,
        verify=_verify_and_record,
        on_attempt=on_attempt,
    )

    # Final rollback if the stack failed. A passed round leaves the
    # apply in place; the caller will run it again for the real apply.
    if not outcome.passed:
        pending = state["pending_rollback"]
        if pending is not None:
            with contextlib.suppress(Exception):
                await rollback(pending)
            state["pending_rollback"] = None

    escalation_required = (
        band == "block" and not outcome.passed and state["last_verdicts"] is not None
    )

    return VerifierStackResult(
        passed=outcome.passed,
        k=k,
        attempts=outcome.attempts,
        gate_result=state["last_gate"],
        verdicts=state["last_verdicts"],
        halted_reason=outcome.halted_reason,
        escalation_required=escalation_required,
        final_generation=outcome.final_generation,
        panel=state["last_panel"],
    )
