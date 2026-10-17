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

from .model_registry import ModelRegistry, RegistryModel, RoleConfig

__all__ = ["FallbackModelProvider", "side_effect_guard"]

RetryCallback = Callable[[int, float, CodeGuardianError], None]

_has_side_effect: ContextVar[bool] = ContextVar("codeguardian_has_side_effect", default=False)


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


class FallbackModelProvider:
    """A ``ModelProvider`` that routes by role and falls back on failure."""

    def __init__(
        self,
        registry: ModelRegistry,
        providers: dict[str, ModelProvider] | None = None,
        on_retry: RetryCallback | None = None,
    ) -> None:
        self._registry = registry
        self._providers: dict[str, ModelProvider] = dict(providers or {})
        self._cooldowns: dict[str, float] = {}
        self._last_used_by_role: dict[str, str] = {}
        self._on_retry = on_retry

    async def complete(
        self,
        prompt: str,
        model: str,
        options: ModelOptions,
    ) -> ModelResponse:
        """Run a completion through the planned attempt sequence."""
        plan = await self._build_plan(model)
        if plan.strict_no_retry:
            attempt = plan.attempts[0]
            response = await attempt.provider.complete(prompt, attempt.model.id, options)
            self._record_success(attempt.model, model)
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
    ) -> AsyncIterator[ModelChunk]:
        """Stream a completion through the planned attempt sequence."""
        return self._stream_impl(prompt, model, options)

    async def _stream_impl(
        self,
        prompt: str,
        model: str,
        options: ModelOptions,
    ) -> AsyncIterator[ModelChunk]:
        plan = await self._build_plan(model)
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

    async def _build_plan(self, model: str) -> _Plan:
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
            )

        excluded = self._excluded_families_for(role)
        eligible = await self._registry.resolve_candidates(
            role,
            excluded_families=excluded,
            adapters=self._providers,
        )
        ordered = self._order_models(role, eligible)
        if not ordered:
            raise CodeGuardianError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                f"no eligible models satisfy role {role.name!r}",
                details={
                    "role": role.name,
                    "excluded_families": sorted(excluded),
                },
            )
        now = time.monotonic()
        filtered = [m for m in ordered if not self._is_cooled_down(m.qualified_id, now)]
        attempts = [
            _Attempt(model=m, provider=self._provider_for(m.provider))
            for m in (filtered or ordered)
        ]
        strict = role.require_no_tool_calls and _has_side_effect.get()
        return _Plan(
            attempts=attempts,
            strict_no_retry=strict,
            role_name=role.name,
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
        by_id = {m.qualified_id: m for m in eligible}
        if role.mode == "pinned":
            if not role.primary:
                return []
            model = by_id.get(role.primary)
            return [model] if model is not None else []
        if role.mode == "chain":
            wanted = [role.primary, *role.fallbacks] if role.primary else list(role.fallbacks)
            return [by_id[qid] for qid in wanted if qid in by_id]
        return sorted(eligible, key=lambda m: self._score(m, role), reverse=True)

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
