"""WAL-mode SQLite access for the metadata and index layer.

This module is the only place in CodeGuardian that opens a SQLite
connection. Everything that stores structured metadata - run history,
checkpoints, model call records, blast radius metadata, verifier
verdicts, consent records, the skills index, cache entries - goes
through the ``SQLiteStore`` class defined here.

The store is deliberately thin. It opens a connection, applies the
PRAGMAs that the design specifies, loads the sqlite-vec extension so
that the vector table in the initial schema can be created, and exposes
three primitives: ``execute`` for a single parameterized statement,
``query`` for a read that returns rows, and ``execute_script`` for a
multi-statement SQL file. It also exposes the two operations the
migration runner needs: ``is_migration_applied`` and ``record_migration``.

Every SQLite error is translated into a ``CodeGuardianError`` so that
callers never see a raw ``sqlite3.Error``. This is the first place in the
pipeline where the Phase 1 error taxonomy is used for real rather than
exercised by tests.

The store does not own transactions across method calls. SQLite is in
autocommit mode; a caller that needs atomicity wraps its own writes in
``BEGIN IMMEDIATE`` and ``COMMIT``. The autocommit default is the
correct choice for WAL mode because SQLite manages its own locking and
a long-lived Python-side transaction would block concurrent readers.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Any

import sqlite_vec

from codeguardian.core.errors import CodeGuardianError, ErrorCode

__all__ = ["SQLiteStore"]


# PRAGMAs applied on every connection. The reasoning for each:
#
#   journal_mode = WAL       Allows readers alongside one writer, which
#                            matters because the CLI reads while the
#                            pipeline writes.
#   synchronous = NORMAL     Same crash-safety as FULL for committed
#                            transactions in WAL mode, at half the fsync
#                            cost.
#   wal_autocheckpoint       Checkpoint every ~40 MB rather than every
#                            few writes, cutting fsync overhead.
#   mmap_size = 256 MB       Reduces read() syscall overhead for
#                            repeated queries.
#   busy_timeout = 5000      Wait five seconds instead of failing
#                            immediately when another writer holds the
#                            lock.
#   cache_size = 64 MB       Page cache for the connection.
_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA wal_autocheckpoint = 10000",
    "PRAGMA mmap_size = 268435456",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA cache_size = -64000",
)


# The bookkeeping table that records which migration versions have been
# applied. It is created by open() rather than by a migration file so
# that it exists before the first migration is considered. Every
# migration runner has some form of this table; Alembic calls it
# alembic_version, Flyway calls it flyway_schema_history.
_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS _schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""


class SQLiteStore:
    """A WAL-mode SQLite connection with typed error translation.

    The store is a context manager. ``with SQLiteStore(path) as store:``
    opens the connection, applies the PRAGMAs, loads the sqlite-vec
    extension, creates the migration tracking table if it is missing,
    and closes the connection on exit even if the body raises.

    The store is not thread-safe. A single ``SQLiteStore`` instance owns
    one connection, and SQLite connections cannot be shared across
    threads by default. Callers that need concurrent access open one
    store per thread or serialize access through a lock.
    """

    def __init__(self, db_path: Path) -> None:
        """Store the database path. The connection is opened by open().

        Args:
            db_path: Path to the SQLite file. The parent directory is
                created on open() if it does not exist.
        """
        self._db_path = Path(db_path)
        self._connection: sqlite3.Connection | None = None

    @property
    def db_path(self) -> Path:
        """The path this store is configured to open."""
        return self._db_path

    @property
    def connection(self) -> sqlite3.Connection:
        """The live SQLite connection.

        Raises:
            CodeGuardianError: With code SYS_INVARIANT_VIOLATED if the
                store has not been opened or has already been closed.
                This is a defect in the caller, not a runtime failure.
        """
        if self._connection is None:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "SQLiteStore is not open.",
                details={"db_path": str(self._db_path)},
            )
        return self._connection

    def open(self) -> None:
        """Open the database and configure it for WAL mode.

        Creates the parent directory if needed, opens the connection,
        loads the sqlite-vec extension, applies the PRAGMAs, and
        ensures the ``_schema_migrations`` table exists. Safe to call
        only once; calling open() on an already-open store raises
        SYS_INVARIANT_VIOLATED.

        Raises:
            CodeGuardianError: With code FILE_PERMISSION_DENIED if the
                parent directory cannot be created or the database file
                cannot be opened for lack of permission. With code
                SYS_NOT_IMPLEMENTED if Python's sqlite3 was built without
                loadable extension support, or the sqlite-vec extension
                cannot be loaded. With code SYS_INTERNAL for any other
                SQLite failure during setup.
        """
        if self._connection is not None:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "SQLiteStore is already open.",
                details={"db_path": str(self._db_path)},
            )

        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            raise CodeGuardianError(
                ErrorCode.FILE_PERMISSION_DENIED,
                f"Cannot create database directory: {self._db_path.parent}",
                details={"path": str(self._db_path.parent)},
            ) from exc
        except OSError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"Cannot create database directory: {exc}",
                details={"path": str(self._db_path.parent)},
            ) from exc

        try:
            # isolation_level=None disables Python's implicit transaction
            # management. SQLite handles its own transactions in WAL
            # mode, and a Python-side transaction would hold a write
            # lock across method calls.
            self._connection = sqlite3.connect(
                str(self._db_path),
                isolation_level=None,
            )
            self._connection.row_factory = sqlite3.Row
        except PermissionError as exc:
            self._connection = None
            raise CodeGuardianError(
                ErrorCode.FILE_PERMISSION_DENIED,
                f"Cannot open SQLite database: {self._db_path}",
                details={"path": str(self._db_path)},
            ) from exc
        except sqlite3.Error as exc:
            self._connection = None
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"Cannot open SQLite database: {exc}",
                details={"path": str(self._db_path)},
            ) from exc

        try:
            self._load_sqlite_vec()
        except CodeGuardianError:
            self.close()
            raise

        try:
            self._apply_pragmas()
        except CodeGuardianError:
            self.close()
            raise

        try:
            self._connection.execute(_SCHEMA_MIGRATIONS_DDL)
        except sqlite3.Error as exc:
            self.close()
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"Cannot create _schema_migrations table: {exc}",
                details={"db_path": str(self._db_path)},
            ) from exc

    def close(self) -> None:
        """Close the connection. Idempotent."""
        if self._connection is None:
            return
        # Closing a connection rarely fails. If it does, the process is
        # exiting or the file is already gone, and there is no recovery
        # action that a caller could take. Clear the reference so a
        # subsequent operation reports the store as closed rather than
        # reusing a broken handle.
        with contextlib.suppress(sqlite3.Error):
            self._connection.close()
        self._connection = None

    def execute(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> sqlite3.Cursor:
        """Execute a single parameterized SQL statement.

        Args:
            sql: The statement. Use ``?`` placeholders for parameters.
            params: Positional parameters, passed to SQLite as-is.

        Returns:
            The cursor, so callers can read ``lastrowid`` or ``rowcount``
            when they need it.

        Raises:
            CodeGuardianError: With code SYS_INTERNAL for any SQLite
                error. The SQL statement is included in ``details`` so
                the caller can see what failed without re-reading the
                source.
        """
        try:
            return self.connection.execute(sql, params)
        except sqlite3.Error as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"SQLite error: {exc}",
                details={"sql": sql},
            ) from exc

    def query(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> list[sqlite3.Row]:
        """Execute a query and return all rows.

        Args:
            sql: The SELECT statement. Use ``?`` placeholders.
            params: Positional parameters.

        Returns:
            A list of ``sqlite3.Row`` objects. Rows can be indexed
            positionally or by column name.

        Raises:
            CodeGuardianError: With code SYS_INTERNAL for any SQLite
                error.
        """
        return list(self.execute(sql, params).fetchall())

    def execute_script(self, script: str) -> None:
        """Execute a multi-statement SQL script.

        ``executescript`` issues an implicit COMMIT before running the
        script, so any pending transaction is committed first. This is
        the correct behavior for DDL migrations, which are idempotent
        and safe to re-run.

        Args:
            script: The SQL text. May contain multiple statements
                separated by semicolons.

        Raises:
            CodeGuardianError: With code SYS_INTERNAL for any SQLite
                error. The failing statement appears in the SQLite
                error message.
        """
        try:
            self.connection.executescript(script)
        except sqlite3.Error as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"SQLite script error: {exc}",
            ) from exc

    def is_migration_applied(self, version: str) -> bool:
        """Return whether a migration version has been recorded.

        Args:
            version: The version string, typically the file stem of the
                migration file (``001_initial``).

        Returns:
            True if the version is present in ``_schema_migrations``.
        """
        rows = self.query(
            "SELECT 1 FROM _schema_migrations WHERE version = ? LIMIT 1",
            (version,),
        )
        return bool(rows)

    def record_migration(self, version: str, applied_at: str) -> None:
        """Record a successfully applied migration.

        Args:
            version: The version string.
            applied_at: An ISO 8601 timestamp.

        Raises:
            CodeGuardianError: With code SYS_INTERNAL if the insert
                fails. A UNIQUE constraint violation indicates the
                caller checked ``is_migration_applied`` incorrectly.
        """
        self.execute(
            "INSERT INTO _schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, applied_at),
        )

    def __enter__(self) -> SQLiteStore:
        """Open the store and return it for use in a with-block."""
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Close the store, even if the with-block raised."""
        self.close()

    def _load_sqlite_vec(self) -> None:
        """Load the sqlite-vec extension onto the open connection.

        The extension is required because the initial migration creates
        a ``vec0`` virtual table. If Python's sqlite3 was built without
        ``SQLITE_OMIT_LOAD_EXTENSION`` undefined, the ``enable_load_extension``
        method is absent and calling it raises AttributeError. If the
        extension itself is missing or fails to load, SQLite raises
        OperationalError or NotSupportedError.

        Raises:
            CodeGuardianError: With code SYS_NOT_IMPLEMENTED when the
                extension cannot be loaded. The message names the two
                possible causes so the user knows which to investigate.
        """
        connection = self.connection
        try:
            connection.enable_load_extension(True)
            sqlite_vec.load(connection)
        except AttributeError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_NOT_IMPLEMENTED,
                "Python's sqlite3 was built without loadable extension support. "
                "Rebuild Python with SQLITE_OMIT_LOAD_EXTENSION undefined, "
                "or use a Python distribution that includes it.",
                details={"db_path": str(self._db_path)},
            ) from exc
        except (sqlite3.OperationalError, sqlite3.NotSupportedError) as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_NOT_IMPLEMENTED,
                f"The sqlite-vec extension could not be loaded: {exc}",
                details={"db_path": str(self._db_path)},
            ) from exc
        finally:
            # Disable further extension loading. No caller in CodeGuardian
            # should load arbitrary shared libraries onto this connection.
            with contextlib.suppress(AttributeError, sqlite3.Error):
                connection.enable_load_extension(False)

    def _apply_pragmas(self) -> None:
        """Apply the PRAGMAs that configure the connection for WAL mode.

        The journal mode is verified after being set. Some filesystems,
        notably network mounts and certain Windows filesystems, silently
        refuse WAL and fall back to the rollback journal. The design
        calls for a fall back to DELETE mode in that case rather than a
        hard failure, because the tool is still usable; the user simply
        loses the concurrent-read property.

        Raises:
            CodeGuardianError: With code SYS_INTERNAL if a PRAGMA fails
                for a reason other than unsupported WAL. The failing
                PRAGMA appears in the error message.
        """
        for pragma in _PRAGMAS:
            try:
                self.connection.execute(pragma)
            except sqlite3.Error as exc:
                raise CodeGuardianError(
                    ErrorCode.SYS_INTERNAL,
                    f"Failed to apply PRAGMA {pragma!r}: {exc}",
                    details={"db_path": str(self._db_path)},
                ) from exc

        # Verify WAL actually took effect. SQLite accepts the statement
        # unconditionally but returns the mode it settled on. If the
        # filesystem does not support shared memory, the returned mode
        # is the previous one.
        try:
            row = self.connection.execute("PRAGMA journal_mode").fetchone()
        except sqlite3.Error as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"Failed to read journal_mode: {exc}",
                details={"db_path": str(self._db_path)},
            ) from exc

        actual_mode = str(row[0]).lower() if row else "unknown"
        if actual_mode != "wal":
            try:
                self.connection.execute("PRAGMA journal_mode = DELETE")
            except sqlite3.Error as exc:
                raise CodeGuardianError(
                    ErrorCode.SYS_INTERNAL,
                    f"Failed to fall back to DELETE journal mode: {exc}",
                    details={"db_path": str(self._db_path), "attempted": "wal"},
                ) from exc
