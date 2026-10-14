"""Runs the SQL migrations that build the metadata schema.

Every ``.sql`` file in the ``migrations/`` package directory is a
migration. The runner sorts them lexicographically by filename, checks
each against the ``_schema_migrations`` table, and applies the missing
ones. A migration whose version is already recorded is skipped. Running
the runner twice in a row therefore applies nothing the second time;
this is what makes it safe to call on every startup.

This module is named ``migration_runner.py`` rather than
``migrations.py`` because the SQL files live in a package directory
called ``migrations/``. A module and a package cannot share a name in
the same parent package: Python resolves ``from .migrations import ...``
to the package directory and never sees a sibling ``migrations.py``.
The runner would be invisible to every importer. The rename removes the
collision permanently.

A migration that fails is reported as ``STORAGE_MIGRATION_FAILED`` with
the file name and the underlying SQLite message. Partial application is
possible: a migration file with three statements that fails on the
second leaves the first one applied. This is why every migration file
is written with ``IF NOT EXISTS`` on every DDL statement. A crash
between the failure and the fix can be recovered by editing the file
and re-running, because each statement is safe to re-execute.

The runner does not hold a lock across migrations. Concurrent runs of
the same migration on the same database are prevented by the
``_schema_migrations`` primary key: the second runner to finish a
migration will fail on the ``INSERT`` and report a migration failure
rather than corrupt the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from codeguardian.core.errors import CodeGuardianError, ErrorCode

from .sqlite_store import SQLiteStore

__all__ = ["MigrationResult", "MigrationRunner"]


_DEFAULT_MIGRATIONS_DIR: Path = Path(__file__).parent / "migrations"


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with a Z suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """The outcome of one call to :meth:`MigrationRunner.run`.

    ``applied`` lists the versions that were executed on this call, in
    the order they were run. ``skipped`` lists the versions that were
    already recorded before this call started. Both tuples are sorted
    by version.
    """

    applied: tuple[str, ...]
    skipped: tuple[str, ...]


class MigrationRunner:
    """Applies pending SQL migrations to a SQLiteStore.

    The runner is constructed with a store and a directory. Calling
    ``run`` applies every migration in the directory that has not been
    recorded. The store's connection is not closed by the runner.
    """

    def __init__(
        self,
        sqlite_store: SQLiteStore,
        migrations_dir: Path | None = None,
    ) -> None:
        """Construct the runner.

        Args:
            sqlite_store: An open store. Not closed by this object.
            migrations_dir: The directory holding ``.sql`` files. If not
                provided, defaults to the ``migrations`` package next to
                this module.
        """
        self._store = sqlite_store
        self._migrations_dir = (
            migrations_dir if migrations_dir is not None else _DEFAULT_MIGRATIONS_DIR
        )

    @property
    def migrations_dir(self) -> Path:
        """The directory the runner reads migrations from."""
        return self._migrations_dir

    def run(self) -> MigrationResult:
        """Apply every migration not yet recorded.

        Returns:
            A ``MigrationResult`` listing what was applied and what was
            skipped.

        Raises:
            CodeGuardianError: With code STORAGE_MIGRATION_FAILED if the
                migrations directory does not exist, a migration file
                cannot be read, a migration's SQL fails to execute, or
                the version cannot be recorded. The failing file's path
                and version appear in the error's ``details``.
        """
        if not self._migrations_dir.exists():
            raise CodeGuardianError(
                ErrorCode.STORAGE_MIGRATION_FAILED,
                f"Migrations directory does not exist: {self._migrations_dir}",
                details={"path": str(self._migrations_dir)},
            )

        applied: list[str] = []
        skipped: list[str] = []

        for sql_path in sorted(self._migrations_dir.glob("*.sql")):
            version = sql_path.stem
            if self._store.is_migration_applied(version):
                skipped.append(version)
                continue

            try:
                script = sql_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise CodeGuardianError(
                    ErrorCode.STORAGE_MIGRATION_FAILED,
                    f"Cannot read migration {sql_path.name}: {exc}",
                    details={"path": str(sql_path), "version": version},
                ) from exc

            try:
                self._store.execute_script(script)
            except CodeGuardianError as exc:
                raise CodeGuardianError(
                    ErrorCode.STORAGE_MIGRATION_FAILED,
                    f"Migration {sql_path.name} failed: {exc.message}",
                    details={"path": str(sql_path), "version": version},
                ) from exc

            try:
                self._store.record_migration(version, _now_iso())
            except CodeGuardianError as exc:
                raise CodeGuardianError(
                    ErrorCode.STORAGE_MIGRATION_FAILED,
                    f"Cannot record migration {sql_path.name}: {exc.message}",
                    details={"path": str(sql_path), "version": version},
                ) from exc

            applied.append(version)

        return MigrationResult(applied=tuple(applied), skipped=tuple(skipped))
