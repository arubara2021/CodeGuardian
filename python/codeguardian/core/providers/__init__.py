"""Concrete implementations of the CodeGuardian boundary contracts.

Every provider in this package conforms to a Protocol defined in
``codeguardian.interfaces``. The package does not import a Protocol;
conformance is structural and is checked by static analysis and by the
contract test suite in ``tests/contracts``.

Submodules are discovered at import time so that adding a provider
never requires editing this file. The same names are also declared in
a ``TYPE_CHECKING`` block so type checkers can see them.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .apply_provider import ApplyProvider as ApplyProvider
    from .github_repository import GitHubProvider as GitHubProvider
    from .gitlab_repository import GitLabProvider as GitLabProvider
    from .local_repository import LocalProvider as LocalProvider

__all__: list[str] = []

for _finder, _module_name, _is_package in pkgutil.iter_modules(__path__):
    if _module_name.startswith("_") or _is_package:
        continue
    _provider_module = importlib.import_module(f"{__name__}.{_module_name}")
    for _symbol in getattr(_provider_module, "__all__", ()):
        if _symbol not in __all__:
            __all__.append(_symbol)
        globals()[_symbol] = getattr(_provider_module, _symbol)
