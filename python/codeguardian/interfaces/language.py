"""Language plugin contract.

A language plugin tells CodeGuardian how to detect, parse, test, and
refactor one language. Plugins are validated against this protocol at
load time using ``isinstance``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from .sandbox import SandboxHandle

__all__ = ["LanguagePlugin", "ReliabilityTier", "TestResult"]

ReliabilityTier = Literal["high", "best-effort"]


@dataclass(frozen=True, slots=True)
class TestResult:
    """Aggregate outcome of running a language's test suite."""

    passed: int
    failed: int
    errored: int
    duration_seconds: float


@runtime_checkable
class LanguagePlugin(Protocol):
    """A language that CodeGuardian can parse, test, and refactor."""

    name: str
    tree_sitter_grammar: str
    test_runner: str
    refactor_rules: tuple[str, ...]
    reliability_tier: ReliabilityTier

    def detect(self, path: Path) -> bool:
        """Return whether this plugin claims the file at ``path``."""
        ...

    def parse(self, path: Path) -> object:
        """Return an AST for the file at ``path``.

        The concrete AST type is owned by the parser implementation
        and treated as opaque by callers.
        """
        ...

    def generate_test(self, context: object) -> str:
        """Render a test file for the given context pack."""
        ...

    def run_tests(self, sandbox: SandboxHandle, test_path: str) -> TestResult:
        """Run the test file inside the sandbox and return its result."""
        ...
