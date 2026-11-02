"""Tests for the blast agent."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("codeguardian._native")

from codeguardian import _native
from codeguardian.core.agents.blast import (
    BlastAgent,
    BlastInput,
    _extract_contract_assertions,
)

_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "blast-radius.json"


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _build_cpg(source: str, path: str = "main.py") -> bytes:
    return _native.build_graph([(path, "python", source)])


_SIMPLE_SOURCE = "def helper(x):\n    return x * 2\n\ndef caller():\n    return helper(5)\n"


_ORPHAN_SOURCE = "def orphan_fn():\n    return 42\n"


async def test_artifact_validates_against_schema(tmp_path: Path, schema: dict) -> None:
    import jsonschema

    cpg_bytes = _build_cpg(_SIMPLE_SOURCE)
    agent = BlastAgent()
    result = await agent.run(
        BlastInput(
            cpg_bytes=cpg_bytes,
            target_path="main.py",
            target_qualified_name="helper",
            clone_root=tmp_path,
        )
    )
    jsonschema.validate(result.to_artifact(), schema)


async def test_direct_callers_detected(tmp_path: Path) -> None:
    cpg_bytes = _build_cpg(_SIMPLE_SOURCE)
    agent = BlastAgent()
    result = await agent.run(
        BlastInput(
            cpg_bytes=cpg_bytes,
            target_path="main.py",
            target_qualified_name="helper",
            clone_root=tmp_path,
        )
    )
    assert len(result.direct_callers) == 1
    assert result.direct_callers[0]["qualified_name"] == "caller"


async def test_ranked_callers_sum_at_least_score(tmp_path: Path) -> None:
    cpg_bytes = _build_cpg(_SIMPLE_SOURCE)
    agent = BlastAgent()
    result = await agent.run(
        BlastInput(
            cpg_bytes=cpg_bytes,
            target_path="main.py",
            target_qualified_name="helper",
            clone_root=tmp_path,
        )
    )
    total = sum(c.contribution for c in result.ranked_callers)
    assert total >= result.blast_score
    assert all(c.contribution > 0 for c in result.ranked_callers)


async def test_recommendation_band_is_valid(tmp_path: Path) -> None:
    cpg_bytes = _build_cpg(_SIMPLE_SOURCE)
    agent = BlastAgent()
    result = await agent.run(
        BlastInput(
            cpg_bytes=cpg_bytes,
            target_path="main.py",
            target_qualified_name="helper",
            clone_root=tmp_path,
        )
    )
    assert result.recommendation_raw in {"proceed", "review", "block"}
    assert result.recommendation_adjusted in {"proceed", "review", "block"}


async def test_calibration_shift_on_incomplete_search() -> None:
    cpg_bytes = _build_cpg(_ORPHAN_SOURCE)
    agent = BlastAgent()
    result = await agent.run(
        BlastInput(
            cpg_bytes=cpg_bytes,
            target_path="main.py",
            target_qualified_name="orphan_fn",
            clone_root=None,
        )
    )
    assert result.caller_search_complete is False
    assert result.recommendation_raw == "proceed"
    assert result.recommendation_adjusted == "review"
    assert result.calibration_reason is not None


async def test_no_calibration_when_search_complete(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text(_SIMPLE_SOURCE, encoding="utf-8")
    cpg_bytes = _build_cpg(_SIMPLE_SOURCE)
    agent = BlastAgent()
    result = await agent.run(
        BlastInput(
            cpg_bytes=cpg_bytes,
            target_path="main.py",
            target_qualified_name="helper",
            clone_root=tmp_path,
        )
    )
    assert result.caller_search_complete is True
    assert result.recommendation_raw == result.recommendation_adjusted
    assert result.calibration_reason is None


async def test_contract_assertions_have_test_expression(tmp_path: Path) -> None:
    """When violations exist, each ContractAssertion has a test_expression.

    The fixture does not guarantee violations under the current narrow
    detector; this test only verifies the property when they occur.
    """
    cpg_bytes = _build_cpg(_SIMPLE_SOURCE)
    agent = BlastAgent()
    result = await agent.run(
        BlastInput(
            cpg_bytes=cpg_bytes,
            target_path="main.py",
            target_qualified_name="helper",
            clone_root=tmp_path,
        )
    )
    for assertion in result.contract_assertions:
        assert assertion.test_expression is not None
        assert "assert" in assertion.test_expression


async def test_target_not_found_raises(tmp_path: Path) -> None:
    from codeguardian.core.errors import CodeGuardianError

    cpg_bytes = _build_cpg(_SIMPLE_SOURCE)
    agent = BlastAgent()
    with pytest.raises(CodeGuardianError):
        await agent.run(
            BlastInput(
                cpg_bytes=cpg_bytes,
                target_path="main.py",
                target_qualified_name="nonexistent",
                clone_root=tmp_path,
            )
        )


class TestCallSiteExtraction:
    async def test_call_expression_and_argument_map_populated(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text(_SIMPLE_SOURCE, encoding="utf-8")
        cpg_bytes = _build_cpg(_SIMPLE_SOURCE)
        result = await BlastAgent().run(
            BlastInput(
                cpg_bytes=cpg_bytes,
                target_path="main.py",
                target_qualified_name="helper",
                clone_root=tmp_path,
            )
        )
        caller = next(c for c in result.ranked_callers if c.qualified_name == "caller")
        assert caller.call_expression is not None
        assert "helper(5)" in caller.call_expression
        assert caller.argument_map == {"x": "5"}
        assert caller.depth == 1

    async def test_keyword_args_extracted(self, tmp_path: Path) -> None:
        source = "def helper(x, y):\n    return x + y\n\ndef caller():\n    return helper(1, y=2)\n"
        (tmp_path / "main.py").write_text(source, encoding="utf-8")
        cpg_bytes = _build_cpg(source)
        result = await BlastAgent().run(
            BlastInput(
                cpg_bytes=cpg_bytes,
                target_path="main.py",
                target_qualified_name="helper",
                clone_root=tmp_path,
            )
        )
        caller = next(c for c in result.ranked_callers if c.qualified_name == "caller")
        assert caller.argument_map == {"x": "1", "y": "2"}

    async def test_no_clone_root_leaves_call_expression_none(self) -> None:
        cpg_bytes = _build_cpg(_SIMPLE_SOURCE)
        result = await BlastAgent().run(
            BlastInput(
                cpg_bytes=cpg_bytes,
                target_path="main.py",
                target_qualified_name="helper",
                clone_root=None,
            )
        )
        for caller in result.ranked_callers:
            assert caller.call_expression is None
            assert caller.argument_map is None

    async def test_transitive_path_is_empty(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text(_SIMPLE_SOURCE, encoding="utf-8")
        cpg_bytes = _build_cpg(_SIMPLE_SOURCE)
        result = await BlastAgent().run(
            BlastInput(
                cpg_bytes=cpg_bytes,
                target_path="main.py",
                target_qualified_name="helper",
                clone_root=tmp_path,
            )
        )
        for caller in result.ranked_callers:
            assert caller.transitive_path == ()


class TestPublicApiDetection:
    async def test_dunder_all_includes_target(self, tmp_path: Path) -> None:
        source = '__all__ = ["helper"]\n\n\ndef helper():\n    return 1\n'
        (tmp_path / "main.py").write_text(source, encoding="utf-8")
        cpg_bytes = _build_cpg(source)
        result = await BlastAgent().run(
            BlastInput(
                cpg_bytes=cpg_bytes,
                target_path="main.py",
                target_qualified_name="helper",
                clone_root=tmp_path,
            )
        )
        assert result.target_public_api is True

    async def test_dunder_all_excludes_target(self, tmp_path: Path) -> None:
        source = '__all__ = ["other"]\n\n\ndef helper():\n    return 1\n'
        (tmp_path / "main.py").write_text(source, encoding="utf-8")
        cpg_bytes = _build_cpg(source)
        result = await BlastAgent().run(
            BlastInput(
                cpg_bytes=cpg_bytes,
                target_path="main.py",
                target_qualified_name="helper",
                clone_root=tmp_path,
            )
        )
        assert result.target_public_api is False

    async def test_underscore_prefix_is_private(self, tmp_path: Path) -> None:
        source = "def _helper():\n    return 1\n"
        (tmp_path / "main.py").write_text(source, encoding="utf-8")
        cpg_bytes = _build_cpg(source)
        result = await BlastAgent().run(
            BlastInput(
                cpg_bytes=cpg_bytes,
                target_path="main.py",
                target_qualified_name="_helper",
                clone_root=tmp_path,
            )
        )
        assert result.target_public_api is False

    async def test_no_dunder_all_public_by_convention(self, tmp_path: Path) -> None:
        source = "def helper():\n    return 1\n"
        (tmp_path / "main.py").write_text(source, encoding="utf-8")
        cpg_bytes = _build_cpg(source)
        result = await BlastAgent().run(
            BlastInput(
                cpg_bytes=cpg_bytes,
                target_path="main.py",
                target_qualified_name="helper",
                clone_root=tmp_path,
            )
        )
        assert result.target_public_api is True

    async def test_no_clone_root_is_not_public(self) -> None:
        cpg_bytes = _build_cpg(_SIMPLE_SOURCE)
        result = await BlastAgent().run(
            BlastInput(
                cpg_bytes=cpg_bytes,
                target_path="main.py",
                target_qualified_name="helper",
                clone_root=None,
            )
        )
        assert result.target_public_api is False


class TestUsageAwareAssertions:
    def _violation(
        self,
        *,
        caller_path: str = "caller.py",
        caller_line: int = 2,
        assumption: str = "returns non-None value",
        risk: str = "warning",
    ) -> dict:
        return {
            "caller": "caller",
            "path": caller_path,
            "line": caller_line,
            "assumption": assumption,
            "risk": risk,
        }

    def test_subscript_usage_produces_len_guard(self, tmp_path: Path) -> None:
        (tmp_path / "caller.py").write_text(
            "def caller():\n    return helper(5)[0]\n", encoding="utf-8"
        )
        assertions = _extract_contract_assertions(
            (self._violation(),),
            clone_root=tmp_path,
            target_qualified_name="helper",
        )
        assert len(assertions) == 1
        assert "len(" in assertions[0].test_expression
        assert "is not None" in assertions[0].test_expression

    def test_attribute_usage_produces_hasattr(self, tmp_path: Path) -> None:
        (tmp_path / "caller.py").write_text(
            "def caller():\n    return helper(5).name\n", encoding="utf-8"
        )
        assertions = _extract_contract_assertions(
            (self._violation(),),
            clone_root=tmp_path,
            target_qualified_name="helper",
        )
        assert len(assertions) == 1
        assert "hasattr" in assertions[0].test_expression
        assert "'name'" in assertions[0].test_expression

    def test_method_usage_produces_hasattr(self, tmp_path: Path) -> None:
        (tmp_path / "caller.py").write_text(
            "def caller():\n    return helper(5).read()\n", encoding="utf-8"
        )
        assertions = _extract_contract_assertions(
            (self._violation(),),
            clone_root=tmp_path,
            target_qualified_name="helper",
        )
        assert "hasattr" in assertions[0].test_expression
        assert "'read'" in assertions[0].test_expression

    def test_len_usage_produces_len_dunder(self, tmp_path: Path) -> None:
        (tmp_path / "caller.py").write_text(
            "def caller():\n    return len(helper(5))\n", encoding="utf-8"
        )
        assertions = _extract_contract_assertions(
            (self._violation(),),
            clone_root=tmp_path,
            target_qualified_name="helper",
        )
        assert "'__len__'" in assertions[0].test_expression

    def test_iter_usage_produces_iter_dunder(self, tmp_path: Path) -> None:
        (tmp_path / "caller.py").write_text(
            "def caller():\n    return iter(helper(5))\n", encoding="utf-8"
        )
        assertions = _extract_contract_assertions(
            (self._violation(),),
            clone_root=tmp_path,
            target_qualified_name="helper",
        )
        assert "'__iter__'" in assertions[0].test_expression

    def test_fallback_when_no_clone_root(self) -> None:
        assertions = _extract_contract_assertions(
            (self._violation(),),
            clone_root=None,
            target_qualified_name="helper",
        )
        assert len(assertions) == 1
        assert assertions[0].test_expression.startswith("assert ")
        assert "is not None" in assertions[0].test_expression

    def test_fallback_when_usage_not_recognized(self, tmp_path: Path) -> None:
        (tmp_path / "caller.py").write_text(
            "def caller():\n    x = helper(5)\n    return x\n", encoding="utf-8"
        )
        assertions = _extract_contract_assertions(
            (self._violation(),),
            clone_root=tmp_path,
            target_qualified_name="helper",
        )
        assert assertions[0].test_expression == "assert result is not None"

    def test_fallback_when_caller_file_missing(self, tmp_path: Path) -> None:
        assertions = _extract_contract_assertions(
            (self._violation(caller_path="does_not_exist.py"),),
            clone_root=tmp_path,
            target_qualified_name="helper",
        )
        assert "is not None" in assertions[0].test_expression
