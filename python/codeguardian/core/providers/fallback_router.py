"""Role-aware model routing with fallback.

The router discovers live models from each provider at planning time.
It never reads a hardcoded model list. When a provider rotates a model,
the next plan picks up the new set with no user action.

Injected adapters are used for discovery. A caller that supplies a
provider adapter via ``providers=`` controls both the execution call
and the discovery call, so a mock transport never has to coexist with
a real network adapter.

A model that failed permanently, or whose retry budget was exhausted,
is put on cooldown and skipped on the next planning step. A context
overflow is not cooled down; it is request-specific.

The ``require_no_tool_calls`` role setting, combined with the
``side_effect_guard`` context manager, prevents a retry or a fallback
after a side-effecting step.

Three additions land in Phase 13. First, an optional ``BudgetEnvelope``
gates every call: a halting envelope raises before the plan is built,
and a successful call records its tokens and estimated cost so the
next call sees the new pressure. Second, a ``blast_band_override``
keyword-only parameter lets the orchestrator force a specific tier for
the four roles whose work is hardest to verify mechanically. Third,
an ``escalation_signals`` keyword-only parameter lets the caller name
the quality signals it observed — a pre-check failure, a schema
violation — so the router can raise the tier on the next attempt. The
signals are objective: the router never escalates on a model's
self-reported confidence.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from codeguardian.core.errors import (
    CodeGuardianError,
    ErrorCategory,
    ErrorCode,
    compute_delay,
    retry_async,
    retry_policy_of,
)
from codeguardian.interfaces.model import (
    ModelChunk,
    ModelInfo,
    ModelOptions,
    ModelProvider,
    ModelResponse,
)

from .budget import BudgetEnvelope, BudgetExceededError, PressureLevel
from .model_registry import ModelRegistry, RegistryModel, RoleConfig
from .routing import (
    CapabilityProfile,
    Tier,
    downgrade_tier,
    escalate_tier,
    select_profile,
    should_escalate,
    tier_for,
)

__all__ = ["FallbackModelProvider", "side_effect_guard"]

RetryCallback = Callable[[int, float, CodeGuardianError], None]

_has_side_effect: ContextVar[bool] = ContextVar("codeguardian_has_side_effect", default=False)

_BLAST_OVERRIDE_ROLES: frozenset[str] = frozenset(
    {
        "writer",
        "correctness_verifier",
        "security_verifier",
        "contract_verifier",
    }
)
"""Roles whose tier the blast band may force.

These four produce or judge the code change itself, so the blast
recommendation is the right override for them. The analyst produces a
spec rather than a change; the tester produces a test. Neither is
overridden.
"""

_METADATA_TIER_TO_ROUTING_TIER: dict[str, Tier] = {
    "local": "cheap",
    "mid": "standard",
    "frontier": "frontier",
    "apex": "apex",
}
"""Map the metadata tier vocabulary onto the routing tier vocabulary.

The metadata file groups models into ``local``, ``mid``, and
``frontier``. The routing layer uses ``cheap``, ``standard``,
``frontier``, and ``apex``. The mapping is total: an unrecognised
metadata tier resolves to ``standard``, which is the safe default.
"""


def _metadata_tier_to_routing_tier(metadata_tier: str) -> Tier:
    """Return the routing tier for a metadata tier string."""
    return _METADATA_TIER_TO_ROUTING_TIER.get(metadata_tier, "standard")


def _resolve_target_tier(
    role_name: str,
    blast_band_override: str | None,
    escalation_signals: tuple[str, ...],
    pressure: PressureLevel,
) -> Tier:
    """Return the routing tier a plan should aim for.

    Four inputs, applied in order. The role's default comes first.
    A blast-band override replaces it for the four roles the band is
    allowed to override. Escalation signals, when they trigger for the
    role, raise it one step. Budget downgrade pressure lowers it one
    step. The order matters: an override wins over the default, an
    escalation wins over the override, and budget pressure is applied
    last so a run that is running out of money still downgrades even a
    block-band target. That last ordering is deliberate — the safety
    override is a default, not a licence to overspend.
    """
    tier = tier_for(role_name)

    if blast_band_override in ("block", "proceed") and role_name in _BLAST_OVERRIDE_ROLES:
        if blast_band_override == "block":
            tier = "frontier"
        elif blast_band_override == "proceed":
            tier = "standard"

    if escalation_signals and should_escalate(role_name, escalation_signals):
        tier = escalate_tier(tier)

    if pressure == "downgrade":
        tier = downgrade_tier(tier)

    return tier


def _to_capability_profile(model: RegistryModel) -> CapabilityProfile:
    """Return a ``CapabilityProfile`` view of a ``RegistryModel``.

    The registry model carries the fields the router needs for
    selection: tier, family, and average cost. The routing layer's
    profile type carries more, because it is also the type the gateway
    abstraction consumes. The fields the registry model does not carry
    are filled with neutral values; they are not read by
    ``select_profile``.
    """
    average_cost = float(getattr(model, "average_cost_per_mtok", 0.0))
    return CapabilityProfile(
        model_id=str(getattr(model, "id", model.qualified_id)),
        provider=str(model.provider),
        family=str(model.family),
        tier=_metadata_tier_to_routing_tier(str(model.tier)),
        context_window=0,
        input_cost_per_m=average_cost / 2.0,
        output_cost_per_m=average_cost / 2.0,
        tokens_per_minute=0,
        requests_per_minute=0,
        supports_json_mode=True,
        supports_tool_calling=False,
        supports_reasoning=False,
        is_local=(str(model.provider) == "local"),
    )


def _estimate_cost(model: RegistryModel, tokens_in: int, tokens_out: int) -> float:
    """Return the estimated USD cost of one call.

    The registry model carries one average cost per million tokens. The
    estimate splits that average evenly across input and output, which
    is the best the available metadata supports. A future metadata
    schema that separates the two prices would make this exact; until
    then the estimate is a conservative approximation.
    """
    total = tokens_in + tokens_out
    average = float(getattr(model, "average_cost_per_mtok", 0.0))
    return (total * average) / 1_000_000.0


@contextmanager
def side_effect_guard() -> Iterator[None]:
    """Mark that a side-effecting step has run for the current call.

    Use as a context manager around a call to ``complete`` or ``stream``
    when the caller knows a tool has already executed on the model's
    behalf. The router will not retry or fall back while the guard is
    active, so a Writer cannot re-apply a diff because a verifier timed
    out.
    """
    token = _has_side_effect.set(True)
    try:
        yield
    finally:
        _has_side_effect.reset(token)


def _should_cooldown(error: CodeGuardianError) -> bool:
    """Return whether ``error`` should cool down the model that raised it."""
    return error.code is not ErrorCode.MODEL_CONTEXT_EXCEEDED


@dataclass(frozen=True, slots=True)
class _Attempt:
    """A single model the router will try, with the provider to reach it."""

    model: RegistryModel
    provider: ModelProvider


@dataclass(frozen=True, slots=True)
class _Plan:
    """An ordered list of attempts plus the strictness flag for the call."""

    attempts: list[_Attempt] = field(default_factory=list)
    strict_no_retry: bool = False
    role_name: str | None = None
    target_tier: Tier | None = None


class FallbackModelProvider:
    """A ``ModelProvider`` that routes by role and falls back on failure.

    Args:
        registry: The model registry that answers discovery and role
            resolution.
        providers: A mapping from provider name to a constructed
            ``ModelProvider``. A provider not present is instantiated
            lazily through the registry.
        on_retry: Optional callback invoked before each retry sleep.
        budget: An optional ``BudgetEnvelope``. When supplied, every
            call checks its pressure before dispatch and records its
            tokens and estimated cost after a successful response.
            ``None`` preserves the pre-Phase-13 behaviour exactly.
    """

    def __init__(
        self,
        registry: ModelRegistry,
        providers: dict[str, ModelProvider] | None = None,
        on_retry: RetryCallback | None = None,
        *,
        budget: BudgetEnvelope | None = None,
    ) -> None:
        self._registry = registry
        self._providers: dict[str, ModelProvider] = dict(providers or {})
        self._cooldowns: dict[str, float] = {}
        self._last_used_by_role: dict[str, str] = {}
        self._on_retry = on_retry
        self._budget = budget

    @property
    def budget(self) -> BudgetEnvelope | None:
        """The budget envelope this router consults, or ``None``."""
        return self._budget

    def _check_budget(self, role_or_model: str) -> PressureLevel:
        """Return the budget pressure, raising when the envelope halts.

        A halting envelope raises ``BudgetExceededError`` before any
        provider is touched. A pausing envelope raises the same error
        with the envelope attached; the caller decides how long to wait
        before retrying. A downgrading envelope returns ``"downgrade"``
        so the plan can pick a cheaper tier. No envelope returns
        ``"ok"``.
        """
        if self._budget is None:
            return "ok"
        pressure = self._budget.check_pressure()
        if pressure in ("halt", "pause"):
            raise BudgetExceededError(role=role_or_model, envelope=self._budget)
        return pressure

    def _record_budget(self, model: RegistryModel, response: ModelResponse) -> None:
        """Record one successful call's tokens and estimated cost."""
        if self._budget is None:
            return
        self._budget.record(
            usd=_estimate_cost(model, response.tokens_in, response.tokens_out),
            tokens=response.tokens_in + response.tokens_out,
        )

    async def complete(
        self,
        prompt: str,
        model: str,
        options: ModelOptions,
        *,
        blast_band_override: str | None = None,
        escalation_signals: tuple[str, ...] = (),
    ) -> ModelResponse:
        """Run a completion through the planned attempt sequence.

        Args:
            prompt: The prompt text.
            model: A role name or a ``provider/model`` string.
            options: Generation parameters.
            blast_band_override: ``"block"``, ``"proceed"``, or
                ``None``. ``"block"`` forces the frontier tier for the
                writer and the three verifiers; ``"proceed"`` forces
                the standard tier for the same four roles. ``None``
                leaves the role's default tier in place.
            escalation_signals: Objective quality signals the caller
                observed — for example ``("schema_violation",)`` after
                a pre-check failure. When any signal is a trigger for
                this role, the plan's tier is raised one step.

        Returns:
            The model's response.

        Raises:
            BudgetExceededError: When the envelope halts or pauses.
            CodeGuardianError: The last error after every attempt has
                been tried.
        """
        pressure = self._check_budget(model)
        plan = await self._build_plan(
            model,
            blast_band_override=blast_band_override,
            escalation_signals=escalation_signals,
            pressure=pressure,
        )
        if plan.strict_no_retry:
            attempt = plan.attempts[0]
            response = await attempt.provider.complete(prompt, attempt.model.id, options)
            self._record_success(attempt.model, model)
            self._record_budget(attempt.model, response)
            return response

        last_error: CodeGuardianError | None = None
        for attempt in plan.attempts:
            try:
                response = await retry_async(
                    attempt.provider.complete,
                    prompt,
                    attempt.model.id,
                    options,
                    retry_categories={ErrorCategory.MODEL, ErrorCategory.NETWORK},
                    on_retry=self._on_retry,
                )
                self._record_success(attempt.model, model)
                self._record_budget(attempt.model, response)
                return response
            except CodeGuardianError as exc:
                last_error = exc
                if _should_cooldown(exc):
                    self._mark_cooldown(attempt.model)
                continue
        if last_error is not None:
            raise last_error
        raise CodeGuardianError(
            ErrorCode.PROVIDER_UNAVAILABLE,
            f"no eligible models produced a response for {model!r}",
        )

    def stream(
        self,
        prompt: str,
        model: str,
        options: ModelOptions,
        *,
        blast_band_override: str | None = None,
        escalation_signals: tuple[str, ...] = (),
    ) -> AsyncIterator[ModelChunk]:
        """Stream a completion through the planned attempt sequence.

        Streaming does not record token counts into the budget, because
        a ``ModelChunk`` carries no token accounting. The pressure check
        still runs, so a halting envelope refuses a stream the same way
        it refuses a completion. The caller that needs per-token
        accounting for a stream calls ``complete`` instead.
        """
        return self._stream_impl(
            prompt,
            model,
            options,
            blast_band_override=blast_band_override,
            escalation_signals=escalation_signals,
        )

    async def _stream_impl(
        self,
        prompt: str,
        model: str,
        options: ModelOptions,
        *,
        blast_band_override: str | None = None,
        escalation_signals: tuple[str, ...] = (),
    ) -> AsyncIterator[ModelChunk]:
        pressure = self._check_budget(model)
        plan = await self._build_plan(
            model,
            blast_band_override=blast_band_override,
            escalation_signals=escalation_signals,
            pressure=pressure,
        )
        if plan.strict_no_retry:
            attempt = plan.attempts[0]
            async for chunk in attempt.provider.stream(prompt, attempt.model.id, options):
                yield chunk
            self._record_success(attempt.model, model)
            return

        last_error: CodeGuardianError | None = None
        for attempt in plan.attempts:
            yielded_any = False
            attempt_number = 1
            while True:
                try:
                    async for chunk in attempt.provider.stream(prompt, attempt.model.id, options):
                        yielded_any = True
                        yield chunk
                    self._record_success(attempt.model, model)
                    return
                except CodeGuardianError as exc:
                    last_error = exc
                    if yielded_any:
                        raise
                    policy = retry_policy_of(exc.code)
                    if not policy.retryable or attempt_number > policy.max_retries:
                        break
                    delay = compute_delay(attempt_number, policy)
                    if self._on_retry is not None:
                        self._on_retry(attempt_number, delay, exc)
                    await asyncio.sleep(delay)
                    attempt_number += 1
            if _should_cooldown(last_error):
                self._mark_cooldown(attempt.model)
        if last_error is not None:
            raise last_error
        raise CodeGuardianError(
            ErrorCode.PROVIDER_UNAVAILABLE,
            f"no eligible models produced a stream for {model!r}",
        )

    def count_tokens(self, text: str, model: str) -> int:
        """Estimate the token count for ``text`` under ``model``."""
        if not text:
            return 0
        ratio = self._registry.chars_per_token_for(model)
        return max(1, int(len(text) / ratio + 0.5))

    def get_available_models(self) -> list[ModelInfo]:
        """Return the metadata-declared models for bootstrap use only."""
        return self._registry.metadata_models()

    async def _build_plan(
        self,
        model: str,
        *,
        blast_band_override: str | None = None,
        escalation_signals: tuple[str, ...] = (),
        pressure: PressureLevel = "ok",
    ) -> _Plan:
        """Turn a role name or a ``provider/model`` string into a plan."""
        try:
            role = self._registry.get_role(model)
        except CodeGuardianError as exc:
            if exc.code is not ErrorCode.MODEL_UNKNOWN:
                raise
            provider_name = model.split("/", 1)[0]
            await self._ensure_discovered(provider_name)
            single = self._registry.get_model(model)
            return _Plan(
                attempts=[
                    _Attempt(
                        model=single,
                        provider=self._provider_for(single.provider),
                    )
                ],
                strict_no_retry=False,
                role_name=None,
                target_tier=_metadata_tier_to_routing_tier(str(single.tier)),
            )

        excluded = self._excluded_families_for(role)
        eligible = await self._registry.resolve_candidates(
            role,
            excluded_families=excluded,
            adapters=self._providers,
        )

        target_tier = _resolve_target_tier(
            role.name,
            blast_band_override,
            escalation_signals,
            pressure,
        )
        apply_tier_filter = (
            blast_band_override in ("block", "proceed")
            or bool(escalation_signals)
            or pressure != "ok"
        )
        if apply_tier_filter:
            tier_filtered = [
                candidate
                for candidate in eligible
                if _metadata_tier_to_routing_tier(str(candidate.tier)) == target_tier
            ]
            pool = tier_filtered or eligible
        else:
            pool = eligible

        ordered = self._order_models(role, pool)
        if not ordered:
            raise CodeGuardianError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                f"no eligible models satisfy role {role.name!r}",
                details={
                    "role": role.name,
                    "excluded_families": sorted(excluded),
                    "target_tier": target_tier,
                },
            )

        # Use the routing layer's selection for the primary attempt so a
        # blast override or a downgrade pressure picks a model at the
        # intended tier, then keep the existing score order for the
        # fallbacks.
        profiles = [_to_capability_profile(candidate) for candidate in ordered]
        primary_id: str | None = None
        try:
            primary_profile = select_profile(
                profiles,
                role=role.name,
                pressure="downgrade" if pressure == "downgrade" else "ok",
                family_exclude=None,
            )
            primary_id = primary_profile.model_id
        except ValueError:
            primary_id = None

        if primary_id is not None:
            primary_model = next(
                (
                    candidate
                    for candidate in ordered
                    if str(getattr(candidate, "id", candidate.qualified_id)) == primary_id
                ),
                ordered[0],
            )
            rest = [
                candidate
                for candidate in ordered
                if candidate.qualified_id != primary_model.qualified_id
            ]
            ordered = [primary_model, *rest]

        now = time.monotonic()
        filtered = [
            candidate
            for candidate in ordered
            if not self._is_cooled_down(candidate.qualified_id, now)
        ]
        attempts = [
            _Attempt(model=candidate, provider=self._provider_for(candidate.provider))
            for candidate in (filtered or ordered)
        ]
        strict = role.require_no_tool_calls and _has_side_effect.get()
        return _Plan(
            attempts=attempts,
            strict_no_retry=strict,
            role_name=role.name,
            target_tier=target_tier,
        )

    async def _ensure_discovered(self, provider_name: str) -> None:
        """Ensure discovery has run for ``provider_name``."""
        adapter = self._providers.get(provider_name)
        await self._registry.discover(provider_name, adapter=adapter)

    def _order_models(
        self,
        role: RoleConfig,
        eligible: list[RegistryModel],
    ) -> list[RegistryModel]:
        """Order eligible models according to the role's mode."""
        by_id = {candidate.qualified_id: candidate for candidate in eligible}
        if role.mode == "pinned":
            if not role.primary:
                return []
            model = by_id.get(role.primary)
            return [model] if model is not None else []
        if role.mode == "chain":
            wanted = [role.primary, *role.fallbacks] if role.primary else list(role.fallbacks)
            return [by_id[qid] for qid in wanted if qid in by_id]
        return sorted(eligible, key=lambda candidate: self._score(candidate, role), reverse=True)

    def _score(self, model: RegistryModel, role: RoleConfig) -> float:
        """Return a quality-minus-cost score for the given model."""
        tier_quality = {"frontier": 1.0, "mid": 0.7, "local": 0.4}.get(model.tier, 0.5)
        capability_bonus = 0.05 * len(model.capabilities & role.constraints.requires_capabilities)
        quality = tier_quality + capability_bonus
        normalized_cost = min(model.average_cost_per_mtok / 10.0, 1.0)
        return (
            role.quality_cost_balance.alpha * quality
            - role.quality_cost_balance.beta * normalized_cost
        )

    def _excluded_families_for(self, role: RoleConfig) -> frozenset[str]:
        """Return families to exclude for verifier independence."""
        other = role.constraints.different_family_from
        if not other:
            return frozenset()
        used = self._last_used_by_role.get(other)
        if used:
            try:
                return frozenset({self._registry.get_model(used).family})
            except CodeGuardianError:
                pass
        return frozenset()

    def _provider_for(self, provider_name: str) -> ModelProvider:
        """Return the cached provider instance for ``provider_name``."""
        cached = self._providers.get(provider_name)
        if cached is not None:
            return cached
        created = self._registry.instantiate_provider(provider_name)
        self._providers[provider_name] = created
        return created

    def _record_success(self, model: RegistryModel, requested_as: str) -> None:
        """Record the family used by a role for verifier independence."""
        if requested_as in self._registry.role_names():
            self._last_used_by_role[requested_as] = model.qualified_id

    def _mark_cooldown(self, model: RegistryModel) -> None:
        """Place ``model`` on cooldown until the configured expiry."""
        self._cooldowns[model.qualified_id] = time.monotonic() + self._registry.cooldown_seconds

    def _is_cooled_down(self, qualified_id: str, now: float) -> bool:
        """Return whether ``qualified_id`` is currently on cooldown."""
        expiry = self._cooldowns.get(qualified_id)
        if expiry is None:
            return False
        if expiry <= now:
            self._cooldowns.pop(qualified_id, None)
            return False
        return True
