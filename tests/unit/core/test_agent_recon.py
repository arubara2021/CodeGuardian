"""Tests for the recon agent."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("codeguardian._native")

from codeguardian.core.agents.recon import ReconAgent, ReconInput

_SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "recon.json"


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def clone_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text(
        "def parse(data):\n"
        "    return data.strip()\n"
        "\n"
        "def process(raw):\n"
        "    return parse(raw) + '!'\n",
        encoding="utf-8",
    )
    (root / "b.py").write_text(
        "from a import parse\n"
        "\n"
        "def main():\n"
        "    return parse('hello')\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )
    return root


async def test_artifact_validates_against_schema(
    clone_root: Path, tmp_path: Path, schema: dict
) -> None:
    import jsonschema

    agent = ReconAgent()
    result = await agent.run(
        ReconInput(
            repo_url="https://example.com/repo",
            provider="local",
            commit_hash="abc123",
            clone_root=clone_root,
            cpg_output_dir=tmp_path / "cpg",
        )
    )
    jsonschema.validate(result.artifact, schema)


async def test_all_eleven_required_fields_present(
    clone_root: Path, tmp_path: Path, schema: dict
) -> None:
    agent = ReconAgent()
    result = await agent.run(
        ReconInput(
            repo_url="https://example.com/repo",
            provider="local",
            commit_hash="abc123",
            clone_root=clone_root,
            cpg_output_dir=tmp_path / "cpg",
        )
    )
    for field_name in schema["required"]:
        assert field_name in result.artifact, field_name


async def test_languages_section_reflects_python_files(clone_root: Path, tmp_path: Path) -> None:
    agent = ReconAgent()
    result = await agent.run(
        ReconInput(
            repo_url="u",
            provider="local",
            commit_hash="c",
            clone_root=clone_root,
            cpg_output_dir=tmp_path / "cpg",
        )
    )
    languages = result.artifact["languages"]
    assert len(languages) == 1
    assert languages[0]["name"] == "python"
    assert languages[0]["files"] == 2
    assert languages[0]["tier"] == "high"


async def test_dependency_graph_contains_import_edge(clone_root: Path, tmp_path: Path) -> None:
    agent = ReconAgent()
    result = await agent.run(
        ReconInput(
            repo_url="u",
            provider="local",
            commit_hash="c",
            clone_root=clone_root,
            cpg_output_dir=tmp_path / "cpg",
        )
    )
    edges = result.artifact["dependency_graph"]["edges"]
    assert any(
        e["from"] == "b.py" and e["to"] == "a.py" and e["kind"] == "import" for e in edges
    ), edges


async def test_hierarchical_index_lists_declarations(clone_root: Path, tmp_path: Path) -> None:
    agent = ReconAgent()
    result = await agent.run(
        ReconInput(
            repo_url="u",
            provider="local",
            commit_hash="c",
            clone_root=clone_root,
            cpg_output_dir=tmp_path / "cpg",
        )
    )
    index = result.artifact["hierarchical_index"]
    assert "a.py" in index
    assert "parse" in index["a.py"]
    assert "process" in index["a.py"]
    assert "main" in index["b.py"]


async def test_entrypoints_detected(clone_root: Path, tmp_path: Path) -> None:
    agent = ReconAgent()
    result = await agent.run(
        ReconInput(
            repo_url="u",
            provider="local",
            commit_hash="c",
            clone_root=clone_root,
            cpg_output_dir=tmp_path / "cpg",
        )
    )
    entrypoints = result.artifact["entrypoints"]
    types_and_files = {(e["file"], e["type"]) for e in entrypoints}
    assert ("b.py", "script") in types_and_files
    assert ("b.py", "function") in types_and_files


async def test_report_statuses_all_present(clone_root: Path, tmp_path: Path) -> None:
    agent = ReconAgent()
    result = await agent.run(
        ReconInput(
            repo_url="u",
            provider="local",
            commit_hash="c",
            clone_root=clone_root,
            cpg_output_dir=tmp_path / "cpg",
        )
    )
    statuses = result.artifact["extensions"]["report_statuses"]
    valid = {"complete", "partial", "skipped"}
    for key in (
        "dependency_graph",
        "hierarchical_index",
        "entrypoints",
        "versions",
        "frameworks",
        "smoke_test",
        "truth_report",
        "drift_report",
    ):
        assert key in statuses, key
        assert statuses[key] in valid, (key, statuses[key])


async def test_smoke_test_skipped(clone_root: Path, tmp_path: Path) -> None:
    agent = ReconAgent()
    result = await agent.run(
        ReconInput(
            repo_url="u",
            provider="local",
            commit_hash="c",
            clone_root=clone_root,
            cpg_output_dir=tmp_path / "cpg",
        )
    )
    smoke = result.artifact["smoke_test"]
    assert smoke["status"] == "skipped"
    assert smoke["passed"] == 0
    assert smoke["failed"] == 0
    assert smoke["errored"] == 0


async def test_truth_and_drift_empty(clone_root: Path, tmp_path: Path) -> None:
    agent = ReconAgent()
    result = await agent.run(
        ReconInput(
            repo_url="u",
            provider="local",
            commit_hash="c",
            clone_root=clone_root,
            cpg_output_dir=tmp_path / "cpg",
        )
    )
    assert result.artifact["truth_report"] == []
    assert result.artifact["drift_report"] == []
    statuses = result.artifact["extensions"]["report_statuses"]
    assert statuses["truth_report"] == "skipped"
    assert statuses["drift_report"] == "skipped"


async def test_cpg_file_written_to_disk(clone_root: Path, tmp_path: Path) -> None:
    agent = ReconAgent()
    result = await agent.run(
        ReconInput(
            repo_url="u",
            provider="local",
            commit_hash="deadbeef",
            clone_root=clone_root,
            cpg_output_dir=tmp_path / "cpg",
        )
    )
    assert result.cpg_path.is_file()
    assert result.cpg_path.name == "deadbeef.cpg"
    assert result.cpg_path.read_bytes() == result.cpg_bytes


async def test_missing_clone_root_raises(
    tmp_path: Path,
) -> None:
    from codeguardian.core.errors import CodeGuardianError, ErrorCode

    agent = ReconAgent()
    with pytest.raises(CodeGuardianError) as exc_info:
        await agent.run(
            ReconInput(
                repo_url="u",
                provider="local",
                commit_hash="c",
                clone_root=tmp_path / "does-not-exist",
                cpg_output_dir=tmp_path / "cpg",
            )
        )
    assert exc_info.value.code is ErrorCode.FILE_NOT_FOUND
