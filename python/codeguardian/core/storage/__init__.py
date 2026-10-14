"""Storage facade for CodeGuardian.

This package holds every layer that persists data beyond a single
process: the SQLite metadata index, the filesystem artifact store, the
binary Code Property Graph file, the cache, the skill library, and the
migration runner. Later modules import from this package rather than
from the individual files, so the internal layout stays free to change.

The exports below are flat re-exports. A caller writes
``from codeguardian.core.storage import SQLiteStore`` and does not need
to know that the implementation lives in ``sqlite_store.py``.
"""

from .cache import Cache
from .cleanup import CleanupSummary, run_cleanup
from .filesystem_store import FilesystemStore
from .graph_store import CPGHeader, GraphStore
from .migration_runner import MigrationResult, MigrationRunner
from .skill_store import SkillStore
from .sqlite_store import SQLiteStore

__all__ = [
    "CPGHeader",
    "Cache",
    "CleanupSummary",
    "FilesystemStore",
    "GraphStore",
    "MigrationResult",
    "MigrationRunner",
    "SkillStore",
    "SQLiteStore",
    "run_cleanup",
]
