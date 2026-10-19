"""SARIF 2.1.0 reporter plugin.

Builds a SARIF document from a run's blast radius findings. Contract
violations become ``warning`` results; coverage gaps become ``note``
results. The reporter reads the run's blast radius artifact if present
and produces a valid SARIF document with an empty results array
otherwise.

The reporter accepts any object whose ``blast_radius_path`` attribute
(or whose ``artifacts['blast_radius.json']`` entry) is a path to a
blast radius JSON file. This keeps the reporter decoupled from the
``RunResult`` dataclass shape, which lives in Phase 4 and is not
introspected here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["SarifReporterPlugin"]

_SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"

_RISK_TO_LEVEL = {
    "critical": "error",
    "warning": "warning",
    "info": "note",
}


class SarifReporterPlugin:
    """Renders a run as a SARIF 2.1.0 document."""

    name = "sarif"
    format = "sarif"

    __manifest__ = {
        "name": "sarif",
        "version": "1.0.0",
        "type": "reporter",
        "interface_version": "1.0",
        "description": "SARIF 2.1.0 reporter.",
        "capabilities": ["sarif", "github-code-scanning"],
        "permissions": {
            "filesystem": "read_only",
            "network": False,
            "process_spawn": False,
        },
    }

    def report(self, run_result: Any) -> str:
        tool_version = _codeguardian_version()
        blast = _load_blast(run_result)

        results: list[dict[str, Any]] = []
        if blast is not None:
            results.extend(_violations_to_sarif(blast))
            results.extend(_coverage_gaps_to_sarif(blast))

        sarif = {
            "$schema": _SARIF_SCHEMA,
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "name": "codeguardian",
                            "version": tool_version,
                            "informationUri": "https://codeguardian.dev/",
                        }
                    },
                    "results": results,
                }
            ],
        }
        return json.dumps(sarif, indent=2, sort_keys=True)


def _codeguardian_version() -> str:
    try:
        from codeguardian.version import get_version

        return get_version()
    except Exception:
        return "0.0.0"


def _load_blast(run_result: Any) -> dict[str, Any] | None:
    path = _resolve_blast_path(run_result)
    if path is None:
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _resolve_blast_path(run_result: Any) -> Path | None:
    direct = getattr(run_result, "blast_radius_path", None)
    if isinstance(direct, (str, Path)):
        return Path(direct)

    artifacts = getattr(run_result, "artifacts", None)
    if isinstance(artifacts, dict):
        entry = artifacts.get("blast_radius.json") or artifacts.get("blast_radius")
        if isinstance(entry, (str, Path)):
            return Path(entry)
    return None


def _violations_to_sarif(blast: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    violations = blast.get("contract_violations")
    if not isinstance(violations, list):
        return results
    for violation in violations:
        if not isinstance(violation, dict):
            continue
        caller = violation.get("caller") or "<unknown>"
        assumption = violation.get("assumption") or "contract violation"
        risk = violation.get("risk", "warning")
        level = _RISK_TO_LEVEL.get(risk, "warning")
        results.append(
            {
                "ruleId": "contract-violation",
                "level": level,
                "message": {"text": str(assumption)},
                "locations": [{"physicalLocation": {"artifactLocation": {"uri": str(caller)}}}],
                "properties": {"risk": str(risk)},
            }
        )
    return results


def _coverage_gaps_to_sarif(blast: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    gaps = blast.get("coverage_gaps")
    if not isinstance(gaps, list):
        return results
    for gap in gaps:
        if not isinstance(gap, dict):
            continue
        caller = gap.get("caller") or "<unknown>"
        reason = gap.get("reason") or "no test coverage"
        results.append(
            {
                "ruleId": "coverage-gap",
                "level": "note",
                "message": {"text": str(reason)},
                "locations": [{"physicalLocation": {"artifactLocation": {"uri": str(caller)}}}],
            }
        )
    return results
