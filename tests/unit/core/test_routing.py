"""Tests for the routing primitives.

Covers the nine role-tier mappings, tier movement, objective
escalation, capability profile cost, profile selection, budget
pressure at every level, and delegation through the three gateways.
The tests are pure: no network, no filesystem, no store.
"""

from __future__ import annotations

import pytest

from codeguardian.core.providers.budget import (
    BudgetEnvelope,
    BudgetExceededError,
)
from codeguardian.core.providers.gateways import (
    DirectGateway,
    LiteLLMGateway,
    ModelGateway,
    OpenRouterGateway,
)
from codeguardian.core.providers.routing import (
    ESCALATION_TRIGGERS,
    ROLE_TIER,
    CapabilityProfile,
    downgrade_tier,
    escalate_tier,
    select_profile,
    should_escalate,
    tier_for,
)
from codeguardian.interfaces.model import ModelOptions, ModelResponse


class FakeProvider:
    """Minimal ModelProvider that records calls and returns a fixed response."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[str, str, ModelOptions]] = []

    async def complete(self, prompt: str, model: str, options: ModelOptions) -> ModelResponse:
        self.calls.append((prompt, model, options))
        return ModelResponse(
            text=f"{self.name}:{model}",
            tokens_in=1,
            tokens_out=1,
            model_id=model,
            finish_reason="stop",
        )

    def stream(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError

    def count_tokens(self, text: str, model: str) -> int:
        return 0

    def get_available_models(self) -> list[object]:
        return []


def _profile(
    *,
    model_id: str,
    provider: str = "fake",
    family: str = "fake",
    tier: str = "standard",
    input_cost_per_m: float = 1.0,
    output_cost_per_m: float = 1.0,
) -> CapabilityProfile:
    return CapabilityProfile(
        model_id=model_id,
        provider=provider,
        family=family,
        tier=tier,  # type: ignore[arg-type]
        context_window=8_000,
        input_cost_per_m=input_cost_per_m,
        output_cost_per_m=output_cost_per_m,
        tokens_per_minute=10_000,
        requests_per_minute=100,
    )


class TestTierRubric:
    def test_covers_nine_roles(self) -> None:
        expected = {
            "recon",
            "blast",
            "analyst",
            "tester",
            "writer",
            "correctness_verifier",
            "security_verifier",
            "contract_verifier",
            "skill_curator",
        }
        assert set(ROLE_TIER.keys()) == expected

    def test_tier_for_known_role(self) -> None:
        assert tier_for("writer") == "frontier"
        assert tier_for("tester") == "standard"
        assert tier_for("recon") == "cheap"

    def test_tier_for_unknown_role_defaults_to_standard(self) -> None:
        assert tier_for("not_a_role") == "standard"


class TestTierMovement:
    def test_escalate_walks_up(self) -> None:
        assert escalate_tier("cheap") == "standard"
        assert escalate_tier("standard") == "frontier"
        assert escalate_tier("frontier") == "apex"

    def test_escalate_clamps_at_apex(self) -> None:
        assert escalate_tier("apex") == "apex"

    def test_downgrade_walks_down(self) -> None:
        assert downgrade_tier("apex") == "frontier"
        assert downgrade_tier("frontier") == "standard"
        assert downgrade_tier("standard") == "cheap"

    def test_downgrade_clamps_at_cheap(self) -> None:
        assert downgrade_tier("cheap") == "cheap"


class TestObjectiveEscalation:
    def test_writer_escalates_on_signature_drift(self) -> None:
        assert should_escalate("writer", ["signature_drift"])

    def test_tester_escalates_on_parse_error(self) -> None:
        assert should_escalate("tester", ["parse_error"])

    def test_verifier_escalates_on_pre_scan_conflict(self) -> None:
        assert should_escalate("security_verifier", ["pre_scan_conflict"])

    def test_does_not_fire_on_confidence(self) -> None:
        assert not should_escalate("writer", ["confidence"])
        assert not should_escalate("writer", ["confidence: 0.4"])

    def test_does_not_fire_on_unknown_role(self) -> None:
        assert not should_escalate("recon", ["schema_violation"])

    def test_every_trigger_has_a_matching_role(self) -> None:
        for role in ESCALATION_TRIGGERS:
            assert role in ROLE_TIER, f"trigger role {role!r} not in ROLE_TIER"


class TestCapabilityProfile:
    def test_estimated_cost(self) -> None:
        profile = _profile(model_id="m", input_cost_per_m=3.0, output_cost_per_m=15.0)
        cost = profile.estimated_cost(tokens_in=1_000_000, tokens_out=1_000_000)
        assert cost == pytest.approx(18.0)

    def test_estimated_cost_zero_tokens(self) -> None:
        profile = _profile(model_id="m")
        assert profile.estimated_cost(0, 0) == 0.0


class TestSelectProfile:
    def test_picks_exact_tier(self) -> None:
        profiles = [
            _profile(model_id="cheap_m", tier="cheap"),
            _profile(model_id="std_m", tier="standard"),
            _profile(model_id="frontier_m", tier="frontier"),
        ]
        chosen = select_profile(profiles, role="tester")
        assert chosen.model_id == "std_m"

    def test_respects_family_exclusion(self) -> None:
        profiles = [
            _profile(model_id="gpt_frontier", family="gpt", tier="frontier"),
            _profile(model_id="claude_std", family="claude", tier="standard"),
        ]
        chosen = select_profile(profiles, role="writer", family_exclude="gpt")
        assert chosen.model_id == "claude_std"
        assert chosen.family == "claude"

    def test_downgrades_under_pressure(self) -> None:
        profiles = [
            _profile(model_id="cheap_m", tier="cheap"),
            _profile(model_id="std_m", tier="standard"),
            _profile(model_id="frontier_m", tier="frontier"),
        ]
        chosen = select_profile(profiles, role="writer", pressure="downgrade")
        assert chosen.tier == "standard"
        assert chosen.model_id == "std_m"

    def test_rejects_halt_pressure(self) -> None:
        profiles = [_profile(model_id="m", tier="standard")]
        with pytest.raises(ValueError, match="does not handle pressure"):
            select_profile(profiles, role="tester", pressure="halt")  # type: ignore[arg-type]

    def test_rejects_empty_profiles(self) -> None:
        with pytest.raises(ValueError, match="empty profile list"):
            select_profile([], role="tester")

    def test_tie_broken_by_cost(self) -> None:
        profiles = [
            _profile(
                model_id="expensive",
                tier="standard",
                input_cost_per_m=10.0,
                output_cost_per_m=10.0,
            ),
            _profile(
                model_id="cheap",
                tier="standard",
                input_cost_per_m=1.0,
                output_cost_per_m=1.0,
            ),
        ]
        chosen = select_profile(profiles, role="tester")
        assert chosen.model_id == "cheap"


class TestBudgetEnvelope:
    def test_no_caps_is_ok(self) -> None:
        envelope = BudgetEnvelope()
        assert envelope.check_pressure() == "ok"

    def test_zero_cost_cap_halts(self) -> None:
        envelope = BudgetEnvelope(max_cost_usd=0.0)
        assert envelope.check_pressure() == "halt"
        assert envelope.halted

    def test_eighty_percent_under_downgrade_policy(self) -> None:
        envelope = BudgetEnvelope(max_cost_usd=1.0, degradation_policy="downgrade", spent_usd=0.80)
        assert envelope.check_pressure() == "downgrade"

    def test_eighty_percent_under_pause_policy(self) -> None:
        envelope = BudgetEnvelope(max_cost_usd=1.0, degradation_policy="pause", spent_usd=0.80)
        assert envelope.check_pressure() == "pause"

    def test_eighty_percent_under_fail_policy_is_ok(self) -> None:
        envelope = BudgetEnvelope(max_cost_usd=1.0, degradation_policy="fail", spent_usd=0.80)
        assert envelope.check_pressure() == "ok"

    def test_hundred_percent_halts(self) -> None:
        envelope = BudgetEnvelope(max_cost_usd=1.0, degradation_policy="downgrade", spent_usd=1.0)
        assert envelope.check_pressure() == "halt"

    def test_record_accumulates(self) -> None:
        envelope = BudgetEnvelope(max_cost_usd=1.0)
        envelope.record(usd=0.25, tokens=100)
        envelope.record(usd=0.25, tokens=100)
        assert envelope.spent_usd == pytest.approx(0.50)
        assert envelope.spent_tokens == 200

    def test_record_rejects_negative(self) -> None:
        envelope = BudgetEnvelope()
        with pytest.raises(ValueError):
            envelope.record(usd=-0.01)
        with pytest.raises(ValueError):
            envelope.record(tokens=-1)

    def test_halt_is_sticky(self) -> None:
        envelope = BudgetEnvelope(max_cost_usd=1.0, spent_usd=1.0)
        assert envelope.check_pressure() == "halt"
        assert envelope.check_pressure() == "halt"

    def test_elapsed_is_non_negative(self) -> None:
        envelope = BudgetEnvelope()
        assert envelope.elapsed_s >= 0.0

    def test_budget_exceeded_carries_role_and_envelope(self) -> None:
        envelope = BudgetEnvelope(max_cost_usd=0.0)
        exc = BudgetExceededError(role="writer", envelope=envelope)
        assert exc.role == "writer"
        assert exc.envelope is envelope


class TestGateways:
    @pytest.mark.asyncio
    async def test_direct_gateway_delegates_by_provider(self) -> None:
        anthropic = FakeProvider("anthropic")
        openai = FakeProvider("openai")
        profiles = [
            _profile(model_id="claude_x", provider="anthropic", tier="frontier"),
            _profile(model_id="gpt_4o", provider="openai", tier="standard"),
        ]
        gateway = DirectGateway(
            providers={"anthropic": anthropic, "openai": openai},
            profiles=profiles,
        )
        await gateway.complete("hi", profiles[0], ModelOptions())
        await gateway.complete("hi", profiles[1], ModelOptions())
        assert len(anthropic.calls) == 1
        assert len(openai.calls) == 1
        assert anthropic.calls[0][1] == "claude_x"
        assert openai.calls[0][1] == "gpt_4o"

    def test_direct_gateway_rejects_unknown_provider(self) -> None:
        profiles = [_profile(model_id="m", provider="missing_provider")]
        with pytest.raises(ValueError, match="no provider registered"):
            DirectGateway(providers={}, profiles=profiles)

    @pytest.mark.asyncio
    async def test_openrouter_gateway_delegates(self) -> None:
        provider = FakeProvider("openrouter")
        profiles = [_profile(model_id="router_model", tier="frontier")]
        gateway = OpenRouterGateway(provider=provider, profiles=profiles)
        await gateway.complete("hi", profiles[0], ModelOptions())
        assert provider.calls[0][1] == "router_model"

    @pytest.mark.asyncio
    async def test_litellm_gateway_delegates(self) -> None:
        provider = FakeProvider("litellm")
        profiles = [_profile(model_id="proxy_model", tier="standard")]
        gateway = LiteLLMGateway(provider=provider, profiles=profiles)
        await gateway.complete("hi", profiles[0], ModelOptions())
        assert provider.calls[0][1] == "proxy_model"

    def test_protocol_conformance(self) -> None:
        fake = FakeProvider("p")
        profiles = [_profile(model_id="m")]
        assert isinstance(DirectGateway({"fake": fake}, profiles), ModelGateway)
        assert isinstance(OpenRouterGateway(fake, profiles), ModelGateway)
        assert isinstance(LiteLLMGateway(fake, profiles), ModelGateway)
