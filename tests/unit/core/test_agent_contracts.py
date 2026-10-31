"""Contract tests for the nine agent classes.

Each agent declares five class attributes that Phase 13 reads:
is_deterministic, requires_tools, can_refuse, prompt_role,
prompt_version. This file verifies the declarations exist, are
consistent with each other, and match the agent's actual tool use.

Tool-use verification is static. The source of each agent module is
parsed and every call of the form
``_call_tool(<anything>, "<literal>", ...)`` or
``fresh_barrier(<anything>, "<literal>", ...)`` is recorded. The set
of called plugin names must equal the set of declared tools. Any
divergence fails, whether it is a call without a declaration or a
declaration without a call.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from types import ModuleType

import pytest

from codeguardian.core.agents import (
    AnalystAgent,
    BlastAgent,
    ContractVerifier,
    CorrectnessVerifier,
    ReconAgent,
    SecurityVerifier,
    SkillCuratorAgent,
    TesterAgent,
    WriterAgent,
)
from codeguardian.core.agents import analyst as analyst_module
from codeguardian.core.agents import blast as blast_module
from codeguardian.core.agents import contract_verifier as contract_module
from codeguardian.core.agents import correctness_verifier as correctness_module
from codeguardian.core.agents import recon as recon_module
from codeguardian.core.agents import security_verifier as security_module
from codeguardian.core.agents import skill_curator as skill_module
from codeguardian.core.agents import tester as tester_module
from codeguardian.core.agents import writer as writer_module
from codeguardian.core.plugins import PluginRegistry

_ALL_AGENTS: tuple[tuple[type, ModuleType, str], ...] = (
    (ReconAgent, recon_module, "recon"),
    (BlastAgent, blast_module, "blast"),
    (AnalystAgent, analyst_module, "analyst"),
    (TesterAgent, tester_module, "tester"),
    (WriterAgent, writer_module, "writer"),
    (CorrectnessVerifier, correctness_module, "correctness_verifier"),
    (SecurityVerifier, security_module, "security_verifier"),
    (ContractVerifier, contract_module, "contract_verifier"),
    (SkillCuratorAgent, skill_module, "skill_curator"),
)

_REQUIRED_ATTRIBUTES: tuple[str, ...] = (
    "is_deterministic",
    "requires_tools",
    "can_refuse",
    "prompt_role",
    "prompt_version",
)


@pytest.fixture(scope="module")
def registry() -> PluginRegistry:
    return PluginRegistry.discover_bundled()


@pytest.mark.parametrize("agent_class,module,expected_name", _ALL_AGENTS)
class TestAgentAttributes:
    def test_name_attribute(self, agent_class, module, expected_name) -> None:
        assert agent_class.name == expected_name

    def test_phase_attribute(self, agent_class, module, expected_name) -> None:
        assert agent_class.phase == expected_name

    def test_declares_all_required_attributes(self, agent_class, module, expected_name) -> None:
        for attr in _REQUIRED_ATTRIBUTES:
            assert hasattr(agent_class, attr), f"{expected_name} missing {attr}"

    def test_is_deterministic_is_bool(self, agent_class, module, expected_name) -> None:
        assert isinstance(agent_class.is_deterministic, bool)

    def test_requires_tools_is_tuple_of_str(self, agent_class, module, expected_name) -> None:
        tools = agent_class.requires_tools
        assert isinstance(tools, tuple), (expected_name, tools)
        for name in tools:
            assert isinstance(name, str), (expected_name, name)

    def test_can_refuse_is_bool(self, agent_class, module, expected_name) -> None:
        assert isinstance(agent_class.can_refuse, bool)

    def test_prompt_role_consistency(self, agent_class, module, expected_name) -> None:
        role = agent_class.prompt_role
        version = agent_class.prompt_version
        if agent_class.is_deterministic:
            assert role is None, (expected_name, role)
            assert version is None, (expected_name, version)
        else:
            assert role == expected_name, (expected_name, role)
            assert isinstance(version, str), (expected_name, version)
            assert version.count(".") == 2, (expected_name, version)

    def test_deterministic_agents_declare_no_tools(
        self, agent_class, module, expected_name
    ) -> None:
        if agent_class.is_deterministic:
            assert agent_class.requires_tools == (), (expected_name, agent_class.requires_tools)


@pytest.mark.parametrize("agent_class,module,expected_name", _ALL_AGENTS)
class TestToolsAreRegistered:
    def test_every_declared_tool_is_a_bundled_plugin(
        self, agent_class, module, expected_name, registry
    ) -> None:
        for plugin_name in agent_class.requires_tools:
            record = registry.get_registered(plugin_name, "context_provider")
            assert record.name == plugin_name

    def test_every_declared_plugin_has_provide_method(
        self, agent_class, module, expected_name, registry
    ) -> None:
        for plugin_name in agent_class.requires_tools:
            record = registry.get_registered(plugin_name, "context_provider")
            assert callable(getattr(record.plugin, "provide", None)), (
                f"{plugin_name} missing callable 'provide'"
            )


@pytest.mark.parametrize("agent_class,module,expected_name", _ALL_AGENTS)
class TestDeclarationMatchesBehavior:
    def test_no_undeclared_plugin_calls(self, agent_class, module, expected_name) -> None:
        declared = set(agent_class.requires_tools)
        called = _static_plugin_calls(module)
        extra = called - declared
        assert not extra, f"{expected_name} calls plugins not in requires_tools: {sorted(extra)}"

    def test_no_dead_tool_declarations(self, agent_class, module, expected_name) -> None:
        declared = set(agent_class.requires_tools)
        called = _static_plugin_calls(module)
        missing = declared - called
        assert not missing, f"{expected_name} declares tools it never calls: {sorted(missing)}"


def _static_plugin_calls(module: ModuleType) -> set[str]:
    """Return plugin names called via ``_call_tool`` or ``fresh_barrier``.

    Scans the module source and records every literal string passed as
    the second positional argument to a call whose function is named
    ``_call_tool`` or ``fresh_barrier``. Calls where the second
    argument is a variable are not detected. The current agent code
    always uses literals; a future change that introduces indirection
    would need this function updated and would weaken the contract.
    """
    source = inspect.getsource(module)
    tree = ast.parse(textwrap.dedent(source))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Name):
            continue
        if func.id not in ("_call_tool", "fresh_barrier"):
            continue
        if len(node.args) < 2:
            continue
        arg = node.args[1]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            names.add(arg.value)
    return names
