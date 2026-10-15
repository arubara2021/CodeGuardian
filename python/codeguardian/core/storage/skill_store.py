"""Skill library: Markdown source of truth plus searchable indexes.

A skill is a directory under ``<root>/skills/`` containing a single
``SKILL.md`` file. The file has a YAML frontmatter block delimited by
``---`` lines, followed by a Markdown body. The frontmatter is validated
against ``schemas/skill.json`` on every write and every read; a skill
whose frontmatter has drifted from the schema is rejected loudly rather
than indexed silently.

Three pieces of state track a skill:

1. The ``SKILL.md`` file on disk. This is the source of truth. If the
   SQLite database is deleted, the library is rebuilt from these files.
2. A row in the ``skills`` table, holding the frontmatter fields that
   are useful for querying.
3. A row in the ``skills_fts`` virtual table, so that keyword search
   over the description and trigger text is fast. The FTS5 table uses
   external content: it does not store its own copy of the text, and it
   must be kept in sync with the ``skills`` table explicitly.

A fourth piece of state is optional: a row in ``skill_embeddings``
holding a vector for semantic search. Embeddings are provided by the
caller. The store never generates them; that is the job of the
embeddings module in a later phase. A skill without an embedding is
still findable by keyword search.

Embedding dimension is fixed at 384, matching the schema for
``skill_embeddings`` and the all-MiniLM-L6-v2 sentence transformer that
the design names as the reference model.
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from codeguardian.core.errors import CodeGuardianError, ErrorCode

from .sqlite_store import SQLiteStore

__all__ = ["SkillStore"]


_EMBEDDING_DIM: int = 384
_DEFAULT_SEARCH_LIMIT: int = 10
_DEFAULT_EFFECTIVENESS: float = 0.5
_FRONTMATTER_DELIMITER: str = "---"


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with a Z suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class _StringSafeLoader(yaml.SafeLoader):
    """SafeLoader variant that leaves timestamp-shaped values as strings.

    PyYAML's default SafeLoader converts an unquoted ``2026-10-13`` into
    a ``datetime.date`` object. That would fail the ``date-time`` format
    check in the schema even though the underlying text is valid. The
    custom constructor below overrides the timestamp tag so that every
    timestamp-shaped value stays a string. This matches the schema's
    expectations and avoids a class of confusing validation failures
    caused by how a user happened to quote a date in a hand-edited file.
    """


def _str_constructor(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    """Return the raw scalar text for a timestamp-tagged YAML node."""
    return loader.construct_scalar(node)


_StringSafeLoader.add_constructor("tag:yaml.org,2002:timestamp", _str_constructor)


def _parse_skill_markdown(text: str, path: Path) -> tuple[dict[str, Any], str]:
    """Split a ``SKILL.md`` file into its frontmatter dict and body.

    The expected layout is::

        ---
        name: skill-name
        description: ...
        ---

        Body text.

    Both delimiters must be on their own lines. The body is everything
    after the closing delimiter, with leading blank lines stripped.

    Args:
        text: The full file content.
        path: The file path, used only for error messages.

    Returns:
        A tuple of the parsed frontmatter dictionary and the body string.

    Raises:
        CodeGuardianError: With code STORAGE_SCHEMA_INVALID if the
            frontmatter delimiters are missing, the YAML block is
            malformed, or the YAML block does not parse to a mapping.
    """
    opening = _FRONTMATTER_DELIMITER + "\n"
    closing = "\n" + _FRONTMATTER_DELIMITER + "\n"

    if not text.startswith(opening):
        raise CodeGuardianError(
            ErrorCode.STORAGE_SCHEMA_INVALID,
            f"Skill file does not start with a frontmatter delimiter: {path}",
            details={"path": str(path)},
        )

    end = text.find(closing, len(opening))
    if end == -1:
        raise CodeGuardianError(
            ErrorCode.STORAGE_SCHEMA_INVALID,
            f"Skill file has no closing frontmatter delimiter: {path}",
            details={"path": str(path)},
        )

    yaml_text = text[len(opening) : end]
    body = text[end + len(closing) :].lstrip("\n")

    try:
        frontmatter = yaml.load(yaml_text, Loader=_StringSafeLoader)
    except yaml.YAMLError as exc:
        raise CodeGuardianError(
            ErrorCode.STORAGE_SCHEMA_INVALID,
            f"Skill frontmatter is not valid YAML: {exc}",
            details={"path": str(path)},
        ) from exc

    if not isinstance(frontmatter, dict):
        raise CodeGuardianError(
            ErrorCode.STORAGE_SCHEMA_INVALID,
            f"Skill frontmatter is not a mapping: {path}",
            details={"path": str(path)},
        )

    return frontmatter, body


class SkillStore:
    """Manages the Markdown skill library and its SQLite indexes.

    One instance per storage root. The store is bound to a single
    ``SQLiteStore`` (the metadata index) and a single schemas directory
    (for the frontmatter validation schema). It holds no file handles
    between method calls.

    Skills are global. They are not scoped to a repository. A pattern
    learned in one project is available to every future project. The
    directory layout is fixed: ``<root>/skills/<name>/SKILL.md``.
    """

    def __init__(
        self,
        sqlite_store: SQLiteStore,
        root: Path,
        schemas_dir: Path,
    ) -> None:
        """Construct the store. Loads and validates the schema eagerly.

        The frontmatter schema is loaded at construction time so that a
        missing or malformed ``skill.json`` fails fast with a clear
        error, rather than surfacing the first time a skill is added.

        Args:
            sqlite_store: An open store. Not closed by this object.
            root: The root of the on-disk storage tree, typically
                ``~/.codeguardian``. Skills live under ``<root>/skills/``.
            schemas_dir: The directory holding the JSON Schema files.

        Raises:
            CodeGuardianError: With code STORAGE_SCHEMA_INVALID if the
                ``skill.json`` schema is missing, malformed, or is not a
                valid JSON Schema.
        """
        self._sqlite = sqlite_store
        self._root = Path(root)
        self._schemas_dir = Path(schemas_dir)
        self._validator = self._build_validator()

    @property
    def root(self) -> Path:
        """The root of the on-disk storage tree."""
        return self._root

    def add(
        self,
        name: str,
        frontmatter: dict[str, Any],
        body: str,
        embedding: list[float] | None = None,
        *,
        overwrite: bool = False,
    ) -> Path:
        """Add or replace a skill in the library.

        The frontmatter is validated against ``skill.json`` before any
        file is written or any row is inserted. Optional fields are
        filled in with defaults when absent so that the resulting
        ``SKILL.md`` is complete and self-describing.

        Args:
            name: The skill name. Must match ``^[a-z0-9-]+$``.
            frontmatter: The frontmatter dict. Must contain at least
                ``description``, ``trigger``, ``skill_type``, and
                ``language``. ``state`` defaults to ``"active"``.
            body: The Markdown body of the skill.
            embedding: An optional 384-dimensional embedding vector. If
                provided, it is stored in ``skill_embeddings`` for
                semantic search.
            overwrite: If True, replace an existing skill. If False (the
                default), raise if a skill with the same name exists.

        Returns:
            The path to the written ``SKILL.md``.

        Raises:
            CodeGuardianError: With code STORAGE_SCHEMA_INVALID if the
                frontmatter fails validation, the ``name`` argument
                disagrees with the ``name`` field in the frontmatter, an
                embedding of the wrong dimension is supplied, or the
                skill already exists and ``overwrite`` is False. With
                code FILE_PERMISSION_DENIED or SYS_INTERNAL if the file
                cannot be written.
        """
        frontmatter = dict(frontmatter)
        frontmatter.setdefault("name", name)
        if frontmatter["name"] != name:
            raise CodeGuardianError(
                ErrorCode.STORAGE_SCHEMA_INVALID,
                f"Frontmatter name {frontmatter['name']!r} does not match "
                f"the skill name argument {name!r}.",
                details={"name": name, "frontmatter_name": frontmatter["name"]},
            )

        frontmatter.setdefault("state", "active")
        frontmatter.setdefault("effectiveness", _DEFAULT_EFFECTIVENESS)
        frontmatter.setdefault("total_uses", 0)
        frontmatter.setdefault("total_successes", 0)
        frontmatter.setdefault("created_at", _now_iso())

        self._validate_frontmatter(frontmatter)

        skill_path = self._skill_path(name)
        if skill_path.exists() and not overwrite:
            raise CodeGuardianError(
                ErrorCode.STORAGE_SCHEMA_INVALID,
                f"Skill already exists: {name}. Pass overwrite=True to replace it.",
                details={"name": name, "path": str(skill_path)},
            )

        if embedding is not None and len(embedding) != _EMBEDDING_DIM:
            raise CodeGuardianError(
                ErrorCode.STORAGE_SCHEMA_INVALID,
                f"Embedding must have {_EMBEDDING_DIM} dimensions, got {len(embedding)}.",
                details={"name": name, "actual_dim": len(embedding)},
            )

        yaml_text = yaml.safe_dump(
            frontmatter,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )
        content = f"{_FRONTMATTER_DELIMITER}\n{yaml_text}{_FRONTMATTER_DELIMITER}\n\n{body}\n"

        try:
            skill_path.parent.mkdir(parents=True, exist_ok=True)
            skill_path.write_text(content, encoding="utf-8")
        except PermissionError as exc:
            raise CodeGuardianError(
                ErrorCode.FILE_PERMISSION_DENIED,
                f"Cannot write skill file: {skill_path}",
                details={"path": str(skill_path)},
            ) from exc
        except OSError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"Cannot write skill file: {exc}",
                details={"path": str(skill_path)},
            ) from exc

        self._upsert_skill_row(name, skill_path, frontmatter)

        if embedding is not None:
            self._sqlite.execute(
                "INSERT OR REPLACE INTO skill_embeddings(name, embedding) VALUES (?, ?)",
                (name, json.dumps(embedding)),
            )

        return skill_path

    def get(self, name: str) -> dict[str, Any] | None:
        """Read a skill from disk and return its parsed content.

        Reads the ``SKILL.md`` file directly rather than the database
        row, because the Markdown file is the source of truth. The
        frontmatter is re-validated on read so that a manually edited
        file with drifted frontmatter is caught rather than silently
        returned.

        Args:
            name: The skill name.

        Returns:
            A dictionary with keys ``frontmatter`` and ``body``, or
            ``None`` if the skill file does not exist.

        Raises:
            CodeGuardianError: With code STORAGE_SCHEMA_INVALID if the
                file exists but its frontmatter is malformed or fails
                validation.
        """
        skill_path = self._skill_path(name)
        if not skill_path.exists():
            return None

        try:
            text = skill_path.read_text(encoding="utf-8")
        except PermissionError as exc:
            raise CodeGuardianError(
                ErrorCode.FILE_PERMISSION_DENIED,
                f"Cannot read skill file: {skill_path}",
                details={"path": str(skill_path)},
            ) from exc

        frontmatter, body = _parse_skill_markdown(text, skill_path)
        self._validate_frontmatter(frontmatter)
        return {"frontmatter": frontmatter, "body": body}

    def remove(self, name: str) -> bool:
        """Remove a skill from disk and from every index.

        Idempotent: removing a skill that does not exist is not an
        error and returns False. The entire skill directory is deleted,
        not just the ``SKILL.md`` file, so any auxiliary files a future
        version adds are cleaned up as well.

        Args:
            name: The skill name.

        Returns:
            True if the skill existed and was removed, False if it did
            not.

        Raises:
            CodeGuardianError: With code FILE_PERMISSION_DENIED or
                SYS_INTERNAL if the directory exists but cannot be
                removed.
        """
        existing = self._sqlite.query(
            "SELECT rowid, name, description, trigger_text FROM skills WHERE name = ?",
            (name,),
        )
        if existing:
            old = existing[0]
            self._sqlite.execute(
                "INSERT INTO skills_fts(skills_fts, rowid, name, description, trigger_text) "
                "VALUES ('delete', ?, ?, ?, ?)",
                (
                    old["rowid"],
                    old["name"],
                    old["description"] or "",
                    old["trigger_text"] or "",
                ),
            )
        self._sqlite.execute("DELETE FROM skills WHERE name = ?", (name,))
        self._sqlite.execute("DELETE FROM skill_embeddings WHERE name = ?", (name,))

        skill_dir = self._skill_path(name).parent
        removed_file = skill_dir.exists()
        if removed_file:
            try:
                shutil.rmtree(skill_dir)
            except PermissionError as exc:
                raise CodeGuardianError(
                    ErrorCode.FILE_PERMISSION_DENIED,
                    f"Cannot delete skill directory: {skill_dir}",
                    details={"path": str(skill_dir)},
                ) from exc
            except OSError as exc:
                raise CodeGuardianError(
                    ErrorCode.SYS_INTERNAL,
                    f"Cannot delete skill directory: {exc}",
                    details={"path": str(skill_dir)},
                ) from exc

        return removed_file or bool(existing)

    def list_all(self) -> list[dict[str, Any]]:
        """Return every skill in the database index.

        This reads from the ``skills`` table, not the filesystem. If the
        filesystem and the database have drifted, run ``rebuild_index``
        first to bring them back into sync.

        Returns:
            A list of dictionaries, one per skill, sorted by name.
        """
        rows = self._sqlite.query(
            """
            SELECT name, path, description, trigger_text, skill_type,
                   language, effectiveness, total_uses, total_successes,
                   state, created_at, last_used_at
            FROM skills
            ORDER BY name
            """
        )
        return [dict(row) for row in rows]

    def count(self) -> int:
        """Return the number of skills in the database index."""
        rows = self._sqlite.query("SELECT COUNT(*) AS c FROM skills")
        return int(rows[0]["c"])

    def search_keyword(
        self,
        query: str,
        limit: int = _DEFAULT_SEARCH_LIMIT,
    ) -> list[dict[str, Any]]:
        """Full-text search over name, description, and trigger text.

        Each whitespace-separated word in the query is quoted before
        being handed to FTS5. This prevents a user-supplied quote or
        asterisk from producing a syntax error. The practical effect is
        that the query is treated as an implicit AND of exact words,
        which is the behavior a user expects from a search box.

        Args:
            query: The search text.
            limit: The maximum number of results.

        Returns:
            A list of dictionaries, one per matching skill, ordered by
            relevance (most relevant first). Empty if no matches, if
            the query is empty, or if the FTS index is empty.

        Raises:
            CodeGuardianError: With code SYS_INTERNAL if the underlying
                FTS query fails.
        """
        words = [w for w in query.split() if w]
        if not words:
            return []

        safe_query = " ".join('"' + w.replace('"', '""') + '"' for w in words)

        try:
            rows = self._sqlite.query(
                """
                SELECT s.name, s.description, s.trigger_text, s.skill_type,
                       s.language, s.effectiveness, s.state,
                       bm25(skills_fts) AS rank
                FROM skills_fts
                JOIN skills s ON s.rowid = skills_fts.rowid
                WHERE skills_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (safe_query, limit),
            )
        except CodeGuardianError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"FTS query failed: {exc.message}",
                details={"query": query},
            ) from exc

        return [dict(row) for row in rows]

    def search_semantic(
        self,
        embedding: list[float],
        limit: int = _DEFAULT_SEARCH_LIMIT,
    ) -> list[dict[str, Any]]:
        """Nearest-neighbor search over the skill embeddings.

        Skills without a stored embedding are not returned. If the
        vector index is empty, the result is an empty list.

        Args:
            embedding: A 384-dimensional query vector.
            limit: The maximum number of results.

        Returns:
            A list of dictionaries with keys ``name`` and ``distance``,
            ordered by increasing distance (nearest first). Empty if no
            embeddings are stored.

        Raises:
            CodeGuardianError: With code STORAGE_SCHEMA_INVALID if the
                vector has the wrong dimension, or SYS_INTERNAL if the
                query fails.
        """
        if len(embedding) != _EMBEDDING_DIM:
            raise CodeGuardianError(
                ErrorCode.STORAGE_SCHEMA_INVALID,
                f"Query embedding must have {_EMBEDDING_DIM} dimensions, got {len(embedding)}.",
                details={"actual_dim": len(embedding)},
            )

        vec_json = json.dumps(embedding)
        try:
            rows = self._sqlite.query(
                """
                SELECT name, distance
                FROM skill_embeddings
                WHERE embedding MATCH ?
                ORDER BY distance
                LIMIT ?
                """,
                (vec_json, limit),
            )
        except CodeGuardianError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"Vector query failed: {exc.message}",
            ) from exc

        return [dict(row) for row in rows]

    def record_feedback(
        self,
        name: str,
        run_id: str | None,
        outcome: str,
        task_description: str | None = None,
    ) -> None:
        """Record the outcome of using a skill and update its totals.

        The skill's ``total_uses`` counter is incremented on every
        feedback event. ``total_successes`` is incremented only when the
        outcome is ``"success"``. The ``effectiveness`` score is
        recomputed as ``total_successes / total_uses``; if no uses have
        been recorded, it stays at its current value (initially 0.5).

        Args:
            name: The skill name.
            run_id: The run that used the skill, or ``None`` if the
                feedback is not tied to a run.
            outcome: One of ``"success"``, ``"partial"``, ``"failure"``.
            task_description: Optional free text describing the task.

        Raises:
            CodeGuardianError: With code STORAGE_SCHEMA_INVALID if the
                outcome is not one of the allowed values, or if the
                skill does not exist in the index.
        """
        if outcome not in ("success", "partial", "failure"):
            raise CodeGuardianError(
                ErrorCode.STORAGE_SCHEMA_INVALID,
                f"outcome must be one of success, partial, failure; got {outcome!r}.",
                details={"outcome": outcome},
            )

        rows = self._sqlite.query(
            "SELECT 1 FROM skills WHERE name = ? LIMIT 1",
            (name,),
        )
        if not rows:
            raise CodeGuardianError(
                ErrorCode.STORAGE_SCHEMA_INVALID,
                f"Cannot record feedback for unknown skill: {name}",
                details={"name": name},
            )

        now = _now_iso()
        self._sqlite.execute(
            """
            INSERT INTO skill_feedback
                (feedback_id, skill_name, run_id, outcome,
                 task_description, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), name, run_id, outcome, task_description, now),
        )

        if outcome == "success":
            self._sqlite.execute(
                """
                UPDATE skills
                SET total_uses = total_uses + 1,
                    total_successes = total_successes + 1,
                    last_used_at = ?
                WHERE name = ?
                """,
                (now, name),
            )
        else:
            self._sqlite.execute(
                """
                UPDATE skills
                SET total_uses = total_uses + 1,
                    last_used_at = ?
                WHERE name = ?
                """,
                (now, name),
            )

        self._recompute_effectiveness(name)

    def rebuild_index(self) -> int:
        """Rebuild the FTS5 and ``skills`` indexes from Markdown files.

        Walks ``<root>/skills/``, parses every ``SKILL.md`` file, and
        upserts a row for each skill that is not already present. Then
        issues an FTS5 rebuild command so that the full-text index
        matches the ``skills`` table exactly.

        Embeddings are not regenerated, because the store does not have
        the embedding model. After a rebuild, semantic search returns
        only the skills whose embeddings survived or were re-added.

        Returns:
            The number of skills inserted from disk. Skills already
            present in the database are not counted.

        Raises:
            CodeGuardianError: With code SYS_INTERNAL if the rebuild
                fails.
        """
        skills_dir = self._root / "skills"
        inserted = 0

        if skills_dir.exists():
            for skill_dir in sorted(skills_dir.iterdir()):
                if not skill_dir.is_dir():
                    continue
                skill_file = skill_dir / "SKILL.md"
                if not skill_file.exists():
                    continue

                try:
                    text = skill_file.read_text(encoding="utf-8")
                    frontmatter, _ = _parse_skill_markdown(text, skill_file)
                    self._validate_frontmatter(frontmatter)
                except CodeGuardianError:
                    # A malformed file is skipped, not fatal. The rebuild
                    # is best-effort; the next add() will surface the
                    # problem to a user with the file path.
                    continue

                name = str(frontmatter.get("name", skill_dir.name))
                existing = self._sqlite.query(
                    "SELECT 1 FROM skills WHERE name = ? LIMIT 1",
                    (name,),
                )
                if existing:
                    continue

                self._upsert_skill_row(name, skill_file, frontmatter, quiet=True)
                inserted += 1

        self._sqlite.execute("INSERT INTO skills_fts(skills_fts) VALUES('rebuild')")
        return inserted

    # --- Internal helpers ------------------------------------------------

    def _skill_path(self, name: str) -> Path:
        """Return the path to the ``SKILL.md`` file for a skill."""
        return self._root / "skills" / name / "SKILL.md"

    def _build_validator(self) -> Draft202012Validator:
        """Load the skill schema and return a validator for it."""
        schema_path = self._schemas_dir / "skill.json"
        try:
            text = schema_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise CodeGuardianError(
                ErrorCode.STORAGE_SCHEMA_INVALID,
                f"Schema file not found: {schema_path}",
                details={"schema": "skill.json", "path": str(schema_path)},
            ) from exc
        except PermissionError as exc:
            raise CodeGuardianError(
                ErrorCode.FILE_PERMISSION_DENIED,
                f"Cannot read schema file: {schema_path}",
                details={"schema": "skill.json"},
            ) from exc

        try:
            schema = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CodeGuardianError(
                ErrorCode.STORAGE_SCHEMA_INVALID,
                f"Schema file is not valid JSON: {schema_path}",
                details={"schema": "skill.json"},
            ) from exc

        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise CodeGuardianError(
                ErrorCode.STORAGE_SCHEMA_INVALID,
                f"Schema file is not a valid JSON Schema: {exc.message}",
                details={"schema": "skill.json"},
            ) from exc

        return Draft202012Validator(schema, format_checker=FormatChecker())

    def _validate_frontmatter(self, frontmatter: dict[str, Any]) -> None:
        """Validate a frontmatter dict against the skill schema."""
        try:
            self._validator.validate(frontmatter)
        except ValidationError as exc:
            field_path = "/".join(str(p) for p in exc.absolute_path) or "(root)"
            raise CodeGuardianError(
                ErrorCode.STORAGE_SCHEMA_INVALID,
                f"Skill frontmatter failed validation: {exc.message}",
                details={"schema": "skill.json", "field_path": field_path},
            ) from exc

    def _upsert_skill_row(
        self,
        name: str,
        skill_path: Path,
        frontmatter: dict[str, Any],
        *,
        quiet: bool = False,
    ) -> None:
        """Insert or replace a row in ``skills`` and sync ``skills_fts``.

        The FTS5 external-content table does not auto-update. Before
        replacing the ``skills`` row, the old FTS entry is deleted using
        the old column values. After inserting the new row, a new FTS
        entry is added using the new rowid. Skipping the delete half
        would leave stale entries in the index.

        When ``quiet`` is True, the FTS delete step is skipped. This is
        used by ``rebuild_index``, which issues a full FTS rebuild at
        the end and does not need incremental sync.
        """
        if not quiet:
            existing = self._sqlite.query(
                "SELECT rowid, name, description, trigger_text FROM skills WHERE name = ?",
                (name,),
            )
            if existing:
                old = existing[0]
                self._sqlite.execute(
                    "INSERT INTO skills_fts(skills_fts, rowid, name, description, trigger_text) "
                    "VALUES ('delete', ?, ?, ?, ?)",
                    (
                        old["rowid"],
                        old["name"],
                        old["description"] or "",
                        old["trigger_text"] or "",
                    ),
                )
            self._sqlite.execute("DELETE FROM skills WHERE name = ?", (name,))

        self._sqlite.execute(
            """
            INSERT INTO skills
                (name, path, description, trigger_text, skill_type, language,
                 effectiveness, total_uses, total_successes, state,
                 created_at, last_used_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                str(skill_path),
                frontmatter.get("description", ""),
                frontmatter.get("trigger", ""),
                frontmatter.get("skill_type", ""),
                frontmatter.get("language", ""),
                float(frontmatter.get("effectiveness", _DEFAULT_EFFECTIVENESS)),
                int(frontmatter.get("total_uses", 0)),
                int(frontmatter.get("total_successes", 0)),
                frontmatter.get("state", "active"),
                frontmatter.get("created_at", _now_iso()),
                frontmatter.get("last_used_at"),
            ),
        )

        if not quiet:
            new = self._sqlite.query(
                "SELECT rowid FROM skills WHERE name = ?",
                (name,),
            )
            if new:
                rowid = new[0]["rowid"]
                self._sqlite.execute(
                    "INSERT INTO skills_fts(rowid, name, description, trigger_text) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        rowid,
                        name,
                        frontmatter.get("description", ""),
                        frontmatter.get("trigger", ""),
                    ),
                )

    def _recompute_effectiveness(self, name: str) -> None:
        """Recompute and persist the effectiveness score for a skill."""
        rows = self._sqlite.query(
            "SELECT total_uses, total_successes FROM skills WHERE name = ?",
            (name,),
        )
        if not rows:
            return
        uses = int(rows[0]["total_uses"])
        successes = int(rows[0]["total_successes"])
        if uses == 0:
            return
        effectiveness = successes / uses
        self._sqlite.execute(
            "UPDATE skills SET effectiveness = ? WHERE name = ?",
            (effectiveness, name),
        )
