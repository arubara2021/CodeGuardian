"""Verifier stack agents for the deep test.

The legacy graph declares three verifier slots in sequence. A single
``run_verifier_stack`` call produces all three verdicts at once, so a
naive one-agent-per-slot registration would call the stack three times
and re-verify the same diff. The shared runner below calls the stack
once on the first verifier dispatch and caches the result; the next
two dispatches read from the cache. Every slot writes its own verdict
to the context, so the exit gate passes three times and the telemetry
rows carry three distinct roles.

The three agents share one runner instance. Each agent carries its own
``name`` (matching its slot) and its own ``_role`` (which of the three
verdicts to surface). The runner is the only piece that talks to
``run_verifier_stack``; the agents are thin selectors.
"""

from __future__ import annotations

import asyncio
from typing import Any

from codeguardian.core.agents._loop import GenerationResult
from codeguardian.core.agents.contract_verifier import (
    ContractVerifier,
    ContractVerifierInput,
)
from codeguardian.core.agents.correctness_verifier import (
    CorrectnessVerifier,
    CorrectnessVerifierInput,
)
from codeguardian.core.agents.security_verifier import (
    SecurityVerifier,
    SecurityVerifierInput,
)
from codeguardian.core.orchestrator.deterministic_gate import (
    DeterministicGateResult,
)
from codeguardian.core.orchestrator.verifier_stack import (
    VerifierStackResult,
    run_verifier_stack,
)
from codeguardian.core.plugins import BudgetTracker
from codeguardian.interfaces.model import ModelProvider

__all__ = [
    "VerifierStackAgent",
    "VerifierStackInput",
    "build_verifier_agents",
]


_VERIFIER_ROLES: tuple[str, str, str] = (
    "correctness",
    "security",
    "contract",
)


class VerifierStackInput:
    """Input the loop hands a verifier stack agent.

    Attributes:
        slot: The slot the agent is filling. One of
            ``verifier_correctness``, ``verifier_security``,
            ``verifier_contract``.
        results: The loop's accumulated results. The agent reads the
            sandbox verify result from it to obtain the diff.
    """

    def __init__(self, slot: str, results: dict[str, Any]) -> None:
        self.slot = slot
        self.results = results


class _SharedVerifierRunner:
    """Runs ``run_verifier_stack`` once and caches the result.

    The runner is constructed once and shared by all three verifier
    agents. The first agent to call ``get_result`` runs the stack;
    the next two read the cache. An ``asyncio.Lock`` makes the
    first-call race safe if the loop ever dispatches the slots
    concurrently (today it dispatches them sequentially, but the
    lock costs nothing and removes the assumption).

    Args:
        provider: The model provider the verifier agents call.
        blast: The blast result, for band and score.
        test_result: The characterization test outcome as ``"PASS"``
            or ``"FAIL: ..."``.
        clone_root: The clone root, for the verifier inputs.
        target_symbol: The target qualified name.
        band: The blast band. Sets the K bound.
    """

    def __init__(
        self,
        *,
        provider: ModelProvider,
        blast: Any,
        test_result: str,
        clone_root: Any,
        target_symbol: str,
        band: str,
    ) -> None:
        self._provider = provider
        self._blast = blast
        self._test_result = test_result
        self._clone_root = clone_root
        self._target_symbol = target_symbol
        self._band = band
        self._cached: VerifierStackResult | None = None
        self._lock = asyncio.Lock()

    async def get_result(self, diff: str) -> VerifierStackResult:
        """Return the stack result, running the stack on first call."""
        async with self._lock:
            if self._cached is not None:
                return self._cached
            self._cached = await self._run(diff)
            return self._cached

    async def _run(self, diff: str) -> VerifierStackResult:
        if not diff:
            return _refused_stack_result(reason="no diff to verify; writer refused")

        blast_score = int(getattr(self._blast, "blast_score", 0))
        budget = BudgetTracker(max_calls=20, max_output_bytes=5_000_000)

        async def generate(feedback: Any) -> GenerationResult[Any]:
            return GenerationResult(payload=diff, attempt=1)

        def gate(payload: Any) -> DeterministicGateResult:
            return DeterministicGateResult(
                passed=True,
                checks=(),
                stderr_tail="",
                diff_hash="",
            )

        async def apply(payload: Any) -> None:
            return None

        async def verify(payload: Any) -> tuple[Any, ...]:
            corr_input = CorrectnessVerifierInput(
                diff=diff,
                change_kind="semantic",
                blast_score=blast_score,
                test_result=self._test_result,
                provider=self._provider,
                model="correctness_verifier",
                budget=budget,
                clone_root=self._clone_root,
                target_symbol=self._target_symbol,
            )
            sec_input = SecurityVerifierInput(
                diff=diff,
                change_kind="semantic",
                blast_score=blast_score,
                original_security_signals=(),
                provider=self._provider,
                model="security_verifier",
                budget=budget,
                clone_root=self._clone_root,
                target_symbol=self._target_symbol,
            )
            con_input = ContractVerifierInput(
                diff=diff,
                change_kind="semantic",
                blast_score=blast_score,
                contract_assertions=tuple(getattr(self._blast, "contract_assertions", ())),
                provider=self._provider,
                model="contract_verifier",
                budget=budget,
                clone_root=self._clone_root,
                target_symbol=self._target_symbol,
            )
            return await asyncio.gather(
                CorrectnessVerifier().run(corr_input),
                SecurityVerifier().run(sec_input),
                ContractVerifier().run(con_input),
            )

        async def rollback(ref: Any) -> None:
            return None

        return await run_verifier_stack(
            band=self._band,
            generate=generate,
            gate=gate,
            apply=apply,
            verify=verify,
            rollback=rollback,
            k_override=1,
        )


def _surface_verdict(
    result: VerifierStackResult,
    role: str,
) -> Any:
    """Return the verifier result the given role owns.

    The stack result carries the three verifier results in fixed
    order: correctness, security, contract. A role that does not
    appear falls back to a synthetic object whose verdict the exit
    gate rejects, so a bug surfaces as a gate failure rather than a
    silently passing phase.
    """
    verdicts = result.verdicts or ()
    for index, name in enumerate(_VERIFIER_ROLES):
        if name == role and index < len(verdicts):
            return verdicts[index]
    raise RuntimeError(
        f"verifier stack returned no verdict for role {role!r}; verdicts={len(verdicts)}"
    )


class VerifierStackAgent:
    """One verifier slot's agent. Three instances share one runner.

    Args:
        name: The slot name this agent fills.
        role: Which verdict to surface. One of ``"correctness"``,
            ``"security"``, ``"contract"``.
        runner: The shared runner.
    """

    phase = "independent_verify"

    def __init__(
        self,
        name: str,
        role: str,
        runner: _SharedVerifierRunner,
    ) -> None:
        self.name = name
        self._role = role
        self._runner = runner

    async def run(self, input: VerifierStackInput) -> Any:
        """Return this slot's verdict from the shared stack result."""
        sandbox_verify = input.results.get("sandbox_verify")
        if sandbox_verify is None:
            raise RuntimeError("verifier dispatch requires a sandbox_verify result")
        diff = getattr(sandbox_verify, "diff", "")
        result = await self._runner.get_result(diff)
        return _surface_verdict(result, self._role)


def _refused_stack_result(reason: str) -> VerifierStackResult:
    """Return a stack result where every verifier refused.

    Used when there is no diff to verify. The stack did not run; no
    model tokens were spent. Each verifier's verdict is ``"refused"``
    so the verifier exit gate accepts the phase as a legitimate
    completion.
    """
    from types import SimpleNamespace

    def refused(verifier_type: str) -> SimpleNamespace:
        return SimpleNamespace(
            verdict=SimpleNamespace(
                verdict="refused",
                verifier_type=verifier_type,
            ),
            verdict_reason=reason,
        )

    return VerifierStackResult(
        passed=False,
        k=1,
        attempts=0,
        gate_result=None,
        verdicts=(
            refused("correctness"),
            refused("security"),
            refused("contract"),
        ),
        halted_reason=reason,
        escalation_required=False,
        final_generation=None,
    )


def build_verifier_agents(
    *,
    provider: ModelProvider,
    blast: Any,
    test_result: str,
    clone_root: Any,
    target_symbol: str,
    band: str,
) -> list[VerifierStackAgent]:
    """Build the three verifier agents that share one stack runner.

    The list order matches the legacy graph's verifier slot order so a
    caller can zip it with the slot names if it needs to.
    """
    runner = _SharedVerifierRunner(
        provider=provider,
        blast=blast,
        test_result=test_result,
        clone_root=clone_root,
        target_symbol=target_symbol,
        band=band,
    )
    return [VerifierStackAgent(f"verifier_{role}", role, runner) for role in _VERIFIER_ROLES]
