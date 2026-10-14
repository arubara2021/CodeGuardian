"""Verifier plugin contract.

A verifier inspects a diff and returns a verdict. Verifiers run on a
fresh model instance with no access to the original code or the
writer's reasoning. The context passed to ``verify`` is the entire
input the verifier receives.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

__all__ = [
    "ChangeKind",
    "ContractAssertion",
    "Finding",
    "Severity",
    "VerdictValue",
    "VerifierContext",
    "VerifierPlugin",
    "VerifierVerdict",
]

ChangeKind = Literal["syntactic", "semantic", "contract", "signature"]
Severity = Literal["info", "warning", "critical"]
VerdictValue = Literal["pass", "fail", "uncertain"]


@dataclass(frozen=True, slots=True)
class ContractAssertion:
    """One caller assumption to be preserved across a refactor."""

    caller: str
    assumption: str
    severity: Severity
    test_expression: str | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True)
class Finding:
    """One observation made by a verifier."""

    severity: Severity
    message: str
    location: str | None = None
    evidence_hash: str | None = None


@dataclass(frozen=True, slots=True)
class VerifierContext:
    """The complete input a verifier receives.

    The context is constructed from scratch for each verifier call.
    It carries the diff and, for the contract verifier, the
    assertions to check against. Nothing else.
    """

    diff: str
    change_kind: ChangeKind
    blast_score: int
    contract_assertions: tuple[ContractAssertion, ...] = ()


@dataclass(frozen=True, slots=True)
class VerifierVerdict:
    """The result of one verifier run."""

    verifier_type: Literal["correctness", "security", "contract"]
    verdict: VerdictValue
    confidence: float
    findings: tuple[Finding, ...]
    context_hash: str
    model_id: str
    timestamp: datetime


@runtime_checkable
class VerifierPlugin(Protocol):
    """A verification lens contributed by a plugin."""

    name: str
    verifier_type: Literal["correctness", "security", "contract"]

    async def verify(self, context: VerifierContext) -> VerifierVerdict:
        """Inspect the context and return a verdict."""
        ...
