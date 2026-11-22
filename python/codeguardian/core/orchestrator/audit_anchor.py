"""External anchors for the audit chain.

A local hash chain detects modification of its entries but not
truncation of its tail. An attacker with filesystem write access can
delete the last N entries and recompute the chain from the truncation
point. The shortened chain verifies because the verifier starts from
whatever ``prev_hash`` the first remaining entry carries.

The fix is an external anchor: a signed, independently stored copy of
the chain head. Verification re-derives the head from the chain and
compares it to the anchored head. A truncated chain produces a head
that does not match the anchor, so the truncation is detected.

Three anchor implementations ship.

``FileAnchor`` writes the head to a user-controlled path outside the
audit directory. The head is signed with HMAC-SHA256 using a key
stored in the OS keyring. The keyring keeps the key outside the audit
file's trust domain, so an attacker who can write the audit file
cannot forge the anchor without also compromising the keyring. This is
the default.

``RFC3161Anchor`` submits the head to an RFC 3161 Time-Stamp
Authority and stores the timestamp token alongside the chain. The TSA
provides a third-party-verifiable timestamp. This is opt-in and
requires the ``openssl`` binary on PATH.

``NoopAnchor`` records nothing. It exists so tests and dry runs can
use the same interface without an external dependency.

``verify_chain_against_anchor`` takes a sequence of audit entries and
an anchor and raises when the chain's re-derived head does not match
the anchored head. The function does not modify either side.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from codeguardian.core.errors import CodeGuardianError, ErrorCode

__all__ = [
    "AuditAnchor",
    "FileAnchor",
    "NoopAnchor",
    "RFC3161Anchor",
    "verify_chain_against_anchor",
]


_DEFAULT_ANCHOR_PATH = Path.home() / ".config" / "codeguardian" / "audit-anchor.json"
_KEYRING_SERVICE = "codeguardian"
_KEYRING_USERNAME = "audit_anchor_hmac_key"
_DEFAULT_TSA_URL = "https://freetsa.org/tsr"
_DEFAULT_OPENSSL = "openssl"
_OPENSSL_TIMEOUT_S = 30


@runtime_checkable
class AuditAnchor(Protocol):
    """A store for the audit chain's head hash.

    Implementations must be safe to call from the thread that writes
    the chain. The write path is called once per run; the read path is
    called once per verification. Neither path raises on a missing
    anchor file; a missing anchor means no anchor has been written
    yet and reads return None.
    """

    def write_head(
        self,
        head_hash: str,
        prev_head_hash: str | None,
    ) -> None:
        """Record the chain's head hash."""
        ...

    def read_head(self) -> str | None:
        """Return the recorded head hash, or None when none exists."""
        ...


def _hmac_sha256(key: bytes, message: bytes) -> str:
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


class FileAnchor:
    """An anchor that writes the signed head to a file outside the chain.

    The file's location is user-controlled. The default is
    ``~/.config/codeguardian/audit-anchor.json``, which is outside the
    audit directory and outside the project clone root. The HMAC key
    lives in the OS keyring, so an attacker who can write the anchor
    file cannot forge a head without also compromising the keyring.

    The signature covers the head hash, the previous head hash, and
    the signed-at timestamp. The signed-at timestamp lets a verifier
    distinguish an anchor written after a truncation from one written
    before it.

    Args:
        path: The anchor file path. Defaults to
            ``~/.config/codeguardian/audit-anchor.json``.
        key: The HMAC key. When omitted, it is loaded from the OS
            keyring. On first use a fresh 32-byte key is generated and
            stored in the keyring. Tests that do not want to depend on
            the keyring should pass ``key`` explicitly.
        keyring_service: The keyring service name. Defaults to
            ``"codeguardian"``.
        keyring_username: The keyring username. Defaults to
            ``"audit_anchor_hmac_key"``.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        key: bytes | None = None,
        keyring_service: str = _KEYRING_SERVICE,
        keyring_username: str = _KEYRING_USERNAME,
    ) -> None:
        self._path = Path(path) if path is not None else _DEFAULT_ANCHOR_PATH
        self._keyring_service = keyring_service
        self._keyring_username = keyring_username
        self._key = key if key is not None else self._load_or_create_key()

    def _load_or_create_key(self) -> bytes:
        try:
            import keyring
        except ImportError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_NOT_IMPLEMENTED,
                "FileAnchor requires the 'keyring' package, or a key passed explicitly via key=",
                details={"path": str(self._path)},
            ) from exc
        stored = keyring.get_password(self._keyring_service, self._keyring_username)
        if stored is not None:
            return bytes.fromhex(stored)
        fresh = os.urandom(32)
        keyring.set_password(self._keyring_service, self._keyring_username, fresh.hex())
        return fresh

    def write_head(
        self,
        head_hash: str,
        prev_head_hash: str | None,
    ) -> None:
        signed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        payload = {
            "head_hash": head_hash,
            "prev_head_hash": prev_head_hash,
            "signed_at": signed_at,
        }
        signature = _hmac_sha256(self._key, _canonical_json(payload))
        record = {**payload, "hmac": signature}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".cg-anchor-", dir=str(self._path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, sort_keys=True)
            os.replace(temp_name, self._path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp_name)
            raise

    def read_head(self) -> str | None:
        if not self._path.exists():
            return None
        try:
            record = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                f"anchor file is unreadable: {exc}",
                details={"path": str(self._path)},
            ) from exc
        payload = {
            "head_hash": record.get("head_hash"),
            "prev_head_hash": record.get("prev_head_hash"),
            "signed_at": record.get("signed_at"),
        }
        expected = _hmac_sha256(self._key, _canonical_json(payload))
        observed = record.get("hmac")
        if not isinstance(observed, str) or not hmac.compare_digest(expected, observed):
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "anchor signature is invalid",
                details={"path": str(self._path)},
            )
        head = record.get("head_hash")
        return str(head) if head else None


class RFC3161Anchor:
    """An anchor that timestamps the head with an RFC 3161 TSA.

    The head is written to a sidecar file and submitted to the TSA.
    The returned timestamp token is stored alongside the chain. On
    verify, the token is checked against the head and the chain's
    re-derived head must match.

    Requires the ``openssl`` binary on PATH. The constructor raises
    ``SYS_NOT_IMPLEMENTED`` with a clear message when the binary is
    missing.

    Args:
        chain_path: The audit chain file path. The sidecar head file
            and the token file are placed next to it.
        tsa_url: The TSA URL. Defaults to ``https://freetsa.org/tsr``,
            a public TSA suitable for development. Production
            deployments should use an organizational TSA.
        openssl: The openssl binary name. Defaults to ``"openssl"``.
    """

    def __init__(
        self,
        chain_path: Path,
        *,
        tsa_url: str = _DEFAULT_TSA_URL,
        openssl: str = _DEFAULT_OPENSSL,
    ) -> None:
        self._chain_path = Path(chain_path)
        self._head_path = self._chain_path.with_suffix(self._chain_path.suffix + ".head")
        self._token_path = self._chain_path.with_suffix(self._chain_path.suffix + ".tsr")
        self._tsa_url = tsa_url
        self._openssl = openssl
        if shutil.which(openssl) is None:
            raise CodeGuardianError(
                ErrorCode.SYS_NOT_IMPLEMENTED,
                "RFC3161Anchor requires the 'openssl' binary on PATH",
                details={"openssl": openssl},
            )

    def _run_openssl(self, argv: list[str], *, purpose: str) -> None:
        try:
            subprocess.run(
                argv,
                check=True,
                capture_output=True,
                text=True,
                timeout=_OPENSSL_TIMEOUT_S,
            )
        except FileNotFoundError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_NOT_IMPLEMENTED,
                f"openssl is not available: {exc}",
                details={"argv": argv},
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"openssl {purpose} timed out after {_OPENSSL_TIMEOUT_S}s",
                details={"argv": argv},
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"openssl {purpose} failed: {(exc.stderr or '')[:200]}",
                details={"argv": argv, "returncode": exc.returncode},
            ) from exc

    def write_head(
        self,
        head_hash: str,
        prev_head_hash: str | None,
    ) -> None:
        del prev_head_hash
        self._head_path.write_text(head_hash, encoding="utf-8")
        query_path = self._head_path.with_suffix(self._head_path.suffix + ".tsq")
        try:
            self._run_openssl(
                [
                    self._openssl,
                    "ts",
                    "-query",
                    "-data",
                    str(self._head_path),
                    "-sha256",
                    "-cert",
                    "-out",
                    str(query_path),
                ],
                purpose="timestamp request",
            )
            self._run_openssl(
                [
                    self._openssl,
                    "ts",
                    "-reply",
                    "-queryfile",
                    str(query_path),
                    "-url",
                    self._tsa_url,
                    "-out",
                    str(self._token_path),
                ],
                purpose="timestamp reply",
            )
        finally:
            with contextlib.suppress(FileNotFoundError):
                query_path.unlink()

    def read_head(self) -> str | None:
        if not self._head_path.exists():
            return None
        if not self._token_path.exists():
            raise CodeGuardianError(
                ErrorCode.SYS_INVARIANT_VIOLATED,
                "RFC3161 token file is missing; the anchor is incomplete",
                details={"head_path": str(self._head_path)},
            )
        self._run_openssl(
            [
                self._openssl,
                "ts",
                "-verify",
                "-data",
                str(self._head_path),
                "-in",
                str(self._token_path),
                "-CApath",
                "/etc/ssl/certs",
            ],
            purpose="timestamp verify",
        )
        return self._head_path.read_text(encoding="utf-8").strip()


class NoopAnchor:
    """An anchor that records nothing.

    Used by tests and dry runs. ``read_head`` always returns None and
    ``verify_chain_against_anchor`` treats a NoopAnchor as "no anchor
    to compare against", so the verification does not raise.
    """

    def write_head(
        self,
        head_hash: str,
        prev_head_hash: str | None,
    ) -> None:
        del head_hash, prev_head_hash

    def read_head(self) -> str | None:
        return None


def _computed_chain_head(entries: Sequence[Mapping[str, Any]]) -> str | None:
    """Return the last entry's hash, or None for an empty chain."""
    if not entries:
        return None
    head = entries[-1].get("entry_hash")
    return str(head) if head else None


def verify_chain_against_anchor(
    entries: Sequence[Mapping[str, Any]],
    anchor: AuditAnchor,
) -> None:
    """Verify the chain's head against the anchor.

    Args:
        entries: The audit entries, in order. Each entry is a mapping
            with at least an ``entry_hash`` key. The chain's own
            forward verification is the caller's job; this function
            only compares the final head to the anchor.
        anchor: The anchor to compare against. A ``NoopAnchor``
            verifies trivially.

    Raises:
        CodeGuardianError: With code ``SYS_INVARIANT_VIOLATED`` when
            the anchor exists but does not match the chain's head, or
            when the anchor is missing but the chain is non-empty (an
            unanchored chain cannot be treated as tamper-evident), or
            when the chain is empty but the anchor expects a head
            (the chain may have been truncated).
    """
    if isinstance(anchor, NoopAnchor):
        return
    anchored = anchor.read_head()
    computed = _computed_chain_head(entries)
    if anchored is None and computed is None:
        return
    if anchored is None:
        raise CodeGuardianError(
            ErrorCode.SYS_INVARIANT_VIOLATED,
            "audit chain is not anchored; a chain without an external "
            "anchor cannot be treated as tamper-evident",
            details={"computed_head": computed},
        )
    if computed is None:
        raise CodeGuardianError(
            ErrorCode.SYS_INVARIANT_VIOLATED,
            "audit chain is empty but the anchor expects a head; the chain may have been truncated",
            details={"anchored_head": anchored},
        )
    if not hmac.compare_digest(anchored, computed):
        raise CodeGuardianError(
            ErrorCode.SYS_INVARIANT_VIOLATED,
            "audit chain head does not match the external anchor; the "
            "chain may have been truncated or replaced",
            details={"anchored_head": anchored, "computed_head": computed},
        )
