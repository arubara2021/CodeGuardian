"""Anthropic Messages API adapter."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.interfaces.model import (
    ModelChunk,
    ModelInfo,
    ModelOptions,
    ModelResponse,
)

__all__ = ["AnthropicProvider"]

_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_TIMEOUT_SECONDS = 60.0
_CHARS_PER_TOKEN_ESTIMATE = 3.5
_MAX_ERROR_BODY_CHARS = 500


class AnthropicProvider:
    """The ``ModelProvider`` implementation for Anthropic."""

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not api_key:
            raise CodeGuardianError(
                ErrorCode.MODEL_AUTH_FAILED,
                "Anthropic api_key is required",
            )
        self._api_key = api_key
        self._base_url = (base_url or _ANTHROPIC_BASE_URL).rstrip("/")
        self._transport = transport
        self._timeout = timeout

    async def complete(
        self,
        prompt: str,
        model: str,
        options: ModelOptions,
    ) -> ModelResponse:
        """Run a single non-streaming completion."""
        payload = self._request_payload(prompt, model, options, stream=False)
        headers = self._request_headers()
        async with self._make_client() as client:
            try:
                response = await client.post("/v1/messages", json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                raise CodeGuardianError(
                    ErrorCode.NET_TIMEOUT,
                    f"Anthropic request timed out: {exc}",
                ) from exc
            except httpx.ConnectError as exc:
                raise CodeGuardianError(
                    ErrorCode.NET_UNREACHABLE,
                    f"Anthropic is unreachable: {exc}",
                ) from exc
            _raise_for_status(response.status_code, response.text, "Anthropic")
            data = response.json()
        return self._parse_response(data, model)

    def stream(
        self,
        prompt: str,
        model: str,
        options: ModelOptions,
    ) -> AsyncIterator[ModelChunk]:
        """Stream a completion as an async iterator of chunks."""
        return self._stream_impl(prompt, model, options)

    async def _stream_impl(
        self,
        prompt: str,
        model: str,
        options: ModelOptions,
    ) -> AsyncIterator[ModelChunk]:
        """Stream a completion, tracking thinking separate from text.

        Extended-thinking models emit ``thinking_delta`` events before
        any ``text_delta`` events. A budget consumed by thinking alone
        yields no visible text and ``stop_reason="max_tokens"``. The
        stream raises ``MODEL_CONTEXT_EXCEEDED`` in that case, with the
        thinking character count in the diagnostic so a caller can tell
        budget exhaustion by thinking from exhaustion by output.
        """
        payload = self._request_payload(prompt, model, options, stream=True)
        headers = self._request_headers()
        any_text = False
        reasoning_chars = 0
        stop_reason = "end_turn"
        async with self._make_client() as client:
            try:
                async with client.stream(
                    "POST", "/v1/messages", json=payload, headers=headers
                ) as response:
                    if not response.is_success:
                        body = (await response.aread()).decode("utf-8", "replace")
                        _raise_for_status(response.status_code, body, "Anthropic")
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        raw = line[len("data: ") :].strip()
                        if not raw:
                            continue
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        event_type = event.get("type")
                        if event_type == "content_block_delta":
                            delta = event.get("delta") or {}
                            delta_type = delta.get("type")
                            if delta_type == "text_delta":
                                text = delta.get("text", "")
                                if text:
                                    any_text = True
                                    yield ModelChunk(text=text, done=False)
                            elif delta_type == "thinking_delta":
                                thinking = delta.get("thinking", "")
                                if isinstance(thinking, str) and thinking:
                                    reasoning_chars += len(thinking)
                        elif event_type == "message_delta":
                            delta = event.get("delta") or {}
                            if delta.get("stop_reason"):
                                stop_reason = str(delta["stop_reason"])
                        elif event_type == "message_stop":
                            break
            except httpx.TimeoutException as exc:
                raise CodeGuardianError(
                    ErrorCode.NET_TIMEOUT,
                    f"Anthropic stream timed out: {exc}",
                ) from exc
            except httpx.ConnectError as exc:
                raise CodeGuardianError(
                    ErrorCode.NET_UNREACHABLE,
                    f"Anthropic is unreachable: {exc}",
                ) from exc

        if not any_text and stop_reason == "max_tokens":
            raise _budget_exhausted("Anthropic", model, stop_reason, reasoning_chars)

        yield ModelChunk(text="", done=True)

    async def list_models(self) -> list[ModelInfo]:
        """Ask Anthropic which models are available."""
        async with self._make_client() as client:
            try:
                response = await client.get("/v1/models", headers=self._request_headers())
            except httpx.TimeoutException as exc:
                raise CodeGuardianError(
                    ErrorCode.NET_TIMEOUT,
                    f"Anthropic model list timed out: {exc}",
                ) from exc
            except httpx.ConnectError as exc:
                raise CodeGuardianError(
                    ErrorCode.NET_UNREACHABLE,
                    f"Anthropic is unreachable: {exc}",
                ) from exc
            _raise_for_status(response.status_code, response.text, "Anthropic")
            data = response.json()
        result: list[ModelInfo] = []
        for item in data.get("data", []):
            model_id = item.get("id")
            if not model_id:
                continue
            context_window = int(item.get("max_input_tokens", 0) or item.get("context_window", 0))
            result.append(
                ModelInfo(
                    id=str(model_id),
                    provider="",
                    context_window=context_window,
                )
            )
        return result

    def count_tokens(self, text: str, model: str) -> int:
        """Estimate the token count for ``text``."""
        if not text:
            return 0
        return max(1, int(len(text) / _CHARS_PER_TOKEN_ESTIMATE + 0.5))

    def get_available_models(self) -> list[ModelInfo]:
        """Return an empty list; discovery is the source of truth."""
        return []

    def _make_client(self) -> httpx.AsyncClient:
        """Return a new ``AsyncClient`` scoped to one call."""
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=self._transport,
        )

    def _request_headers(self) -> dict[str, str]:
        """Return the headers every Anthropic request must carry."""
        return {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    def _request_payload(
        self,
        prompt: str,
        model: str,
        options: ModelOptions,
        *,
        stream: bool,
    ) -> dict[str, object]:
        """Build the request body for a completion or a stream call."""
        payload: dict[str, object] = {
            "model": model,
            "max_tokens": options.max_tokens,
            "temperature": options.temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if stream:
            payload["stream"] = True
        return payload

    def _parse_response(self, data: dict, model: str) -> ModelResponse:
        """Translate an Anthropic response body into ``ModelResponse``."""
        text_parts: list[str] = []
        reasoning_chars = 0
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(str(block.get("text", "")))
            elif block_type == "thinking":
                thinking = block.get("thinking", "")
                if isinstance(thinking, str) and thinking:
                    reasoning_chars += len(thinking)
        text = "".join(text_parts)
        stop_reason = str(data.get("stop_reason", "end_turn"))
        if not text and stop_reason == "max_tokens":
            raise _budget_exhausted("Anthropic", model, stop_reason, reasoning_chars)
        usage = data.get("usage") or {}
        return ModelResponse(
            text=text,
            tokens_in=int(usage.get("input_tokens", 0)),
            tokens_out=int(usage.get("output_tokens", 0)),
            model_id=str(data.get("model", model)),
            finish_reason=stop_reason,
        )


def _budget_exhausted(
    provider: str,
    model: str,
    finish_reason: str,
    reasoning_chars: int,
) -> CodeGuardianError:
    """Build a ``MODEL_CONTEXT_EXCEEDED`` error for a truncated response."""
    details: dict[str, object] = {
        "model": model,
        "finish_reason": finish_reason,
    }
    if reasoning_chars > 0:
        details["reasoning_chars"] = reasoning_chars
        return CodeGuardianError(
            ErrorCode.MODEL_CONTEXT_EXCEEDED,
            f"{provider} consumed the token budget on thinking "
            f"({reasoning_chars} thinking chars) before producing any "
            "visible output; increase max_tokens",
            details=details,
        )
    return CodeGuardianError(
        ErrorCode.MODEL_CONTEXT_EXCEEDED,
        f"{provider} consumed the token budget before producing any " "output; increase max_tokens",
        details=details,
    )


def _raise_for_status(status: int, body: str, provider: str) -> None:
    """Map an HTTP status onto a typed CodeGuardian error."""
    if 200 <= status < 300:
        return
    snippet = body[:_MAX_ERROR_BODY_CHARS] if body else ""
    lowered = snippet.lower()
    if status == 404 or "model_not_found" in lowered:
        raise CodeGuardianError(
            ErrorCode.PROVIDER_UNAVAILABLE,
            f"{provider} model not found or not available to this key: {snippet}",
        )
    if status in (401, 403):
        raise CodeGuardianError(
            ErrorCode.MODEL_AUTH_FAILED,
            f"{provider} authentication failed: {snippet}",
        )
    if status == 429:
        raise CodeGuardianError(
            ErrorCode.MODEL_RATE_LIMITED,
            f"{provider} rate limited the request: {snippet}",
        )
    if status == 400 and "context" in lowered:
        raise CodeGuardianError(
            ErrorCode.MODEL_CONTEXT_EXCEEDED,
            f"{provider} context window exceeded: {snippet}",
        )
    if 500 <= status < 600:
        raise CodeGuardianError(
            ErrorCode.MODEL_UNAVAILABLE,
            f"{provider} server error {status}: {snippet}",
        )
    raise CodeGuardianError(
        ErrorCode.MODEL_UNKNOWN,
        f"{provider} returned status {status}: {snippet}",
    )
