"""Provider-agnostic access to language models.

Every LLM call in CodeGuardian goes through ``ModelProvider``. The
system does not depend on any specific vendor: cloud providers and
OpenAI-compatible local runtimes are treated the same way. The
``complete`` and ``stream`` methods are asynchronous because the
pipeline issues model calls concurrently; ``count_tokens`` and
``get_available_models`` are synchronous because they do not perform
network I/O in the common case.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "ModelChunk",
    "ModelInfo",
    "ModelOptions",
    "ModelProvider",
    "ModelResponse",
]


@dataclass(frozen=True, slots=True)
class ModelOptions:
    """Generation parameters for a single model call.

    Attributes:
        temperature: Sampling temperature. Defaults to zero for
            deterministic output, which is required for verifier
            verdicts to be reproducible.
        max_tokens: Maximum tokens to generate.
        top_p: Nucleus sampling threshold.
    """

    temperature: float = 0.0
    max_tokens: int = 4096
    top_p: float = 1.0


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """The result of a non-streaming model call.

    Attributes:
        text: The generated text.
        tokens_in: Number of tokens in the prompt.
        tokens_out: Number of tokens in the response.
        model_id: Identifier of the model that answered.
        finish_reason: Why the model stopped, for example ``stop``,
            ``length``, or ``tool_use``.
    """

    text: str
    tokens_in: int
    tokens_out: int
    model_id: str
    finish_reason: str


@dataclass(frozen=True, slots=True)
class ModelChunk:
    """One chunk of a streaming model response.

    Attributes:
        text: Text delta for this chunk. Empty on the final chunk.
        done: Whether this is the final chunk of the stream.
    """

    text: str
    done: bool


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Identifying information about a model.

    Attributes:
        id: Provider-specific model identifier.
        provider: Name of the provider exposing the model.
        context_window: Maximum context length in tokens.
    """

    id: str
    provider: str
    context_window: int


class ModelProvider(Protocol):
    """Provider-agnostic access to a language model.

    Implementations translate provider-specific errors into typed
    ``CodeGuardianError`` codes. Callers never see a raw HTTP error
    or an SDK-specific exception.
    """

    async def complete(
        self,
        prompt: str,
        model: str,
        options: ModelOptions,
    ) -> ModelResponse:
        """Run a single non-streaming completion.

        Args:
            prompt: The prompt text.
            model: Provider-specific model identifier.
            options: Generation parameters.

        Returns:
            The model's response.

        Raises:
            CodeGuardianError: ``MODEL_UNAVAILABLE`` if the provider
                cannot be reached, ``MODEL_RATE_LIMITED`` if the
                provider is throttling, ``MODEL_CONTEXT_EXCEEDED``
                if the prompt is too long, ``MODEL_AUTH_FAILED`` if
                the credentials are invalid, ``MODEL_UNKNOWN`` if the
                model identifier is not recognized.
        """
        ...

    def stream(
        self,
        prompt: str,
        model: str,
        options: ModelOptions,
    ) -> AsyncIterator[ModelChunk]:
        """Stream a completion as an async iterator of chunks.

        The returned iterator yields chunks in order and terminates
        with a chunk whose ``done`` flag is true. The method is a
        regular function because implementations are async
        generators, which callers consume with ``async for``.

        Args:
            prompt: The prompt text.
            model: Provider-specific model identifier.
            options: Generation parameters.

        Returns:
            An async iterator of ``ModelChunk`` values.

        Raises:
            CodeGuardianError: Same codes as ``complete``.
        """
        ...

    def count_tokens(self, text: str, model: str) -> int:
        """Estimate the number of tokens ``text`` occupies.

        Args:
            text: Text to count.
            model: Provider-specific model identifier.

        Returns:
            Estimated token count.

        Raises:
            CodeGuardianError: ``MODEL_UNKNOWN`` if the model is not
                recognized.
        """
        ...

    def get_available_models(self) -> list[ModelInfo]:
        """Return models the provider can currently serve.

        Returns:
            A list of available models.

        Raises:
            CodeGuardianError: ``PROVIDER_UNAVAILABLE`` if the
                provider's model list cannot be fetched.
        """
        ...
