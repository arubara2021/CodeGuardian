"""Per-phase runtime context for context provider plugins.

The tool contract is ``call(input: dict) -> dict``. The input is
JSON-shaped. It cannot carry a Python object, and it should not: a
tool's input is what the model requested, not what the runtime
happens to have loaded.

A context provider needs two runtime facts that are not part of the
tool input:

- The deserialized Code Property Graph handle for the current run.
- The clone root, so that ``function_source`` can resolve file paths
  against a known-safe root and reject anything outside it.

Both facts travel through contextvars, the same way ``permissions.py``
carries the active plugin and sandbox root. Setting them once per phase
means every context provider call sees them without any contract
change.

# Why contextvars, not a parameter

Passing the CPG handle and clone root through the tool input would
require every caller to know the tool wants them. That couples the
caller to the tool. Contextvars decouple them: the runtime sets the
context, the tool reads it, the caller does not know either side
exists.

# Failure mode

A context provider called outside ``cpg_available`` cannot do its job.
It has no graph to read from and no root to resolve paths against.
Both accessors raise ``SYS_INVARIANT_VIOLATED`` in that case. The
error is a bug in the orchestrator, not a runtime condition; it should
never fire in a correct pipeline.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from codeguardian.core.errors import CodeGuardianError, ErrorCode

__all__ = [
    "cpg_available",
    "current_clone_root",
    "current_cpg",
]


_CURRENT_CPG: ContextVar[Any | None] = ContextVar("_cg_current_cpg", default=None)
_CURRENT_CLONE_ROOT: ContextVar[Path | None] = ContextVar("_cg_current_clone_root", default=None)


def current_cpg() -> Any:
    """Return the active CPG handle.

    The handle is the ``_native.PyCPG`` instance set by
    ``cpg_available``. It is typed as ``object`` here because the
    kernel's PyO3 module is optional; importing it eagerly would
    break the core when the native extension is not built.

    Returns:
        The active handle.

    Raises:
        CodeGuardianError: ``SYS_INVARIANT_VIOLATED`` if no
            ``cpg_available`` block is active.
    """
    handle = _CURRENT_CPG.get()
    if handle is None:
        raise CodeGuardianError(
            ErrorCode.SYS_INVARIANT_VIOLATED,
            "no CPG handle is active; wrap the phase in cpg_available(...)",
        )
    return handle


def current_clone_root() -> Path:
    """Return the active clone root.

    Returns:
        The absolute path to the clone root set by ``cpg_available``.

    Raises:
        CodeGuardianError: ``SYS_INVARIANT_VIOLATED`` if no
            ``cpg_available`` block is active.
    """
    root = _CURRENT_CLONE_ROOT.get()
    if root is None:
        raise CodeGuardianError(
            ErrorCode.SYS_INVARIANT_VIOLATED,
            "no clone root is active; wrap the phase in cpg_available(...)",
        )
    return root


@contextmanager
def cpg_available(cpg: object, clone_root: Path) -> Iterator[None]:
    """Set the active CPG handle and clone root for the block.

    Args:
        cpg: The ``_native.PyCPG`` instance for the current run.
        clone_root: Absolute path to the clone root. All paths a
            context provider resolves are checked against this
            directory; anything outside it is rejected.

    Yields:
        ``None``. The context is available to any context provider
        call made inside the block.
    """
    cpg_token = _CURRENT_CPG.set(cpg)
    root_token = _CURRENT_CLONE_ROOT.set(Path(clone_root))
    try:
        yield
    finally:
        _CURRENT_CLONE_ROOT.reset(root_token)
        _CURRENT_CPG.reset(cpg_token)
