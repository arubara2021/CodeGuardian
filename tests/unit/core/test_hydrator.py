"""Tests for the budget-aware skill hydrator.

Covers budget enforcement, prerequisite-first ordering, truncation
metadata, and behaviour on missing bodies or empty inputs.
"""

from __future__ import annotations

from codeguardian.core.skills.hydrator import DEFAULT_TOKEN_COUNTER, hydrate
from codeguardian.core.skills.types import RetrievalItem


def _item(name: str, score: float = 1.0, reason: str = "seed") -> RetrievalItem:
    return RetrievalItem(name=name, score=score, reason=reason)


def _loader(bodies: dict[str, str]):
    def load(name: str) -> str | None:
        return bodies.get(name)

    return load


class TestTokenCounter:
    def test_empty_string_returns_zero(self) -> None:
        assert DEFAULT_TOKEN_COUNTER("") == 0

    def test_approximate_ratio(self) -> None:
        assert DEFAULT_TOKEN_COUNTER("a" * 400) == 100

    def test_small_string_returns_at_least_one(self) -> None:
        assert DEFAULT_TOKEN_COUNTER("a") == 1


class TestEmptyInputs:
    def test_no_items_returns_empty_bundle(self) -> None:
        bundle = hydrate((), lambda _: None, token_budget=1000)
        assert bundle.items == ()
        assert bundle.truncated is False
        assert bundle.total_tokens == 0

    def test_zero_budget_loads_nothing(self) -> None:
        items = (_item("a"),)
        loader = _loader({"a": "x" * 400})
        bundle = hydrate(items, loader, token_budget=0)
        assert bundle.truncated is False
        assert bundle.total_tokens == 0
        assert bundle.items[0].body is None

    def test_negative_budget_loads_nothing(self) -> None:
        items = (_item("a"),)
        loader = _loader({"a": "x" * 400})
        bundle = hydrate(items, loader, token_budget=-5)
        assert bundle.truncated is False
        assert bundle.items[0].body is None


class TestBudgetEnforcement:
    def test_ample_budget_loads_everything(self) -> None:
        bodies = {"a": "x" * 400, "b": "y" * 400}
        items = (_item("a"), _item("b"))
        bundle = hydrate(items, _loader(bodies), token_budget=10_000)
        assert bundle.truncated is False
        assert bundle.truncation_strategy == "none"
        assert all(item.body is not None for item in bundle.items)

    def test_tight_budget_truncates(self) -> None:
        bodies = {"a": "x" * 400, "b": "y" * 400, "c": "z" * 400}
        items = (_item("a", 0.9), _item("b", 0.8), _item("c", 0.7))
        bundle = hydrate(items, _loader(bodies), token_budget=150)
        assert bundle.truncated is True
        hydrated = [item.name for item in bundle.items if item.body is not None]
        assert len(hydrated) == 1

    def test_total_tokens_reflects_hydrated(self) -> None:
        bodies = {"a": "x" * 400}
        items = (_item("a"),)
        bundle = hydrate(items, _loader(bodies), token_budget=1000)
        assert bundle.total_tokens == 100

    def test_items_ordered_identically_to_input(self) -> None:
        bodies = {"a": "x" * 400, "b": "y" * 400}
        items = (_item("a", 0.9), _item("b", 0.8))
        bundle = hydrate(items, _loader(bodies), token_budget=10_000)
        names = [item.name for item in bundle.items]
        assert names == ["a", "b"]


class TestPriorityOrder:
    def test_priority_order_loads_specified_names_first(self) -> None:
        bodies = {"a": "x" * 400, "b": "y" * 400, "c": "z" * 400}
        items = (_item("a", 0.9), _item("b", 0.8), _item("c", 0.7))
        bundle = hydrate(
            items,
            _loader(bodies),
            token_budget=150,
            priority_order=("c", "a", "b"),
        )
        hydrated = [item.name for item in bundle.items if item.body]
        assert hydrated == ["c"]

    def test_priority_order_sets_strategy_label(self) -> None:
        bodies = {"a": "x" * 400, "b": "y" * 400}
        items = (_item("a", 0.9), _item("b", 0.8))
        bundle = hydrate(
            items,
            _loader(bodies),
            token_budget=150,
            priority_order=("b", "a"),
        )
        assert bundle.truncation_strategy == "prerequisite_priority"

    def test_no_priority_order_uses_score_strategy(self) -> None:
        bodies = {"a": "x" * 400, "b": "y" * 400}
        items = (_item("a", 0.9), _item("b", 0.8))
        bundle = hydrate(items, _loader(bodies), token_budget=150)
        assert bundle.truncation_strategy == "score_order"

    def test_priority_names_not_in_items_are_ignored(self) -> None:
        bodies = {"a": "x" * 400}
        items = (_item("a", 0.9),)
        bundle = hydrate(
            items,
            _loader(bodies),
            token_budget=10_000,
            priority_order=("missing", "a"),
        )
        assert bundle.items[0].body is not None


class TestMissingBodies:
    def test_missing_body_kept_in_bundle_without_cost(self) -> None:
        bodies = {"a": "x" * 400}
        items = (_item("a"), _item("b"))
        bundle = hydrate(items, _loader(bodies), token_budget=10_000)
        by_name = {item.name: item for item in bundle.items}
        assert by_name["a"].body is not None
        assert by_name["b"].body is None
        assert by_name["b"].token_count == 0

    def test_missing_body_does_not_trigger_truncation(self) -> None:
        items = (_item("a"),)
        bundle = hydrate(items, lambda _: None, token_budget=1000)
        assert bundle.truncated is False


class TestCustomCounter:
    def test_custom_counter_used_for_budget(self) -> None:
        items = (_item("a"),)
        bodies = {"a": "x" * 400}
        calls = {"n": 0}

        def counter(text: str) -> int:
            calls["n"] += 1
            return 1

        bundle = hydrate(
            items,
            _loader(bodies),
            token_budget=1000,
            token_counter=counter,
        )
        assert calls["n"] == 1
        assert bundle.total_tokens == 1


class TestMetadataPreservation:
    def test_hydrated_item_preserves_name_score_reason(self) -> None:
        items = (RetrievalItem(name="a", score=0.42, reason="seed"),)
        bodies = {"a": "x" * 40}
        bundle = hydrate(items, _loader(bodies), token_budget=1000)
        hydrated = bundle.items[0]
        assert hydrated.name == "a"
        assert hydrated.score == 0.42
        assert hydrated.reason == "seed"

    def test_negative_flag_preserved(self) -> None:
        items = (RetrievalItem(name="neg", score=0.5, reason="seed", is_negative=True),)
        bundle = hydrate(items, _loader({"neg": "x" * 40}), token_budget=1000)
        assert bundle.items[0].is_negative is True
