"""Model gateway abstraction.

A ModelGateway is the single call surface the router uses. Three
implementations cover the three deployment archetypes:

- DirectGateway routes by profile.provider to a provider adapter the
  caller has already constructed. Suitable for a developer with one
  or two direct API keys.
- OpenRouterGateway wraps an injected provider that is pointed at
  OpenRouter. Suitable for a developer who wants 400+ models behind
  one key.
- LiteLLMGateway wraps an injected provider that is pointed at a
  LiteLLM proxy. Suitable for an enterprise with centralised spend.

All three implement the same Protocol. The router does not know which
one it is holding.

The gateways do not construct network clients. The caller builds the
provider adapter and injects it. This keeps the gateways testable
without network access and lets the caller choose how to authenticate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from codeguardian.interfaces.model import ModelOptions, ModelProvider, ModelResponse

from .routing import CapabilityProfile

__all__ = [
    "DirectGateway",
    "LiteLLMGateway",
    "ModelGateway",
    "OpenRouterGateway",
]


@runtime_checkable
class ModelGateway(Protocol):
    """The call surface the router uses.

    A gateway answers two questions: which profiles it serves, and
    what response a given profile produces for a prompt. It does not
    decide which profile to use. That decision belongs to the router.
    """

    def list_profiles(self) -> Sequence[CapabilityProfile]:
        """Return every profile this gateway can serve."""
        ...

    async def complete(
        self,
        prompt: str,
        profile: CapabilityProfile,
        options: ModelOptions,
    ) -> ModelResponse:
        """Call the model described by ``profile`` and return its response."""
        ...


class DirectGateway:
    """Gateway that routes by profile.provider to an injected adapter.

    Args:
        providers: A mapping from provider name to a constructed
            ModelProvider. Every provider named by a profile must have
            an entry here.
        profiles: The profiles this gateway serves.

    Raises:
        ValueError: If a profile names a provider that is not in the
            mapping. Failing at construction is deliberate: a missing
            provider at call time is a programming error that should
            surface before the first request.
    """

    def __init__(
        self,
        providers: Mapping[str, ModelProvider],
        profiles: Sequence[CapabilityProfile],
    ) -> None:
        missing = {p.provider for p in profiles} - set(providers)
        if missing:
            raise ValueError(
                f"no provider registered for: {sorted(missing)}; registered: {sorted(providers)}"
            )
        self._providers = dict(providers)
        self._profiles = tuple(profiles)

    def list_profiles(self) -> Sequence[CapabilityProfile]:
        """Return every profile this gateway serves."""
        return self._profiles

    async def complete(
        self,
        prompt: str,
        profile: CapabilityProfile,
        options: ModelOptions,
    ) -> ModelResponse:
        """Dispatch the call to the provider named by ``profile``."""
        provider = self._providers.get(profile.provider)
        if provider is None:
            raise ValueError(f"no provider registered for {profile.provider!r}")
        return await provider.complete(prompt, profile.model_id, options)


class OpenRouterGateway:
    """Gateway that delegates every call to one injected provider.

    The caller constructs an OpenAI-compatible provider pointed at
    OpenRouter's endpoint and injects it. The gateway holds the
    profiles and does not know the endpoint URL.

    Args:
        provider: The constructed provider to delegate to.
        profiles: The profiles this gateway serves.
    """

    def __init__(
        self,
        provider: ModelProvider,
        profiles: Sequence[CapabilityProfile],
    ) -> None:
        self._provider = provider
        self._profiles = tuple(profiles)

    def list_profiles(self) -> Sequence[CapabilityProfile]:
        """Return every profile this gateway serves."""
        return self._profiles

    async def complete(
        self,
        prompt: str,
        profile: CapabilityProfile,
        options: ModelOptions,
    ) -> ModelResponse:
        """Delegate to the injected provider."""
        return await self._provider.complete(prompt, profile.model_id, options)


class LiteLLMGateway:
    """Gateway that delegates every call to one injected provider.

    The caller constructs an OpenAI-compatible provider pointed at a
    LiteLLM proxy and injects it. The gateway holds the profiles and
    does not know the proxy URL.

    Args:
        provider: The constructed provider to delegate to.
        profiles: The profiles this gateway serves.
    """

    def __init__(
        self,
        provider: ModelProvider,
        profiles: Sequence[CapabilityProfile],
    ) -> None:
        self._provider = provider
        self._profiles = tuple(profiles)

    def list_profiles(self) -> Sequence[CapabilityProfile]:
        """Return every profile this gateway serves."""
        return self._profiles

    async def complete(
        self,
        prompt: str,
        profile: CapabilityProfile,
        options: ModelOptions,
    ) -> ModelResponse:
        """Delegate to the injected provider."""
        return await self._provider.complete(prompt, profile.model_id, options)
