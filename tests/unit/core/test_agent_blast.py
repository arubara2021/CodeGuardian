"""Tests for the blast agent."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("codeguardian._native")

from codeguardian import _native
from codeguardian.core.agents.blast import BlastAgent, BlastInput

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
