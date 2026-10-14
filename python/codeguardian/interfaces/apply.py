"""Consent-gated application of changes to a repository.

The ``ApplyProvider`` is separate from ``RepositoryProvider``. It is
never instantiated during agent execution; it runs only after the
user approves a diff. Every call carries a ``ConsentRecord`` that
the implementation writes to the audit log before touching the
repository.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

__all__ = [
    "ApplyProvider",
    "ApplyResult",
    "ConsentRecord",
    "ConsentScope",
    "RollbackResult",
]


ConsentScope = Literal["apply", "verify"]


@dataclass(frozen=True, slots=True)
class ConsentRecord:
    """A user's recorded approval for a change or a verification.

    Attributes:
        consent_id: Identifier for this consent event.
        run_id: The run the consent belongs to.
        granted_at: Timestamp the user granted consent.
        granted_by: Local session identifier of the granting user.
        scope: What the consent authorizes.
        diff_hash: Hash of the diff that was approved.
        rollback_ref: Reference that can be used to roll back.
    """

    consent_id: str
    run_id: str
    granted_at: datetime
    granted_by: str
    scope: ConsentScope
    diff_hash: str
    rollback_ref: str


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """The outcome of applying a diff.

    Attributes:
        success: Whether the diff was applied cleanly.
        applied_files: Repository-relative paths that were modified.
        rollback_ref: Reference that can be used to roll back.
    """

    success: bool
    applied_files: tuple[str, ...]
    rollback_ref: str


@dataclass(frozen=True, slots=True)
class RollbackResult:
    """The outcome of rolling back a previously applied change.

    Attributes:
        success: Whether the rollback succeeded.
        restored_files: Repository-relative paths that were restored.
    """

    success: bool
    restored_files: tuple[str, ...]


class ApplyProvider(Protocol):
    """Consent-gated mutation of a repository.

    Implementations write to the original repository only after
    verifying the supplied ``ConsentRecord`` and recording it in the
    audit log. This is the only interface in the system that modifies
    real code.
    """

    def apply(
        self,
        repo_url: str,
        diff: str,
        consent: ConsentRecord,
    ) -> ApplyResult:
        """Apply ``diff`` to ``repo_url`` under the given consent.

        Args:
            repo_url: The repository to modify.
            diff: A unified diff produced by the pipeline.
            consent: The user's recorded approval.

        Returns:
            The result of the apply operation.

        Raises:
            CodeGuardianError: ``APPLY_CONFLICT`` if the diff cannot
                be applied cleanly, ``APPLY_AUTH_FAILED`` if the
                credentials are insufficient, ``APPLY_PARTIAL`` if
                only part of the diff landed.
        """
        ...

    def rollback(self, rollback_ref: str) -> RollbackResult:
        """Roll back a previously applied change.

        Args:
            rollback_ref: Reference produced by a prior ``apply``.

        Returns:
            The result of the rollback operation.

        Raises:
            CodeGuardianError: ``ROLLBACK_FAILED`` if the rollback
                cannot complete, ``ROLLBACK_REF_NOT_FOUND`` if the
                reference is unknown.
        """
        ...
