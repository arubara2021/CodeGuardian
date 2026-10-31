"""Active store context for agent telemetry.

Agents write one telemetry row per invocation. The row needs an open
SQLiteStore. Phase 13 will set one at the start of a run; isolated
Phase 11 tests and CLI-less tooling have no store, and agents must
remain callable without one.

The context var decouples the two. When a store is present, agents
write telemetry. When absent, they skip. Nothing else changes.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from codeguardian.core.storage.sqlite_store import SQLiteStore

__all__ = [
    "active_store",
    "get_active_store",
    "store_available",
]


_ACTIVE_STORE: ContextVar[SQLiteStore | None] = ContextVar(
    "codeguardian_active_store", default=None
)


def get_active_store() -> SQLiteStore | None:
    """Return the active store for the current context, or None."""
    return _ACTIVE_STORE.get()


@contextmanager
def active_store(store: SQLiteStore) -> Iterator[None]:
    """Set ``store`` as the active store for the duration of the block."""
    token = _ACTIVE_STORE.set(store)
    try:
        yield
    finally:
        _ACTIVE_STORE.reset(token)


def store_available() -> bool:
    """Return True when an active store is set in this context."""
    return _ACTIVE_STORE.get() is not None
