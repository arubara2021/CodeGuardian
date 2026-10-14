"""SQL migration files for the SQLite metadata store.

Each ``.sql`` file in this directory is a migration. Files are applied
in lexicographic order by the migration runner in the parent package,
which is why files are named with a zero-padded numeric prefix:
``001_initial.sql``, ``002_add_x.sql``, and so on.

Migrations are append-only. Once a file has shipped and been applied by
any user, it is never edited. A change to the schema is a new file, not
an edit to an existing one. Editing an applied migration would make the
database state depend on when it was last opened, which is a class of
bug that is very hard to diagnose after the fact.

Every migration is written to be idempotent. Each statement uses
``IF NOT EXISTS`` so that a partially applied migration can be re-run
safely after a crash. The migration runner records successful application
in the ``_schema_migrations`` table, but the idempotent form means a
crash in the middle of a migration does not require manual repair.
"""
