"""Invariants for the error code taxonomy and the retry engine.

The taxonomy is the vocabulary every other component depends on. The
retry engine is the single loop that acts on the taxonomy's policies.
When either drifts, the failures are silent: a retry loop that never
fires, a code that maps to no category, an exception that loses its
identity when it crosses a component boundary. This test catches all of
those before they reach a runtime path.
"""

import asyncio

import pytest

from codeguardian.core.errors import (
    Backoff,
    CodeGuardianError,
    ErrorCategory,
    ErrorCode,
    RetryPolicy,
    UnknownCodeError,
    category_of,
    codes,
    compute_delay,
    retry_async,
    retry_policy_of,
    retry_sync,
    taxonomy,
)


class _RecordingSleep:
    """A sync sleep stand-in that records the delays it was given."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _RecordingAsyncSleep:
    """An async sleep stand-in that records the delays it was given."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def test_error_code_values_are_unique():
    values = [member.value for member in ErrorCode]
    assert len(values) == len(set(values)), "duplicate ErrorCode values found"


def test_codes_module_matches_enum():
    """Every constant in codes.py is an enum member, and vice versa."""
    module_codes = {
        name: getattr(codes, name)
        for name in dir(codes)
        if name.isupper() and not name.startswith("_")
    }
    enum_codes = {member.name: member.value for member in ErrorCode}
    assert module_codes == enum_codes, (
        "codes.py and ErrorCode are out of sync; add or remove the missing "
        "entries so that the two sets are identical"
    )


def test_every_code_has_a_category():
    for member in ErrorCode:
        assert member in taxonomy.CATEGORY_MAP, f"{member.value} is missing from CATEGORY_MAP"


def test_category_map_values_are_categories():
    for category in taxonomy.CATEGORY_MAP.values():
        assert isinstance(category, ErrorCategory)


def test_every_category_has_a_retry_policy():
    for category in ErrorCategory:
        assert category in taxonomy.RETRY_POLICY, f"{category.value} is missing from RETRY_POLICY"


def test_retry_policy_values_are_policies():
    for policy in taxonomy.RETRY_POLICY.values():
        assert isinstance(policy, RetryPolicy)


def test_retry_policy_fields_agree():
    """retryable must be True if and only if max_retries is greater than zero."""
    for policy in taxonomy.RETRY_POLICY.values():
        assert policy.retryable is (
            policy.max_retries > 0
        ), "RetryPolicy has inconsistent retryable and max_retries fields"


def test_overrides_replace_category_default():
    """The context-exceeded override must not be retryable."""
    override = retry_policy_of(ErrorCode.MODEL_CONTEXT_EXCEEDED)
    assert override.retryable is False
    assert override.max_retries == 0
    assert override.backoff is Backoff.NONE


def test_category_of_returns_expected_category():
    assert category_of(ErrorCode.REPO_CLONE_FAILED) is ErrorCategory.NETWORK
    assert category_of(ErrorCode.REPO_AUTH_FAILED) is ErrorCategory.AUTH
    assert category_of(ErrorCode.SANDBOX_TIMEOUT) is ErrorCategory.SANDBOX
    assert category_of(ErrorCode.PLUGIN_LOAD_FAILED) is ErrorCategory.PLUGIN
    assert category_of(ErrorCode.SYS_INTERNAL) is ErrorCategory.SYSTEM
    assert category_of(ErrorCode.FILE_PERMISSION_DENIED) is ErrorCategory.USER


def test_category_of_accepts_string_values():
    assert category_of("NET_TIMEOUT") is ErrorCategory.NETWORK
    assert category_of("FILE_NOT_FOUND") is ErrorCategory.USER


def test_category_of_raises_on_unknown_code():
    with pytest.raises(UnknownCodeError) as excinfo:
        category_of("NOT_A_REAL_CODE")
    assert excinfo.value.code == "NOT_A_REAL_CODE"


def test_retry_policy_of_returns_network_policy():
    policy = retry_policy_of(ErrorCode.NET_TIMEOUT)
    assert policy.retryable is True
    assert policy.max_retries == 5
    assert policy.backoff is Backoff.EXPONENTIAL


def test_retry_policy_of_returns_user_policy():
    policy = retry_policy_of(ErrorCode.FILE_NOT_FOUND)
    assert policy.retryable is False
    assert policy.max_retries == 0
    assert policy.backoff is Backoff.NONE


def test_retry_policy_of_returns_sandbox_policy():
    policy = retry_policy_of(ErrorCode.SANDBOX_TIMEOUT)
    assert policy.retryable is True
    assert policy.max_retries == 1
    assert policy.backoff is Backoff.FIXED


def test_retry_policy_of_accepts_string_values():
    policy = retry_policy_of("NET_UNREACHABLE")
    assert policy.retryable is True
    assert policy.max_retries == 5


def test_retry_policy_of_raises_on_unknown_code():
    with pytest.raises(UnknownCodeError):
        retry_policy_of("NOT_A_REAL_CODE")


def test_codeguardian_error_carries_all_metadata():
    error = CodeGuardianError(
        code=ErrorCode.NET_TIMEOUT,
        message="connection to api.example.com timed out",
        details={"host": "api.example.com", "attempt": 3},
        evidence="run-id 42",
    )
    assert error.code is ErrorCode.NET_TIMEOUT
    assert error.message == "connection to api.example.com timed out"
    assert error.details == {"host": "api.example.com", "attempt": 3}
    assert error.evidence == "run-id 42"
    assert error.category is ErrorCategory.NETWORK
    assert error.retry_policy.max_retries == 5


def test_codeguardian_error_copies_details_dict():
    """Mutating the caller's dict after raising must not change the error."""
    source = {"key": "value"}
    error = CodeGuardianError(code=ErrorCode.SYS_INTERNAL, message="boom", details=source)
    source["key"] = "changed"
    assert error.details == {"key": "value"}


def test_codeguardian_error_defaults_details_to_empty_dict():
    error = CodeGuardianError(code=ErrorCode.SYS_INTERNAL, message="boom")
    assert error.details == {}
    assert error.evidence is None


def test_codeguardian_error_str_contains_code_and_message():
    error = CodeGuardianError(code=ErrorCode.SYS_INTERNAL, message="boom")
    text = str(error)
    assert "SYS_INTERNAL" in text
    assert "boom" in text


def test_codeguardian_error_str_includes_evidence_when_present():
    error = CodeGuardianError(code=ErrorCode.SYS_INTERNAL, message="boom", evidence="log line 17")
    assert "log line 17" in str(error)


def test_codeguardian_error_is_catchable_as_exception():
    with pytest.raises(CodeGuardianError):
        raise CodeGuardianError(code=ErrorCode.SYS_INTERNAL, message="boom")

    with pytest.raises(Exception):  # noqa: B017
        raise CodeGuardianError(code=ErrorCode.SYS_INTERNAL, message="boom")


def test_category_map_is_immutable():
    """The mapping must not be mutable at runtime."""
    with pytest.raises(TypeError):
        taxonomy.CATEGORY_MAP[ErrorCode.SYS_INTERNAL] = ErrorCategory.USER  # type: ignore[index]


def test_retry_policy_map_is_immutable():
    with pytest.raises(TypeError):
        taxonomy.RETRY_POLICY[ErrorCategory.SYSTEM] = RetryPolicy(  # type: ignore[index]
            retryable=False, max_retries=0, backoff=Backoff.NONE
        )


def test_compute_delay_returns_zero_for_none_backoff():
    policy = RetryPolicy(retryable=False, max_retries=0, backoff=Backoff.NONE)
    assert compute_delay(1, policy) == 0.0


def test_compute_delay_fixed_returns_constant():
    policy = RetryPolicy(retryable=True, max_retries=3, backoff=Backoff.FIXED)
    assert compute_delay(1, policy) == compute_delay(2, policy)
    assert compute_delay(2, policy) == compute_delay(5, policy)


def test_compute_delay_exponential_within_expected_bounds():
    policy = RetryPolicy(retryable=True, max_retries=10, backoff=Backoff.EXPONENTIAL)
    # Attempt 1: base * 1 * jitter in [0.5, 1.5] -> [0.25, 0.75].
    for _ in range(50):
        delay = compute_delay(1, policy)
        assert 0.25 <= delay <= 0.75
    # Attempt 3: base * 4 * jitter in [0.5, 1.5] -> [1.0, 3.0].
    for _ in range(50):
        delay = compute_delay(3, policy)
        assert 1.0 <= delay <= 3.0


def test_compute_delay_caps_at_max():
    policy = RetryPolicy(retryable=True, max_retries=100, backoff=Backoff.EXPONENTIAL)
    # At a very high attempt the delay must still be bounded.
    for _ in range(50):
        delay = compute_delay(50, policy)
        assert delay <= 30.0


def test_compute_delay_rejects_attempt_below_one():
    policy = RetryPolicy(retryable=True, max_retries=3, backoff=Backoff.EXPONENTIAL)
    with pytest.raises(ValueError):
        compute_delay(0, policy)
    with pytest.raises(ValueError):
        compute_delay(-1, policy)


def test_compute_delay_accepts_seeded_rng_for_determinism():
    import random

    policy = RetryPolicy(retryable=True, max_retries=3, backoff=Backoff.EXPONENTIAL)
    rng_a = random.Random(1234)
    rng_b = random.Random(1234)
    assert compute_delay(1, policy, rng=rng_a) == compute_delay(1, policy, rng=rng_b)


def test_retry_sync_returns_immediately_on_success():
    sleep = _RecordingSleep()
    calls = {"n": 0}

    def succeed() -> int:
        calls["n"] += 1
        return 42

    assert retry_sync(succeed, sleep=sleep) == 42
    assert calls["n"] == 1
    assert sleep.calls == []


def test_retry_sync_passes_arguments_through():
    sleep = _RecordingSleep()

    def add(a: int, b: int, *, factor: int = 1) -> int:
        return (a + b) * factor

    assert retry_sync(add, 2, 3, factor=10, sleep=sleep) == 50


def test_retry_sync_retries_network_until_exhaustion():
    sleep = _RecordingSleep()
    calls = {"n": 0}

    def fail() -> None:
        calls["n"] += 1
        raise CodeGuardianError(ErrorCode.NET_TIMEOUT, "boom")

    with pytest.raises(CodeGuardianError) as excinfo:
        retry_sync(fail, sleep=sleep)
    assert excinfo.value.code is ErrorCode.NET_TIMEOUT
    # 1 initial attempt + 5 retries from NETWORK policy.
    assert calls["n"] == 6
    assert len(sleep.calls) == 5


def test_retry_sync_retries_sandbox_once():
    sleep = _RecordingSleep()
    calls = {"n": 0}

    def fail() -> None:
        calls["n"] += 1
        raise CodeGuardianError(ErrorCode.SANDBOX_TIMEOUT, "slow")

    with pytest.raises(CodeGuardianError):
        retry_sync(fail, sleep=sleep)
    # 1 initial + 1 retry from SANDBOX policy.
    assert calls["n"] == 2
    assert len(sleep.calls) == 1


def test_retry_sync_does_not_retry_user_error():
    sleep = _RecordingSleep()
    calls = {"n": 0}

    def fail() -> None:
        calls["n"] += 1
        raise CodeGuardianError(ErrorCode.FILE_NOT_FOUND, "missing")

    with pytest.raises(CodeGuardianError):
        retry_sync(fail, sleep=sleep)
    assert calls["n"] == 1
    assert sleep.calls == []


def test_retry_sync_respects_category_filter():
    sleep = _RecordingSleep()
    calls = {"n": 0}

    def fail() -> None:
        calls["n"] += 1
        raise CodeGuardianError(ErrorCode.NET_TIMEOUT, "boom")

    # NET_TIMEOUT is in the NETWORK category. Restricting to MODEL
    # prevents any retry, even though the underlying policy is retryable.
    with pytest.raises(CodeGuardianError):
        retry_sync(fail, retry_categories={ErrorCategory.MODEL}, sleep=sleep)
    assert calls["n"] == 1
    assert sleep.calls == []


def test_retry_sync_translates_stdlib_exception():
    sleep = _RecordingSleep()
    calls = {"n": 0}

    def fail() -> None:
        calls["n"] += 1
        raise ConnectionRefusedError("nope")

    with pytest.raises(CodeGuardianError) as excinfo:
        retry_sync(fail, sleep=sleep)
    assert excinfo.value.code is ErrorCode.NET_UNREACHABLE
    # NETWORK category, so 1 initial + 5 retries.
    assert calls["n"] == 6


def test_retry_sync_preserves_cause_chain():
    sleep = _RecordingSleep()

    def fail() -> None:
        raise ValueError("original")

    with pytest.raises(CodeGuardianError) as excinfo:
        retry_sync(fail, sleep=sleep)
    # The typed error is SYS_INTERNAL. Its __cause__ is the original.
    assert excinfo.value.code is ErrorCode.SYS_INTERNAL
    assert isinstance(excinfo.value.__cause__, ValueError)


def test_retry_sync_calls_on_retry_hook():
    sleep = _RecordingSleep()
    hook_calls: list[tuple[int, float, ErrorCode]] = []

    def hook(attempt: int, delay: float, exc: CodeGuardianError) -> None:
        hook_calls.append((attempt, delay, exc.code))

    def fail() -> None:
        raise CodeGuardianError(ErrorCode.SANDBOX_TIMEOUT, "slow")

    with pytest.raises(CodeGuardianError):
        retry_sync(fail, on_retry=hook, sleep=sleep)
    assert len(hook_calls) == 1
    assert hook_calls[0][0] == 1
    assert hook_calls[0][2] is ErrorCode.SANDBOX_TIMEOUT


def test_retry_sync_propagates_keyboard_interrupt():
    sleep = _RecordingSleep()
    calls = {"n": 0}

    def fail() -> None:
        calls["n"] += 1
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        retry_sync(fail, sleep=sleep)
    assert calls["n"] == 1
    assert sleep.calls == []


def test_retry_sync_propagates_system_exit():
    sleep = _RecordingSleep()
    calls = {"n": 0}

    def fail() -> None:
        calls["n"] += 1
        raise SystemExit(2)

    with pytest.raises(SystemExit):
        retry_sync(fail, sleep=sleep)
    assert calls["n"] == 1


async def test_retry_async_returns_immediately_on_success():
    sleep = _RecordingAsyncSleep()
    calls = {"n": 0}

    async def succeed() -> str:
        calls["n"] += 1
        return "ok"

    assert await retry_async(succeed, sleep=sleep) == "ok"
    assert calls["n"] == 1
    assert sleep.calls == []


async def test_retry_async_retries_network_until_exhaustion():
    sleep = _RecordingAsyncSleep()
    calls = {"n": 0}

    async def fail() -> None:
        calls["n"] += 1
        raise CodeGuardianError(ErrorCode.NET_TIMEOUT, "boom")

    with pytest.raises(CodeGuardianError):
        await retry_async(fail, sleep=sleep)
    assert calls["n"] == 6
    assert len(sleep.calls) == 5


async def test_retry_async_propagates_cancelled_error():
    sleep = _RecordingAsyncSleep()
    calls = {"n": 0}

    async def fail() -> None:
        calls["n"] += 1
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await retry_async(fail, sleep=sleep)
    assert calls["n"] == 1
    assert sleep.calls == []


async def test_retry_async_respects_category_filter():
    sleep = _RecordingAsyncSleep()
    calls = {"n": 0}

    async def fail() -> None:
        calls["n"] += 1
        raise CodeGuardianError(ErrorCode.NET_TIMEOUT, "boom")

    with pytest.raises(CodeGuardianError):
        await retry_async(
            fail,
            retry_categories={ErrorCategory.SANDBOX},
            sleep=sleep,
        )
    assert calls["n"] == 1
    assert sleep.calls == []
