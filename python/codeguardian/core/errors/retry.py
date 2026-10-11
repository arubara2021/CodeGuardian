"""Retry engine that honors the per-category retry policy.

The taxonomy in ``taxonomy.py`` classifies every error into a category
and assigns that category a retry policy. This module is the single place
that acts on those policies. Every component that makes a fallible call
and wants to retry it uses ``retry_sync`` or ``retry_async`` rather than
implementing its own loop, so the retry behavior stays consistent across
the codebase.

The engine never guesses. If an exception is not a ``CodeGuardianError``,
it is translated through the translation layer from B-011 and the
resulting typed error's policy is used. Control-flow exceptions such as
``KeyboardInterrupt`` and ``asyncio.CancelledError`` are never caught and
never retried; they signal intent to stop, not a transient failure.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

from .taxonomy import Backoff, CodeGuardianError, ErrorCategory, RetryPolicy
from .translation import translate

__all__ = ["compute_delay", "retry_async", "retry_sync"]

P = ParamSpec("P")
R = TypeVar("R")

# The hook a caller may pass to be told a retry is about to happen.
# It receives the 1-indexed attempt that just failed, the delay that
# will be slept before the next attempt, and the typed error that
# triggered the retry.
OnRetryHook = Callable[[int, float, CodeGuardianError], None]

# Control-flow exceptions are never translated and never retried. They
# signal that the process, task, or generator is being torn down, which
# is a deliberate action rather than a transient failure. Catching and
# retrying any of these would defeat the intent of whoever raised them.
_CONTROL_FLOW_EXCEPTIONS: tuple[type[BaseException], ...] = (
    KeyboardInterrupt,
    SystemExit,
    GeneratorExit,
    asyncio.CancelledError,
)

# Exponential backoff uses a base delay of 0.5 seconds and doubles per
# attempt, with a multiplicative jitter factor between 0.5 and 1.5.
# Attempt 1 sleeps in [0.25, 0.75] seconds; attempt 5 sleeps in
# [4.0, 12.0] seconds; the cap is reached at attempt 7. The jitter
# exists to prevent a thundering herd when many retries fire at once,
# which is exactly the scenario that produces provider rate limits in
# the first place.
_EXPONENTIAL_BASE: float = 0.5
_FIXED_DELAY: float = 0.5
_MAX_DELAY: float = 30.0
_JITTER_LOW: float = 0.5
_JITTER_HIGH: float = 1.5


def _uniform(low: float, high: float, rng: random.Random | None) -> float:
    """Return a uniform sample, using the supplied RNG when one is given.

    The module-level ``random.uniform`` is used in production. Tests
    pass a seeded ``random.Random`` to make the delay deterministic.
    """
    if rng is None:
        return random.uniform(low, high)
    return rng.uniform(low, high)


def compute_delay(
    attempt: int,
    policy: RetryPolicy,
    *,
    rng: random.Random | None = None,
) -> float:
    """Return the delay in seconds before the next retry attempt.

    ``attempt`` is 1-indexed and refers to the attempt that just failed.
    The delay returned applies before the next attempt, which is
    ``attempt + 1``. A policy with ``Backoff.NONE`` returns zero, which
    is the correct behavior for the non-retryable categories where this
    function should never be called anyway; the zero is defensive.

    Args:
        attempt: The 1-indexed attempt number that just failed.
        policy: The retry policy attached to the failure's category.
        rng: An optional seeded RNG for deterministic tests.

    Returns:
        The delay in seconds.

    Raises:
        ValueError: If ``attempt`` is less than 1.
    """
    if attempt < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")

    if policy.backoff is Backoff.NONE:
        return 0.0
    if policy.backoff is Backoff.FIXED:
        return _FIXED_DELAY

    raw = _EXPONENTIAL_BASE * (2 ** (attempt - 1))
    jitter_factor = _uniform(_JITTER_LOW, _JITTER_HIGH, rng)
    return min(_MAX_DELAY, raw * jitter_factor)


def _should_retry(
    typed_exc: CodeGuardianError,
    attempt: int,
    retry_categories: set[ErrorCategory] | None,
) -> bool:
    """Return whether the just-failed attempt should be retried.

    Three conditions must all hold. The category's policy must be
    retryable. The attempt count must be within the policy's budget. And
    if the caller restricted retries to a specific set of categories,
    the failing category must be a member of that set.

    The category filter is expressed as an early ``return True`` when no
    filter is set, followed by the membership test. Ruff's SIM103 rule
    prefers this shape over the equivalent guard-then-True form, and the
    early return also reads more directly: no filter means retry, a
    filter means retry only if the category is in it.
    """
    policy = typed_exc.retry_policy
    if not policy.retryable:
        return False
    if attempt > policy.max_retries:
        return False
    if retry_categories is None:
        return True
    return typed_exc.category in retry_categories


def _reraise(typed_exc: CodeGuardianError, original: BaseException) -> None:
    """Re-raise the typed error, chaining to the original when different.

    When the caught exception was already a ``CodeGuardianError``,
    ``translate`` returned it unchanged and the re-raise preserves its
    traceback. When it was a standard library exception, ``translate``
    produced a new object and the re-raise sets ``__cause__`` so the
    original exception appears in the traceback chain.
    """
    if typed_exc is original:
        raise typed_exc
    raise typed_exc from original


def retry_sync(
    fn: Callable[P, R],
    *args: P.args,
    retry_categories: set[ErrorCategory] | None = None,
    on_retry: OnRetryHook | None = None,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: P.kwargs,
) -> R:
    """Call ``fn`` and retry on retryable failures according to policy.

    The call is attempted once. If it raises a ``CodeGuardianError`` (or
    a standard library exception that translates into one), the
    resulting typed error's retry policy is consulted. When the policy
    is retryable and the attempt budget is not exhausted, the function
    sleeps for the computed delay and tries again. When the budget is
    exhausted or the policy is not retryable, the typed error is raised.

    Args:
        fn: The callable to invoke.
        *args: Positional arguments passed to ``fn``.
        retry_categories: An optional set restricting which categories
            may be retried. ``None`` retries any retryable category.
        on_retry: An optional hook invoked before each retry sleep.
        sleep: The sleep function. Injectable for tests.
        **kwargs: Keyword arguments passed to ``fn``.

    Returns:
        Whatever ``fn`` returns on the first successful attempt.

    Raises:
        CodeGuardianError: The final typed error, if the attempts are
            exhausted or the failure is not retryable.
        KeyboardInterrupt, SystemExit, GeneratorExit: Propagated
            unchanged. These are never retried.
    """
    attempt = 1
    while True:
        try:
            return fn(*args, **kwargs)
        except _CONTROL_FLOW_EXCEPTIONS:
            raise
        except BaseException as exc:
            typed_exc = translate(exc)
            if not _should_retry(typed_exc, attempt, retry_categories):
                _reraise(typed_exc, exc)
            delay = compute_delay(attempt, typed_exc.retry_policy)
            if on_retry is not None:
                on_retry(attempt, delay, typed_exc)
            sleep(delay)
            attempt += 1


async def retry_async(
    fn: Callable[P, Awaitable[R]],
    *args: P.args,
    retry_categories: set[ErrorCategory] | None = None,
    on_retry: OnRetryHook | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    **kwargs: P.kwargs,
) -> R:
    """Async sibling of :func:`retry_sync`.

    The behavior is identical. The only differences are that ``fn`` is
    awaited and the default sleep function is ``asyncio.sleep`` so the
    event loop is not blocked during the backoff.
    """
    attempt = 1
    while True:
        try:
            return await fn(*args, **kwargs)
        except _CONTROL_FLOW_EXCEPTIONS:
            raise
        except BaseException as exc:
            typed_exc = translate(exc)
            if not _should_retry(typed_exc, attempt, retry_categories):
                _reraise(typed_exc, exc)
            delay = compute_delay(attempt, typed_exc.retry_policy)
            if on_retry is not None:
                on_retry(attempt, delay, typed_exc)
            await sleep(delay)
            attempt += 1
