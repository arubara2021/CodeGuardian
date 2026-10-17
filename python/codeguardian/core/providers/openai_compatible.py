"""Adapter for any OpenAI-compatible endpoint.

Groq, Ollama, vLLM, llama.cpp, and LM Studio all expose the OpenAI
Chat Completions API and the OpenAI ``/models`` shape. This subclass
requires a ``base_url`` and permits an empty API key.
"""

from __future__ import annotations

from codeguardian.core.errors import CodeGuardianError, ErrorCode

from .openai_model import OpenAIProvider

__all__ = ["OpenAICompatibleProvider"]


class OpenAICompatibleProvider(OpenAIProvider):
    """The ``ModelProvider`` implementation for an OpenAI-compatible API."""

    _provider_name: str = "openai_compatible"
    _require_api_key: bool = False

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str,
        transport=None,
        timeout: float = 60.0,
    ) -> None:
        if not base_url:
            raise CodeGuardianError(
                ErrorCode.MODEL_AUTH_FAILED,
                "openai-compatible provider requires a base_url",
            )
        super().__init__(
            api_key,
            base_url=base_url,
            transport=transport,
            timeout=timeout,
        )
