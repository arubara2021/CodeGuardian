"""Load the user's config.toml into a typed Config.

Path resolution follows a fixed priority. An explicit path passed to
``load_config`` wins. Failing that, the ``CODEGUARDIAN_CONFIG``
environment variable names the file. Failing that,
``CODEGUARDIAN_HOME/config.toml`` is used when the variable is set.
Falling through all of those, ``~/.codeguardian/config.toml`` is the
default.

A missing file is not an error. The function returns a ``Config`` with
every default applied. This lets a user run CodeGuardian without ever
creating a config file, and lets the tests exercise the loader without
touching the real filesystem.

The loader performs no schema validation of its own. It reads bytes,
parses TOML, and hands the resulting dict to ``validate`` from
``schema.py``. Every structural check lives there so that the two entry
points (loaded file and in-memory dict) behave identically. The loader
does distinguish two failure classes: a file whose TOML syntax is broken
raises ``CONFIG_PARSE_ERROR``, and a file whose content violates the
schema raises ``CONFIG_INVALID``.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path

from codeguardian.core.errors import CodeGuardianError, ErrorCode

from .defaults import (
    CODEGUARDIAN_CONFIG_ENV,
    CODEGUARDIAN_HOME_ENV,
    DEFAULT_CONFIG_DIR,
    DEFAULT_CONFIG_FILENAME,
)
from .schema import Config, validate

__all__ = ["load_config"]


def _resolve_config_path(explicit: str | Path | None, env: Mapping[str, str]) -> Path:
    """Return the config path to read, applying the resolution order.

    The value returned has the user's home directory expanded, so a
    path like ``~/.codeguardian/config.toml`` becomes an absolute path
    before it is opened.
    """
    if explicit is not None:
        return Path(explicit).expanduser()

    config_env = env.get(CODEGUARDIAN_CONFIG_ENV)
    if config_env:
        return Path(config_env).expanduser()

    home_env = env.get(CODEGUARDIAN_HOME_ENV)
    if home_env:
        return Path(home_env).expanduser() / DEFAULT_CONFIG_FILENAME

    return Path(DEFAULT_CONFIG_DIR).expanduser() / DEFAULT_CONFIG_FILENAME


def load_config(
    path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Config:
    """Load and validate the CodeGuardian config file.

    Args:
        path: An explicit path to the config file. When ``None``, the
            resolution order described in the module docstring applies.
        env: The environment mapping to consult for the override
            variables. When ``None``, ``os.environ`` is used. Tests pass
            a plain dict so they do not depend on the real environment.

    Returns:
        A fully typed ``Config``. When the file does not exist, a
        ``Config`` with every default applied.

    Raises:
        CodeGuardianError: With code ``CONFIG_PARSE_ERROR`` when the
            file is not valid TOML, with code ``CONFIG_INVALID`` when
            the parsed content fails schema validation or the path is
            not a regular file, and with code ``FILE_PERMISSION_DENIED``
            when the file cannot be read due to filesystem permissions.
    """
    if env is None:
        env = os.environ

    resolved = _resolve_config_path(path, env)

    if not resolved.exists():
        return Config()

    if not resolved.is_file():
        raise CodeGuardianError(
            code=ErrorCode.CONFIG_INVALID,
            message=f"{resolved} is not a regular file",
            details={"path": str(resolved)},
        )

    try:
        with resolved.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise CodeGuardianError(
            code=ErrorCode.CONFIG_PARSE_ERROR,
            message=f"{resolved}: invalid TOML: {exc}",
            details={"path": str(resolved)},
        ) from exc
    except PermissionError as exc:
        raise CodeGuardianError(
            code=ErrorCode.FILE_PERMISSION_DENIED,
            message=f"cannot read config file {resolved}: permission denied",
            details={"path": str(resolved)},
        ) from exc
    except OSError as exc:
        raise CodeGuardianError(
            code=ErrorCode.CONFIG_INVALID,
            message=f"cannot read config file {resolved}: {exc}",
            details={"path": str(resolved)},
        ) from exc

    return validate(raw)
