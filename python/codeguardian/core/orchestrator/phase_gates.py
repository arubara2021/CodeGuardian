"""Per-phase entry and exit conditions.

Each slot in a PhaseGraph may declare a gate name. The loop looks up
the gate pair for that name before invoking the slot's agent and again
after the agent returns. An entry gate that fails means the loop
cannot proceed with the slot; an exit gate that fails means the agent
produced an output that does not satisfy the pipeline's contract with
the next slot.

Gates are pure functions over a ``PhaseContext``. They read the
accumulated results of the phases so far and either return (pass) or
raise ``CodeGuardianError`` (fail). They never call a model, never
touch the network, and never mutate the context.

``PhaseContext`` carries the run's identity, its clone root, and a
mapping from slot name to the value the slot's agent returned. The
gate implementations index that mapping by slot name. When a slot
declares a gate whose implementation is missing, the lookup returns a
no-op pair; a graph author who names a gate the module does not know
is silently permitted. That is deliberate: the graph format lets a
future slot declare a gate that a future module will implement, and
today's loop must not fail because the module has not caught up yet.

The legacy gates are:

- ``recon``: exit requires ``cpg_bytes`` on the ReconResult.
- ``blast``: exit requires ``recommendation_adjusted`` on the
  BlastResult.
- ``analyst``: exit requires the result is not a refusal.
- ``tester``: exit requires the result carries either a valid test
  file or a refusal (never an invalid unrefused result).
- ``writer``: exit requires the result carries either candidates or a
  refusal.
- ``sandbox_verify``: exit requires the apply succeeded and the
  characterization test passed, or a typed refusal.
- ``verifier``: entry requires a diff was produced. Exit requires the
  verdict is one of the four valid strings.
- ``skill_curator``: exit requires the result is not a refusal.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codeguardian.core.errors import CodeGuardianError, ErrorCode

from .evidence import HotEvent

__all__ = [
    "GatePair",
    "Gates",
    "PhaseContext",
    "get_gate",
    "has_gate",
    "known_gates",
]


@dataclass(frozen=True, slots=True)
class PhaseContext:
    """The context a gate inspects.

    Attributes:
        run_id: The current run identifier.
        repo_url: The repository URL.
        commit_hash: The repository commit.
        clone_root: The clone root path.
        results: A mapping from slot name to the value the slot's agent
            returned. Populated incrementally as the loop advances.
        evidence: The hot-tier events accumulated up to the moment the
            gate runs. Gates do not currently read them, but they are
            available for future gate implementations that key on
            specific event types.
    """

    run_id: str
    repo_url: str
    commit_hash: str
    clone_root: Path
    results: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[HotEvent, ...] = ()


Gate = Callable[[PhaseContext], None]
"""A gate function. Returns None to pass, raises to fail."""


@dataclass(frozen=True, slots=True)
class GatePair:
    """The entry and exit gates for one phase name."""

    entry: Gate
    exit: Gate


def _noop_gate(_ctx: PhaseContext) -> None:
    """A gate that always passes."""


class Gates:
    """Bundled gate implementations.

    Every gate is a static method so the class can be treated as a
    namespace. ``get_gate`` returns a ``GatePair`` for any name; a
    name the module does not recognise yields a no-op pair.
    """

    @staticmethod
    def recon_exit(ctx: PhaseContext) -> None:
        """Recon must produce CPG bytes."""
        result = ctx.results.get("recon")
        if result is None:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "recon exit gate: no result for slot 'recon'",
                details={"slot": "recon"},
            )
        cpg_bytes = getattr(result, "cpg_bytes", None)
        if not isinstance(cpg_bytes, (bytes, bytearray)) or len(cpg_bytes) == 0:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "recon exit gate: ReconResult has no cpg_bytes",
                details={"slot": "recon"},
            )

    @staticmethod
    def blast_exit(ctx: PhaseContext) -> None:
        """Blast must produce a recommendation_adjusted value."""
        result = ctx.results.get("blast")
        if result is None:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "blast exit gate: no result for slot 'blast'",
                details={"slot": "blast"},
            )
        recommendation = getattr(result, "recommendation_adjusted", None)
        if recommendation not in ("proceed", "review", "block"):
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                f"blast exit gate: recommendation_adjusted "
                f"{recommendation!r} is not one of "
                "proceed/review/block",
                details={
                    "slot": "blast",
                    "recommendation": recommendation,
                },
            )

    @staticmethod
    def analyst_exit(ctx: PhaseContext) -> None:
        """Analyst must produce a non-refused result."""
        result = ctx.results.get("analyst")
        if result is None:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "analyst exit gate: no result for slot 'analyst'",
                details={"slot": "analyst"},
            )
        if getattr(result, "is_refusal", False):
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "analyst exit gate: analyst refused; downstream phases cannot run without a spec",
                details={"slot": "analyst"},
            )
        if getattr(result, "needs_input", False):
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "analyst exit gate: analyst needs more input; "
                "the pipeline cannot continue with a clarification",
                details={"slot": "analyst"},
            )

    @staticmethod
    def tester_exit(ctx: PhaseContext) -> None:
        """Tester must produce either a valid test or a refusal."""
        result = ctx.results.get("tester")
        if result is None:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "tester exit gate: no result for slot 'tester'",
                details={"slot": "tester"},
            )
        is_valid = getattr(result, "is_valid", False)
        is_refusal = getattr(result, "is_refusal", False)
        if not is_valid and not is_refusal:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "tester exit gate: TesterResult is neither valid nor a refusal",
                details={
                    "slot": "tester",
                    "failure_reason": getattr(result, "failure_reason", None),
                },
            )

    @staticmethod
    def writer_exit(ctx: PhaseContext) -> None:
        """Writer must produce candidates or a refusal."""
        result = ctx.results.get("writer")
        if result is None:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "writer exit gate: no result for slot 'writer'",
                details={"slot": "writer"},
            )
        candidates = getattr(result, "candidates", ())
        is_refusal = getattr(result, "is_refusal", False)
        if not candidates and not is_refusal:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "writer exit gate: WriterResult has no candidates and is not a refusal",
                details={"slot": "writer"},
            )

    @staticmethod
    def sandbox_verify_exit(ctx: PhaseContext) -> None:
        """Sandbox verify must produce an apply result or a refusal."""
        result = ctx.results.get("sandbox_verify")
        if result is None:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "sandbox_verify exit gate: no result for slot 'sandbox_verify'",
                details={"slot": "sandbox_verify"},
            )
        # The concrete shape of the verify result is left to block 3's
        # verifier_stack module. This gate only checks that the slot
        # produced something; the verifier_stack gate refines it.

    @staticmethod
    def verifier_entry(ctx: PhaseContext) -> None:
        """A verified diff must exist before a verifier runs.

        The ``sandbox_verify`` slot is the phase that applies the diff
        and runs the characterization test. Its output being present
        means a diff was produced and survived the deterministic gate.
        A verifier that runs without that precondition is inspecting a
        diff the pipeline never validated.
        """
        apply_result = ctx.results.get("sandbox_verify")
        if apply_result is None:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "verifier entry gate: no verified diff was produced; "
                "verifiers cannot run without one",
                details={"results_keys": sorted(ctx.results)},
            )

    @staticmethod
    def verifier_exit(ctx: PhaseContext) -> None:
        """The current verifier must have produced a valid verdict."""
        # Slot names are verifier_correctness, verifier_security,
        # verifier_contract. The gate applies to whichever ran last;
        # the loop passes the slot name via results.
        for name in (
            "verifier_correctness",
            "verifier_security",
            "verifier_contract",
        ):
            if name not in ctx.results:
                continue
            result = ctx.results[name]
            verdict = getattr(result, "verdict", None)
            verdict_value = getattr(verdict, "verdict", None)
            if verdict_value is not None and verdict_value not in (
                "pass",
                "fail",
                "uncertain",
                "refused",
            ):
                raise CodeGuardianError(
                    ErrorCode.SYS_INVARIANT_VIOLATED,
                    f"{name} exit gate: verdict "
                    f"{verdict_value!r} is not one of "
                    "pass/fail/uncertain/refused",
                    details={
                        "slot": name,
                        "verdict": verdict_value,
                    },
                )

    @staticmethod
    def skill_curator_exit(ctx: PhaseContext) -> None:
        """Skill curator must produce a well-shaped result.

        A refusal is a legitimate outcome. The curator refuses when
        its model output does not validate as a skill proposal — for
        example when the model writes an unrecognized ``skill_type``.
        That means "no reusable pattern to propose," not "the phase
        failed." The pipeline does not depend on the curator's
        proposal, so the gate accepts the refusal and the pipeline
        continues.

        What the gate rejects is a missing result. A curator that
        somehow returned ``None`` is a wiring error, not an honest
        refusal.
        """
        result = ctx.results.get("skill_curator")
        if result is None:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "skill_curator exit gate: no result for slot 'skill_curator'",
                details={"slot": "skill_curator"},
            )


_GATE_TABLE: Mapping[str, GatePair] = {
    "recon": GatePair(entry=_noop_gate, exit=Gates.recon_exit),
    "blast": GatePair(entry=_noop_gate, exit=Gates.blast_exit),
    "analyst": GatePair(entry=_noop_gate, exit=Gates.analyst_exit),
    "tester": GatePair(entry=_noop_gate, exit=Gates.tester_exit),
    "writer": GatePair(entry=_noop_gate, exit=Gates.writer_exit),
    "sandbox_verify": GatePair(entry=_noop_gate, exit=Gates.sandbox_verify_exit),
    "verifier": GatePair(entry=Gates.verifier_entry, exit=Gates.verifier_exit),
    "skill_curator": GatePair(entry=_noop_gate, exit=Gates.skill_curator_exit),
}


def get_gate(name: str | None) -> GatePair:
    """Return the GatePair for ``name``.

    An unknown name or ``None`` yields a no-op pair so a graph author
    can declare a gate whose implementation is a future phase without
    breaking today's loop.
    """
    if name is None:
        return GatePair(entry=_noop_gate, exit=_noop_gate)
    return _GATE_TABLE.get(name, GatePair(entry=_noop_gate, exit=_noop_gate))


def has_gate(name: str | None) -> bool:
    """Return True when the module implements a gate for ``name``."""
    if name is None:
        return False
    return name in _GATE_TABLE


def known_gates() -> tuple[str, ...]:
    """Return every gate name this module implements."""
    return tuple(sorted(_GATE_TABLE))
