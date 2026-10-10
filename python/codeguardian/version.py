"""Single source of truth for the CodeGuardian version string.

Any component that reports a version imports from this module rather than
duplicating the number. Keeping one definition prevents the runtime value
and the packaged metadata from drifting apart.
"""

__all__ = ["__version__", "get_version"]

# PEP 440 release segment. This is the only place the version is written
# by hand. Packaging tooling reads the same value from pyproject.toml, and
# the two are kept in sync at release time.
__version__ = "1.0.0"


def get_version() -> str:
    """Return the current CodeGuardian version string.

    This helper exists so that callers do not depend on the module-level
    constant directly. If the version ever needs to be resolved from
    another source, such as installed package metadata, only this module
    changes.

    Returns:
        The current version as a PEP 440 string.
    """
    return __version__
