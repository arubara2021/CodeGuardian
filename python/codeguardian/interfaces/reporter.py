"""Reporter plugin contract.

A reporter turns a completed run into a formatted string. Reporters
are synchronous because they read and format already-produced
artifacts.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .orchestrator import RunResult

__all__ = ["ReporterPlugin"]


@runtime_checkable
class ReporterPlugin(Protocol):
    """A report format contributed by a plugin."""

    name: str
    format: str

    def report(self, run_result: RunResult) -> str:
        """Render the run as a string in the reporter's format."""
        ...
