"""Tests for the configuration schema, loader, and secrets module.

Every test here describes one rule the configuration system must obey.
A failure means a broken config file reaches the runtime and fails
later with a less useful message, or worse, silently uses a wrong
default. The tests check both that valid input is accepted and that
each class of invalid input produces a typed error whose message names
the offending location.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeguardian.core.config import (
    Config,
    ExclusionsConfig,
    GeneralConfig,
    MCPConfig,
    ModelRoleConfig,
    ModelsConfig,
    ProviderConfig,
    SandboxConfig,
    load_config,
    validate,
)
from codeguardian.core.errors import (
    CodeGuardianError,
    ErrorCategory,
    ErrorCode,
    category_of,
    retry_policy_of,
)


def test_validate_none_returns_default_config():
    config = validate(None)
    assert isinstance(config, Config)
    assert config.general == GeneralConfig()
    assert config.models == ModelsConfig()
    assert config.sandbox == SandboxConfig()


def test_validate_empty_dict_returns_default_config():
    config = validate({})
    assert isinstance(config, Config)
    assert config.general.mode == "legacy"
    assert config.general.sandbox_backend == "auto"
    assert config.general.log_level == "info"


def test_validate_general_section():
    config = validate(
        {
            "general": {
                "mode": "greenfield",
                "sandbox_backend": "docker",
                "log_level": "debug",
            }
        }
    )
    assert config.general.mode == "greenfield"
    assert config.general.sandbox_backend == "docker"
    assert config.general.log_level == "debug"


def test_validate_general_partial_uses_defaults():
    config = validate({"general": {"mode": "greenfield"}})
    assert config.general.mode == "greenfield"
    assert config.general.sandbox_backend == "auto"
    assert config.general.log_level == "info"


def test_validate_rejects_unknown_mode():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"general": {"mode": "invalid"}})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert excinfo.value.category is ErrorCategory.USER
    assert "config.general.mode" in excinfo.value.message


def test_validate_rejects_unknown_sandbox_backend():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"general": {"sandbox_backend": "kvm"}})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "config.general.sandbox_backend" in excinfo.value.message


def test_validate_rejects_unknown_log_level():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"general": {"log_level": "trace"}})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "config.general.log_level" in excinfo.value.message


def test_validate_rejects_non_string_mode():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"general": {"mode": 42}})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "expected a string" in excinfo.value.message


def test_validate_rejects_unknown_top_level_key():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"unknown_section": {}})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "unknown key" in excinfo.value.message


def test_validate_rejects_unknown_general_key():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"general": {"unknown_field": "x"}})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "unknown key" in excinfo.value.message


def test_validate_rejects_general_not_a_table():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"general": "not a table"})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "expected a table" in excinfo.value.message


def test_validate_models_section_with_one_role():
    config = validate({"models": {"writer": {"provider": "anthropic", "model": "claude-opus-5.5"}}})
    assert config.models.writer == ModelRoleConfig(provider="anthropic", model="claude-opus-5.5")
    assert config.models.analyst is None


def test_validate_models_section_with_all_roles():
    roles = {
        "analyst": {"provider": "deepseek", "model": "deepseek-v4-flash"},
        "tester": {"provider": "anthropic", "model": "claude-opus-5.5"},
        "writer": {"provider": "anthropic", "model": "claude-opus-5.5"},
        "correctness_verifier": {"provider": "openai", "model": "gpt-6-astra"},
        "security_verifier": {"provider": "openai", "model": "gpt-6-astra"},
        "contract_verifier": {"provider": "openai", "model": "gpt-6-astra"},
        "skill_curator": {"provider": "deepseek", "model": "deepseek-v4-flash"},
    }
    config = validate({"models": roles})
    assert config.models.writer is not None
    assert config.models.writer.provider == "anthropic"
    assert config.models.contract_verifier is not None
    assert config.models.contract_verifier.provider == "openai"


def test_validate_rejects_model_role_missing_provider():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"models": {"writer": {"model": "x"}}})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "missing required key 'provider'" in excinfo.value.message


def test_validate_rejects_model_role_missing_model():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"models": {"writer": {"provider": "anthropic"}}})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "missing required key 'model'" in excinfo.value.message


def test_validate_rejects_empty_provider():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"models": {"writer": {"provider": "", "model": "x"}}})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "must not be empty" in excinfo.value.message


def test_validate_rejects_unknown_model_role():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"models": {"unknown_role": {"provider": "a", "model": "b"}}})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "unknown key" in excinfo.value.message


def test_validate_rejects_non_string_model_provider():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"models": {"writer": {"provider": 42, "model": "x"}}})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "expected a string" in excinfo.value.message


def test_validate_providers_cloud_provider():
    config = validate({"providers": {"anthropic": {"api_key_source": "keychain"}}})
    assert config.providers.anthropic == ProviderConfig(api_key_source="keychain")
    assert config.providers.local is None


def test_validate_providers_local_provider():
    config = validate({"providers": {"local": {"base_url": "http://localhost:11434/v1"}}})
    assert config.providers.local is not None
    assert config.providers.local.base_url == "http://localhost:11434/v1"


def test_validate_rejects_unknown_provider():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"providers": {"google": {"api_key_source": "keychain"}}})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "unknown key" in excinfo.value.message


def test_validate_rejects_empty_provider_entry():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"providers": {"anthropic": {}}})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "at least one of" in excinfo.value.message


def test_validate_rejects_invalid_api_key_source():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"providers": {"anthropic": {"api_key_source": "file"}}})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "config.providers.anthropic.api_key_source" in excinfo.value.message


def test_validate_storage_defaults():
    config = validate({})
    assert config.storage.artifacts_retention_days == 30
    assert config.storage.graph_retention_days == 90
    assert config.storage.audit_retention_days == 0


def test_validate_storage_overrides():
    config = validate(
        {
            "storage": {
                "database": "/tmp/cg.db",
                "artifacts_retention_days": 7,
                "graph_retention_days": 14,
                "audit_retention_days": 365,
            }
        }
    )
    assert config.storage.database == "/tmp/cg.db"
    assert config.storage.artifacts_retention_days == 7
    assert config.storage.graph_retention_days == 14
    assert config.storage.audit_retention_days == 365


def test_validate_rejects_negative_retention():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"storage": {"artifacts_retention_days": -1}})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "config.storage.artifacts_retention_days" in excinfo.value.message


def test_validate_rejects_bool_as_int():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"storage": {"artifacts_retention_days": True}})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "expected an integer" in excinfo.value.message


def test_validate_sandbox_defaults():
    config = validate({})
    assert config.sandbox.memory_limit_mb == 1024
    assert config.sandbox.cpu_limit_cores == 2
    assert config.sandbox.process_limit == 256
    assert config.sandbox.network_during_deps is True


def test_validate_sandbox_overrides():
    config = validate(
        {
            "sandbox": {
                "memory_limit_mb": 2048,
                "cpu_limit_cores": 4,
                "process_limit": 512,
                "network_during_deps": False,
            }
        }
    )
    assert config.sandbox.memory_limit_mb == 2048
    assert config.sandbox.cpu_limit_cores == 4
    assert config.sandbox.process_limit == 512
    assert config.sandbox.network_during_deps is False


def test_validate_rejects_zero_memory():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"sandbox": {"memory_limit_mb": 0}})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "config.sandbox.memory_limit_mb" in excinfo.value.message


def test_validate_rejects_non_bool_network_flag():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"sandbox": {"network_during_deps": "yes"}})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "expected a boolean" in excinfo.value.message


def test_validate_mcp_empty_servers():
    config = validate({"mcp": {"servers": []}})
    assert config.mcp == MCPConfig(servers=())


def test_validate_mcp_stdio_server():
    config = validate(
        {
            "mcp": {
                "servers": [
                    {
                        "name": "ast-grep",
                        "command": "npx",
                        "args": ["-y", "ast-grep-mcp"],
                    }
                ]
            }
        }
    )
    assert len(config.mcp.servers) == 1
    server = config.mcp.servers[0]
    assert server.name == "ast-grep"
    assert server.command == "npx"
    assert server.args == ("-y", "ast-grep-mcp")
    assert server.url is None


def test_validate_mcp_http_server():
    config = validate(
        {"mcp": {"servers": [{"name": "remote", "url": "http://localhost:3000/mcp"}]}}
    )
    server = config.mcp.servers[0]
    assert server.url == "http://localhost:3000/mcp"
    assert server.command is None
    assert server.args == ()


def test_validate_rejects_mcp_server_missing_name():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"mcp": {"servers": [{"command": "npx"}]}})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "missing required key 'name'" in excinfo.value.message


def test_validate_rejects_mcp_server_neither_command_nor_url():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"mcp": {"servers": [{"name": "x"}]}})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "must define either" in excinfo.value.message


def test_validate_rejects_mcp_server_both_command_and_url():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate(
            {
                "mcp": {
                    "servers": [
                        {
                            "name": "x",
                            "command": "npx",
                            "url": "http://localhost/",
                        }
                    ]
                }
            }
        )
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "not both" in excinfo.value.message


def test_validate_rejects_non_string_mcp_arg():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"mcp": {"servers": [{"name": "x", "command": "npx", "args": [1, 2]}]}})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "config.mcp.servers[0].args[0]" in excinfo.value.message


def test_validate_exclusions_empty():
    config = validate({"exclusions": {}})
    assert config.exclusions == ExclusionsConfig(patterns=())


def test_validate_exclusions_patterns():
    config = validate({"exclusions": {"patterns": ["docs/generated/", "*.generated.py"]}})
    assert config.exclusions.patterns == ("docs/generated/", "*.generated.py")


def test_validate_rejects_non_list_patterns():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"exclusions": {"patterns": "not a list"}})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "expected an array" in excinfo.value.message


def test_validate_full_config_file():
    config = validate(
        {
            "general": {
                "mode": "legacy",
                "sandbox_backend": "auto",
                "log_level": "info",
            },
            "models": {
                "writer": {
                    "provider": "anthropic",
                    "model": "claude-opus-5.5",
                },
                "tester": {
                    "provider": "anthropic",
                    "model": "claude-opus-5.5",
                },
            },
            "providers": {
                "anthropic": {"api_key_source": "keychain"},
                "local": {"base_url": "http://localhost:11434/v1"},
            },
            "storage": {"artifacts_retention_days": 14},
            "sandbox": {"memory_limit_mb": 2048},
            "mcp": {
                "servers": [
                    {
                        "name": "ast-grep",
                        "command": "npx",
                        "args": ["-y", "ast-grep-mcp"],
                    }
                ]
            },
            "exclusions": {"patterns": ["docs/generated/"]},
        }
    )
    assert config.general.mode == "legacy"
    assert config.models.writer is not None
    assert config.providers.anthropic is not None
    assert config.storage.artifacts_retention_days == 14
    assert config.sandbox.memory_limit_mb == 2048
    assert len(config.mcp.servers) == 1
    assert config.exclusions.patterns == ("docs/generated/",)


def test_validate_error_details_contains_path():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"general": {"mode": "invalid"}})
    assert excinfo.value.details.get("path") == "config.general.mode"


def test_validate_error_is_not_retryable():
    with pytest.raises(CodeGuardianError) as excinfo:
        validate({"general": {"mode": "invalid"}})
    assert excinfo.value.retry_policy.retryable is False


def test_load_config_missing_file_returns_defaults(tmp_path: Path):
    missing = tmp_path / "does-not-exist.toml"
    config = load_config(missing, env={})
    assert config == Config()


def test_load_config_reads_valid_file(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[general]\nmode = "greenfield"\n',
        encoding="utf-8",
    )
    config = load_config(path, env={})
    assert config.general.mode == "greenfield"
    assert config.general.sandbox_backend == "auto"


def test_load_config_invalid_toml_raises_parse_error(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text("this is not = = valid toml", encoding="utf-8")
    with pytest.raises(CodeGuardianError) as excinfo:
        load_config(path, env={})
    assert excinfo.value.code is ErrorCode.CONFIG_PARSE_ERROR
    assert excinfo.value.category is ErrorCategory.USER


def test_load_config_invalid_schema_raises_invalid(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('[general]\nmode = "nope"\n', encoding="utf-8")
    with pytest.raises(CodeGuardianError) as excinfo:
        load_config(path, env={})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "config.general.mode" in excinfo.value.message


def test_load_config_directory_raises_invalid(tmp_path: Path):
    with pytest.raises(CodeGuardianError) as excinfo:
        load_config(tmp_path, env={})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert "not a regular file" in excinfo.value.message


def test_load_config_explicit_path_wins_over_env(tmp_path: Path):
    explicit = tmp_path / "explicit.toml"
    explicit.write_text('[general]\nlog_level = "debug"\n', encoding="utf-8")

    other = tmp_path / "other.toml"
    other.write_text('[general]\nlog_level = "error"\n', encoding="utf-8")

    config = load_config(explicit, env={"CODEGUARDIAN_CONFIG": str(other)})
    assert config.general.log_level == "debug"


def test_load_config_env_var_used_when_no_path(tmp_path: Path):
    path = tmp_path / "from-env.toml"
    path.write_text('[general]\nlog_level = "warning"\n', encoding="utf-8")

    config = load_config(env={"CODEGUARDIAN_CONFIG": str(path)})
    assert config.general.log_level == "warning"


def test_load_config_home_env_var_used_when_no_config_env(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('[general]\nlog_level = "error"\n', encoding="utf-8")

    config = load_config(env={"CODEGUARDIAN_HOME": str(tmp_path)})
    assert config.general.log_level == "error"


def test_load_config_uses_process_env_when_env_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "config.toml"
    path.write_text('[general]\nlog_level = "debug"\n', encoding="utf-8")
    monkeypatch.setenv("CODEGUARDIAN_CONFIG", str(path))

    config = load_config()
    assert config.general.log_level == "debug"


def test_secrets_module_exports_service_name():
    from codeguardian.core.config import SERVICE_NAME

    assert SERVICE_NAME == "codeguardian"


def test_get_api_key_returns_none_when_keyring_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    from codeguardian.core.config import secrets

    monkeypatch.setattr(secrets, "_KEYRING_AVAILABLE", False)
    assert secrets.get_api_key("anthropic") is None


def test_get_api_key_returns_none_when_backend_errors(
    monkeypatch: pytest.MonkeyPatch,
):
    from codeguardian.core.config import secrets

    monkeypatch.setattr(secrets, "_KEYRING_AVAILABLE", True)

    class FakeKeyring:
        @staticmethod
        def get_password(service: str, provider: str) -> str:
            raise secrets.KeyringError("no backend")

    monkeypatch.setattr(secrets, "keyring", FakeKeyring)
    assert secrets.get_api_key("anthropic") is None


def test_get_api_key_returns_stored_value(monkeypatch: pytest.MonkeyPatch):
    from codeguardian.core.config import secrets

    monkeypatch.setattr(secrets, "_KEYRING_AVAILABLE", True)

    class FakeKeyring:
        @staticmethod
        def get_password(service: str, provider: str) -> str:
            assert service == "codeguardian"
            assert provider == "anthropic"
            return "sk-ant-stored"

    monkeypatch.setattr(secrets, "keyring", FakeKeyring)
    assert secrets.get_api_key("anthropic") == "sk-ant-stored"


def test_set_api_key_raises_when_keyring_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    from codeguardian.core.config import secrets

    monkeypatch.setattr(secrets, "_KEYRING_AVAILABLE", False)
    with pytest.raises(CodeGuardianError) as excinfo:
        secrets.set_api_key("anthropic", "sk-ant-x")
    assert excinfo.value.code is ErrorCode.SECRET_KEYCHAIN_ERROR


def test_set_api_key_rejects_empty_provider(
    monkeypatch: pytest.MonkeyPatch,
):
    from codeguardian.core.config import secrets

    monkeypatch.setattr(secrets, "_KEYRING_AVAILABLE", True)
    with pytest.raises(CodeGuardianError) as excinfo:
        secrets.set_api_key("", "sk-ant-x")
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID


def test_set_api_key_rejects_empty_value(
    monkeypatch: pytest.MonkeyPatch,
):
    from codeguardian.core.config import secrets

    monkeypatch.setattr(secrets, "_KEYRING_AVAILABLE", True)
    with pytest.raises(CodeGuardianError) as excinfo:
        secrets.set_api_key("anthropic", "")
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID


def test_set_api_key_stores_value(monkeypatch: pytest.MonkeyPatch):
    from codeguardian.core.config import secrets

    monkeypatch.setattr(secrets, "_KEYRING_AVAILABLE", True)
    calls: list[tuple[str, str, str]] = []

    class FakeKeyring:
        @staticmethod
        def set_password(service: str, provider: str, value: str) -> None:
            calls.append((service, provider, value))

    monkeypatch.setattr(secrets, "keyring", FakeKeyring)
    secrets.set_api_key("anthropic", "sk-ant-stored")
    assert calls == [("codeguardian", "anthropic", "sk-ant-stored")]


def test_set_api_key_raises_keychain_error_on_backend_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    from codeguardian.core.config import secrets

    monkeypatch.setattr(secrets, "_KEYRING_AVAILABLE", True)

    class FakeKeyring:
        @staticmethod
        def set_password(service: str, provider: str, value: str) -> None:
            raise secrets.KeyringError("backend locked")

    monkeypatch.setattr(secrets, "keyring", FakeKeyring)
    with pytest.raises(CodeGuardianError) as excinfo:
        secrets.set_api_key("anthropic", "sk-ant-stored")
    assert excinfo.value.code is ErrorCode.SECRET_KEYCHAIN_ERROR


def test_delete_api_key_succeeds_when_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    from codeguardian.core.config import secrets

    monkeypatch.setattr(secrets, "_KEYRING_AVAILABLE", True)

    class FakeKeyring:
        @staticmethod
        def delete_password(service: str, provider: str) -> None:
            raise secrets.PasswordDeleteError("not present")

    monkeypatch.setattr(secrets, "keyring", FakeKeyring)
    # Should not raise.
    secrets.delete_api_key("anthropic")


def test_delete_api_key_raises_keychain_error_on_backend_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    from codeguardian.core.config import secrets

    monkeypatch.setattr(secrets, "_KEYRING_AVAILABLE", True)

    class FakeKeyring:
        @staticmethod
        def delete_password(service: str, provider: str) -> None:
            raise secrets.KeyringError("backend locked")

    monkeypatch.setattr(secrets, "keyring", FakeKeyring)
    with pytest.raises(CodeGuardianError) as excinfo:
        secrets.delete_api_key("anthropic")
    assert excinfo.value.code is ErrorCode.SECRET_KEYCHAIN_ERROR


def test_delete_api_key_rejects_empty_provider(
    monkeypatch: pytest.MonkeyPatch,
):
    from codeguardian.core.config import secrets

    monkeypatch.setattr(secrets, "_KEYRING_AVAILABLE", True)
    with pytest.raises(CodeGuardianError) as excinfo:
        secrets.delete_api_key("")
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID


def test_config_parse_error_exists_in_taxonomy():
    assert category_of(ErrorCode.CONFIG_PARSE_ERROR) is ErrorCategory.USER
    policy = retry_policy_of(ErrorCode.CONFIG_PARSE_ERROR)
    assert policy.retryable is False
    assert policy.max_retries == 0


def test_secret_keychain_error_exists_in_taxonomy():
    assert category_of(ErrorCode.SECRET_KEYCHAIN_ERROR) is ErrorCategory.USER
    policy = retry_policy_of(ErrorCode.SECRET_KEYCHAIN_ERROR)
    assert policy.retryable is False
    assert policy.max_retries == 0
