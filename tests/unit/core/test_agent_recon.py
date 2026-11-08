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


class TestRepoMap:
    async def test_repo_map_populated(self, clone_root: Path, tmp_path: Path) -> None:
        result = await ReconAgent().run(
            ReconInput(
                repo_url="u",
                provider="local",
                commit_hash="c",
                clone_root=clone_root,
                cpg_output_dir=tmp_path / "cpg",
            )
        )
        repo_map = result.artifact["extensions"]["repo_map"]
        assert isinstance(repo_map, list)
        assert len(repo_map) >= 1
        for entry in repo_map:
            assert "path" in entry
            assert "importance" in entry
            assert "symbols" in entry
            assert "token_cost" in entry
            assert "total_file_tokens" in entry

    async def test_repo_map_respects_token_budget(self, clone_root: Path, tmp_path: Path) -> None:
        result = await ReconAgent().run(
            ReconInput(
                repo_url="u",
                provider="local",
                commit_hash="c",
                clone_root=clone_root,
                cpg_output_dir=tmp_path / "cpg",
                repo_map_token_budget=5,
            )
        )
        repo_map = result.artifact["extensions"]["repo_map"]
        assert len(repo_map) >= 1
        if len(repo_map) > 1:
            total = sum(e["token_cost"] for e in repo_map)
            assert total <= 5

    async def test_repo_map_target_file_scores_higher(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "target.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
        (root / "a.py").write_text("from target import helper\n", encoding="utf-8")
        (root / "b.py").write_text("from target import helper\n", encoding="utf-8")
        result = await ReconAgent().run(
            ReconInput(
                repo_url="u",
                provider="local",
                commit_hash="c",
                clone_root=root,
                cpg_output_dir=tmp_path / "cpg",
            )
        )
        importance = {
            entry["path"]: entry["importance"]
            for entry in result.artifact["extensions"]["repo_map"]
        }
        assert importance["target.py"] > importance["a.py"]
        assert importance["target.py"] > importance["b.py"]


class TestMultiLanguageImports:
    async def test_typescript_relative_import_resolved(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "c.ts").write_text(
            "export function helper(): number {\n    return 1;\n}\n",
            encoding="utf-8",
        )
        (root / "d.ts").write_text(
            "import { helper } from './c';\n"
            "export function run(): number {\n    return helper();\n}\n",
            encoding="utf-8",
        )
        result = await ReconAgent().run(
            ReconInput(
                repo_url="u",
                provider="local",
                commit_hash="c",
                clone_root=root,
                cpg_output_dir=tmp_path / "cpg",
            )
        )
        edges = result.artifact["dependency_graph"]["edges"]
        assert any(e["from"] == "d.ts" and e["to"] == "c.ts" for e in edges), edges

    async def test_java_import_resolved_by_basename(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "Foo.java").write_text(
            "public class Foo {\n    public static int helper() { return 1; }\n}\n",
            encoding="utf-8",
        )
        (root / "Bar.java").write_text(
            "import Foo;\n"
            "public class Bar {\n"
            "    public static int run() { return Foo.helper(); }\n"
            "}\n",
            encoding="utf-8",
        )
        result = await ReconAgent().run(
            ReconInput(
                repo_url="u",
                provider="local",
                commit_hash="c",
                clone_root=root,
                cpg_output_dir=tmp_path / "cpg",
            )
        )
        edges = result.artifact["dependency_graph"]["edges"]
        assert any(e["from"] == "Bar.java" and e["to"] == "Foo.java" for e in edges), edges

    async def test_mixed_repo_has_both_edges(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "a.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
        (root / "b.py").write_text("from a import helper\n", encoding="utf-8")
        (root / "c.ts").write_text(
            "export function x(): number {\n    return 1;\n}\n",
            encoding="utf-8",
        )
        (root / "d.ts").write_text("import { x } from './c';\n", encoding="utf-8")
        result = await ReconAgent().run(
            ReconInput(
                repo_url="u",
                provider="local",
                commit_hash="c",
                clone_root=root,
                cpg_output_dir=tmp_path / "cpg",
            )
        )
        edges = result.artifact["dependency_graph"]["edges"]
        assert any(e["from"] == "b.py" and e["to"] == "a.py" for e in edges)
        assert any(e["from"] == "d.ts" and e["to"] == "c.ts" for e in edges)


class TestProjectDetection:
    async def test_pytest_detected_from_pyproject(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "pyproject.toml").write_text(
            '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            encoding="utf-8",
        )
        (root / "main.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        result = await ReconAgent().run(
            ReconInput(
                repo_url="u",
                provider="local",
                commit_hash="c",
                clone_root=root,
                cpg_output_dir=tmp_path / "cpg",
            )
        )
        ext = result.artifact["extensions"]
        assert "pytest" in ext["test_frameworks"]
        assert ext["test_command"] == "pytest"

    async def test_jest_detected_from_package_json(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "package.json").write_text(
            '{"devDependencies": {"jest": "^29.0.0"}}', encoding="utf-8"
        )
        (root / "main.js").write_text("function f() { return 1; }\n", encoding="utf-8")
        result = await ReconAgent().run(
            ReconInput(
                repo_url="u",
                provider="local",
                commit_hash="c",
                clone_root=root,
                cpg_output_dir=tmp_path / "cpg",
            )
        )
        ext = result.artifact["extensions"]
        assert "jest" in ext["test_frameworks"]

    async def test_cargo_detected_from_cargo_toml(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "Cargo.toml").write_text('[package]\nname = "test"\n', encoding="utf-8")
        (root / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
        result = await ReconAgent().run(
            ReconInput(
                repo_url="u",
                provider="local",
                commit_hash="c",
                clone_root=root,
                cpg_output_dir=tmp_path / "cpg",
            )
        )
        ext = result.artifact["extensions"]
        assert ext["build_system"] == "cargo"
        assert "cargo test" in ext["test_frameworks"]
        assert ext["test_command"] == "cargo test"

    async def test_no_indicators_returns_empty(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "main.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        result = await ReconAgent().run(
            ReconInput(
                repo_url="u",
                provider="local",
                commit_hash="c",
                clone_root=root,
                cpg_output_dir=tmp_path / "cpg",
            )
        )
        ext = result.artifact["extensions"]
        assert ext["test_frameworks"] == []
        assert ext["test_command"] is None
        assert ext["build_system"] is None


class TestConventions:
    async def test_agents_md_read(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "AGENTS.md").write_text(
            "# Conventions\n\nUse black for formatting.\n",
            encoding="utf-8",
        )
        (root / "main.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        result = await ReconAgent().run(
            ReconInput(
                repo_url="u",
                provider="local",
                commit_hash="c",
                clone_root=root,
                cpg_output_dir=tmp_path / "cpg",
            )
        )
        conventions = result.artifact["extensions"]["conventions"]
        assert "AGENTS.md" in conventions["agent_files"]
        assert "Use black" in conventions["agent_files"]["AGENTS.md"]

    async def test_configs_detected(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (root / ".eslintrc.json").write_text("{}", encoding="utf-8")
        (root / "tsconfig.json").write_text("{}", encoding="utf-8")
        (root / "main.js").write_text("function f() { return 1; }\n", encoding="utf-8")
        result = await ReconAgent().run(
            ReconInput(
                repo_url="u",
                provider="local",
                commit_hash="c",
                clone_root=root,
                cpg_output_dir=tmp_path / "cpg",
            )
        )
        configs = result.artifact["extensions"]["conventions"]["configs_present"]
        assert ".eslintrc.json" in configs
        assert "tsconfig.json" in configs


class TestReportStatusesExtensions:
    async def test_new_statuses_present(self, clone_root: Path, tmp_path: Path) -> None:
        result = await ReconAgent().run(
            ReconInput(
                repo_url="u",
                provider="local",
                commit_hash="c",
                clone_root=clone_root,
                cpg_output_dir=tmp_path / "cpg",
            )
        )
        statuses = result.artifact["extensions"]["report_statuses"]
        assert "repo_map" in statuses
        assert "conventions" in statuses
        assert statuses["repo_map"] in ("complete", "partial", "skipped")
        assert statuses["conventions"] in ("complete", "partial", "skipped")


class TestDeclaredConstraints:
    async def test_pyproject_requires_python(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "pyproject.toml").write_text(
            '[project]\nname = "test"\nrequires-python = ">=3.11"\n',
            encoding="utf-8",
        )
        (root / "main.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        result = await ReconAgent().run(
            ReconInput(
                repo_url="u",
                provider="local",
                commit_hash="c",
                clone_root=root,
                cpg_output_dir=tmp_path / "cpg",
            )
        )
        declared = result.artifact["extensions"]["declared"]
        assert "python" in declared
        assert declared["python"]["requires_python"] == ">=3.11"

    async def test_pyproject_dependencies(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "pyproject.toml").write_text(
            "[project]\n"
            'name = "test"\n'
            'requires-python = ">=3.9"\n'
            "dependencies = [\n"
            '    "urllib3>=1.21",\n'
            '    "certifi>=2017.4.17",\n'
            "]\n",
            encoding="utf-8",
        )
        (root / "main.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        result = await ReconAgent().run(
            ReconInput(
                repo_url="u",
                provider="local",
                commit_hash="c",
                clone_root=root,
                cpg_output_dir=tmp_path / "cpg",
            )
        )
        declared = result.artifact["extensions"]["declared"]
        assert "python" in declared
        deps = declared["python"].get("dependencies", [])
        assert any("urllib3" in d for d in deps), deps
        assert any("certifi" in d for d in deps), deps
