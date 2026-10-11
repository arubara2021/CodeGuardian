"""Read and write API keys through the operating system keychain.

API keys are stored in the OS keychain rather than in ``config.toml``.
The keychain encrypts at rest with the same mechanism the operating
system uses for stored browser passwords. A key in a plain text file is
a key that will eventually be committed to version control by accident.

This module exposes three functions. ``get_api_key`` reads a key by
provider name and returns ``None`` when the key is absent. ``set_api_key``
writes a key. ``delete_api_key`` removes one. All three are idempotent
where the semantics permit: deleting a key that is not present is a
success, not an error.

The module tolerates one environment where the keychain is not
available: a minimal Linux container with no Secret Service backend, or
a CI runner with no keychain daemon at all. ``get_api_key`` swallows
the resulting error and returns ``None`` so a caller can fall back to
environment variables. ``set_api_key`` and ``delete_api_key`` re-raise
it as a typed error, because a user who explicitly asked to store or
remove a key must learn why the operation could not complete.
"""

from __future__ import annotations

import logging

from codeguardian.core.errors import CodeGuardianError, ErrorCode

__all__ = [
    "SERVICE_NAME",
    "delete_api_key",
    "get_api_key",
    "set_api_key",
]

logger = logging.getLogger(__name__)

# The service name under which keys are stored. Changing it after a
# user has stored a key would make that key invisible. It is therefore
# a stable constant, not a configuration option.
SERVICE_NAME = "codeguardian"

try:
    import keyring
    from keyring.errors import KeyringError, PasswordDeleteError

    _KEYRING_AVAILABLE = True
except ImportError:
    keyring = None  # type: ignore[assignment]
    KeyringError = Exception  # type: ignore[misc,assignment]
    PasswordDeleteError = Exception  # type: ignore[misc,assignment]
    _KEYRING_AVAILABLE = False


def _require_keyring() -> None:
    """Raise a typed error when the ``keyring`` package is not installed.

    The package is a declared dependency of CodeGuardian, so this should
    never fire in a correct installation. It exists so that a
    misconfigured environment produces a clear message rather than an
    ImportError from the middle of an unrelated call.
    """
    if not _KEYRING_AVAILABLE:
        raise CodeGuardianError(
            code=ErrorCode.SECRET_KEYCHAIN_ERROR,
            message=(
                "the keyring package is not installed; "
                "run pip install keyring to enable API key storage"
            ),
            details={"service": SERVICE_NAME},
        )


def get_api_key(provider: str) -> str | None:
    """Return the stored API key for a provider, or ``None``.

    A ``None`` return covers three cases: the keyring package is not
    installed, the operating system has no usable keychain backend, or
    no key has been stored for the given provider. The caller is
    expected to treat all three the same way: fall back to an
    environment variable, and if that is also absent, raise a clear
    error telling the user to run ``codeguardian config set-key``.

    Args:
        provider: The provider name, such as ``"anthropic"``.

    Returns:
        The API key as a string, or ``None`` when no key is available.
    """
    if not _KEYRING_AVAILABLE:
        return None
    try:
        return keyring.get_password(SERVICE_NAME, provider)
    except KeyringError as exc:
        # A missing keychain backend is common in CI and in minimal
        # containers. Returning None lets the caller fall back to
        # environment variables without special-casing the environment.
        logger.warning(
            "keychain backend unavailable for provider %r: %s",
            provider,
            exc,
        )
        return None


def set_api_key(provider: str, value: str) -> None:
    """Store an API key for a provider in the OS keychain.

    Args:
        provider: The provider name, such as ``"anthropic"``.
        value: The API key. Must not be empty.

    Raises:
        CodeGuardianError: With code ``SECRET_KEYCHAIN_ERROR`` when the
            keyring package is not installed or the keychain backend
            rejects the write. With code ``CONFIG_INVALID`` when the
            provider name or the value is empty.
    """
    _require_keyring()
    if not provider:
        raise CodeGuardianError(
            code=ErrorCode.CONFIG_INVALID,
            message="provider name must not be empty",
            details={"service": SERVICE_NAME},
        )
    if not value:
        raise CodeGuardianError(
            code=ErrorCode.CONFIG_INVALID,
            message=f"API key for provider {provider!r} must not be empty",
            details={"service": SERVICE_NAME, "provider": provider},
        )
    try:
        keyring.set_password(SERVICE_NAME, provider, value)
    except KeyringError as exc:
        raise CodeGuardianError(
            code=ErrorCode.SECRET_KEYCHAIN_ERROR,
            message=(f"keychain backend unavailable for provider {provider!r}: " f"{exc}"),
            details={"service": SERVICE_NAME, "provider": provider},
        ) from exc


def delete_api_key(provider: str) -> None:
    """Remove a stored API key for a provider.

    Deleting a key that is not present is a success, not an error. The
    caller asked for the key to be gone, and after the call it is gone.

    Args:
        provider: The provider name, such as ``"anthropic"``.

    Raises:
        CodeGuardianError: With code ``SECRET_KEYCHAIN_ERROR`` when the
            keyring package is not installed or the keychain backend
            rejects the operation. With code ``CONFIG_INVALID`` when
            the provider name is empty.
    """
    _require_keyring()
    if not provider:
        raise CodeGuardianError(
            code=ErrorCode.CONFIG_INVALID,
            message="provider name must not be empty",
            details={"service": SERVICE_NAME},
        )
    try:
        keyring.delete_password(SERVICE_NAME, provider)
    except PasswordDeleteError:
        # The key was not present. That is the desired end state.
        return
    except KeyringError as exc:
        raise CodeGuardianError(
            code=ErrorCode.SECRET_KEYCHAIN_ERROR,
            message=(f"keychain backend unavailable for provider {provider!r}: " f"{exc}"),
            details={"service": SERVICE_NAME, "provider": provider},
        ) from exc
