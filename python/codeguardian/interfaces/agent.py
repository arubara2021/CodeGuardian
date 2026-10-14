"""Agent contract for pipeline phases.

Each pipeline phase is executed by one agent. Agents call models and
therefore expose an asynchronous ``run`` method. The protocol is
generic over the agent's input and output so that each concrete agent
keeps its own types.
"""

from __future__ import annotations

from typing import Protocol, TypeVar

__all__ = ["Agent"]

_InputT = TypeVar("_InputT", contravariant=True)
_OutputT = TypeVar("_OutputT", covariant=True)


class Agent(Protocol[_InputT, _OutputT]):
    """A single pipeline phase executor.

    The orchestrator invokes each agent once per phase. Agents hold no
    state across runs; everything passes through the input and output
    values.
    """

    name: str
    phase: str

    async def run(self, input: _InputT) -> _OutputT:
        """Execute the agent's phase and return its output."""
        ...
