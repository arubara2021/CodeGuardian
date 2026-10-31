"""Tests for the skill curator agent."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeguardian.core.agents import _store_context
from codeguardian.core.agents.skill_curator import (
    SkillCuratorAgent,
    SkillCuratorInput,
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
