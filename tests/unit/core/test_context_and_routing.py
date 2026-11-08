"""Tests for the context engine, context budget, and routing layer."""

from __future__ import annotations

from typing import Any

from codeguardian.core.agents.context_budget import (
    BudgetSignal,
    ContextBudget,
)
from codeguardian.core.orchestrator.context_engine import ContextEngine
from codeguardian.core.providers.budget import BudgetEnvelope, BudgetExceededError
from codeguardian.core.providers.fallback_router import (
    _metadata_tier_to_routing_tier,
    _resolve_target_tier,
)


def make_summarizer(chars: int):
    def summarizer(slot_name: str, output: Any) -> tuple[str, dict[str, Any]]:
        return (f"{slot_name} " + ("x" * chars), {"phase": slot_name})

    return summarizer


class TestContextEngine:
    def test_records_phase(self) -> None:
        engine = ContextEngine(budget=ContextBudget(window=1000))
        record = engine.record_phase("recon", None)
        assert record.phase == "recon"
        assert record.phase in engine.completed_phases

    def test_compiles_summaries(self) -> None:
        engine = ContextEngine(
            budget=ContextBudget(window=10_000),
            summarizer=make_summarizer(10),
        )
        engine.record_phase("recon", None)
        engine.record_phase("blast", None)
        compiled = engine.compile_for("analyst")
        assert "recon" in compiled.block
        assert "blast" in compiled.block
        assert compiled.block.startswith("## prior phases")

    def test_empty_block_when_no_phases(self) -> None:
        engine = ContextEngine(budget=ContextBudget(window=1000))
        compiled = engine.compile_for("recon")
        assert compiled.block == ""
        assert compiled.phases_included == ()

    def test_newest_first_inclusion_order(self) -> None:
        engine = ContextEngine(
            budget=ContextBudget(window=10_000),
            summarizer=make_summarizer(10),
        )
        engine.record_phase("a", None)
        engine.record_phase("b", None)
        engine.record_phase("c", None)
        compiled = engine.compile_for("d")
        assert compiled.phases_included == ("a", "b", "c")

    def test_drops_when_budget_short(self) -> None:
        engine = ContextEngine(
            budget=ContextBudget(window=1000),
            summarizer=make_summarizer(400),
            share=0.1,
        )
        engine.record_phase("p0", None)
        engine.record_phase("p1", None)
        engine.record_phase("p2", None)
        compiled = engine.compile_for("next")
        assert compiled.phases_included == ("p2",)
        assert set(compiled.phases_dropped) == {"p0", "p1"}

    def test_empty_block_when_refused(self) -> None:
        engine = ContextEngine(budget=ContextBudget(window=100))
        budget = engine.budget
        budget.record(tokens_in=90, tokens_out=0)
        compiled = engine.compile_for("next")
        assert compiled.block == ""
        assert compiled.budget_signal == "refuse"

    def test_should_compact_and_should_refuse(self) -> None:
        engine = ContextEngine(budget=ContextBudget(window=100))
        assert engine.should_compact() is False
        assert engine.should_refuse() is False

        engine.budget.record(tokens_in=70, tokens_out=0)
        assert engine.should_compact() is True
        assert engine.should_refuse() is False

        engine.budget.record(tokens_in=20, tokens_out=0)
        assert engine.should_refuse() is True

    def test_reset_with_summary(self) -> None:
        engine = ContextEngine(budget=ContextBudget(window=100))
        engine.budget.record(tokens_in=70, tokens_out=0)
        assert engine.should_compact() is True
        engine.reset_with_summary(summary_tokens=10)
        assert engine.should_compact() is False


class TestContextBudget:
    def test_ok_below_threshold(self) -> None:
        budget = ContextBudget(window=100)
        budget.record(tokens_in=59, tokens_out=0)
        assert budget.check() is BudgetSignal.OK

    def test_compact_at_60_percent(self) -> None:
        budget = ContextBudget(window=100)
        budget.record(tokens_in=60, tokens_out=0)
        assert budget.check() is BudgetSignal.COMPACT

    def test_refuse_at_85_percent(self) -> None:
        budget = ContextBudget(window=100)
        budget.record(tokens_in=85, tokens_out=0)
        assert budget.check() is BudgetSignal.REFUSE

    def test_boundary_just_below_compact(self) -> None:
        budget = ContextBudget(window=1000)
        budget.record(tokens_in=599, tokens_out=0)
        assert budget.check() is BudgetSignal.OK

    def test_boundary_just_below_refuse(self) -> None:
        budget = ContextBudget(window=1000)
        budget.record(tokens_in=849, tokens_out=0)
        assert budget.check() is BudgetSignal.COMPACT

    def test_reset_returns_to_ok(self) -> None:
        budget = ContextBudget(window=100)
        budget.record(tokens_in=80, tokens_out=0)
        assert budget.check() is BudgetSignal.COMPACT
        budget.reset(summary_tokens=5)
        assert budget.check() is BudgetSignal.OK

    def test_snapshot_shape(self) -> None:
        budget = ContextBudget(window=100)
        budget.record(tokens_in=50, tokens_out=10)
        snap = budget.snapshot(role="writer")
        assert snap["role"] == "writer"
        assert snap["window"] == 100
        assert snap["total_tokens"] == 60
        assert "fill_percent" in snap


class TestMetadataTierMapping:
    def test_local_maps_to_cheap(self) -> None:
        assert _metadata_tier_to_routing_tier("local") == "cheap"

    def test_mid_maps_to_standard(self) -> None:
        assert _metadata_tier_to_routing_tier("mid") == "standard"

    def test_frontier_maps_to_frontier(self) -> None:
        assert _metadata_tier_to_routing_tier("frontier") == "frontier"

    def test_apex_maps_to_apex(self) -> None:
        assert _metadata_tier_to_routing_tier("apex") == "apex"

    def test_unknown_defaults_to_standard(self) -> None:
        assert _metadata_tier_to_routing_tier("unknown") == "standard"


class TestResolveTargetTier:
    def test_role_default_writer(self) -> None:
        assert _resolve_target_tier("writer", None, (), "ok") == "frontier"

    def test_role_default_recon(self) -> None:
        assert _resolve_target_tier("recon", None, (), "ok") == "cheap"

    def test_blast_override_block_on_contract_verifier(self) -> None:
        assert _resolve_target_tier("contract_verifier", "block", (), "ok") == "frontier"

    def test_blast_override_proceed_on_writer(self) -> None:
        assert _resolve_target_tier("writer", "proceed", (), "ok") == "standard"

    def test_blast_override_ignored_for_non_override_role(self) -> None:
        assert _resolve_target_tier("recon", "block", (), "ok") == "cheap"

    def test_blast_override_unknown_value_ignored(self) -> None:
        assert _resolve_target_tier("writer", "unknown", (), "ok") == "frontier"

    def test_escalation_raises_writer(self) -> None:
        assert _resolve_target_tier("writer", None, ("schema_violation",), "ok") == "apex"

    def test_escalation_ignores_untriggered_signal(self) -> None:
        assert _resolve_target_tier("writer", None, ("unknown_signal",), "ok") == "frontier"

    def test_downgrade_pressure(self) -> None:
        assert _resolve_target_tier("writer", None, (), "downgrade") == "standard"

    def test_downgrade_clamps_at_cheap(self) -> None:
        assert _resolve_target_tier("recon", None, (), "downgrade") == "cheap"


class TestBudgetEnvelope:
    def test_ok_below_80_percent(self) -> None:
        envelope = BudgetEnvelope(max_cost_usd=1.0)
        envelope.record(usd=0.5)
        assert envelope.check_pressure() == "ok"

    def test_downgrade_at_80_percent(self) -> None:
        envelope = BudgetEnvelope(max_cost_usd=1.0)
        envelope.record(usd=0.80)
        assert envelope.check_pressure() == "downgrade"

    def test_halt_at_cap(self) -> None:
        envelope = BudgetEnvelope(max_cost_usd=1.0)
        envelope.record(usd=1.0)
        assert envelope.check_pressure() == "halt"

    def test_halted_is_sticky(self) -> None:
        envelope = BudgetEnvelope(max_cost_usd=1.0)
        envelope.record(usd=1.0)
        assert envelope.check_pressure() == "halt"
        envelope.record(usd=-0.0)
        assert envelope.check_pressure() == "halt"

    def test_zero_cap_halts_immediately(self) -> None:
        envelope = BudgetEnvelope(max_cost_usd=0.0)
        assert envelope.check_pressure() == "halt"

    def test_fail_policy_ok_until_cap(self) -> None:
        envelope = BudgetEnvelope(max_cost_usd=1.0, degradation_policy="fail")
        envelope.record(usd=0.80)
        assert envelope.check_pressure() == "ok"
        envelope.record(usd=0.20)
        assert envelope.check_pressure() == "halt"

    def test_pause_policy_at_80_percent(self) -> None:
        envelope = BudgetEnvelope(max_cost_usd=1.0, degradation_policy="pause")
        envelope.record(usd=0.80)
        assert envelope.check_pressure() == "pause"

    def test_token_cap_enforced(self) -> None:
        envelope = BudgetEnvelope(max_tokens=1000)
        envelope.record(tokens=1000)
        assert envelope.check_pressure() == "halt"

    def test_raise_carries_role_and_envelope(self) -> None:
        envelope = BudgetEnvelope(max_cost_usd=0.0)
        error = BudgetExceededError(role="writer", envelope=envelope)
        assert error.role == "writer"
        assert error.envelope is envelope
