"""Tests for the skill curator agent."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from codeguardian.core.agents import _store_context
from codeguardian.core.agents.skill_curator import (
    MIN_RUNS_FOR_PROPOSAL,
    RecentRun,
    SkillCuratorAgent,
    SkillCuratorInput,
    SkillEvalCase,
)
from codeguardian.core.plugins import BudgetTracker
from codeguardian.core.storage import MigrationRunner, SQLiteStore
from codeguardian.interfaces.model import ModelOptions, ModelResponse

_VALID_MARKDOWN = """\
---
name: python-add-type-hints-from-returns
description: Add return type hints by inferring from return expressions
trigger: When a Python function has no return annotation and its return statements share a type
skill_type: refactor_pattern
language: python
state: draft
---

When a Python function returns values of a single syntactic type,
add that type as the return annotation. Read the body first; infer
only when every return statement agrees on the type. Do not narrow
an existing annotation.
"""

_VALID_SKILL_JSON = json.dumps(
    {
        "skill_name": "python-add-type-hints-from-returns",
        "skill_markdown": _VALID_MARKDOWN,
        "confidence": 0.72,
    }
)

_NEGATIVE_SKILL_JSON = json.dumps(
    {
        "skill_name": "python-avoid-none-return-wrapping",
        "skill_markdown": """\
---
name: python-avoid-none-return-wrapping
description: Do not wrap expressions in None checks when the original code did not
trigger: When a refactor adds an is-None guard to a function whose original behavior raised on None
skill_type: refactor_pattern
language: python
state: draft
---

The original code raised TypeError on None input. Adding a None
check changes behavior for that input. When the blast report shows a
caller catching TypeError, do not add a None guard; the guard turns a
loud failure into silent corruption.
""",
        "confidence": 0.68,
    }
)

_NONE_SENTINEL_JSON = json.dumps(
    {
        "skill_name": "(none)",
        "skill_markdown": "no reusable pattern in this refactor",
        "confidence": 0.0,
    }
)

_NO_FRONTMATTER_JSON = json.dumps(
    {
        "skill_name": "some-skill",
        "skill_markdown": "This body has no frontmatter at all.",
        "confidence": 0.5,
    }
)

_MISMATCHED_NAME_JSON = json.dumps(
    {
        "skill_name": "declared-name",
        "skill_markdown": _VALID_MARKDOWN.replace(
            "name: python-add-type-hints-from-returns",
            "name: frontmatter-name",
        ),
        "confidence": 0.5,
    }
)


class FakeProvider:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, ModelOptions]] = []

    async def complete(self, prompt: str, model: str, options: ModelOptions) -> ModelResponse:
        self.calls.append((prompt, model, options))
        if not self._responses:
            raise RuntimeError("fake provider out of responses")
        text = self._responses.pop(0)
        return ModelResponse(
            text=text,
            tokens_in=10,
            tokens_out=10,
            model_id="fake",
            finish_reason="stop",
        )

    def stream(self, *a, **kw):
        raise NotImplementedError

    def count_tokens(self, t, m):
        return 0

    def get_available_models(self):
        return []


@pytest.fixture
def store(tmp_path: Path) -> SQLiteStore:
    instance = SQLiteStore(tmp_path / "t.db")
    instance.open()
    MigrationRunner(instance).run()
    try:
        yield instance
    finally:
        instance.close()


def _make_input(
    provider: FakeProvider,
    *,
    outcome: str = "success",
    failure_reason: str = "",
) -> SkillCuratorInput:
    return SkillCuratorInput(
        outcome=outcome,
        directive="add type hints",
        target_symbol="parse",
        target_path="a.py",
        diff="--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-def parse(x):\n+def parse(x: str) -> str:\n",
        verifier_verdicts="correctness: pass\nsecurity: pass\ncontract: pass",
        test_results="1 passed",
        failure_reason=failure_reason,
        provider=provider,
        model="skill_curator",
        budget=BudgetTracker(),
        clone_root=Path("/tmp"),
    )


async def test_positive_polarity_on_success() -> None:
    provider = FakeProvider([_VALID_SKILL_JSON])
    result = await SkillCuratorAgent().run(_make_input(provider))
    assert result.has_proposal is True
    assert result.polarity == "positive"
    assert result.state == "draft"
    assert result.is_refusal is False
    assert result.proposal is not None
    assert result.proposal.skill_name == "python-add-type-hints-from-returns"
    assert 0.0 <= result.proposal.confidence <= 1.0


async def test_negative_polarity_on_failure() -> None:
    provider = FakeProvider([_NEGATIVE_SKILL_JSON])
    result = await SkillCuratorAgent().run(
        _make_input(
            provider,
            outcome="failure",
            failure_reason=("behavior drift: added None guard changes return type"),
        )
    )
    assert result.has_proposal is True
    assert result.polarity == "negative"
    assert result.state == "draft"
    assert result.proposal is not None
    assert "None" in result.proposal.skill_markdown


async def test_negative_prompt_injects_failure_context() -> None:
    provider = FakeProvider([_NEGATIVE_SKILL_JSON])
    await SkillCuratorAgent().run(
        _make_input(
            provider,
            outcome="failure",
            failure_reason="specific drift reason XYZ",
        )
    )
    prompt = provider.calls[0][0]
    assert "REFACTOR OUTCOME: FAILURE" in prompt
    assert "specific drift reason XYZ" in prompt


async def test_success_prompt_has_no_failure_context() -> None:
    provider = FakeProvider([_VALID_SKILL_JSON])
    await SkillCuratorAgent().run(_make_input(provider, outcome="success"))
    prompt = provider.calls[0][0]
    assert "REFACTOR OUTCOME: FAILURE" not in prompt


async def test_none_sentinel_returns_no_proposal() -> None:
    provider = FakeProvider([_NONE_SENTINEL_JSON])
    result = await SkillCuratorAgent().run(_make_input(provider))
    assert result.has_proposal is False
    assert result.proposal is None
    assert result.polarity is None
    assert result.state is None
    assert result.is_refusal is False
    assert result.warnings == ()


async def test_every_proposal_carries_draft_state() -> None:
    for response, outcome in (
        (_VALID_SKILL_JSON, "success"),
        (_NEGATIVE_SKILL_JSON, "failure"),
    ):
        provider = FakeProvider([response])
        result = await SkillCuratorAgent().run(_make_input(provider, outcome=outcome))
        assert result.has_proposal is True
        assert result.state == "draft"


async def test_invalid_markdown_returns_no_proposal() -> None:
    provider = FakeProvider([_NO_FRONTMATTER_JSON])
    result = await SkillCuratorAgent().run(_make_input(provider))
    assert result.has_proposal is False
    assert result.is_refusal is False
    assert result.proposal is None
    assert any("frontmatter" in w for w in result.warnings)


async def test_mismatched_markdown_name_returns_no_proposal() -> None:
    provider = FakeProvider([_MISMATCHED_NAME_JSON])
    result = await SkillCuratorAgent().run(_make_input(provider))
    assert result.has_proposal is False
    assert result.is_refusal is False
    assert any("name" in w for w in result.warnings)


async def test_invalid_json_returns_refusal() -> None:
    provider = FakeProvider(["not json"])
    result = await SkillCuratorAgent().run(_make_input(provider))
    assert result.is_refusal is True
    assert result.has_proposal is False
    assert result.refusal_reason is not None
    assert "JSON" in result.refusal_reason


async def test_invalid_confidence_returns_refusal() -> None:
    over_confident = json.dumps(
        {
            "skill_name": "python-add-type-hints-from-returns",
            "skill_markdown": _VALID_MARKDOWN,
            "confidence": 1.5,
        }
    )
    provider = FakeProvider([over_confident])
    result = await SkillCuratorAgent().run(_make_input(provider))
    assert result.is_refusal is True
    assert result.has_proposal is False
    assert result.refusal_reason is not None
    assert "schema" in result.refusal_reason


class TestMarkdownValidation:
    def test_valid_markdown_accepted(self) -> None:
        from codeguardian.core.agents.skill_curator import (
            _validate_skill_markdown,
        )

        valid, reason = _validate_skill_markdown(_VALID_MARKDOWN)
        assert valid is True
        assert reason is None

    def test_missing_frontmatter_rejected(self) -> None:
        from codeguardian.core.agents.skill_curator import (
            _validate_skill_markdown,
        )

        valid, reason = _validate_skill_markdown("no frontmatter\nbody")
        assert valid is False
        assert reason is not None
        assert "frontmatter" in reason

    def test_unclosed_frontmatter_rejected(self) -> None:
        from codeguardian.core.agents.skill_curator import (
            _validate_skill_markdown,
        )

        valid, reason = _validate_skill_markdown("---\nname: x\nbody")
        assert valid is False
        assert reason is not None

    def test_empty_body_rejected(self) -> None:
        from codeguardian.core.agents.skill_curator import (
            _validate_skill_markdown,
        )

        valid, reason = _validate_skill_markdown(
            "---\nname: x\ndescription: y\ntrigger: z\nskill_type: refactor_pattern\n---\n\n"
        )
        assert valid is False
        assert reason is not None
        assert "body" in reason

    def test_bad_skill_type_rejected(self) -> None:
        from codeguardian.core.agents.skill_curator import (
            _validate_skill_markdown,
        )

        bad = _VALID_MARKDOWN.replace(
            "skill_type: refactor_pattern",
            "skill_type: bogus_type",
        )
        valid, reason = _validate_skill_markdown(bad)
        assert valid is False
        assert reason is not None
        assert "skill_type" in reason

    def test_bad_name_pattern_rejected(self) -> None:
        from codeguardian.core.agents.skill_curator import (
            _validate_skill_markdown,
        )

        bad = _VALID_MARKDOWN.replace(
            "name: python-add-type-hints-from-returns",
            "name: BadName With Spaces",
        )
        valid, reason = _validate_skill_markdown(bad)
        assert valid is False
        assert reason is not None
        assert "name" in reason


class TestTelemetry:
    async def test_success_writes_agent_call_row(self, store: SQLiteStore) -> None:
        provider = FakeProvider([_VALID_SKILL_JSON])
        with _store_context.active_store(store):
            await SkillCuratorAgent().run(_make_input(provider))
        rows = store.query("SELECT * FROM agent_calls WHERE role='skill_curator'")
        assert len(rows) == 1
        row = rows[0]
        assert row["outcome"] == "success"
        assert row["failure_category"] is None
        assert row["verdict_reason"] is None
        assert row["prompt_role"] == "skill_curator"
        assert row["prompt_version"] == "1.0.0"


def _runs_of(
    *,
    count: int,
    directive: str = "add type hints",
    outcome: str = "success",
) -> tuple[RecentRun, ...]:
    return tuple(
        RecentRun(
            run_id=f"r{i + 1}",
            directive=directive,
            outcome=outcome,
            verdict_reason=None,
            target_symbol="parse",
        )
        for i in range(count)
    )


class TestMining:
    async def test_mining_bypassed_when_recent_runs_none(self) -> None:
        provider = FakeProvider([_VALID_SKILL_JSON])
        result = await SkillCuratorAgent().run(_make_input(provider))
        assert result.has_proposal is True
        assert result.source_runs == ()

    async def test_zero_runs_returns_no_proposal_without_model_call(self) -> None:
        provider = FakeProvider([])
        inp = replace(_make_input(provider), recent_runs=())
        result = await SkillCuratorAgent().run(inp)
        assert result.has_proposal is False
        assert result.is_refusal is False
        assert len(provider.calls) == 0

    async def test_one_run_returns_no_proposal(self) -> None:
        provider = FakeProvider([])
        inp = replace(_make_input(provider), recent_runs=_runs_of(count=1))
        result = await SkillCuratorAgent().run(inp)
        assert result.has_proposal is False
        assert len(provider.calls) == 0
        assert any("repeated pattern" in w for w in result.warnings)

    async def test_two_runs_returns_no_proposal(self) -> None:
        provider = FakeProvider([])
        inp = replace(
            _make_input(provider),
            recent_runs=_runs_of(count=MIN_RUNS_FOR_PROPOSAL - 1),
        )
        result = await SkillCuratorAgent().run(inp)
        assert result.has_proposal is False
        assert len(provider.calls) == 0

    async def test_three_matching_runs_proposes_with_sources(self) -> None:
        provider = FakeProvider([_VALID_SKILL_JSON])
        inp = replace(
            _make_input(provider),
            recent_runs=_runs_of(count=MIN_RUNS_FOR_PROPOSAL),
        )
        result = await SkillCuratorAgent().run(inp)
        assert result.has_proposal is True
        assert result.source_runs == ("r1", "r2", "r3")
        assert len(provider.calls) == 1

    async def test_current_directive_must_match_pattern(self) -> None:
        provider = FakeProvider([])
        runs = _runs_of(count=MIN_RUNS_FOR_PROPOSAL, directive="add type hints")
        inp = replace(
            _make_input(provider),
            recent_runs=runs,
            directive="extract a helper",
        )
        result = await SkillCuratorAgent().run(inp)
        assert result.has_proposal is False
        assert len(provider.calls) == 0

    async def test_mixed_outcomes_do_not_cluster(self) -> None:
        provider = FakeProvider([])
        runs = (
            RecentRun(
                run_id="r1",
                directive="add type hints",
                outcome="success",
                verdict_reason=None,
                target_symbol="parse",
            ),
            RecentRun(
                run_id="r2",
                directive="add type hints",
                outcome="success",
                verdict_reason=None,
                target_symbol="parse",
            ),
            RecentRun(
                run_id="r3",
                directive="add type hints",
                outcome="failure",
                verdict_reason="behavior_drift",
                target_symbol="parse",
            ),
        )
        inp = replace(_make_input(provider), recent_runs=runs)
        result = await SkillCuratorAgent().run(inp)
        assert result.has_proposal is False
        assert len(provider.calls) == 0


class TestDedupe:
    async def test_duplicate_name_rejected(self) -> None:
        provider = FakeProvider([_VALID_SKILL_JSON])
        inp = replace(
            _make_input(provider),
            existing_skills=("python-add-type-hints-from-returns",),
        )
        result = await SkillCuratorAgent().run(inp)
        assert result.has_proposal is False
        assert result.dedupe_status == "duplicate"
        assert any("duplicates" in w for w in result.warnings)

    async def test_normalized_duplicate_caught(self) -> None:
        provider = FakeProvider([_VALID_SKILL_JSON])
        inp = replace(
            _make_input(provider),
            existing_skills=("Python_Add_Type_Hints_From_Returns",),
        )
        result = await SkillCuratorAgent().run(inp)
        assert result.has_proposal is False
        assert result.dedupe_status == "duplicate"

    async def test_unique_name_accepted(self) -> None:
        provider = FakeProvider([_VALID_SKILL_JSON])
        inp = replace(
            _make_input(provider),
            existing_skills=("some-other-skill",),
        )
        result = await SkillCuratorAgent().run(inp)
        assert result.has_proposal is True
        assert result.dedupe_status == "unique"

    async def test_empty_existing_skills_is_unique(self) -> None:
        provider = FakeProvider([_VALID_SKILL_JSON])
        inp = replace(_make_input(provider), existing_skills=())
        result = await SkillCuratorAgent().run(inp)
        assert result.has_proposal is True
        assert result.dedupe_status == "unique"


class TestEvalGate:
    async def test_eval_skipped_when_no_cases(self) -> None:
        provider = FakeProvider([_VALID_SKILL_JSON])
        inp = replace(_make_input(provider), eval_cases=())
        result = await SkillCuratorAgent().run(inp)
        assert result.has_proposal is True
        assert result.eval_score is None

    async def test_eval_pass_keeps_proposal(self) -> None:
        provider = FakeProvider([_VALID_SKILL_JSON])
        inp = replace(
            _make_input(provider),
            eval_cases=(
                SkillEvalCase(
                    case_id="c1",
                    expected_contains=("When a Python function returns values",),
                ),
            ),
        )
        result = await SkillCuratorAgent().run(inp)
        assert result.has_proposal is True
        assert result.eval_score == 1.0

    async def test_eval_fail_rejects_proposal(self) -> None:
        provider = FakeProvider([_VALID_SKILL_JSON])
        inp = replace(
            _make_input(provider),
            eval_cases=(
                SkillEvalCase(
                    case_id="c1",
                    expected_contains=("phrase_that_is_not_in_the_body",),
                ),
            ),
        )
        result = await SkillCuratorAgent().run(inp)
        assert result.has_proposal is False
        assert result.eval_score == 0.0
        assert any("eval gate failed" in w for w in result.warnings)

    async def test_partial_eval_below_threshold_rejects(self) -> None:
        provider = FakeProvider([_VALID_SKILL_JSON])
        inp = replace(
            _make_input(provider),
            eval_cases=(
                SkillEvalCase(
                    case_id="c1",
                    expected_contains=("When a Python function returns values",),
                ),
                SkillEvalCase(
                    case_id="c2",
                    expected_contains=("phrase_that_is_not_in_the_body",),
                ),
            ),
            eval_min_score=0.6,
        )
        result = await SkillCuratorAgent().run(inp)
        assert result.has_proposal is False
        assert result.eval_score == 0.5


class TestSkillMdFrontmatter:
    async def test_missing_optional_fields_normalized(self) -> None:
        provider = FakeProvider([_VALID_SKILL_JSON])
        result = await SkillCuratorAgent().run(_make_input(provider))
        assert result.has_proposal is True
        assert result.proposal is not None
        markdown = result.proposal.skill_markdown
        assert "allowed-tools:" in markdown
        assert "version:" in markdown

    async def test_existing_optional_fields_preserved(self) -> None:
        json_with_fields = json.dumps(
            {
                "skill_name": "python-add-type-hints-from-returns",
                "skill_markdown": (
                    "---\n"
                    "name: python-add-type-hints-from-returns\n"
                    "description: Add return type hints from return expressions\n"
                    "trigger: When a function has no return annotation\n"
                    "skill_type: refactor_pattern\n"
                    "allowed-tools:\n"
                    "- function-body\n"
                    "- code-skeleton\n"
                    "version: 1.0.0\n"
                    "---\n"
                    "body content\n"
                ),
                "confidence": 0.7,
            }
        )
        provider = FakeProvider([json_with_fields])
        result = await SkillCuratorAgent().run(_make_input(provider))
        assert result.has_proposal is True
        assert result.proposal is not None
        assert "1.0.0" in result.proposal.skill_markdown
        assert "function-body" in result.proposal.skill_markdown

    async def test_invalid_allowed_tools_rejected(self) -> None:
        invalid = json.dumps(
            {
                "skill_name": "python-add-type-hints-from-returns",
                "skill_markdown": (
                    "---\n"
                    "name: python-add-type-hints-from-returns\n"
                    "description: Add return type hints\n"
                    "trigger: When no return annotation\n"
                    "skill_type: refactor_pattern\n"
                    "allowed-tools: not-a-list\n"
                    "---\n"
                    "body\n"
                ),
                "confidence": 0.5,
            }
        )
        provider = FakeProvider([invalid])
        result = await SkillCuratorAgent().run(_make_input(provider))
        assert result.has_proposal is False
        assert any("allowed-tools" in w for w in result.warnings)

    async def test_invalid_version_rejected(self) -> None:
        invalid = json.dumps(
            {
                "skill_name": "python-add-type-hints-from-returns",
                "skill_markdown": (
                    "---\n"
                    "name: python-add-type-hints-from-returns\n"
                    "description: Add return type hints\n"
                    "trigger: When no return annotation\n"
                    "skill_type: refactor_pattern\n"
                    "version: not-semver\n"
                    "---\n"
                    "body\n"
                ),
                "confidence": 0.5,
            }
        )
        provider = FakeProvider([invalid])
        result = await SkillCuratorAgent().run(_make_input(provider))
        assert result.has_proposal is False
        assert any("version" in w for w in result.warnings)
