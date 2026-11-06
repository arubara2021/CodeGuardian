"""Budget-aware hydration of retrieval items.

The retrieval pipeline selects a set of skill names, and this module
turns that selection into a bundle whose items carry their full
Markdown bodies. Hydration is bounded by a token budget and ordered so
that prerequisites load before the skills that depend on them; a
truncated bundle always retains the prerequisites it needed.

Two ordering strategies are supported. When the caller supplies an
explicit ``priority_order``, that order is used verbatim. Otherwise
the items hydrate in the order they were supplied, which is usually
score order. The pipeline uses the explicit order when a graph is
available, because a bundle that stops at the budget must not lose
the prerequisites that make the retained skills usable.

The token counter defaults to a length-based approximation that
matches the rest of the codebase (one token per four characters). A
caller that has a real tokenizer may supply one. The counter is
applied to the body only; the surrounding prompt text is charged by
the caller.

Hydration never raises on a missing body. An item whose body cannot be
loaded is left in the bundle with ``body=None`` and ``token_count=0``,
so the caller can distinguish "not loaded" from "loaded but empty".
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from .types import RetrievalBundle, RetrievalItem

__all__ = ["DEFAULT_TOKEN_COUNTER", "hydrate"]

logger = logging.getLogger(__name__)


def DEFAULT_TOKEN_COUNTER(text: str) -> int:  # noqa: N802
    """Return a rough token count for the given text.

    The approximation counts one token per four characters, rounded up,
    with a minimum of one. It is the same approximation used elsewhere
    in the codebase where the cost of loading a real tokenizer is not
    justified. Callers that need exact counts may supply their own.
    """
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


BodyLoader = Callable[[str], str | None]
TokenCounter = Callable[[str], int]


def _resolve_order(
    items: tuple[RetrievalItem, ...],
    priority_order: tuple[str, ...] | None,
) -> list[RetrievalItem]:
    """Return the items in the order they should be hydrated."""
    if not priority_order:
        return list(items)
    rank = {name: index for index, name in enumerate(priority_order)}
    by_name = {item.name: item for item in items}
    ordered: list[RetrievalItem] = []
    for name in priority_order:
        if name in by_name:
            ordered.append(by_name[name])
    for item in items:
        if item.name not in rank:
            ordered.append(item)
    return ordered


def hydrate(
    items: tuple[RetrievalItem, ...],
    body_loader: BodyLoader,
    *,
    token_budget: int,
    priority_order: tuple[str, ...] | None = None,
    token_counter: TokenCounter = DEFAULT_TOKEN_COUNTER,
) -> RetrievalBundle:
    """Load skill bodies up to a token budget.

    Args:
        items: The selected items, typically in score order.
        body_loader: A callable that returns the Markdown body for a
            skill name, or ``None`` if the body cannot be loaded.
        token_budget: The maximum number of tokens the hydrated bodies
            may consume in total.
        priority_order: When supplied, the names are hydrated in this
            order before any name not in the list. Use this to load
            prerequisites before the skills that depend on them.
        token_counter: A callable that returns the token cost of a
            body string. Defaults to the length-based approximation.

    Returns:
        A ``RetrievalBundle`` containing every input item. Items whose
        bodies were hydrated have ``body`` and ``token_count`` set.
        Items that were skipped because the budget was exhausted have
        ``body=None`` and ``token_count=0``. ``truncated`` is ``True``
        when at least one item was skipped, and ``truncation_strategy``
        names the ordering that was used: ``"none"`` for no truncation,
        ``"prerequisite_priority"`` when a ``priority_order`` was
        supplied, or ``"score_order"`` when the input order was used.
    """
    if token_budget <= 0 or not items:
        return RetrievalBundle(
            items=items,
            truncated=False,
            truncation_strategy="none",
            total_tokens=0,
            stages_run=("hydrate",),
            fallback_used=False,
        )

    ordered = _resolve_order(items, priority_order)
    hydrated: dict[str, RetrievalItem] = {}
    used_tokens = 0
    truncated = False

    for item in ordered:
        body = body_loader(item.name)
        if body is None:
            hydrated[item.name] = item
            continue
        cost = token_counter(body)
        if used_tokens + cost > token_budget:
            hydrated[item.name] = item
            truncated = True
            continue
        hydrated[item.name] = RetrievalItem(
            name=item.name,
            score=item.score,
            reason=item.reason,
            body=body,
            token_count=cost,
            is_negative=item.is_negative,
        )
        used_tokens += cost

    result_items = tuple(hydrated[item.name] for item in items)

    if not truncated:
        strategy = "none"
    elif priority_order:
        strategy = "prerequisite_priority"
    else:
        strategy = "score_order"

    return RetrievalBundle(
        items=result_items,
        truncated=truncated,
        truncation_strategy=strategy,
        total_tokens=used_tokens,
        stages_run=("hydrate",),
        fallback_used=False,
    )
