"""DeepSeek adapter."""

from __future__ import annotations

from .openai_model import OpenAIProvider

__all__ = ["DeepSeekProvider"]

_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


class DeepSeekProvider(OpenAIProvider):
    """The ``ModelProvider`` implementation for the DeepSeek API."""

    _provider_name: str = "deepseek"
    _require_api_key: bool = True

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str | None = None,
        transport=None,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(
            api_key,
            base_url=base_url or _DEEPSEEK_BASE_URL,
            transport=transport,
            timeout=timeout,
        )
