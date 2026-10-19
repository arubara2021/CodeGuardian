"""Behavioral contract for the verifier plugin type.

No verifier plugin ships in Phase 9. This file proves the contract is
satisfiable and testable via a fake implementation. Real verifiers are
Phase 11. Any verifier plugin registered by a later phase is validated
against the same behavioral assertions through the ``registered_verifiers``
fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from codeguardian.core.plugins import PluginRegistry
from codeguardian.interfaces import (
    ContractAssertion,
    Finding,
    VerifierContext,
    VerifierPlugin,
    VerifierVerdict,
)


class _FakeVerifier:
    name = "fake-verifier"
    verifier_type = "correctness"

    async def verify(self, context: VerifierContext) -> VerifierVerdict:
        if not context.diff.strip():
            return VerifierVerdict(
                verifier_type="correctness",
                verdict="fail",
                confidence=0.9,
                findings=(Finding(severity="critical", message="empty diff"),),
                context_hash="fake-context-hash",
                model_id="fake-model",
                timestamp=datetime.now(UTC),
            )
        return VerifierVerdict(
            verifier_type="correctness",
            verdict="pass",
            confidence=0.9,
            findings=(),
            context_hash="fake-context-hash",
            model_id="fake-model",
            timestamp=datetime.now(UTC),
        )


def _context(diff: str) -> VerifierContext:
    return VerifierContext(
        diff=diff,
        change_kind="semantic",
        blast_score=0,
        contract_assertions=(),
    )


@pytest.fixture(scope="module")
def registry() -> PluginRegistry:
    return PluginRegistry.discover_bundled()


@pytest.fixture(scope="module")
def registered_verifiers(registry: PluginRegistry) -> list[tuple[str, object]]:
    return [
        (record.name, registry.get_verifier(record.name))
        for record in registry.list_plugins()
        if record.type == "verifier"
    ]


def test_fake_verifier_satisfies_protocol() -> None:
    instance = _FakeVerifier()
    assert isinstance(instance, VerifierPlugin)


async def test_fake_verifier_returns_pass_on_non_empty_diff() -> None:
    verdict = await _FakeVerifier().verify(_context("--- a\n+++ b\n-foo\n+bar\n"))
    assert isinstance(verdict, VerifierVerdict)
    assert verdict.verdict == "pass"
    assert verdict.findings == ()


async def test_fake_verifier_returns_fail_on_empty_diff() -> None:
    verdict = await _FakeVerifier().verify(_context(""))
    assert verdict.verdict == "fail"
    assert len(verdict.findings) > 0
    assert verdict.findings[0].severity == "critical"


@pytest.mark.parametrize(
    "diff,expected_has_findings",
    [("", True), ("--- a\n+++ b\n", False)],
)
async def test_verdict_findings_match_verdict(diff: str, expected_has_findings: bool) -> None:
    verdict = await _FakeVerifier().verify(_context(diff))
    if verdict.verdict == "fail":
        assert verdict.findings, "fail verdict must carry at least one finding"
    if expected_has_findings:
        assert verdict.findings


async def test_verdict_carries_required_metadata() -> None:
    verdict = await _FakeVerifier().verify(_context("--- a\n+++ b\n"))
    assert verdict.verdict in {"pass", "fail", "uncertain"}
    assert 0.0 <= verdict.confidence <= 1.0
    assert isinstance(verdict.context_hash, str) and verdict.context_hash
    assert isinstance(verdict.model_id, str) and verdict.model_id
    assert isinstance(verdict.timestamp, datetime)


def test_registered_verifiers_satisfy_protocol(
    registered_verifiers: list[tuple[str, object]],
) -> None:
    if not registered_verifiers:
        pytest.skip("no verifier plugins registered in Phase 9")
    for name, barrier in registered_verifiers:
        assert isinstance(barrier.plugin, VerifierPlugin), name


async def test_registered_verifiers_behavioral_contract(
    registered_verifiers: list[tuple[str, object]],
) -> None:
    if not registered_verifiers:
        pytest.skip("no verifier plugins registered in Phase 9")
    for name, barrier in registered_verifiers:
        verdict = await barrier.call_async(
            "verify",
            VerifierContext(
                diff="--- a\n+++ b\n-foo\n+bar\n",
                change_kind="semantic",
                blast_score=0,
                contract_assertions=(
                    ContractAssertion(
                        caller="x.py:y",
                        assumption="f returns int",
                        severity="warning",
                    ),
                ),
            ),
        )
        assert isinstance(verdict, VerifierVerdict), name
        assert verdict.verdict in {"pass", "fail", "uncertain"}, name
        assert verdict.context_hash, name
        assert verdict.model_id, name
