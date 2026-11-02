"""Security verifier agent.

Receives the diff and, when a sandbox is supplied, the path of the
applied file inside that sandbox. The verifier layers three
deterministic pre-scan passes and one model pass:

- A regex pre-scan over the diff, unchanged from the previous block.
- A taint pre-scan over the diff for five CWEs: SQL injection, command
  injection, path traversal, unsafe deserialization, and XSS. Each
  finding carries a CWE identifier and a severity. The check is
  heuristic, not a full data-flow analysis; findings are leads, not
  proofs.
- Two external scanners, semgrep and bandit, run against the applied
  file when both are installed in the sandbox. Their JSON output is
  parsed into the same ExternalFinding shape used by the taint scan,
  so severity and CWE handling are uniform.

The model receives every finding the pre-scan produces, plus the
original security signals if the caller supplied any. When the model
returns pass but a critical or high finding exists, the evidence
overrides the verdict. Medium and low findings do not override on
their own; the model decides those.

For the first critical or high finding, the verifier also generates a
PoC test that documents the payload that would trigger the issue. The
PoC is attached to the result and returned to the caller. It is not
executed by the verifier; the caller decides whether to run it.

Independence. The verifier does not see the correctness test result,
the original source, or the other verifiers' evidence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.plugins import BudgetTracker
from codeguardian.core.prompts import VerdictOutput, render_prompt, validate_output
from codeguardian.interfaces.model import ModelOptions, ModelProvider

from ._runtime import tracked_model_call
from ._verifier_common import (
    SandboxExecutor,
    apply_evidence,
    apply_veto,
    assemble_annotated_diff,
    compute_blind_prediction,
    diff_stats,
    parse_and_strip,
    schema_refusal_verdict,
)
from .telemetry import EvidenceReceipt, record_call

__all__ = [
    "ExternalFinding",
    "SecurityPoC",
    "SecurityVerifier",
    "SecurityVerifierInput",
    "SecurityVerifierResult",
]


_SEVERITY_ORDER: dict[str, int] = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
    "info": 0,
}
_OVERRIDE_SEVERITIES = frozenset({"critical", "high"})

_SECURITY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("eval_call", re.compile(r"\beval\s*\(")),
    ("exec_call", re.compile(r"\bexec\s*\(")),
    ("shell_true", re.compile(r"subprocess\.[\w.]+\s*\([^)]*shell\s*=\s*True")),
    ("os_system", re.compile(r"\bos\.system\s*\(")),
    ("sql_fstring", re.compile(r"\.execute\s*\(\s*f[\"']")),
    ("sql_format", re.compile(r"\.execute\s*\([^)]*%\s*[\(\w]")),
    ("pickle_loads", re.compile(r"\bpickle\.loads\s*\(")),
    ("weak_hash", re.compile(r"\b(?:md5|sha1)\s*\(")),
    ("weak_cipher", re.compile(r"\b(?:DES|RC4)\b")),
    (
        "secret_literal",
        re.compile(
            r"(?:api[_-]?key|password|secret|token)\s*=\s*"
            r"[\"'][A-Za-z0-9_+/=\-]{16,}[\"']"
        ),
    ),
)

_CWE_BY_PATTERN: dict[str, str] = {
    "eval_call": "CWE-95",
    "exec_call": "CWE-95",
    "shell_true": "CWE-78",
    "os_system": "CWE-78",
    "sql_fstring": "CWE-89",
    "sql_format": "CWE-89",
    "pickle_loads": "CWE-502",
    "weak_hash": "CWE-327",
    "weak_cipher": "CWE-327",
    "secret_literal": "CWE-798",
}

_SEMGREP_SEVERITY_MAP: dict[str, str] = {
    "ERROR": "high",
    "WARNING": "medium",
    "INFO": "info",
}

_BANDIT_SEVERITY_MAP: dict[str, str] = {
    "HIGH": "high",
    "MEDIUM": "medium",
    "LOW": "low",
}

_CWE_SQLI = "CWE-89"
_CWE_CMD = "CWE-78"
_CWE_PATH = "CWE-22"
_CWE_DESER = "CWE-502"
_CWE_XSS = "CWE-79"


@dataclass(frozen=True, slots=True)
class ExternalFinding:
    """A security finding from any deterministic source.

    Attributes:
        cwe_id: The CWE identifier, for example "CWE-89", or None when
            the source did not provide one.
        severity: One of "critical", "high", "medium", "low", "info".
        source_tool: "semgrep", "bandit", "taint", or "regex".
        line: The 1-based line number of the finding, or None when the
            source did not provide one.
        snippet: A short excerpt of the offending code.
        message: A human-readable description of the issue.
    """

    cwe_id: str | None
    severity: str
    source_tool: str
    line: int | None
    snippet: str
    message: str


@dataclass(frozen=True, slots=True)
class SecurityPoC:
    """A generated proof-of-concept test for a security finding.

    Attributes:
        cwe_id: The CWE the PoC demonstrates.
        severity: The finding severity.
        test_source: A runnable pytest file, or a template if the
            target's argument shape is unknown.
        explanation: A one-paragraph note describing what the payload
            exercises and what a fix would look like.
    """

    cwe_id: str
    severity: str
    test_source: str
    explanation: str


@dataclass(frozen=True, slots=True)
class SecurityVerifierInput:
    """Input to the security verifier."""

    __test__: ClassVar[bool] = False

    diff: str
    change_kind: str
    blast_score: int
    original_security_signals: tuple[str, ...]
    provider: ModelProvider
    model: str
    budget: BudgetTracker
    clone_root: Path
    target_symbol: str = ""
    prompt_version: str = "1.0.0"
    sandbox_executor: SandboxExecutor | None = None
    target_file_path: str | None = None


@dataclass(frozen=True, slots=True)
class SecurityVerifierResult:
    """Output of the security verifier."""

    verdict: VerdictOutput
    verdict_reason: str | None
    pre_scan: dict[str, Any]
    pre_scan_conflict: bool
    refusal_code: str | None = None
    is_refusal: bool = False
    evidence_receipt: EvidenceReceipt | None = None
    external_findings: tuple[ExternalFinding, ...] = ()
    taint_findings: tuple[ExternalFinding, ...] = ()
    poc: SecurityPoC | None = None
    evidence_conflict: bool = False
    blind_prediction: str | None = None
    informed_verdict: str | None = None

    @property
    def disagreement(self) -> bool:
        """True when the blind prediction and the informed verdict differ."""
        if self.blind_prediction is None or self.informed_verdict is None:
            return False
        return self.blind_prediction != self.informed_verdict


class SecurityVerifier:
    """Model-backed security verifier."""

    name = "security_verifier"
    phase = "security_verifier"
    is_deterministic = False
    requires_tools: tuple[str, ...] = ()
    can_refuse = True
    prompt_role = "security_verifier"
    prompt_version = "1.0.0"

    async def run(self, input: SecurityVerifierInput) -> SecurityVerifierResult:
        """Run the security verifier."""
        pre_scan = _compute_pre_scan(input)
        annotated_diff = _annotate_diff(input.diff, pre_scan, input.original_security_signals)

        variables = {
            "change_kind": input.change_kind,
            "blast_score": str(input.blast_score),
            "diff": annotated_diff,
        }
        prompt_result = render_prompt("security_verifier", input.prompt_version, variables)

        response, latency_ms = await tracked_model_call(
            input.provider,
            prompt_result.rendered,
            input.model,
            ModelOptions(),
            "security_verifier",
        )

        parsed, refusal_code, reason_error = parse_and_strip(response.text)
        if reason_error is not None:
            _record(
                input,
                prompt_result.rendered,
                response,
                latency_ms,
                outcome="schema_violation",
                failure_category="schema",
            )
            return _schema_refusal(reason_error, pre_scan)

        try:
            validated = validate_output("security_verifier", parsed)
        except CodeGuardianError as exc:
            _record(
                input,
                prompt_result.rendered,
                response,
                latency_ms,
                outcome="schema_violation",
                failure_category="schema",
            )
            return _schema_refusal(f"schema: {exc}", pre_scan)

        if not isinstance(validated, VerdictOutput):
            _record(
                input,
                prompt_result.rendered,
                response,
                latency_ms,
                outcome="schema_violation",
                failure_category="schema",
            )
            return _schema_refusal("unexpected output type", pre_scan)

        informed_verdict = validated.verdict
        pre_scan_conflict = validated.verdict == "pass" and len(pre_scan.get("matches", [])) > 0
        evidence_passed = _evidence_passed(pre_scan)
        final_verdict, evidence_conflict = apply_evidence(validated, evidence_passed)
        final_verdict = apply_veto(final_verdict, pre_scan_conflict)

        verdict_reason: str | None = None
        if final_verdict.verdict == "fail":
            verdict_reason = "vulnerability_introduced"

        receipt = _build_evidence_receipt(input, pre_scan)
        poc = _generate_poc(pre_scan, input)

        _record(
            input,
            prompt_result.rendered,
            response,
            latency_ms,
            outcome="success",
            failure_category=None,
            verdict_reason=verdict_reason,
            evidence_receipt=receipt,
        )

        return SecurityVerifierResult(
            verdict=final_verdict,
            verdict_reason=verdict_reason,
            pre_scan=pre_scan,
            pre_scan_conflict=pre_scan_conflict,
            refusal_code=refusal_code,
            is_refusal=final_verdict.verdict == "refused",
            evidence_receipt=receipt,
            external_findings=tuple(pre_scan.get("external_findings", ())),
            taint_findings=tuple(pre_scan.get("taint_findings", ())),
            poc=poc,
            evidence_conflict=evidence_conflict,
            blind_prediction=compute_blind_prediction(
                original_security_signals=input.original_security_signals,
            ),
            informed_verdict=informed_verdict,
        )


def _compute_pre_scan(input: SecurityVerifierInput) -> dict[str, Any]:
    """Return the combined deterministic pre-scan."""
    stats = diff_stats(input.diff)
    matches = _regex_pre_scan(input.diff)
    external = (
        _run_external_scanners(input.sandbox_executor, input.target_file_path)
        if input.sandbox_executor is not None and input.target_file_path is not None
        else []
    )
    taint = _taint_pre_scan(input.diff)
    return {
        "diff_stats": stats,
        "matches": matches,
        "external_findings": external,
        "taint_findings": taint,
    }


def _regex_pre_scan(diff: str) -> list[dict[str, Any]]:
    """Run the regex pattern list over added lines of the diff."""
    matches: list[dict[str, Any]] = []
    line_no = 0
    for raw in diff.splitlines():
        line_no += 1
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        content = raw[1:]
        for pattern_id, pattern in _SECURITY_PATTERNS:
            if pattern.search(content):
                matches.append(
                    {
                        "id": pattern_id,
                        "cwe_id": _CWE_BY_PATTERN.get(pattern_id),
                        "line": line_no,
                        "snippet": content.strip()[:120],
                    }
                )
    return matches


def _run_external_scanners(
    executor: SandboxExecutor,
    target_file_path: str,
) -> list[ExternalFinding]:
    """Run semgrep and bandit against the file; parse their outputs.

    Each scanner is attempted only when ``which <scanner>`` succeeds.
    A scanner that is absent, times out, or returns an unparseable
    output contributes nothing; the regex pre-scan remains the
    fallback.
    """
    findings: list[ExternalFinding] = []
    findings.extend(_run_semgrep(executor, target_file_path))
    findings.extend(_run_bandit(executor, target_file_path))
    return findings


def _binary_present(executor: SandboxExecutor, name: str) -> bool:
    """Return True when the sandbox has ``name`` on PATH."""
    try:
        outcome = executor.run(["which", name], timeout_s=5.0)
    except Exception:
        return False
    return outcome.exit_code == 0


def _run_semgrep(
    executor: SandboxExecutor,
    target_file_path: str,
) -> list[ExternalFinding]:
    """Run semgrep on the file and parse its JSON output."""
    if not _binary_present(executor, "semgrep"):
        return []
    try:
        outcome = executor.run(
            [
                "semgrep",
                "--config=p/security-audit",
                "--json",
                "--quiet",
                "--no-git-ignore",
                target_file_path,
            ],
            timeout_s=120.0,
        )
    except Exception:
        return []
    if not outcome.stdout:
        return []
    return _parse_semgrep_json(outcome.stdout)


def _parse_semgrep_json(text: str) -> list[ExternalFinding]:
    """Parse semgrep --json output into ExternalFinding objects."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    results = data.get("results")
    if not isinstance(results, list):
        return []

    findings: list[ExternalFinding] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        extra = entry.get("extra") if isinstance(entry.get("extra"), dict) else {}
        metadata = extra.get("metadata") if isinstance(extra.get("metadata"), dict) else {}
        cwe_list = metadata.get("cwe")
        cwe_id = _first_cwe(cwe_list) if isinstance(cwe_list, list) else None
        severity_raw = str(extra.get("severity", "WARNING")).upper()
        severity = _SEMGREP_SEVERITY_MAP.get(severity_raw, "medium")
        message = str(extra.get("message", "")).strip()[:200]
        snippet = str(extra.get("lines", "")).strip()[:160]
        line = None
        start = entry.get("start")
        if isinstance(start, dict) and isinstance(start.get("line"), int):
            line = start["line"]
        findings.append(
            ExternalFinding(
                cwe_id=cwe_id,
                severity=severity,
                source_tool="semgrep",
                line=line,
                snippet=snippet,
                message=message or "semgrep finding",
            )
        )
    return findings


def _first_cwe(cwe_list: list[Any]) -> str | None:
    """Return the first CWE identifier from a semgrep metadata list."""
    for item in cwe_list:
        if isinstance(item, str):
            match = re.search(r"CWE-(\d+)", item)
            if match:
                return f"CWE-{match.group(1)}"
    return None


def _run_bandit(
    executor: SandboxExecutor,
    target_file_path: str,
) -> list[ExternalFinding]:
    """Run bandit on the file and parse its JSON output."""
    if not _binary_present(executor, "bandit"):
        return []
    try:
        outcome = executor.run(
            ["bandit", "-f", "json", "-q", target_file_path],
            timeout_s=120.0,
        )
    except Exception:
        return []
    if not outcome.stdout:
        return []
    return _parse_bandit_json(outcome.stdout)


def _parse_bandit_json(text: str) -> list[ExternalFinding]:
    """Parse bandit -f json output into ExternalFinding objects."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    results = data.get("results")
    if not isinstance(results, list):
        return []

    findings: list[ExternalFinding] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        severity_raw = str(entry.get("issue_severity", "MEDIUM")).upper()
        severity = _BANDIT_SEVERITY_MAP.get(severity_raw, "medium")
        cwe_id = None
        issue_cwe = entry.get("issue_cwe")
        if isinstance(issue_cwe, dict) and isinstance(issue_cwe.get("id"), int):
            cwe_id = f"CWE-{issue_cwe['id']}"
        message = str(entry.get("issue_text", "")).strip()[:200]
        line = entry.get("line_number") if isinstance(entry.get("line_number"), int) else None
        code = entry.get("code")
        snippet = ""
        if isinstance(code, str):
            snippet = code.strip().splitlines()[0][:160] if code.strip() else ""
        findings.append(
            ExternalFinding(
                cwe_id=cwe_id,
                severity=severity,
                source_tool="bandit",
                line=line,
                snippet=snippet,
                message=message or "bandit finding",
            )
        )
    return findings


_SQLI_RE = re.compile(r"\.execute\s*\(\s*(f[\"']|[\"'][^\"']*%\s*[\(\w])")
_CMD_INJECTION_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bos\.system\s*\("),
    re.compile(r"\bos\.popen\s*\("),
    re.compile(r"subprocess\.[\w.]+\s*\([^)]*shell\s*=\s*True"),
    re.compile(r"subprocess\.[\w.]+\s*\([^)]*\+"),
)
_PATH_TRAVERSAL_RE = re.compile(
    r"(open|Path|read_text|read_bytes)\s*\([^)]*\+\s*\w+|"
    r"(open|Path|read_text|read_bytes)\s*\(\s*f[\"'][^\"']*\{"
)
_DESER_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bpickle\.loads\s*\("),
    re.compile(r"\byaml\.load\s*\((?![^)]*SafeLoader)"),
    re.compile(r"\bmar(make|shal)\.loads\s*\("),
)
_XSS_RE = re.compile(
    r"render_template_string\s*\(|"
    r"return\s+f[\"'][^\"']*<(?:script|div|span|a\b)",
    re.IGNORECASE,
)


def _taint_pre_scan(diff: str) -> list[ExternalFinding]:
    """Detect tainted flows for five CWEs in added lines.

    Each check is a regex heuristic. No interprocedural analysis is
    performed. The findings are leads for the model and, at critical
    or high severity, for the deterministic override rule.
    """
    findings: list[ExternalFinding] = []
    line_no = 0
    for raw in diff.splitlines():
        line_no += 1
        if not raw.startswith("+") or raw.startswith("+++"):
            continue
        content = raw[1:]
        stripped = content.strip()
        if not stripped:
            continue

        if _SQLI_RE.search(content):
            findings.append(
                ExternalFinding(
                    cwe_id=_CWE_SQLI,
                    severity="high",
                    source_tool="taint",
                    line=line_no,
                    snippet=stripped[:160],
                    message="SQL query built by string interpolation",
                )
            )
            continue

        for pattern in _CMD_INJECTION_RES:
            if pattern.search(content):
                findings.append(
                    ExternalFinding(
                        cwe_id=_CWE_CMD,
                        severity="high",
                        source_tool="taint",
                        line=line_no,
                        snippet=stripped[:160],
                        message="Shell command built from untrusted input",
                    )
                )
                break

        if _PATH_TRAVERSAL_RE.search(content):
            findings.append(
                ExternalFinding(
                    cwe_id=_CWE_PATH,
                    severity="high",
                    source_tool="taint",
                    line=line_no,
                    snippet=stripped[:160],
                    message="Filesystem path built without relative_to guard",
                )
            )

        for pattern in _DESER_RES:
            if pattern.search(content):
                findings.append(
                    ExternalFinding(
                        cwe_id=_CWE_DESER,
                        severity="critical",
                        source_tool="taint",
                        line=line_no,
                        snippet=stripped[:160],
                        message="Deserialization of untrusted data without safe loader",
                    )
                )
                break

        if _XSS_RE.search(content):
            findings.append(
                ExternalFinding(
                    cwe_id=_CWE_XSS,
                    severity="high",
                    source_tool="taint",
                    line=line_no,
                    snippet=stripped[:160],
                    message="Untrusted input rendered into HTML without escaping",
                )
            )
    return findings


def _evidence_passed(pre_scan: dict[str, Any]) -> bool | None:
    """Return the deterministic evidence signal for the verdict override.

    A critical or high finding from an external scanner or the taint
    scan is treated as conclusive evidence of failure. Lower-severity
    findings are left to the model. Regex-only matches are not
    conclusive on their own; they are already surfaced via
    pre_scan_conflict when the model returns pass.
    """
    for finding in pre_scan.get("external_findings", ()):
        if finding.severity in _OVERRIDE_SEVERITIES:
            return False
    for finding in pre_scan.get("taint_findings", ()):
        if finding.severity in _OVERRIDE_SEVERITIES:
            return False
    return None


_POC_TEMPLATES: dict[str, str] = {
    _CWE_SQLI: '''"""PoC for CWE-89: SQL injection."""
import pytest


def test_sql_injection_payload():
    """Call the target with a classic SQL injection payload.

    Replace the function and module imports below with the target's
    actual path. The payload is a string that, if interpolated into
    a SQL query without parameterization, would change the query's
    meaning.
    """
    from target_module import target_function

    payload = "' OR '1'='1"
    try:
        result = target_function(payload)
    except Exception:
        pytest.skip("target raised; inspect manually")
    assert result is not None, (
        "payload did not reach a query; verify parameterization manually"
    )
''',
    _CWE_CMD: '''"""PoC for CWE-78: OS command injection."""
import pytest


def test_command_injection_payload():
    from target_module import target_function

    payload = "safe; echo CODE_GUARDIAN_POC"
    try:
        result = target_function(payload)
    except Exception:
        pytest.skip("target raised; inspect manually")
    assert "CODE_GUARDIAN_POC" not in str(result), (
        "payload reached the shell; parameterize or avoid shell=True"
    )
''',
    _CWE_PATH: '''"""PoC for CWE-22: Path traversal."""
import pytest


def test_path_traversal_payload():
    from target_module import target_function

    payload = "../../etc/passwd"
    try:
        result = target_function(payload)
    except Exception:
        pytest.skip("target raised; inspect manually")
    assert "root:" not in str(result), (
        "payload escaped the intended root; use Path.resolve().relative_to(root)"
    )
''',
    _CWE_DESER: '''"""PoC for CWE-502: Unsafe deserialization."""
import pickle


def test_unsafe_deserialization_payload():
    from target_module import target_function

    payload = pickle.dumps({"poc": True})
    try:
        result = target_function(payload)
    except Exception:
        pass
''',
    _CWE_XSS: '''"""PoC for CWE-79: Cross-site scripting."""
import pytest


def test_xss_payload():
    from target_module import target_function

    payload = "<script>alert(1)</script>"
    try:
        result = target_function(payload)
    except Exception:
        pytest.skip("target raised; inspect manually")
    assert "<script>" not in str(result), (
        "payload rendered unescaped; use an HTML templating engine"
    )
''',
}

_POC_EXPLANATIONS: dict[str, str] = {
    _CWE_SQLI: (
        "A string payload is passed into the function. If the function "
        "interpolates the input into a SQL query without parameter "
        "binding, the query's meaning changes. The fix is to pass the "
        "value as a bound parameter rather than formatting it into the "
        "SQL text."
    ),
    _CWE_CMD: (
        "A payload containing a shell metacharacter is passed to the "
        "function. If the function builds a command by string "
        "concatenation or interpolation, the payload runs a second "
        "command. The fix is to use argv-style subprocess calls without "
        "shell=True, or to remove the shell invocation entirely."
    ),
    _CWE_PATH: (
        "A payload containing directory traversal sequences is passed to "
        "the function. If the function joins the input to a root path "
        "without validating the result, the payload escapes the root. "
        "The fix is to resolve the joined path and assert it starts with "
        "the intended root."
    ),
    _CWE_DESER: (
        "A pickled payload is passed to the function. If the function "
        "deserializes it without restriction, arbitrary code execution "
        "is possible. The fix is to use a safe format (JSON) or a loader "
        "with explicit allowlists."
    ),
    _CWE_XSS: (
        "An HTML payload is passed to the function. If the function "
        "renders it directly into an HTML response, the browser executes "
        "it. The fix is to use an HTML templating engine that escapes "
        "by default."
    ),
}


def _generate_poc(
    pre_scan: dict[str, Any],
    input: SecurityVerifierInput,
) -> SecurityPoC | None:
    """Return a PoC for the first critical or high finding, or None."""
    candidates: list[ExternalFinding] = [
        *pre_scan.get("external_findings", ()),
        *pre_scan.get("taint_findings", ()),
    ]
    candidates.sort(key=lambda f: -_SEVERITY_ORDER.get(f.severity, 0))

    for finding in candidates:
        if finding.severity not in _OVERRIDE_SEVERITIES:
            continue
        cwe = finding.cwe_id
        if cwe not in _POC_TEMPLATES:
            continue
        return SecurityPoC(
            cwe_id=cwe,
            severity=finding.severity,
            test_source=_POC_TEMPLATES[cwe],
            explanation=_POC_EXPLANATIONS[cwe],
        )
    return None


def _build_evidence_receipt(
    input: SecurityVerifierInput,
    pre_scan: dict[str, Any],
) -> EvidenceReceipt | None:
    """Return the evidence receipt when a sandbox ran any checks."""
    artifacts: list[str] = []
    commands: list[str] = []
    if input.sandbox_executor is not None and input.target_file_path is not None:
        artifacts.append(input.target_file_path)
        commands.extend(
            [
                "which semgrep",
                "which bandit",
                f"semgrep --config=p/security-audit --json {input.target_file_path}",
                f"bandit -f json {input.target_file_path}",
            ]
        )
    if not commands:
        return None
    evidence_ids: list[str] = []
    for finding in pre_scan.get("external_findings", ()):
        if finding.cwe_id:
            evidence_ids.append(finding.cwe_id)
    for finding in pre_scan.get("taint_findings", ()):
        if finding.cwe_id:
            evidence_ids.append(finding.cwe_id)
    return {
        "evidence_ids": sorted(set(evidence_ids)),
        "artifacts_checked": artifacts,
        "commands_run": commands,
    }


def _annotate_diff(
    diff: str,
    pre_scan: dict[str, Any],
    original_signals: tuple[str, ...],
) -> str:
    """Prepend pre-scan evidence and original signals to the diff."""
    evidence: list[str] = []
    for match in pre_scan["matches"]:
        evidence.append(f"# match: {match['id']} at diff line {match['line']}: {match['snippet']}")
    for finding in pre_scan.get("external_findings", ()):
        evidence.append(_format_finding_line(finding))
    for finding in pre_scan.get("taint_findings", ()):
        evidence.append(_format_finding_line(finding))
    if original_signals:
        evidence.append("# original_security_signals:")
        for signal in original_signals:
            evidence.append(f"#   - {signal}")
    return assemble_annotated_diff(diff, pre_scan["diff_stats"], evidence)


def _format_finding_line(finding: ExternalFinding) -> str:
    """Return one annotated-diff line for an ExternalFinding."""
    cwe = finding.cwe_id or "CWE-unknown"
    line = f" line {finding.line}" if finding.line is not None else ""
    return (
        f"# finding: {cwe} [{finding.severity}] from {finding.source_tool}{line}: {finding.message}"
    )


def _schema_refusal(reason: str, pre_scan: dict[str, Any]) -> SecurityVerifierResult:
    """Return a schema-refusal result."""
    return SecurityVerifierResult(
        verdict=schema_refusal_verdict("security_verifier", reason),
        verdict_reason=None,
        pre_scan=pre_scan,
        pre_scan_conflict=False,
        is_refusal=True,
    )


def _record(
    input: SecurityVerifierInput,
    prompt: str,
    response: Any,
    latency_ms: int,
    *,
    outcome: str,
    failure_category: Any,
    verdict_reason: Any = None,
    evidence_receipt: EvidenceReceipt | None = None,
) -> None:
    from codeguardian.core.agents import _store_context

    store = _store_context.get_active_store()
    if store is None:
        return
    record_call(
        store,
        role="security_verifier",
        outcome=outcome,
        failure_category=failure_category,
        verdict_reason=verdict_reason,
        target_symbol=input.target_symbol or None,
        prompt=prompt,
        output=response.text,
        tokens_in=response.tokens_in,
        tokens_out=response.tokens_out,
        latency_ms=latency_ms,
        model_id=response.model_id,
        prompt_role="security_verifier",
        prompt_version=input.prompt_version,
        model_options={"temperature": 0.0},
        evidence_receipt=evidence_receipt,
    )
