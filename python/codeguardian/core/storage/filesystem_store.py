"""Filesystem artifacts, validated against their JSON Schemas.

Every structured artifact CodeGuardian produces lives as a JSON file
under ``~/.codeguardian/repos/<repo-id>/runs/<run-id>/``. This module
writes and reads those files, and it validates every write against the
schema that describes the artifact's shape.

Validation on write is the design's chosen boundary. The schemas in the
``schemas/`` directory describe what a recon manifest or a blast radius
report or a verification verdict must look like. Enforcing them at the
storage layer means an invalid artifact never reaches disk, which in
turn means every reader - the CLI, the audit writer, a later phase -
can trust what it reads without re-validating.

Schema files are loaded lazily and cached for the lifetime of the store.
The cache is keyed by filename, so a store that writes one kind of
artifact loads one schema. The cache is not invalidated; a schema file
changing mid-run is not a scenario the pipeline supports.

The store does not create the repository directory structure until the
first write. This keeps the store cheap to construct for read-only or
inspection paths.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from codeguardian.core.errors import CodeGuardianError, ErrorCode

__all__ = ["FilesystemStore"]


class FilesystemStore:
    """Reads and writes JSON and text artifacts under a repository root.

    The store is constructed with two paths. The first is the root of
    the on-disk storage tree, typically ``~/.codeguardian``. The second
    is the directory holding the JSON Schemas, typically the ``schemas/``
    directory at the root of the CodeGuardian repository or installation.

    All artifact paths are constructed from ``(repo_id, run_id, filename)``
    so that the layout matches the design document exactly. Callers do
    not construct paths themselves; they call ``write_artifact`` and the
    path is returned for logging or inspection.
    """

    def __init__(self, root: Path, schemas_dir: Path) -> None:
        """Construct the store. No I/O happens until the first write.

        Args:
            root: The root of the on-disk storage tree. Created lazily.
            schemas_dir: The directory holding the JSON Schema files.
                Every ``schema_filename`` passed to ``write_artifact``
                is resolved relative to this directory.
        """
        self._root = Path(root)
        self._schemas_dir = Path(schemas_dir)
        self._schema_cache: dict[str, dict[str, Any]] = {}

    @property
    def root(self) -> Path:
        """The root of the on-disk storage tree."""
        return self._root

    @property
    def schemas_dir(self) -> Path:
        """The directory holding the JSON Schema files."""
        return self._schemas_dir

    def artifact_path(self, repo_id: str, run_id: str, filename: str) -> Path:
        """Return the full path an artifact would be written to.

        Does not create the path or touch the filesystem. Useful for
        logging, for a caller that needs to pass the path to a
        subprocess, and for tests that want to assert on layout.

        Args:
            repo_id: The repository identifier, typically a truncated
                SHA-256 of the repository URL.
            run_id: The run identifier, a UUID.
            filename: The artifact filename, including its extension.
                For example ``"recon.json"`` or ``"diff.patch"``.

        Returns:
            The absolute or relative Path, joined with the store root.
        """
        return self._root / "repos" / repo_id / "runs" / run_id / filename

    def write_artifact(
        self,
        repo_id: str,
        run_id: str,
        filename: str,
        data: Mapping[str, Any],
        schema_filename: str,
    ) -> Path:
        """Validate ``data`` against its schema and write it as JSON.

        The file is written with two-space indentation and sorted keys
        so that two runs producing the same data produce byte-identical
        files. This matters because the audit log records a hash of each
        artifact and a reader comparing two artifacts should see a diff
        only when the data differs, not when the key order does.

        Args:
            repo_id: The repository identifier.
            run_id: The run identifier.
            filename: The output filename, including ``.json``.
            data: The artifact data. Validated before writing.
            schema_filename: The name of a schema file inside
                ``schemas_dir``. For example ``"recon.json"`` or
                ``"blast-radius.json"``.

        Returns:
            The path the artifact was written to.

        Raises:
            CodeGuardianError: With code STORAGE_SCHEMA_INVALID if the
                schema is missing, malformed, or the data fails
                validation. With code FILE_PERMISSION_DENIED if the
                directory or file cannot be written for lack of
                permission. With code SYS_INTERNAL for any other I/O
                failure.
        """
        self._validate(data, schema_filename)

        path = self.artifact_path(repo_id, run_id, filename)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            raise CodeGuardianError(
                ErrorCode.FILE_PERMISSION_DENIED,
                f"Cannot create artifact directory: {path.parent}",
                details={"path": str(path.parent)},
            ) from exc
        except OSError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"Cannot create artifact directory: {exc}",
                details={"path": str(path.parent)},
            ) from exc

        # ensure_ascii=False preserves non-ASCII characters as themselves.
        # sort_keys=True makes the output deterministic. A trailing
        # newline matches the POSIX convention and satisfies the
        # end-of-file-fixer pre-commit hook.
        payload = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
        try:
            path.write_text(payload + "\n", encoding="utf-8")
        except PermissionError as exc:
            raise CodeGuardianError(
                ErrorCode.FILE_PERMISSION_DENIED,
                f"Cannot write artifact: {path}",
                details={"path": str(path)},
            ) from exc
        except OSError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"Cannot write artifact: {exc}",
                details={"path": str(path)},
            ) from exc

        return path

    def read_artifact(
        self,
        repo_id: str,
        run_id: str,
        filename: str,
    ) -> dict[str, Any]:
        """Read a JSON artifact and return it as a dictionary.

        The artifact is not re-validated on read. The store validated it
        on write, and the file has not been modified since. A caller
        that needs to defend against external modification of the
        storage tree should validate the returned dictionary itself.

        Args:
            repo_id: The repository identifier.
            run_id: The run identifier.
            filename: The artifact filename, including ``.json``.

        Returns:
            The parsed artifact.

        Raises:
            CodeGuardianError: With code FILE_NOT_FOUND if the file does
                not exist. With code FILE_PERMISSION_DENIED if it exists
                but cannot be read. With code STORAGE_SCHEMA_INVALID if
                the file is not valid JSON.
        """
        path = self.artifact_path(repo_id, run_id, filename)
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise CodeGuardianError(
                ErrorCode.FILE_NOT_FOUND,
                f"Artifact not found: {path}",
                details={"path": str(path)},
            ) from exc
        except PermissionError as exc:
            raise CodeGuardianError(
                ErrorCode.FILE_PERMISSION_DENIED,
                f"Cannot read artifact: {path}",
                details={"path": str(path)},
            ) from exc

        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise CodeGuardianError(
                ErrorCode.STORAGE_SCHEMA_INVALID,
                f"Artifact is not valid JSON: {exc}",
                details={"path": str(path), "lineno": exc.lineno, "colno": exc.colno},
            ) from exc

    def write_text(
        self,
        repo_id: str,
        run_id: str,
        filename: str,
        content: str,
    ) -> Path:
        """Write a text artifact without validation.

        This is the path for artifacts that are not JSON: diffs, test
        files, the refactored source, the Markdown run summary. They
        have no schema; their shape is defined by their own format.

        Args:
            repo_id: The repository identifier.
            run_id: The run identifier.
            filename: The output filename, including its extension.
            content: The text content.

        Returns:
            The path the file was written to.

        Raises:
            CodeGuardianError: With code FILE_PERMISSION_DENIED or
                SYS_INTERNAL if the write fails.
        """
        path = self.artifact_path(repo_id, run_id, filename)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except PermissionError as exc:
            raise CodeGuardianError(
                ErrorCode.FILE_PERMISSION_DENIED,
                f"Cannot write file: {path}",
                details={"path": str(path)},
            ) from exc
        except OSError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"Cannot write file: {exc}",
                details={"path": str(path)},
            ) from exc
        return path

    def read_text(self, repo_id: str, run_id: str, filename: str) -> str:
        """Read a text artifact.

        Args:
            repo_id: The repository identifier.
            run_id: The run identifier.
            filename: The filename, including its extension.

        Returns:
            The file content as a string.

        Raises:
            CodeGuardianError: With code FILE_NOT_FOUND or
                FILE_PERMISSION_DENIED if the read fails.
        """
        path = self.artifact_path(repo_id, run_id, filename)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise CodeGuardianError(
                ErrorCode.FILE_NOT_FOUND,
                f"File not found: {path}",
                details={"path": str(path)},
            ) from exc
        except PermissionError as exc:
            raise CodeGuardianError(
                ErrorCode.FILE_PERMISSION_DENIED,
                f"Cannot read file: {path}",
                details={"path": str(path)},
            ) from exc

    def delete_run(self, repo_id: str, run_id: str) -> None:
        """Remove a run's artifact directory and everything inside it.

        Idempotent: deleting a run that does not exist is not an error.
        This is what the retention sweep calls when a run's artifacts
        have aged out of the retention window.

        Args:
            repo_id: The repository identifier.
            run_id: The run identifier.

        Raises:
            CodeGuardianError: With code FILE_PERMISSION_DENIED if the
                directory exists but cannot be removed. With code
                SYS_INTERNAL for any other removal failure.
        """
        run_dir = self._root / "repos" / repo_id / "runs" / run_id
        if not run_dir.exists():
            return
        try:
            shutil.rmtree(run_dir)
        except PermissionError as exc:
            raise CodeGuardianError(
                ErrorCode.FILE_PERMISSION_DENIED,
                f"Cannot delete run directory: {run_dir}",
                details={"path": str(run_dir)},
            ) from exc
        except OSError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"Cannot delete run directory: {exc}",
                details={"path": str(run_dir)},
            ) from exc

    def _validate(self, data: Mapping[str, Any], schema_filename: str) -> None:
        """Validate an artifact against its schema.

        Args:
            data: The artifact data.
            schema_filename: The schema file name inside ``schemas_dir``.

        Raises:
            CodeGuardianError: With code STORAGE_SCHEMA_INVALID if the
                data fails validation. The error's ``details`` carries
                the JSON Pointer path to the failing field so the caller
                can report exactly which value was wrong.
        """
        schema = self._load_schema(schema_filename)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        try:
            validator.validate(data)
        except ValidationError as exc:
            field_path = "/".join(str(part) for part in exc.absolute_path) or "(root)"
            raise CodeGuardianError(
                ErrorCode.STORAGE_SCHEMA_INVALID,
                f"Artifact failed validation against {schema_filename}: {exc.message}",
                details={
                    "schema": schema_filename,
                    "field_path": field_path,
                },
            ) from exc

    def _load_schema(self, schema_filename: str) -> dict[str, Any]:
        """Load a schema file from disk, caching it for later lookups.

        Args:
            schema_filename: The schema file name.

        Returns:
            The parsed schema as a dictionary.

        Raises:
            CodeGuardianError: With code STORAGE_SCHEMA_INVALID if the
                schema file is missing, is not valid JSON, or is not a
                valid JSON Schema.
        """
        cached = self._schema_cache.get(schema_filename)
        if cached is not None:
            return cached

        schema_path = self._schemas_dir / schema_filename
        try:
            content = schema_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise CodeGuardianError(
                ErrorCode.STORAGE_SCHEMA_INVALID,
                f"Schema file not found: {schema_path}",
                details={"schema": schema_filename, "path": str(schema_path)},
            ) from exc
        except PermissionError as exc:
            raise CodeGuardianError(
                ErrorCode.FILE_PERMISSION_DENIED,
                f"Cannot read schema file: {schema_path}",
                details={"schema": schema_filename, "path": str(schema_path)},
            ) from exc

        try:
            schema = json.loads(content)
        except json.JSONDecodeError as exc:
            raise CodeGuardianError(
                ErrorCode.STORAGE_SCHEMA_INVALID,
                f"Schema file is not valid JSON: {schema_path}",
                details={
                    "schema": schema_filename,
                    "path": str(schema_path),
                    "lineno": exc.lineno,
                    "colno": exc.colno,
                },
            ) from exc

        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise CodeGuardianError(
                ErrorCode.STORAGE_SCHEMA_INVALID,
                f"Schema file is not a valid JSON Schema: {exc.message}",
                details={"schema": schema_filename, "path": str(schema_path)},
            ) from exc

        self._schema_cache[schema_filename] = schema
        return schema
