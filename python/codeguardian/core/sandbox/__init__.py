"""Concrete implementations of the ``SandboxBackend`` contract.

The backends in this package are JSON-RPC clients to
``codeguardian-rpcd``. They do not spawn processes themselves; every
spawn happens inside the Rust daemon. If Python were compromised, it
could not remove the sandbox enforcement — it can only ask the daemon
politely.

Submodules are discovered at import time so that adding a backend
never requires editing this file. The same names are also declared in
a ``TYPE_CHECKING`` block so type checkers can see them.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backend_selector import select_backend as select_backend
    from .bubblewrap_backend import BubblewrapBackend as BubblewrapBackend
    from .docker_backend import DockerBackend as DockerBackend
    from .seatbelt_backend import SeatbeltBackend as SeatbeltBackend

__all__: list[str] = []

for _finder, _module_name, _is_package in pkgutil.iter_modules(__path__):
    if _module_name.startswith("_") or _is_package:
        continue
    _sandbox_module = importlib.import_module(f"{__name__}.{_module_name}")
    for _symbol in getattr(_sandbox_module, "__all__", ()):
        if _symbol not in __all__:
            __all__.append(_symbol)
        globals()[_symbol] = getattr(_sandbox_module, _symbol)
