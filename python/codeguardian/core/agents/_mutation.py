"""AST-level mutation testing for characterization tests.

The tester uses this module to generate small deterministic mutations
of a target function and check whether the generated test catches them.
A test that passes on the original code but also passes on every mutant
asserts nothing meaningful about the target's behavior. A test that
fails on most mutants is doing its job.

This module is intentionally simple. It does not aim for parity with
mutmut or cosmic-ray. It generates four common mutation classes
(comparison operators, boolean operators, boolean constants, integer
constants, and return values), stops at a caller-supplied limit, and
reports a coarse signal. A later block can expand the mutation set
without changing the caller contract.

The mutation gate requires a sandbox that can write files. Tests can
use a fake executor. Real runs use the bubblewrap sandbox. When the
executor does not provide write_file, the gate returns None rather
than a fabricated score.
"""

from __future__ import annotations

import ast
import copy
from dataclasses import dataclass
from typing import Protocol

from codeguardian.core.agents._verifier_common import SandboxExecutor

__all__ = [
    "Mutation",
    "WritableSandboxExecutor",
    "generate_mutations",
]


class WritableSandboxExecutor(SandboxExecutor, Protocol):
    """SandboxExecutor that can also write files.

    Mutation testing writes each mutant to a file in the sandbox so
    the test imports it. Executors without write_file cannot be used
    for the mutation gate; the tester checks with hasattr and falls
    back to a None mutation score.
    """

    def write_file(self, path: str, content: str) -> None:
        """Write content to path inside the sandbox working directory."""
        ...


@dataclass(frozen=True, slots=True)
class Mutation:
    """One mutant of a target function.

    Attributes:
        description: A short human-readable description of the change,
            for example "< -> <=" or "return replaced with None".
        mutated_source: The full module source with the target function
            replaced by the mutated version. Includes imports and other
            top-level code so the module remains valid.
    """

    description: str
    mutated_source: str


_COMPARE_SWAPS: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
}


def generate_mutations(
    source: str,
    function_name: str,
    limit: int = 5,
) -> list[Mutation]:
    """Return up to ``limit`` mutations of ``function_name`` in ``source``.

    Args:
        source: The full source of the file that contains the function.
        function_name: The name of the function to mutate. When the
            function is a method, use its qualified name, for example
            "Class.method".
        limit: The maximum number of mutations to return. Defaults to 5.

    Returns:
        A list of Mutation objects. Empty when the function is not
        found, when the source does not parse, or when the function
        contains no mutable constructs.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    target = _find_function(tree, function_name)
    if target is None:
        return []

    candidates = _collect_candidates(target)
    mutations: list[Mutation] = []

    for index, mutator, description in candidates[:limit]:
        mutated_tree = copy.deepcopy(tree)
        mutated_target = _find_function(mutated_tree, function_name)
        if mutated_target is None:
            continue
        for walk_index, node in enumerate(ast.walk(mutated_target)):
            if walk_index == index:
                try:
                    mutator(node)
                except Exception:
                    break
                break
        try:
            mutated_source = ast.unparse(mutated_tree)
        except Exception:
            continue
        mutations.append(Mutation(description=description, mutated_source=mutated_source))

    return mutations


def _find_function(tree: ast.Module, name: str) -> ast.AST | None:
    """Return the FunctionDef or AsyncFunctionDef matching ``name``.

    Handles qualified names (``Class.method``) by walking the module
    body one part at a time. Returns None when any part is not found
    or when the final node is not a function definition.
    """
    parts = name.split(".")
    current: ast.AST = tree
    for part in parts:
        found: ast.AST | None = None
        for node in getattr(current, "body", []):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == part
            ):
                found = node
                break
        if found is None:
            return None
        current = found
    if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return current
    return None


def _collect_candidates(func: ast.AST) -> list[tuple[int, object, str]]:
    """Return mutation candidates in walk order.

    Each candidate is a three-tuple of the node index in ast.walk(func),
    a mutator callable, and a description. The index selects the same
    node in the copied tree during mutation generation.
    """
    candidates: list[tuple[int, object, str]] = []

    for index, node in enumerate(ast.walk(func)):
        if isinstance(node, ast.Compare):
            for op_index, op in enumerate(node.ops):
                swap_cls = _COMPARE_SWAPS.get(type(op))
                if swap_cls is None:
                    continue
                op_name = type(op).__name__
                swap_name = swap_cls.__name__

                def _mutate_compare(
                    n: ast.Compare,
                    i: int = op_index,
                    cls: type = swap_cls,
                ) -> None:
                    n.ops[i] = cls()

                candidates.append((index, _mutate_compare, f"{op_name} -> {swap_name}"))
                break
        elif isinstance(node, ast.BoolOp):
            swap = ast.Or if isinstance(node.op, ast.And) else ast.And
            op_name = type(node.op).__name__
            swap_name = swap.__name__

            def _mutate_boolop(n: ast.BoolOp, cls: type = swap) -> None:
                n.op = cls()

            candidates.append((index, _mutate_boolop, f"{op_name} -> {swap_name}"))
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                swapped = not node.value

                def _mutate_bool(n: ast.Constant, value: bool = swapped) -> None:
                    n.value = value

                candidates.append((index, _mutate_bool, f"bool {node.value} -> {swapped}"))
            elif isinstance(node.value, int) and not isinstance(node.value, bool):
                swapped_int = node.value + 1

                def _mutate_int(n: ast.Constant, value: int = swapped_int) -> None:
                    n.value = value

                candidates.append((index, _mutate_int, f"int {node.value} -> {swapped_int}"))
        elif isinstance(node, ast.Return) and node.value is not None:

            def _mutate_return(n: ast.Return) -> None:
                n.value = ast.Constant(value=None)

            candidates.append((index, _mutate_return, "return value replaced with None"))

    return candidates
