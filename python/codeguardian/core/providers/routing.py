"""Model routing primitives.

Three decisions live here. A CapabilityProfile describes what a model
can do and what it costs. A TierRubric says which tier each agent
role should default to. An ObjectiveEscalation says which observable
failures should raise the tier for the next attempt.

The CapabilityProfile set is the routing input. The TierRubric is the
routing default. The ObjectiveEscalation is the routing correction.
The router composes the three; the composition lives in
fallback_router.py, not here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

__all__ = [
    "ESCALATION_TRIGGERS",
    "ROLE_TIER",
    "CapabilityProfile",
    "Tier",
    "downgrade_tier",
    "escalate_tier",
    "select_profile",
    "should_escalate",
    "tier_for",
]


Tier = Literal["cheap", "standard", "frontier", "apex"]

_TIER_ORDER: tuple[Tier, ...] = ("cheap", "standard", "frontier", "apex")


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    """What a model can do and what it costs.

    Attributes:
        model_id: The provider-specific model identifier.
        provider: The provider name this model is served by.
        family: The model family, used for verifier independence.
        tier: The routing tier the model belongs to.
        context_window: Maximum context window, in tokens.
        input_cost_per_m: Cost per million input tokens, in USD.
        output_cost_per_m: Cost per million output tokens, in USD.
        tokens_per_minute: Rate limit, tokens per minute.
        requests_per_minute: Rate limit, requests per minute.
        supports_json_mode: Whether the model supports structured JSON
            output modes.
        supports_tool_calling: Whether the model supports API-level
            tool calling.
        supports_reasoning: Whether the model returns a reasoning
            trace alongside its output.
        is_local: Whether the model is served from a local runtime.
    """

    model_id: str
    provider: str
    family: str
    tier: Tier
    context_window: int
    input_cost_per_m: float
    output_cost_per_m: float
    tokens_per_minute: int
    requests_per_minute: int
    supports_json_mode: bool = True
    supports_tool_calling: bool = False
    supports_reasoning: bool = False
    is_local: bool = False

    def estimated_cost(self, tokens_in: int, tokens_out: int) -> float:
        """Return the cost in US dollars for the given token counts."""
        return (
            tokens_in * self.input_cost_per_m + tokens_out * self.output_cost_per_m
        ) / 1_000_000.0


ROLE_TIER: Mapping[str, Tier] = MappingProxyType(
    {
        "recon": "cheap",
        "blast": "cheap",
        "analyst": "frontier",
        "tester": "standard",
        "writer": "frontier",
        "correctness_verifier": "standard",
        "security_verifier": "frontier",
        "contract_verifier": "standard",
        "skill_curator": "cheap",
    }
)

_DEFAULT_TIER: Tier = "standard"


ESCALATION_TRIGGERS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "writer": frozenset({"signature_drift", "pre_check_failed", "schema_violation"}),
        "tester": frozenset({"parse_error", "no_assertions", "old_code_failure"}),
        "analyst": frozenset({"schema_violation", "context_contradicts"}),
        "correctness_verifier": frozenset({"pre_scan_conflict"}),
        "security_verifier": frozenset({"pre_scan_conflict"}),
        "contract_verifier": frozenset({"pre_scan_conflict"}),
    }
)


def tier_for(role: str) -> Tier:
    """Return the default tier for an agent role.

    Unknown roles fall back to "standard" so that adding a new agent
    does not require a routing change until the rubric is updated.
    """
    return ROLE_TIER.get(role, _DEFAULT_TIER)


def escalate_tier(tier: Tier) -> Tier:
    """Return the next tier up, clamped at "apex"."""
    index = _TIER_ORDER.index(tier)
    return _TIER_ORDER[min(index + 1, len(_TIER_ORDER) - 1)]


def downgrade_tier(tier: Tier) -> Tier:
    """Return the next tier down, clamped at "cheap"."""
    index = _TIER_ORDER.index(tier)
    return _TIER_ORDER[max(index - 1, 0)]


def should_escalate(role: str, signals: Iterable[str]) -> bool:
    """Return True if any observed signal is a trigger for this role.

    The role's trigger set lives in ESCALATION_TRIGGERS. Roles without
    an entry never escalate. A signal that matches no trigger returns
    False, which is deliberate: confidence values and other
    non-objective signals must not raise the tier.
    """
    triggers = ESCALATION_TRIGGERS.get(role, frozenset())
    if not triggers:
        return False
    return any(signal in triggers for signal in signals)


def select_profile(
    profiles: Sequence[CapabilityProfile],
    *,
    role: str,
    pressure: Literal["ok", "downgrade"] = "ok",
    family_exclude: str | None = None,
) -> CapabilityProfile:
    """Return the profile the router should use for this call.

    Selection order is deterministic. First, candidates are filtered
    by family exclusion when one is supplied. Second, each candidate
    is scored by the distance between its tier and the preferred tier
    (which is one step lower under "downgrade" pressure). Third, ties
    are broken by cheaper combined cost, then by model_id. The result
    is stable across runs.

    Args:
        profiles: The candidate profiles. Must not be empty.
        role: The agent role, used to look up the default tier.
        pressure: "ok" or "downgrade". "halt" and "pause" are handled
            by the router before this function is called, because they
            require a decision the profile list cannot make.
        family_exclude: A family name to exclude. Used to keep a
            verifier independent of the writer's family.

    Returns:
        The selected profile.

    Raises:
        ValueError: If profiles is empty, or pressure is not "ok" or
            "downgrade".
    """
    if not profiles:
        raise ValueError("cannot select from an empty profile list")
    if pressure not in ("ok", "downgrade"):
        raise ValueError(
            f"select_profile does not handle pressure {pressure!r}; "
            "the router must resolve 'halt' and 'pause' before calling"
        )

    preferred = tier_for(role)
    if pressure == "downgrade":
        preferred = downgrade_tier(preferred)

    pool = [p for p in profiles if p.family != family_exclude] or list(profiles)

    preferred_index = _TIER_ORDER.index(preferred)

    def sort_key(profile: CapabilityProfile) -> tuple[int, float, str]:
        distance = abs(_TIER_ORDER.index(profile.tier) - preferred_index)
        cost = profile.input_cost_per_m + profile.output_cost_per_m
        return (distance, cost, profile.model_id)

    return min(pool, key=sort_key)
