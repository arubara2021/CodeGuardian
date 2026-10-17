"""Load, discover, and resolve models.

The registry is the single source of truth for *how to reach a
provider*. It is not the source of truth for which models exist; the
provider itself is. Discovery asks each adapter for the live model
list and caches it for the process lifetime.

Metadata enrichment lives in ``metadata.json``. A model missing from
that file is still usable; the router assigns conservative defaults.
This means a newly released model works immediately, and a
hand-edited metadata file is never a gate on functionality.

Discovery accepts an adapter override. When the caller supplies one,
the registry uses it instead of instantiating its own. This is what
lets a test inject a mock transport without the registry falling back
to a real network adapter.
"""

from __future__ import annotations

import importlib
import importlib.resources
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from codeguardian.core.config import get_api_key
from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.interfaces.model import ModelInfo, ModelProvider

__all__ = [
    "AdapterRegistry",
    "ModelRegistry",
    "QualityCostBalance",
    "RegistryModel",
    "RoleConfig",
    "RoleConstraints",
]

_ADAPTER_CLASSES: dict[str, tuple[str, str]] = {
    "anthropic": ("codeguardian.core.providers.anthropic_model", "AnthropicProvider"),
    "openai": ("codeguardian.core.providers.openai_model", "OpenAIProvider"),
    "deepseek": ("codeguardian.core.providers.deepseek_model", "DeepSeekProvider"),
    "openai_compatible": (
        "codeguardian.core.providers.openai_compatible",
        "OpenAICompatibleProvider",
    ),
}

_REQUIRED_ROLES: frozenset[str] = frozenset(
    {
        "analyst",
        "tester",
        "writer",
        "correctness_verifier",
        "security_verifier",
        "contract_verifier",
        "skill_curator",
    }
)

_TIER_ORDER: dict[str, int] = {"local": 0, "mid": 1, "frontier": 2}

_DEFAULT_CHARS_PER_TOKEN = 4.0
_DEFAULT_MAX_OUTPUT_TOKENS = 4096


def _invalid(message: str) -> None:
    """Raise a CONFIG_INVALID error naming the offending field."""
    raise CodeGuardianError(ErrorCode.CONFIG_INVALID, message)


def _tier_at_least(tier: str, minimum: str | None) -> bool:
    """Return whether ``tier`` ranks at or above ``minimum``."""
    if minimum is None:
        return True
    return _TIER_ORDER.get(tier, -1) >= _TIER_ORDER.get(minimum, 99)


@dataclass(frozen=True, slots=True)
class RegistryModel:
    """One model, as returned by discovery and enriched by metadata."""

    provider: str
    id: str
    context_window: int
    max_output_tokens: int
    tier: str
    family: str
    capabilities: frozenset[str]
    input_cost_per_mtok: float
    output_cost_per_mtok: float
    chars_per_token: float

    @property
    def qualified_id(self) -> str:
        """Return ``provider/model``, the canonical key."""
        return f"{self.provider}/{self.id}"

    @property
    def average_cost_per_mtok(self) -> float:
        """Return a simple mean of input and output price."""
        return (self.input_cost_per_mtok + self.output_cost_per_mtok) / 2.0


@dataclass(frozen=True, slots=True)
class _ModelMetadata:
    """One entry from ``metadata.json``."""

    tier: str
    family: str
    capabilities: frozenset[str]
    input_cost_per_mtok: float
    output_cost_per_mtok: float
    chars_per_token: float
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class RoleConstraints:
    """Hard filters applied to a role's eligible model set."""

    min_tier: str | None = None
    requires_capabilities: frozenset[str] = frozenset()
    min_context_window: int = 0
    different_family_from: str | None = None


@dataclass(frozen=True, slots=True)
class QualityCostBalance:
    """The alpha and beta weights for the auto-mode scoring function."""

    alpha: float
    beta: float


@dataclass(frozen=True, slots=True)
class RoleConfig:
    """One role's mode, constraints, and scoring weights."""

    name: str
    mode: str
    primary: str | None
    fallbacks: tuple[str, ...]
    constraints: RoleConstraints
    quality_cost_balance: QualityCostBalance
    require_no_tool_calls: bool


@dataclass(frozen=True, slots=True)
class _ProviderEntry:
    """The parsed form of one provider in the registry."""

    name: str
    adapter: str
    base_url: str | None
    api_key_source: str
    api_key_env: str | None


class AdapterRegistry:
    """Lazy importer that maps adapter names to provider classes."""

    def __init__(self) -> None:
        self._cache: dict[str, type[ModelProvider]] = {}

    def get_class(self, adapter: str) -> type[ModelProvider]:
        """Return the adapter class for ``adapter``."""
        cached = self._cache.get(adapter)
        if cached is not None:
            return cached
        if adapter not in _ADAPTER_CLASSES:
            _invalid(
                f"unknown adapter {adapter!r}; known adapters are " f"{sorted(_ADAPTER_CLASSES)}"
            )
        module_name, class_name = _ADAPTER_CLASSES[adapter]
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_NOT_IMPLEMENTED,
                f"adapter module is not available: {module_name}",
                details={"adapter": adapter, "module": module_name},
            ) from exc
        adapter_class = getattr(module, class_name, None)
        if adapter_class is None:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"adapter class not found: {module_name}.{class_name}",
            )
        self._cache[adapter] = adapter_class
        return adapter_class


class ModelRegistry:
    """Loads the registry, discovers live models, and resolves roles."""

    def __init__(
        self,
        providers: Mapping[str, _ProviderEntry],
        roles: Mapping[str, RoleConfig],
        cooldown_seconds: float,
        metadata: Mapping[str, _ModelMetadata],
        adapters: AdapterRegistry,
    ) -> None:
        self._providers = MappingProxyType(dict(providers))
        self._roles = MappingProxyType(dict(roles))
        self._cooldown_seconds = cooldown_seconds
        self._metadata = MappingProxyType(dict(metadata))
        self._adapters = adapters
        self._adapter_cache: dict[str, ModelProvider] = {}
        self._discovered: dict[str, list[ModelInfo]] = {}

    @classmethod
    def load(cls, path: Path | None = None) -> ModelRegistry:
        """Load the registry and the metadata file."""
        registry_data = cls._read_json("registry.json", path)
        metadata_data = cls._read_json("metadata.json", None)
        _validate_registry(registry_data)
        metadata = _parse_metadata(metadata_data)
        return cls.from_dict(registry_data, metadata)

    @classmethod
    def from_dict(
        cls,
        data: Any,
        metadata: Mapping[str, _ModelMetadata] | None = None,
    ) -> ModelRegistry:
        """Build a registry from an already-parsed mapping."""
        _validate_registry(data)
        providers: dict[str, _ProviderEntry] = {}
        for provider_name, provider_data in data["providers"].items():
            providers[provider_name] = _ProviderEntry(
                name=provider_name,
                adapter=provider_data["adapter"],
                base_url=provider_data.get("base_url"),
                api_key_source=provider_data["api_key_source"],
                api_key_env=provider_data.get("api_key_env"),
            )

        roles: dict[str, RoleConfig] = {}
        for role_name, role_data in data["roles"].items():
            constraints_data = role_data.get("constraints") or {}
            balance_data = role_data.get("quality_cost_balance") or {
                "alpha": 0.5,
                "beta": 0.5,
            }
            roles[role_name] = RoleConfig(
                name=role_name,
                mode=role_data["mode"],
                primary=role_data.get("primary"),
                fallbacks=tuple(role_data.get("fallbacks") or ()),
                constraints=RoleConstraints(
                    min_tier=constraints_data.get("min_tier"),
                    requires_capabilities=frozenset(
                        constraints_data.get("requires_capabilities") or ()
                    ),
                    min_context_window=int(constraints_data.get("min_context_window") or 0),
                    different_family_from=constraints_data.get("different_family_from"),
                ),
                quality_cost_balance=QualityCostBalance(
                    alpha=float(balance_data["alpha"]),
                    beta=float(balance_data["beta"]),
                ),
                require_no_tool_calls=bool(role_data.get("require_no_tool_calls", False)),
            )

        return cls(
            providers=providers,
            roles=roles,
            cooldown_seconds=float(data["cooldown_seconds"]),
            metadata=metadata or {},
            adapters=AdapterRegistry(),
        )

    @property
    def cooldown_seconds(self) -> float:
        """Return the configured cooldown duration in seconds."""
        return self._cooldown_seconds

    def role_names(self) -> frozenset[str]:
        """Return every role name defined in the registry."""
        return frozenset(self._roles)

    def get_role(self, role_name: str) -> RoleConfig:
        """Return the role configuration for ``role_name``."""
        role = self._roles.get(role_name)
        if role is None:
            raise CodeGuardianError(
                ErrorCode.MODEL_UNKNOWN,
                f"unknown role: {role_name}",
                details={"role": role_name, "known": sorted(self._roles)},
            )
        return role

    async def discover(
        self,
        provider_name: str,
        adapter: ModelProvider | None = None,
    ) -> list[ModelInfo]:
        """Return the live model list for ``provider_name``, cached.

        ``adapter``, when supplied, is used for the discovery call
        instead of the registry's own instance. This is what lets a
        caller inject a mock transport without the registry falling
        back to the network.
        """
        cached = self._discovered.get(provider_name)
        if cached is not None:
            return cached
        provider = self._providers.get(provider_name)
        if provider is None:
            raise CodeGuardianError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                f"unknown provider: {provider_name}",
            )
        resolved = adapter if adapter is not None else self._get_adapter(provider_name)
        list_method = getattr(resolved, "list_models", None)
        if list_method is None:
            raise CodeGuardianError(
                ErrorCode.SYS_NOT_IMPLEMENTED,
                f"adapter for {provider_name} does not implement list_models()",
            )
        models = await list_method()
        normalized = [
            ModelInfo(
                id=info.id,
                provider=provider_name,
                context_window=info.context_window,
            )
            for info in models
        ]
        self._discovered[provider_name] = normalized
        return normalized

    async def resolve_candidates(
        self,
        role: RoleConfig,
        excluded_families: frozenset[str] | None = None,
        adapters: Mapping[str, ModelProvider] | None = None,
    ) -> list[RegistryModel]:
        """Return every discovered model that satisfies ``role``.

        ``adapters``, when supplied, overrides the registry's own
        adapter for the named providers during discovery.
        """
        excluded = excluded_families or frozenset()
        overrides = adapters or {}
        candidates: list[RegistryModel] = []
        for provider_name in self._providers:
            try:
                discovered = await self.discover(
                    provider_name,
                    adapter=overrides.get(provider_name),
                )
            except CodeGuardianError:
                continue
            for info in discovered:
                model = self._build_registry_model(info, provider_name)
                if model.family in excluded:
                    continue
                if not _tier_at_least(model.tier, role.constraints.min_tier):
                    continue
                if not role.constraints.requires_capabilities <= model.capabilities:
                    continue
                if model.context_window < role.constraints.min_context_window:
                    continue
                candidates.append(model)
        return candidates

    def get_model(self, qualified_id: str) -> RegistryModel:
        """Return the model identified by ``provider/model``.

        When discovery has run for the provider and the model is not
        in the discovered set, ``MODEL_UNKNOWN`` is raised before any
        HTTP call. This is what turns a typo in ``pinned`` mode into a
        fail-fast error instead of a wasted request.
        """
        provider_name, separator, model_id = qualified_id.partition("/")
        if not separator or not provider_name or not model_id:
            raise CodeGuardianError(
                ErrorCode.MODEL_UNKNOWN,
                f"invalid qualified model id: {qualified_id!r}",
            )
        if provider_name not in self._providers:
            raise CodeGuardianError(
                ErrorCode.MODEL_UNKNOWN,
                f"unknown provider: {provider_name}",
            )
        meta = self._metadata.get(qualified_id)
        if meta is not None:
            return self._build_from_metadata(provider_name, model_id, meta)
        discovered = self._discovered.get(provider_name)
        if discovered is not None:
            for info in discovered:
                if info.id == model_id:
                    return self._build_registry_model(info, provider_name)
            raise CodeGuardianError(
                ErrorCode.MODEL_UNKNOWN,
                f"model {qualified_id!r} is not available from {provider_name}",
                details={"model": qualified_id},
            )
        return self._synthetic(provider_name, model_id)

    def chars_per_token_for(self, model: str) -> float:
        """Return the best known character-to-token ratio for ``model``."""
        meta = self._metadata.get(model)
        if meta is not None:
            return meta.chars_per_token
        try:
            role = self.get_role(model)
        except CodeGuardianError:
            return _DEFAULT_CHARS_PER_TOKEN
        best: _ModelMetadata | None = None
        for qualified, entry in self._metadata.items():
            provider_name = qualified.split("/", 1)[0]
            if provider_name not in self._providers:
                continue
            if not role.constraints.requires_capabilities <= entry.capabilities:
                continue
            if not _tier_at_least(entry.tier, role.constraints.min_tier):
                continue
            if best is None or entry.chars_per_token < best.chars_per_token:
                best = entry
        return best.chars_per_token if best else _DEFAULT_CHARS_PER_TOKEN

    def metadata_models(self) -> list[ModelInfo]:
        """Return the models declared in metadata, for bootstrap use only."""
        return [
            ModelInfo(
                id=qualified.split("/", 1)[1],
                provider=qualified.split("/", 1)[0],
                context_window=0,
            )
            for qualified in self._metadata
        ]

    def instantiate_provider(
        self,
        provider_name: str,
        **adapter_kwargs: Any,
    ) -> ModelProvider:
        """Build the provider adapter for ``provider_name``."""
        provider = self._providers.get(provider_name)
        if provider is None:
            raise CodeGuardianError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                f"unknown provider: {provider_name}",
            )
        api_key = _resolve_api_key(provider)
        if api_key is None and provider.api_key_source != "none":
            raise CodeGuardianError(
                ErrorCode.MODEL_AUTH_FAILED,
                f"no API key available for provider {provider_name}",
                details={
                    "provider": provider_name,
                    "source": provider.api_key_source,
                },
            )
        adapter_class = self._adapters.get_class(provider.adapter)
        return adapter_class(
            api_key=api_key,
            base_url=provider.base_url,
            **adapter_kwargs,
        )

    def _get_adapter(self, provider_name: str) -> ModelProvider:
        """Return a cached registry-owned adapter for ``provider_name``."""
        cached = self._adapter_cache.get(provider_name)
        if cached is not None:
            return cached
        adapter = self.instantiate_provider(provider_name)
        self._adapter_cache[provider_name] = adapter
        return adapter

    def _build_registry_model(
        self,
        info: ModelInfo,
        provider_name: str,
    ) -> RegistryModel:
        """Merge a discovered model with its metadata entry."""
        qualified = f"{provider_name}/{info.id}"
        meta = self._metadata.get(qualified)
        if meta is not None:
            return RegistryModel(
                provider=provider_name,
                id=info.id,
                context_window=info.context_window or 0,
                max_output_tokens=meta.max_output_tokens,
                tier=meta.tier,
                family=meta.family,
                capabilities=meta.capabilities,
                input_cost_per_mtok=meta.input_cost_per_mtok,
                output_cost_per_mtok=meta.output_cost_per_mtok,
                chars_per_token=meta.chars_per_token,
            )
        return self._synthetic(provider_name, info.id, info.context_window or 0)

    def _build_from_metadata(
        self,
        provider_name: str,
        model_id: str,
        meta: _ModelMetadata,
    ) -> RegistryModel:
        """Build a model entry entirely from metadata."""
        return RegistryModel(
            provider=provider_name,
            id=model_id,
            context_window=0,
            max_output_tokens=meta.max_output_tokens,
            tier=meta.tier,
            family=meta.family,
            capabilities=meta.capabilities,
            input_cost_per_mtok=meta.input_cost_per_mtok,
            output_cost_per_mtok=meta.output_cost_per_mtok,
            chars_per_token=meta.chars_per_token,
        )

    def _synthetic(
        self,
        provider_name: str,
        model_id: str,
        context_window: int = 0,
    ) -> RegistryModel:
        """Return a conservative default model entry."""
        return RegistryModel(
            provider=provider_name,
            id=model_id,
            context_window=context_window,
            max_output_tokens=_DEFAULT_MAX_OUTPUT_TOKENS,
            tier="mid",
            family=provider_name,
            capabilities=frozenset(),
            input_cost_per_mtok=0.0,
            output_cost_per_mtok=0.0,
            chars_per_token=_DEFAULT_CHARS_PER_TOKEN,
        )

    @staticmethod
    def _read_json(filename: str, path: Path | None) -> Any:
        """Read a JSON file from an explicit path or the package resources."""
        try:
            if path is None:
                resource = importlib.resources.files("codeguardian.core.providers").joinpath(
                    filename
                )
                raw = resource.read_text(encoding="utf-8")
            else:
                raw = Path(path).read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise CodeGuardianError(
                ErrorCode.FILE_NOT_FOUND,
                f"{filename} not found",
            ) from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CodeGuardianError(
                ErrorCode.CONFIG_PARSE_ERROR,
                f"{filename} is not valid JSON: {exc}",
            ) from exc


def _resolve_api_key(provider: _ProviderEntry) -> str | None:
    """Look up the API key for a provider, honoring its declared source."""
    if provider.api_key_source == "none":
        return None
    if provider.api_key_source == "keychain":
        return get_api_key(provider.name)
    if provider.api_key_source == "env":
        if not provider.api_key_env:
            _invalid(f"provider {provider.name} uses env source but has no api_key_env")
        return os.environ.get(provider.api_key_env)
    _invalid(
        f"provider {provider.name} has unknown api_key_source: " f"{provider.api_key_source!r}"
    )


def _parse_metadata(data: Any) -> dict[str, _ModelMetadata]:
    """Parse and validate the metadata file."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        _invalid("metadata must be a JSON object")
    if data.get("schema_version") != "1.0":
        _invalid("metadata schema_version must be '1.0'")
    models = data.get("models")
    if not isinstance(models, dict):
        _invalid("metadata 'models' must be an object")
    result: dict[str, _ModelMetadata] = {}
    for qualified_id, entry in models.items():
        if not isinstance(entry, dict):
            _invalid(f"metadata entry {qualified_id} must be an object")
        required = (
            "tier",
            "family",
            "capabilities",
            "input_cost_per_mtok",
            "output_cost_per_mtok",
            "chars_per_token",
            "max_output_tokens",
        )
        for field_name in required:
            if field_name not in entry:
                _invalid(f"metadata entry {qualified_id} missing {field_name!r}")
        if entry["tier"] not in _TIER_ORDER:
            _invalid(f"metadata entry {qualified_id} has invalid tier")
        result[qualified_id] = _ModelMetadata(
            tier=entry["tier"],
            family=str(entry["family"]),
            capabilities=frozenset(entry["capabilities"]),
            input_cost_per_mtok=float(entry["input_cost_per_mtok"]),
            output_cost_per_mtok=float(entry["output_cost_per_mtok"]),
            chars_per_token=float(entry["chars_per_token"]),
            max_output_tokens=int(entry["max_output_tokens"]),
        )
    return result


def _validate_registry(data: Any) -> None:
    """Validate the registry mapping against the documented rules."""
    if not isinstance(data, dict):
        _invalid("registry must be a JSON object")
    if data.get("schema_version") != "2.0":
        _invalid(f"schema_version must be '2.0', got {data.get('schema_version')!r}")
    providers = data.get("providers")
    if not isinstance(providers, dict) or not providers:
        _invalid("'providers' must be a non-empty object")
    for name, entry in providers.items():
        if not isinstance(entry, dict):
            _invalid(f"provider {name} must be an object")
        for field_name in ("adapter", "api_key_source"):
            if field_name not in entry:
                _invalid(f"provider {name} missing {field_name!r}")
        if entry["api_key_source"] not in ("keychain", "env", "none"):
            _invalid(f"provider {name} has invalid api_key_source")
        if entry["api_key_source"] == "env" and not entry.get("api_key_env"):
            _invalid(f"provider {name} uses env source but has no api_key_env")

    roles = data.get("roles")
    if not isinstance(roles, dict):
        _invalid("'roles' must be an object")
    missing = _REQUIRED_ROLES - set(roles)
    if missing:
        _invalid(f"roles missing required entries: {sorted(missing)}")
    for role_name, role_data in roles.items():
        _validate_role(role_name, role_data)

    if not isinstance(data.get("cooldown_seconds"), int | float):
        _invalid("'cooldown_seconds' must be a number")


def _validate_role(role_name: str, data: Any) -> None:
    """Validate one role entry."""
    if not isinstance(data, dict):
        _invalid(f"role {role_name} must be an object")
    mode = data.get("mode")
    if mode not in ("pinned", "chain", "auto"):
        _invalid(f"role {role_name} mode must be 'pinned', 'chain', or 'auto'")
    if mode == "pinned" and not data.get("primary"):
        _invalid(f"role {role_name} is pinned but has no 'primary'")
    fallbacks = data.get("fallbacks") or []
    if not isinstance(fallbacks, list):
        _invalid(f"role {role_name} fallbacks must be an array")
    balance = data.get("quality_cost_balance")
    if balance is not None:
        if not isinstance(balance, dict):
            _invalid(f"role {role_name} quality_cost_balance must be an object")
        for key in ("alpha", "beta"):
            if key not in balance or not isinstance(balance[key], int | float):
                _invalid(f"role {role_name} quality_cost_balance.{key} must be a number")
