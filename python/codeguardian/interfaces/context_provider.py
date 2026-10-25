"""Context provider plugin contract.

A context provider returns a text snippet the agent inserts into its
prompt. Unlike a tool, which returns structured data the agent may
inspect or transform, a context provider returns text the agent reads
directly.

# Why this is a separate plugin type

Tools return data. Context providers return context. A tool's output
may be filtered, cached, or transformed before it reaches the model.
A context provider's output goes straight into the prompt. Keeping the
two types separate makes it clear which plugins navigate and which
control.

# Read-only by construction

Every context provider reads from the Code Property Graph or the local
clone. None writes, executes, or reaches the network. The
``is_read_only`` field is set to ``True`` on every implementation and
is validated by the test suite. A context provider that claims
otherwise is a contract violation.

# Output

The ``provide`` method returns a string. The caller concatenates it
into the prompt. The string may be a code skeleton, a function body, a
call graph slice, or a symbol list. The shape is the provider's choice;
the caller treats it as opaque text.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["ContextProvider"]


@runtime_checkable
class ContextProvider(Protocol):
    """A source of text context for an agent prompt."""

    name: str
    description: str
    input_schema: dict[str, object]
    output_schema: dict[str, object]
    is_read_only: bool

    async def provide(self, input: dict[str, object]) -> str:
        """Return text the agent inserts into its prompt."""
        ...
