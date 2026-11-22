"""Pause and cancellation primitives for the orchestration loop.

The orchestrator needs to stop a running phase cleanly when the user
requests a pause or when the run is cancelled from outside. Two pieces
do that work.

``CancellationToken`` is a one-shot signal carrying a reason and a
monotonic generation counter. Unlike ``asyncio.Event``, the token
distinguishes a pause from a prior pause (via the generation counter),
records when it was set and why, and can be checked synchronously at
any await boundary. The token is not an ``asyncio.Event`` because the
generation counter and reason are the information the loop needs at
the point of cancellation; an Event carries only the boolean.

``cancellable`` is an async context manager that checks the token on
entry and exit. A phase that is already cancelled does not start; a
phase that is cancelled during the block raises on exit.

``wait_with_cancellation`` wraps an awaitable so that a cancellation
request interrupts the awaitable rather than the surrounding task. The
awaited coroutine's cleanup runs to completion before the cancellation
propagates. This is the pattern the loop uses to interrupt a model
call or a sandbox execution without leaving the sandbox half-torn-down.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import TypeVar

__all__ = [
    "CancellationRequested",
    "CancellationToken",
    "cancellable",
    "wait_with_cancellation",
]


T = TypeVar("T")


class CancellationRequested(Exception):  # noqa: N818
    """Raised when a ``CancellationToken`` has been set.

    This is a control-flow signal, not an error condition. The loop
    catches it and translates it into a clean pause rather than a
    phase failure. It is deliberately not a ``CodeGuardianError``
    because a pause is not an error.

    Attributes:
        reason: The reason the token was set, or a default string.
        generation: The token's generation counter at the moment the
            exception was raised. A caller that catches this exception
            and re-enters the loop can compare generations to detect
            a stale token from a prior run.
    """

    def __init__(self, reason: str, generation: int) -> None:
        super().__init__(f"cancellation requested: {reason} (generation {generation})")
        self.reason = reason
        self.generation = generation


class CancellationToken:
    """A one-shot pause or cancellation signal with reason and generation.

    The token is created fresh per run. Setting it once is sufficient;
    subsequent calls are no-ops. The generation counter increments on
    the first successful set, so a token from run N can never be
    mistaken for a token from run N+1 even if both were set with the
    same reason.

    The token holds an internal ``asyncio.Event`` so that
    ``wait_with_cancellation`` can await it. The Event is an
    implementation detail. Callers use ``is_set``, ``wait``, and
    ``check``.
    """

    __slots__ = ("_event", "_generation", "_reason", "_set_at")

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._generation = 0
        self._reason: str | None = None
        self._set_at: datetime | None = None

    def set(self, reason: str = "pause requested") -> bool:
        """Set the token.

        Idempotent: the first call wins for the reason and timestamp.
        A subsequent call is a no-op and returns ``False``.

        Args:
            reason: A short human-readable reason. Recorded on the
                token and on the exception that ``check`` raises.

        Returns:
            True when this call set the token; False when the token
            was already set.
        """
        if self._event.is_set():
            return False
        self._reason = reason
        self._set_at = datetime.now(UTC)
        self._generation += 1
        self._event.set()
        return True

    def is_set(self) -> bool:
        """Return True when the token has been set."""
        return self._event.is_set()

    async def wait(self) -> None:
        """Await until the token is set.

        Returns immediately when the token is already set.
        """
        await self._event.wait()

    def check(self) -> None:
        """Raise ``CancellationRequested`` when the token is set.

        Returns ``None`` when the token is not set. The method is safe
        to call synchronously at any await boundary.
        """
        if self._event.is_set():
            raise CancellationRequested(
                self._reason or "pause requested",
                self._generation,
            )

    @property
    def generation(self) -> int:
        """The number of times the token has been set."""
        return self._generation

    @property
    def reason(self) -> str | None:
        """The reason the token was set, or None when not set."""
        return self._reason

    @property
    def set_at(self) -> datetime | None:
        """The UTC timestamp of the first set, or None when not set."""
        return self._set_at


@asynccontextmanager
async def cancellable(token: CancellationToken) -> AsyncIterator[None]:
    """Run a block that raises ``CancellationRequested`` when cancelled.

    The token is checked on entry and on exit. A phase that is
    already cancelled does not start. A phase that is cancelled
    during the block raises on exit. The block's own cleanup runs
    because ``CancellationRequested`` is raised from the ``finally``
    clause, after the body's exception handlers have had their chance.

    Usage::

        async with cancellable(token):
            await do_work()

    Args:
        token: The token to check.

    Raises:
        CancellationRequested: On entry when the token is set, or on
            exit when the token was set during the block.
    """
    token.check()
    try:
        yield
    finally:
        token.check()


async def wait_with_cancellation(
    awaitable: Awaitable[T],
    token: CancellationToken,
) -> T:
    """Await an awaitable, interrupting it when the token is set.

    The awaitable runs in its own task. When the token is set before
    the task completes, the task is cancelled and its cleanup runs to
    completion before this function raises. When the task completes
    first, its result is returned and the token is ignored. A task
    that completes despite the token being set is honoured, because
    the loop checks the token again before the next phase.

    The pattern differs from ``asyncio.wait_for``: the token is the
    caller's signal, not a timeout, and the cancellation is
    cooperative. The awaited coroutine's cleanup runs to completion;
    ``asyncio.shield`` is not used because the cleanup is expected to
    be short and the caller's token is the only signal in flight.

    Args:
        awaitable: The coroutine or awaitable to run.
        token: The cancellation token to watch.

    Returns:
        The result of ``awaitable`` when it completes before the
        token is set.

    Raises:
        CancellationRequested: When the token is set before the
            awaitable completes. The awaitable's cleanup has already
            run.
        BaseException: When the awaitable raises. The exception is
            propagated unchanged.
    """
    task: asyncio.Task[T] = asyncio.ensure_future(awaitable)
    cancel_waiter: asyncio.Task[None] = asyncio.ensure_future(token.wait())

    try:
        done, _ = await asyncio.wait(
            {task, cancel_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        raise
    finally:
        if not cancel_waiter.done():
            cancel_waiter.cancel()
            with suppress(asyncio.CancelledError):
                await cancel_waiter

    if task in done:
        return task.result()

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
    token.check()
    raise AssertionError("unreachable: token.check() must have raised because the token is set")
