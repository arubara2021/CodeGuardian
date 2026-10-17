"""Contract tests for every ``ModelProvider`` implementation.

The four adapters — Anthropic, OpenAI, DeepSeek, OpenAI-compatible —
are tested against a mock HTTP transport, so no network and no API key
are required. The router is tested against an in-process fake provider
that returns canned model lists, so the family-exclusion and fallback
logic are exercised without touching the registry's discover step.

Every error code that a model call can produce is tested at least once,
on every adapter. The response parsing and the streaming frame parser
are exercised for real; only the wire is replaced.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping

import httpx
import pytest

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.providers.anthropic_model import AnthropicProvider
from codeguardian.core.providers.deepseek_model import DeepSeekProvider
from codeguardian.core.providers.fallback_router import FallbackModelProvider
from codeguardian.core.providers.model_registry import ModelRegistry
from codeguardian.core.providers.openai_compatible import OpenAICompatibleProvider
from codeguardian.core.providers.openai_model import OpenAIProvider
from codeguardian.interfaces.model import (
    ModelChunk,
    ModelInfo,
    ModelOptions,
    ModelResponse,
)


def _anthropic_success(model: str) -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": "hello from anthropic"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 4},
    }


def _anthropic_stream() -> bytes:
    frames = [
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello "}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "world"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        {"type": "message_stop"},
    ]
    return b"".join(b"data: " + json.dumps(f).encode() + b"\n\n" for f in frames)


def _openai_success(model: str) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello from openai"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 15, "completion_tokens": 5},
    }


def _openai_stream() -> bytes:
    frames = [
        {"choices": [{"delta": {"content": "hello "}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "world"}, "finish_reason": None}]},
    ]
    payload = b"".join(b"data: " + json.dumps(f).encode() + b"\n\n" for f in frames)
    return payload + b"data: [DONE]\n\n"


def _anthropic_factory(transport):
    return AnthropicProvider("key", transport=transport)


def _openai_factory(transport):
    return OpenAIProvider("key", transport=transport)


def _deepseek_factory(transport):
    return DeepSeekProvider("key", transport=transport)


def _compat_factory(transport):
    return OpenAICompatibleProvider("key", base_url="http://localhost:9999/v1", transport=transport)


ADAPTERS = [
    pytest.param(
        "anthropic",
        _anthropic_factory,
        _anthropic_success,
        _anthropic_stream,
        id="anthropic",
    ),
    pytest.param("openai", _openai_factory, _openai_success, _openai_stream, id="openai"),
    pytest.param(
        "deepseek",
        _deepseek_factory,
        _openai_success,
        _openai_stream,
        id="deepseek",
    ),
    pytest.param(
        "openai_compatible",
        _compat_factory,
        _openai_success,
        _openai_stream,
        id="openai_compatible",
    ),
]


def _success_handler(body_factory: Callable[[str], dict]) -> Callable:
    def handler(request: httpx.Request) -> httpx.Response:
        parsed = json.loads(request.content) if request.content else {}
        model = parsed.get("model", "unknown")
        return httpx.Response(200, json=body_factory(model))

    return handler


def _error_handler(status: int, message: str) -> Callable:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": message}})

    return handler


@pytest.mark.parametrize("name,factory,success_body,stream_bytes", ADAPTERS)
async def test_complete_returns_response(name, factory, success_body, stream_bytes):
    del name, stream_bytes
    transport = httpx.MockTransport(_success_handler(success_body))
    adapter = factory(transport)
    response = await adapter.complete("hello", "test-model", ModelOptions(max_tokens=100))
    assert isinstance(response, ModelResponse)
    assert "hello" in response.text
    assert response.tokens_in > 0
    assert response.tokens_out > 0
    assert response.model_id == "test-model"


@pytest.mark.parametrize("name,factory,success_body,stream_bytes", ADAPTERS)
async def test_stream_yields_chunks_and_done(name, factory, success_body, stream_bytes):
    del name, success_body

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=stream_bytes())

    transport = httpx.MockTransport(handler)
    adapter = factory(transport)
    chunks: list[ModelChunk] = []
    async for chunk in adapter.stream("hello", "test-model", ModelOptions()):
        chunks.append(chunk)
    text = "".join(c.text for c in chunks)
    assert "hello" in text
    assert chunks[-1].done is True


@pytest.mark.parametrize("name,factory,success_body,stream_bytes", ADAPTERS)
async def test_401_raises_auth_failed(name, factory, success_body, stream_bytes):
    del name, success_body, stream_bytes
    transport = httpx.MockTransport(_error_handler(401, "bad key"))
    adapter = factory(transport)
    with pytest.raises(CodeGuardianError) as exc:
        await adapter.complete("hi", "m", ModelOptions())
    assert exc.value.code is ErrorCode.MODEL_AUTH_FAILED


@pytest.mark.parametrize("name,factory,success_body,stream_bytes", ADAPTERS)
async def test_429_raises_rate_limited(name, factory, success_body, stream_bytes):
    del name, success_body, stream_bytes
    transport = httpx.MockTransport(_error_handler(429, "slow down"))
    adapter = factory(transport)
    with pytest.raises(CodeGuardianError) as exc:
        await adapter.complete("hi", "m", ModelOptions())
    assert exc.value.code is ErrorCode.MODEL_RATE_LIMITED


@pytest.mark.parametrize("name,factory,success_body,stream_bytes", ADAPTERS)
async def test_404_raises_provider_unavailable(name, factory, success_body, stream_bytes):
    del name, success_body, stream_bytes
    transport = httpx.MockTransport(_error_handler(404, "model_not_found"))
    adapter = factory(transport)
    with pytest.raises(CodeGuardianError) as exc:
        await adapter.complete("hi", "m", ModelOptions())
    assert exc.value.code is ErrorCode.PROVIDER_UNAVAILABLE


@pytest.mark.parametrize("name,factory,success_body,stream_bytes", ADAPTERS)
async def test_500_raises_unavailable(name, factory, success_body, stream_bytes):
    del name, success_body, stream_bytes
    transport = httpx.MockTransport(_error_handler(500, "boom"))
    adapter = factory(transport)
    with pytest.raises(CodeGuardianError) as exc:
        await adapter.complete("hi", "m", ModelOptions())
    assert exc.value.code is ErrorCode.MODEL_UNAVAILABLE


class _CannedProvider:
    """An in-process ModelProvider for router tests."""

    def __init__(
        self,
        models: list[ModelInfo],
        failures: Mapping[str, Exception] | None = None,
    ) -> None:
        self._models = models
        self._failures = dict(failures or {})
        self.calls: list[str] = []

    async def list_models(self) -> list[ModelInfo]:
        return list(self._models)

    async def complete(self, prompt: str, model: str, options: ModelOptions) -> ModelResponse:
        self.calls.append(model)
        if model in self._failures:
            raise self._failures[model]
        return ModelResponse(
            text="ok",
            tokens_in=10,
            tokens_out=3,
            model_id=model,
            finish_reason="stop",
        )

    def stream(self, prompt, model, options):
        async def gen():
            yield ModelChunk(text="ok", done=False)
            yield ModelChunk(text="", done=True)

        return gen()

    def count_tokens(self, text: str, model: str) -> int:
        return max(1, len(text) // 4)

    def get_available_models(self) -> list[ModelInfo]:
        return list(self._models)


_GROQ_MODELS = [
    ModelInfo(id="openai/gpt-oss-120b", provider="", context_window=131072),
    ModelInfo(id="openai/gpt-oss-20b", provider="", context_window=131072),
    ModelInfo(id="qwen/qwen3.8-27b", provider="", context_window=131072),
]


class TestFallbackRouter:
    async def test_writer_uses_frontier_family(self):
        registry = ModelRegistry.load()
        provider = _CannedProvider(_GROQ_MODELS)
        router = FallbackModelProvider(registry, providers={"groq": provider})
        response = await router.complete("test", "writer", ModelOptions())
        assert response.model_id == "openai/gpt-oss-120b"

    async def test_verifier_excludes_writer_family(self):
        registry = ModelRegistry.load()
        provider = _CannedProvider(_GROQ_MODELS)
        router = FallbackModelProvider(registry, providers={"groq": provider})
        await router.complete("test", "writer", ModelOptions())
        verifier_response = await router.complete("test", "correctness_verifier", ModelOptions())
        assert verifier_response.model_id == "qwen/qwen3.8-27b"

    async def test_fallback_on_auth_failure(self):
        registry = ModelRegistry.load()
        provider = _CannedProvider(
            _GROQ_MODELS,
            failures={"openai/gpt-oss-120b": CodeGuardianError(ErrorCode.MODEL_AUTH_FAILED, "bad")},
        )
        router = FallbackModelProvider(registry, providers={"groq": provider})
        response = await router.complete("test", "analyst", ModelOptions())
        assert "openai/gpt-oss-120b" in provider.calls
        assert response.model_id != "openai/gpt-oss-120b"

    async def test_unknown_model_raises_before_http(self):
        registry = ModelRegistry.load()
        provider = _CannedProvider(_GROQ_MODELS)
        router = FallbackModelProvider(registry, providers={"groq": provider})
        with pytest.raises(CodeGuardianError) as exc:
            await router.complete("test", "groq/no-such-model", ModelOptions())
        assert exc.value.code is ErrorCode.MODEL_UNKNOWN
        assert provider.calls == []

    async def test_pinned_model_bypasses_roles(self):
        registry = ModelRegistry.load()
        provider = _CannedProvider(_GROQ_MODELS)
        router = FallbackModelProvider(registry, providers={"groq": provider})
        response = await router.complete("test", "groq/openai/gpt-oss-20b", ModelOptions())
        assert response.model_id == "openai/gpt-oss-20b"

    def test_count_tokens_returns_positive(self):
        registry = ModelRegistry.load()
        provider = _CannedProvider(_GROQ_MODELS)
        router = FallbackModelProvider(registry, providers={"groq": provider})
        assert router.count_tokens("hello world", "writer") > 0
        assert router.count_tokens("", "writer") == 0
