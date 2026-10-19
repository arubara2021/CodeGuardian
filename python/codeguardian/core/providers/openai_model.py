"""OpenAI Chat Completions adapter."""

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

__all__ = ["OpenAIProvider"]

_OPENAI_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_TIMEOUT_SECONDS = 60.0
_CHARS_PER_TOKEN_ESTIMATE = 4.0
_MAX_ERROR_BODY_CHARS = 500


class OpenAIProvider:
    """The ``ModelProvider`` implementation for the OpenAI API."""

    _provider_name: str = "openai"
    _require_api_key: bool = True

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if self._require_api_key and not api_key:
            raise CodeGuardianError(
                ErrorCode.MODEL_AUTH_FAILED,
                f"{self._provider_name} api_key is required",
            )
        self._api_key = api_key
        self._base_url = (base_url or _OPENAI_BASE_URL).rstrip("/")
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
        async with self._make_client() as client:
            try:
                response = await client.post(
                    "/chat/completions",
                    json=payload,
                    headers=self._request_headers(),
                )
            except httpx.TimeoutException as exc:
                raise CodeGuardianError(
                    ErrorCode.NET_TIMEOUT,
                    f"{self._provider_name} request timed out: {exc}",
                ) from exc
            except httpx.ConnectError as exc:
                raise CodeGuardianError(
                    ErrorCode.NET_UNREACHABLE,
                    f"{self._provider_name} is unreachable: {exc}",
                ) from exc
            _raise_for_status(response.status_code, response.text, self._provider_name)
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
        """Stream a completion, tracking reasoning separate from content.

        Reasoning models such as Groq's gpt-oss variants send their
        internal reasoning as ``delta.reasoning_content`` deltas and only
        emit visible ``delta.content`` after reasoning completes. A
        budget small enough to be consumed by reasoning alone yields no
        visible text and ``finish_reason="length"``. The stream raises
        ``MODEL_CONTEXT_EXCEEDED`` in that case, and the diagnostic
        distinguishes "reasoning consumed the budget" from "no output for
        an unspecified reason" so the caller knows which knob to turn.
        """
        payload = self._request_payload(prompt, model, options, stream=True)
        any_text = False
        reasoning_chars = 0
        final_finish_reason = "stop"
        async with self._make_client() as client:
            try:
                async with client.stream(
                    "POST",
                    "/chat/completions",
                    json=payload,
                    headers=self._request_headers(),
                ) as response:
                    if not response.is_success:
                        body = (await response.aread()).decode("utf-8", "replace")
                        _raise_for_status(response.status_code, body, self._provider_name)
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        raw = line[len("data: ") :].strip()
                        if not raw:
                            continue
                        if raw == "[DONE]":
                            break
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        choices = event.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        finish_reason = choice.get("finish_reason")
                        if finish_reason:
                            final_finish_reason = str(finish_reason)
                        delta = choice.get("delta") or {}
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                        if isinstance(reasoning, str) and reasoning:
                            reasoning_chars += len(reasoning)
                        content = delta.get("content") or ""
                        if content:
                            any_text = True
                            yield ModelChunk(text=content, done=False)
            except httpx.TimeoutException as exc:
                raise CodeGuardianError(
                    ErrorCode.NET_TIMEOUT,
                    f"{self._provider_name} stream timed out: {exc}",
                ) from exc
            except httpx.ConnectError as exc:
                raise CodeGuardianError(
                    ErrorCode.NET_UNREACHABLE,
                    f"{self._provider_name} is unreachable: {exc}",
                ) from exc

        if not any_text and final_finish_reason == "length":
            raise _budget_exhausted(
                self._provider_name, model, final_finish_reason, reasoning_chars
            )

        yield ModelChunk(text="", done=True)

    async def list_models(self) -> list[ModelInfo]:
        """Ask the provider which models it currently serves."""
        async with self._make_client() as client:
            try:
                response = await client.get("/models", headers=self._request_headers())
            except httpx.TimeoutException as exc:
                raise CodeGuardianError(
                    ErrorCode.NET_TIMEOUT,
                    f"{self._provider_name} model list timed out: {exc}",
                ) from exc
            except httpx.ConnectError as exc:
                raise CodeGuardianError(
                    ErrorCode.NET_UNREACHABLE,
                    f"{self._provider_name} is unreachable: {exc}",
                ) from exc
            _raise_for_status(response.status_code, response.text, self._provider_name)
            data = response.json()
        result: list[ModelInfo] = []
        for item in data.get("data", []):
            model_id = item.get("id")
            if not model_id:
                continue
            result.append(
                ModelInfo(
                    id=str(model_id),
                    provider="",
                    context_window=int(item.get("context_window", 0)),
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
        """Return request headers, omitting auth when no key is present."""
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

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
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": options.max_tokens,
            "temperature": options.temperature,
            "top_p": options.top_p,
        }
        if stream:
            payload["stream"] = True
        return payload

    def _parse_response(self, data: dict, model: str) -> ModelResponse:
        """Translate an OpenAI response body into ``ModelResponse``."""
        choices = data.get("choices") or []
        if not choices:
            raise CodeGuardianError(
                ErrorCode.MODEL_UNKNOWN,
                f"{self._provider_name} response contains no choices",
            )
        choice = choices[0]
        message = choice.get("message") or {}
        content = str(message.get("content") or "")
        finish_reason = str(choice.get("finish_reason", "stop"))
        if not content and finish_reason == "length":
            reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
            reasoning_chars = len(reasoning) if isinstance(reasoning, str) else 0
            raise _budget_exhausted(self._provider_name, model, finish_reason, reasoning_chars)
        usage = data.get("usage") or {}
        return ModelResponse(
            text=content,
            tokens_in=int(usage.get("prompt_tokens", 0)),
            tokens_out=int(usage.get("completion_tokens", 0)),
            model_id=str(data.get("model", model)),
            finish_reason=finish_reason,
        )


def _budget_exhausted(
    provider: str,
    model: str,
    finish_reason: str,
    reasoning_chars: int,
) -> CodeGuardianError:
    """Build a ``MODEL_CONTEXT_EXCEEDED`` error for a truncated response.

    When reasoning was produced but no visible content was returned,
    the message names that explicitly so a caller knows the failure was
    the model reasoning past the budget, not a silent empty response.
    """
    details: dict[str, object] = {
        "model": model,
        "finish_reason": finish_reason,
    }
    if reasoning_chars > 0:
        details["reasoning_chars"] = reasoning_chars
        return CodeGuardianError(
            ErrorCode.MODEL_CONTEXT_EXCEEDED,
            f"{provider} consumed the token budget on reasoning "
            f"({reasoning_chars} reasoning chars) before producing any "
            "visible output; increase max_tokens",
            details=details,
        )
    return CodeGuardianError(
        ErrorCode.MODEL_CONTEXT_EXCEEDED,
        f"{provider} consumed the token budget before producing any output; increase max_tokens",
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
    if status in (401, 403) or "invalid_api_key" in lowered:
        raise CodeGuardianError(
            ErrorCode.MODEL_AUTH_FAILED,
            f"{provider} authentication failed: {snippet}",
        )
    if status == 429:
        raise CodeGuardianError(
            ErrorCode.MODEL_RATE_LIMITED,
            f"{provider} rate limited the request: {snippet}",
        )
    if status == 400 and (
        "context_length" in lowered or "maximum context" in lowered or "context window" in lowered
    ):
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
