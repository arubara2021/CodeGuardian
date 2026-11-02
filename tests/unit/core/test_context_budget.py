"""Tests for ContextBudget and its integration with the runtime helpers.

The budget is exercised in isolation: construction, record, check,
reset, snapshot. Three integration tests prove that tracked_model_call
records tokens when a budget is supplied and that fresh_barrier stores
the budget on the returned barrier.
"""

from __future__ import annotations

import asyncio

import pytest

from codeguardian.core.agents import _runtime
from codeguardian.core.agents._runtime import (
    ContextBudget,
    fresh_barrier,
    tracked_model_call,
)
from codeguardian.core.agents.context_budget import (
    DEFAULT_WINDOW,
    BudgetSignal,
)
from codeguardian.core.agents.context_budget import (
    ContextBudget as ContextBudgetDirect,
)
from codeguardian.core.plugins import PluginRegistry
from codeguardian.interfaces.model import ModelOptions, ModelResponse


class FakeProvider:
    """ModelProvider that returns a fixed response with known token counts."""

    def __init__(
        self,
        *,
        tokens_in: int = 100,
        tokens_out: int = 50,
        text: str = "response",
    ) -> None:
        self._tokens_in = tokens_in
        self._tokens_out = tokens_out
        self._text = text
        self.calls: list[tuple[str, str, ModelOptions]] = []

    async def complete(self, prompt: str, model: str, options: ModelOptions) -> ModelResponse:
        self.calls.append((prompt, model, options))
        return ModelResponse(
            text=self._text,
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
            model_id=model,
            finish_reason="stop",
        )

    def stream(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError

    def count_tokens(self, text: str, model: str) -> int:
        return 0

    def get_available_models(self) -> list[object]:
        return []


@pytest.fixture(scope="module")
def registry() -> PluginRegistry:
    return PluginRegistry.discover_bundled()


class TestConstruction:
    def test_default_window(self) -> None:
        budget = ContextBudget()
        assert budget.window == DEFAULT_WINDOW

    def test_explicit_window(self) -> None:
        budget = ContextBudget(window=8_000)
        assert budget.window == 8_000

    def test_zero_window_falls_back(self) -> None:
        budget = ContextBudget(window=0)
        assert budget.window == DEFAULT_WINDOW

    def test_negative_window_falls_back(self) -> None:
        budget = ContextBudget(window=-100)
        assert budget.window == DEFAULT_WINDOW

    def test_inverted_thresholds_raise(self) -> None:
        with pytest.raises(ValueError, match="thresholds must satisfy"):
            ContextBudget(window=1_000, compact_at=0.90, refuse_at=0.60)

    def test_zero_compact_at_raises(self) -> None:
        with pytest.raises(ValueError, match="thresholds must satisfy"):
            ContextBudget(window=1_000, compact_at=0.0, refuse_at=0.85)

    def test_refuse_at_above_one_raises(self) -> None:
        with pytest.raises(ValueError, match="thresholds must satisfy"):
            ContextBudget(window=1_000, compact_at=0.60, refuse_at=1.5)

    def test_equal_thresholds_raise(self) -> None:
        with pytest.raises(ValueError, match="thresholds must satisfy"):
            ContextBudget(window=1_000, compact_at=0.60, refuse_at=0.60)


class TestFillAndRecord:
    def test_initial_fill_is_zero(self) -> None:
        budget = ContextBudget(window=1_000)
        assert budget.fill == 0.0
        assert budget.total_tokens == 0

    def test_record_accumulates(self) -> None:
        budget = ContextBudget(window=1_000)
        budget.record(tokens_in=100, tokens_out=50)
        budget.record(tokens_in=50, tokens_out=25)
        assert budget.input_tokens == 150
        assert budget.output_tokens == 75
        assert budget.total_tokens == 225
        assert budget.fill == 0.225

    def test_record_rejects_negative_tokens_in(self) -> None:
        budget = ContextBudget()
        with pytest.raises(ValueError, match="tokens_in must be non-negative"):
            budget.record(tokens_in=-1)

    def test_record_rejects_negative_tokens_out(self) -> None:
        budget = ContextBudget()
        with pytest.raises(ValueError, match="tokens_out must be non-negative"):
            budget.record(tokens_out=-1)

    def test_record_zero_is_noop(self) -> None:
        budget = ContextBudget(window=1_000)
        budget.record()
        assert budget.total_tokens == 0


class TestCheck:
    def test_below_compact_threshold_is_ok(self) -> None:
        budget = ContextBudget(window=1_000)
        budget.record(tokens_in=500)
        assert budget.check() == BudgetSignal.OK

    def test_at_compact_threshold_is_compact(self) -> None:
        budget = ContextBudget(window=1_000)
        budget.record(tokens_in=600)
        assert budget.check() == BudgetSignal.COMPACT

    def test_between_thresholds_is_compact(self) -> None:
        budget = ContextBudget(window=1_000)
        budget.record(tokens_in=700)
        assert budget.check() == BudgetSignal.COMPACT

    def test_at_refuse_threshold_is_refuse(self) -> None:
        budget = ContextBudget(window=1_000)
        budget.record(tokens_in=850)
        assert budget.check() == BudgetSignal.REFUSE

    def test_above_refuse_threshold_is_refuse(self) -> None:
        budget = ContextBudget(window=1_000)
        budget.record(tokens_in=900)
        assert budget.check() == BudgetSignal.REFUSE

    def test_custom_thresholds(self) -> None:
        budget = ContextBudget(window=1_000, compact_at=0.30, refuse_at=0.50)
        budget.record(tokens_in=350)
        assert budget.check() == BudgetSignal.COMPACT
        budget.record(tokens_in=200)
        assert budget.check() == BudgetSignal.REFUSE


class TestReset:
    def test_reset_clears_to_zero(self) -> None:
        budget = ContextBudget(window=1_000)
        budget.record(tokens_in=800)
        budget.reset()
        assert budget.input_tokens == 0
        assert budget.output_tokens == 0
        assert budget.check() == BudgetSignal.OK

    def test_reset_with_summary_tokens(self) -> None:
        budget = ContextBudget(window=1_000)
        budget.record(tokens_in=800)
        budget.reset(summary_tokens=100)
        assert budget.input_tokens == 100
        assert budget.output_tokens == 0
        assert budget.check() == BudgetSignal.OK

    def test_reset_rejects_negative(self) -> None:
        budget = ContextBudget()
        with pytest.raises(ValueError, match="summary_tokens must be non-negative"):
            budget.reset(summary_tokens=-1)


class TestSnapshot:
    def test_snapshot_fields(self) -> None:
        budget = ContextBudget(window=1_000)
        budget.record(tokens_in=400, tokens_out=100)
        snap = budget.snapshot(role="writer", last_message="applying type hints")
        assert snap["role"] == "writer"
        assert snap["fill_percent"] == 50.0
        assert snap["input_tokens"] == 400
        assert snap["output_tokens"] == 100
        assert snap["total_tokens"] == 500
        assert snap["window"] == 1_000
        assert snap["compact_at"] == 0.60
        assert snap["refuse_at"] == 0.85
        assert snap["last_message"] == "applying type hints"

    def test_snapshot_last_message_optional(self) -> None:
        budget = ContextBudget(window=1_000)
        snap = budget.snapshot(role="analyst")
        assert snap["last_message"] is None

    def test_snapshot_is_json_serialisable(self) -> None:
        import json

        budget = ContextBudget(window=1_000)
        budget.record(tokens_in=100)
        snap = budget.snapshot(role="tester")
        serialised = json.dumps(snap)
        assert json.loads(serialised)["role"] == "tester"


class TestRuntimeIntegration:
    def test_tracked_model_call_records_tokens(self) -> None:
        provider = FakeProvider(tokens_in=150, tokens_out=75)
        budget = ContextBudget(window=1_000)
        _, latency_ms = asyncio.run(
            tracked_model_call(
                provider,
                "prompt",
                "model",
                ModelOptions(),
                "writer",
                context_budget=budget,
            )
        )
        assert budget.input_tokens == 150
        assert budget.output_tokens == 75
        assert latency_ms >= 0

    def test_tracked_model_call_without_budget_unchanged(self) -> None:
        provider = FakeProvider(tokens_in=150, tokens_out=75)
        response, latency_ms = asyncio.run(
            tracked_model_call(
                provider,
                "prompt",
                "model",
                ModelOptions(),
                "writer",
            )
        )
        assert response.tokens_in == 150
        assert latency_ms >= 0

    def test_fresh_barrier_accepts_budget(self, registry: PluginRegistry) -> None:
        budget = ContextBudget(window=1_000)
        with fresh_barrier(
            registry,
            "function-body",
            mode="plan",
            allowed_tools=("provide",),
            context_budget=budget,
        ) as barrier:
            assert barrier.context_budget is budget

    def test_fresh_barrier_without_budget(self, registry: PluginRegistry) -> None:
        with fresh_barrier(
            registry,
            "function-body",
            mode="plan",
            allowed_tools=("provide",),
        ) as barrier:
            assert barrier.context_budget is None


class TestReExport:
    def test_runtime_reexports_context_budget(self) -> None:
        assert ContextBudget is ContextBudgetDirect

    def test_runtime_all_includes_context_budget(self) -> None:
        assert "ContextBudget" in _runtime.__all__
