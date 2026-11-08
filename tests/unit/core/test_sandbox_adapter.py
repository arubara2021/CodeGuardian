"""Tests for the sandbox adapter and capability attenuation.

The adapter tests use a fake backend rather than a real Bubblewrap
sandbox. The adapter's job is translation and containment, not
isolation; the backend is responsible for isolation and is tested in
the Rust sandbox crate. A fake backend exercises every adapter branch,
including the timeout and OOM exit-code mappings, without needing
``bwrap`` on the test host.

The attenuation tests exercise the permission meet, the audit-writer
contract, and the context binding. They do not test audit-hook
enforcement; that is Phase 9's responsibility and is covered by the
plugins test suite.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from codeguardian.core.agents._verifier_common import (
    ExecutionOutcome,
    SandboxExecutor,
)
from codeguardian.core.errors import CodeGuardianError, ErrorCode
from codeguardian.core.orchestrator.capability_attenuation import (
    attenuated_dispatch,
    intersect_permissions,
    meet_filesystem,
    meet_permissions,
)
from codeguardian.core.orchestrator.sandbox_adapter import (
    BubblewrapExecutorAdapter,
    SandboxExecutorAdapter,
)
from codeguardian.core.plugins import FilesystemLevel, PluginPermissions
from codeguardian.interfaces.sandbox import (
    ExecutionResult,
    ResourceLimits,
    SandboxHandle,
    SandboxSpec,
)


class FakeBackend:
    def __init__(self) -> None:
        self.created: list[SandboxSpec] = []
        self.commands: list[tuple[list[str], int | None]] = []
        self.destroyed_handles: list[str] = []
        self.result_queue: list[ExecutionResult] = []
        self.destroy_raises: CodeGuardianError | None = None

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        self.created.append(spec)
        return SandboxHandle(
            id="fake-1",
            backend="fake",
            language=spec.language,
            working_dir=spec.working_dir,
            network_enabled=False,
            memory_mb=spec.memory_mb,
            cpu_cores=spec.cpu_cores,
            process_limit=spec.process_limit,
        )

    def execute(
        self,
        handle: SandboxHandle,
        command,
        *,
        env=None,
        cwd=None,
        timeout_ms=None,
        stdin=None,
    ) -> ExecutionResult:
        self.commands.append((list(command), timeout_ms))
        if self.result_queue:
            return self.result_queue.pop(0)
        return ExecutionResult(
            stdout="ok",
            stderr="",
            exit_code=0,
            timed_out=False,
            duration_ms=1,
            oom_killed=False,
            output_truncated=False,
        )

    def destroy(self, handle: SandboxHandle) -> None:
        if self.destroy_raises is not None:
            raise self.destroy_raises
        self.destroyed_handles.append(handle.id)

    def supports_language(self, language: str) -> bool:
        return True

    def get_resource_limits(self) -> ResourceLimits:
        return ResourceLimits(
            memory_mb=256,
            cpu_cores=1,
            process_limit=64,
            network_enabled=False,
        )


def make_spec(tmp: Path) -> SandboxSpec:
    return SandboxSpec(
        language="python",
        working_dir=tmp,
        memory_mb=256,
        cpu_cores=1,
        process_limit=64,
        network_during_deps=False,
    )


class RecordingAuditWriter:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def __call__(self, entry: dict[str, Any]) -> str:
        self.entries.append(entry)
        return f"hash_{len(self.entries):04d}"


class TestAdapterRun:
    def test_run_returns_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = FakeBackend()
            with SandboxExecutorAdapter(backend, make_spec(Path(tmp))) as executor:
                outcome = executor.run(["python3", "-c", "print(1)"])
            assert isinstance(outcome, ExecutionOutcome)
            assert outcome.exit_code == 0
            assert outcome.stdout == "ok"

    def test_timeout_maps_to_124(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = FakeBackend()
            backend.result_queue.append(
                ExecutionResult(
                    stdout="",
                    stderr="",
                    exit_code=0,
                    timed_out=True,
                    duration_ms=5000,
                    oom_killed=False,
                    output_truncated=False,
                )
            )
            with SandboxExecutorAdapter(backend, make_spec(Path(tmp))) as executor:
                outcome = executor.run(["sleep", "999"])
            assert outcome.exit_code == 124
            assert "timeout" in outcome.stderr.lower()

    def test_oom_maps_to_137(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = FakeBackend()
            backend.result_queue.append(
                ExecutionResult(
                    stdout="",
                    stderr="",
                    exit_code=0,
                    timed_out=False,
                    duration_ms=100,
                    oom_killed=True,
                    output_truncated=False,
                )
            )
            with SandboxExecutorAdapter(backend, make_spec(Path(tmp))) as executor:
                outcome = executor.run(["malloc", "999"])
            assert outcome.exit_code == 137
            assert "out-of-memory" in outcome.stderr.lower()

    def test_timeout_converted_to_milliseconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = FakeBackend()
            with SandboxExecutorAdapter(backend, make_spec(Path(tmp))) as executor:
                executor.run(["echo"], timeout_s=2.5)
            _command, timeout_ms = backend.commands[-1]
            assert timeout_ms == 2500

    def test_empty_argv_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = FakeBackend()
            with (
                SandboxExecutorAdapter(backend, make_spec(Path(tmp))) as executor,
                pytest.raises(CodeGuardianError) as info,
            ):
                executor.run([])
            assert info.value.code is ErrorCode.SYS_INVARIANT_VIOLATED

    def test_non_positive_timeout_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = FakeBackend()
            with (
                SandboxExecutorAdapter(backend, make_spec(Path(tmp))) as executor,
                pytest.raises(CodeGuardianError),
            ):
                executor.run(["echo"], timeout_s=0)


class TestAdapterWriteFile:
    def test_writes_inside_working_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = FakeBackend()
            root = Path(tmp)
            with SandboxExecutorAdapter(backend, make_spec(root)) as executor:
                executor.write_file("nested/hello.txt", "hi")
            assert (root / "nested" / "hello.txt").read_text() == "hi"

    def test_refuses_relative_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = FakeBackend()
            root = Path(tmp)
            with (
                SandboxExecutorAdapter(backend, make_spec(root)) as executor,
                pytest.raises(CodeGuardianError) as info,
            ):
                executor.write_file("../escape.txt", "nope")
            assert info.value.code is ErrorCode.SANDBOX_EXEC_FAILED
            assert "outside the sandbox" in info.value.message

    def test_refuses_absolute_outside(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = FakeBackend()
            root = Path(tmp)
            with (
                SandboxExecutorAdapter(backend, make_spec(root)) as executor,
                pytest.raises(CodeGuardianError),
            ):
                executor.write_file("/etc/escape.txt", "nope")

    def test_refuses_before_enter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = FakeBackend()
            adapter = SandboxExecutorAdapter(backend, make_spec(Path(tmp)))
            with pytest.raises(CodeGuardianError) as info:
                adapter.write_file("x.txt", "y")
            assert info.value.code is ErrorCode.SYS_INVARIANT_VIOLATED


class TestAdapterLifecycle:
    def test_destroy_called_on_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = FakeBackend()
            with SandboxExecutorAdapter(backend, make_spec(Path(tmp))):
                pass
            assert backend.destroyed_handles == ["fake-1"]

    def test_destroy_failure_suppressed_when_body_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = FakeBackend()
            backend.destroy_raises = CodeGuardianError(ErrorCode.SANDBOX_NOT_FOUND, "already gone")
            with (
                pytest.raises(RuntimeError, match="body raised"),
                SandboxExecutorAdapter(backend, make_spec(Path(tmp))),
            ):
                raise RuntimeError("body raised")

    def test_destroy_failure_raised_when_body_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = FakeBackend()
            backend.destroy_raises = CodeGuardianError(ErrorCode.SANDBOX_NOT_FOUND, "already gone")
            with (
                pytest.raises(CodeGuardianError),
                SandboxExecutorAdapter(backend, make_spec(Path(tmp))),
            ):
                pass

    def test_alias_identity(self) -> None:
        assert BubblewrapExecutorAdapter is SandboxExecutorAdapter


class TestAdapterSixFeatures:
    def test_adapter_satisfies_writable_executor_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = FakeBackend()
            with SandboxExecutorAdapter(backend, make_spec(Path(tmp))) as executor:
                assert hasattr(executor, "run")
                assert hasattr(executor, "write_file")
                assert callable(executor.run)
                assert callable(executor.write_file)

    def test_adapter_usable_where_sandbox_executor_expected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = FakeBackend()
            with SandboxExecutorAdapter(backend, make_spec(Path(tmp))) as executor:

                def takes_executor(e: SandboxExecutor) -> ExecutionOutcome:
                    return e.run(["true"])

                outcome = takes_executor(executor)
            assert outcome.exit_code == 0


class TestMeetFilesystem:
    def test_lower_rank_wins(self) -> None:
        assert meet_filesystem("full", "read_only") == "read_only"
        assert meet_filesystem("read_only", "full") == "read_only"

    def test_none_wins_over_all(self) -> None:
        assert meet_filesystem("none", "full") == "none"
        assert meet_filesystem("full", "none") == "none"

    def test_ties_return_left_operand(self) -> None:
        assert meet_filesystem("sandbox_only", "sandbox_only") == "sandbox_only"

    def test_rank_order(self) -> None:
        levels: list[FilesystemLevel] = [
            "none",
            "sandbox_only",
            "read_only",
            "full",
        ]
        for left in levels:
            for right in levels:
                if levels.index(left) <= levels.index(right):
                    assert meet_filesystem(left, right) == left
                else:
                    assert meet_filesystem(left, right) == right


class TestMeetPermissions:
    def test_dimensionwise_intersection(self) -> None:
        a = PluginPermissions(
            filesystem="full",
            network=True,
            process_spawn=True,
            env_read=("HOME", "PATH", "TOKEN"),
        )
        b = PluginPermissions(
            filesystem="read_only",
            network=False,
            process_spawn=True,
            env_read=("HOME", "PATH"),
        )
        result = meet_permissions(a, b)
        assert result.filesystem == "read_only"
        assert result.network is False
        assert result.process_spawn is True
        assert result.env_read == ("HOME", "PATH")

    def test_env_read_is_sorted(self) -> None:
        a = PluginPermissions(
            filesystem="full",
            network=True,
            env_read=("Z", "A", "M"),
        )
        b = PluginPermissions(
            filesystem="full",
            network=True,
            env_read=("M", "A"),
        )
        result = meet_permissions(a, b)
        assert result.env_read == ("A", "M")

    def test_meet_is_commutative(self) -> None:
        a = PluginPermissions(
            filesystem="read_only",
            network=True,
            process_spawn=False,
        )
        b = PluginPermissions(
            filesystem="full",
            network=False,
            process_spawn=True,
        )
        assert meet_permissions(a, b) == meet_permissions(b, a)


class TestIntersectPermissions:
    def test_single_operand_returned(self) -> None:
        p = PluginPermissions(
            filesystem="read_only",
            network=False,
            process_spawn=False,
        )
        assert intersect_permissions(p) == p

    def test_none_operands_skipped(self) -> None:
        p = PluginPermissions(
            filesystem="read_only",
            network=False,
        )
        assert intersect_permissions(None, p, None) == p

    def test_three_way_meet(self) -> None:
        parent = PluginPermissions(
            filesystem="full",
            network=True,
            process_spawn=True,
            env_read=("A", "B", "C"),
        )
        agent = PluginPermissions(
            filesystem="read_only",
            network=False,
            process_spawn=True,
            env_read=("A", "B"),
        )
        skill = PluginPermissions(
            filesystem="sandbox_only",
            network=True,
            process_spawn=False,
            env_read=("A",),
        )
        result = intersect_permissions(parent, agent, skill)
        assert result.filesystem == "sandbox_only"
        assert result.network is False
        assert result.process_spawn is False
        assert result.env_read == ("A",)

    def test_associative_and_commutative(self) -> None:
        parent = PluginPermissions(filesystem="full", network=True)
        agent = PluginPermissions(filesystem="read_only", network=True)
        skill = PluginPermissions(filesystem="sandbox_only", network=False)
        assert (
            intersect_permissions(parent, agent, skill)
            == intersect_permissions(skill, parent, agent)
            == intersect_permissions(agent, skill, parent)
        )

    def test_all_none_raises(self) -> None:
        with pytest.raises(CodeGuardianError) as info:
            intersect_permissions(None, None)
        assert info.value.code is ErrorCode.SYS_INVARIANT_VIOLATED


class TestAttenuatedDispatch:
    def test_effective_is_intersection(self) -> None:
        parent = PluginPermissions(
            filesystem="full",
            network=True,
            process_spawn=True,
        )
        agent = PluginPermissions(
            filesystem="read_only",
            network=False,
            process_spawn=True,
        )
        with attenuated_dispatch(
            agent_name="writer",
            agent_permissions=agent,
            parent_permissions=parent,
        ) as record:
            assert record.effective.filesystem == "read_only"
            assert record.effective.network is False
            assert record.effective.process_spawn is True

    def test_narrowed_notes_relative_to_parent(self) -> None:
        parent = PluginPermissions(
            filesystem="full",
            network=True,
            env_read=("A", "B"),
        )
        agent = PluginPermissions(
            filesystem="read_only",
            network=True,
            env_read=("A", "B"),
        )
        with attenuated_dispatch(
            agent_name="writer",
            agent_permissions=agent,
            parent_permissions=parent,
        ) as record:
            assert any(
                "filesystem" in note and "full" in note and "read_only" in note
                for note in record.narrowed
            )

    def test_no_parent_no_narrowing(self) -> None:
        agent = PluginPermissions(
            filesystem="read_only",
            network=False,
        )
        with attenuated_dispatch(
            agent_name="writer",
            agent_permissions=agent,
        ) as record:
            assert record.narrowed == ()

    def test_skill_manifest_participates(self) -> None:
        parent = PluginPermissions(filesystem="full", network=True)
        agent = PluginPermissions(filesystem="full", network=True)
        skill = PluginPermissions(filesystem="sandbox_only", network=False)
        with attenuated_dispatch(
            agent_name="writer",
            agent_permissions=agent,
            parent_permissions=parent,
            skill_permissions=skill,
        ) as record:
            assert record.effective.filesystem == "sandbox_only"
            assert record.effective.network is False
            assert any("skill" in note for note in record.narrowed)

    def test_audit_writer_receives_intersection(self) -> None:
        writer = RecordingAuditWriter()
        parent = PluginPermissions(filesystem="full", network=True)
        agent = PluginPermissions(filesystem="read_only", network=False)
        with attenuated_dispatch(
            agent_name="writer",
            agent_permissions=agent,
            parent_permissions=parent,
            phase="refactor",
            audit_writer=writer,
            run_id="run-1",
        ) as record:
            assert record.audit_hash == "hash_0001"

        assert len(writer.entries) == 1
        entry = writer.entries[0]
        assert entry["phase"] == "refactor"
        assert entry["run_id"] == "run-1"
        assert entry["agent"] == "writer"
        assert entry["result"]["status"] == "success"
        effective = entry["result"]["attenuation"]["effective"]
        assert effective["filesystem"] == "read_only"
        assert effective["network"] is False

    def test_invalid_phase_raises(self) -> None:
        agent = PluginPermissions(filesystem="read_only", network=False)
        with (
            pytest.raises(CodeGuardianError) as info,
            attenuated_dispatch(
                agent_name="writer",
                agent_permissions=agent,
                phase="not_a_real_phase",
            ),
        ):
            pass
        assert info.value.code is ErrorCode.SYS_INVARIANT_VIOLATED

    def test_invalid_agent_name_raises(self) -> None:
        agent = PluginPermissions(filesystem="read_only", network=False)
        with (
            pytest.raises(CodeGuardianError),
            attenuated_dispatch(
                agent_name="Bad-Name",
                agent_permissions=agent,
            ),
        ):
            pass

    def test_decision_log_entry_shape(self) -> None:
        parent = PluginPermissions(filesystem="full", network=True)
        agent = PluginPermissions(filesystem="read_only", network=False)
        with attenuated_dispatch(
            agent_name="writer",
            agent_permissions=agent,
            parent_permissions=parent,
        ) as record:
            entry = record.to_decision_log_entry()
        assert entry["step"] == "capability_attenuation"
        assert entry["verdict"] == "attenuated"
        assert entry["candidate"] is None
        assert "attenuation" in entry
        assert entry["attenuation"]["agent"] == "writer"

    def test_unchanged_verdict_when_nothing_narrowed(self) -> None:
        agent = PluginPermissions(filesystem="read_only", network=False)
        parent = PluginPermissions(filesystem="read_only", network=False)
        with attenuated_dispatch(
            agent_name="writer",
            agent_permissions=agent,
            parent_permissions=parent,
        ) as record:
            entry = record.to_decision_log_entry()
        assert entry["verdict"] == "unchanged"

    def test_nested_dispatch_inherits_outer(self) -> None:
        outer_parent = PluginPermissions(
            filesystem="full",
            network=True,
            process_spawn=True,
        )
        outer_agent = PluginPermissions(
            filesystem="read_only",
            network=True,
            process_spawn=True,
        )
        inner_agent = PluginPermissions(
            filesystem="full",
            network=True,
            process_spawn=True,
        )
        with (
            attenuated_dispatch(
                agent_name="writer",
                agent_permissions=outer_agent,
                parent_permissions=outer_parent,
            ) as outer,
            attenuated_dispatch(
                agent_name="tester",
                agent_permissions=inner_agent,
                parent_permissions=outer.effective,
            ) as inner,
        ):
            assert inner.effective.filesystem == "read_only"
            assert inner.effective.network is True
