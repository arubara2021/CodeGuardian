"""CodeGuardian: a safety-first refactoring agent.

The package exposes its version at two import paths. ``codeguardian.version``
is the canonical module; ``codeguardian.__version__`` is a convenience alias
so callers do not need to know the submodule name. Both resolve to the same
string.
"""

from . import version
from .version import __version__

__all__ = ["__version__", "version"]
