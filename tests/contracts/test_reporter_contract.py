"""Behavioral contract for every registered reporter plugin."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeguardian.core.plugins import PluginRegistry

_REPORTER_NAMES = ("sarif",)


class _MockRunWithBlast:
    def __init__(self, blast_path: Path) -> None:
        self.blast_radius_path = str(blast_path)


class _MockRunEmpty:
    pass


@pytest.fixture(scope="module")
def registry() -> PluginRegistry:
    return PluginRegistry.discover_bundled()


@pytest.mark.parametrize("reporter_name", _REPORTER_NAMES)
def test_report_returns_non_empty_string(registry: PluginRegistry, reporter_name: str) -> None:
    barrier = registry.get_reporter(reporter_name)
    output = barrier.call("report", _MockRunEmpty())
    assert isinstance(output, str)
    assert len(output) > 20


def test_sarif_report_is_valid_sarif_210(registry: PluginRegistry, tmp_path) -> None:
    blast = tmp_path / "blast_radius.json"
    blast.write_text(
        json.dumps(
            {
                "contract_violations": [
                    {
                        "caller": "src/y.py:g",
                        "assumption": "f never returns None",
                        "risk": "warning",
                    }
                ],
                "coverage_gaps": [{"caller": "src/z.py:h", "reason": "no test covers this path"}],
            }
        )
    )
    barrier = registry.get_reporter("sarif")
    output = barrier.call("report", _MockRunWithBlast(blast))
    sarif = json.loads(output)

    assert sarif["version"] == "2.1.0"
    assert isinstance(sarif["runs"], list) and len(sarif["runs"]) == 1
    run = sarif["runs"][0]
    assert run["tool"]["driver"]["name"] == "codeguardian"
    assert isinstance(run["tool"]["driver"]["version"], str)
    assert isinstance(run["results"], list)
    assert len(run["results"]) == 2

    rule_ids = {result["ruleId"] for result in run["results"]}
    assert "contract-violation" in rule_ids
    assert "coverage-gap" in rule_ids

    for result in run["results"]:
        assert result["level"] in {"error", "warning", "note"}
        assert isinstance(result["message"]["text"], str)
        assert isinstance(result["locations"], list)


def test_sarif_report_empty_run_is_valid(registry: PluginRegistry) -> None:
    barrier = registry.get_reporter("sarif")
    output = barrier.call("report", _MockRunEmpty())
    sarif = json.loads(output)
    assert sarif["version"] == "2.1.0"
    assert sarif["runs"][0]["results"] == []


def test_sarif_reporter_format_attribute(registry: PluginRegistry) -> None:
    plugin = registry.get_reporter("sarif").plugin
    assert plugin.format == "sarif"
    assert plugin.name == "sarif"
