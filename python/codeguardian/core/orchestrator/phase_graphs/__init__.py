"""Bundled phase graphs.

Each module in this package defines one PhaseGraph constant. Adding a
new pipeline mode is adding a module here; the orchestrator reads the
constant, and no orchestrator code changes.

Modules:

- ``legacy``: the ten-slot legacy pipeline that ships in v1 plus one
  optional slot reserved for a future full-test-suite agent. This is
  the default graph when no other graph is configured.
"""

from .legacy import LEGACY_GRAPH

__all__ = ["LEGACY_GRAPH"]
