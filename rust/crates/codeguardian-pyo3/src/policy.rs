//! Policy bindings for the CodeGuardian native extension.
//!
//! Three functions expose the policy crate to Python. `check_sandbox_spec`
//! validates a sandbox specification against a policy. `check_plugin_permissions`
//! validates a plugin's declared permissions against a policy.
//! `detect_host_limits` reads the host's resource capabilities.
//!
//! All three use the `_native.KernelError` exception class so the
//! Python side has one error type to translate.

use std::path::PathBuf;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use codeguardian_policy::{
    check_plugin_permissions as policy_check_plugin_permissions,
    check_sandbox_spec as policy_check_sandbox_spec, FilesystemLevel, HostLimits, PluginPermissions,
    PluginPolicy, PolicyError, SandboxPolicy,
};
use codeguardian_sandbox::{ResourceQuota, SandboxSpec};

use crate::kernel::build_exception;

/// Check a sandbox spec against a sandbox policy.
///
/// Takes the spec's language, quota, and the policy's ceiling as
/// primitive arguments. Returns `None` on success.
///
/// Raises `KernelError` with code `SANDBOX_QUOTA_EXCEEDED` when the
/// spec exceeds the policy's bound on any field, or when the spec's
/// language is not in the policy's allow list.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn check_sandbox_spec(
    py: Python<'_>,
    language: String,
    memory_mb: u32,
    cpu_cores: u32,
    process_limit: u32,
    max_memory_mb: u32,
    max_cpu_cores: u32,
    max_process_limit: u32,
    allowed_languages: Vec<String>,
) -> PyResult<()> {
    let mut spec = SandboxSpec::new(language, PathBuf::from("."));
    spec.quota = ResourceQuota::new(memory_mb, cpu_cores, process_limit);
    let policy = SandboxPolicy::new(
        ResourceQuota::new(max_memory_mb, max_cpu_cores, max_process_limit),
        allowed_languages,
    );
    policy_check_sandbox_spec(&spec, &policy).map_err(|err| map_policy_error(py, &err))
}

/// Check a plugin's declared permissions against a plugin policy.
///
/// Returns `None` on success.
///
/// Raises `ValueError` if a filesystem level name is not one of
/// `"none"`, `"sandbox_only"`, `"read_only"`, or `"full"`.
/// Raises `KernelError` with code `PLUGIN_PERMISSION_DENIED` when the
/// plugin requests a permission the policy does not grant.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn check_plugin_permissions(
    py: Python<'_>,
    plugin_name: String,
    filesystem: String,
    network: bool,
    process_spawn: bool,
    env_read: Vec<String>,
    allowed_filesystem: String,
    allow_network: bool,
    allow_process_spawn: bool,
    allowed_env_keys: Vec<String>,
) -> PyResult<()> {
    let requested_level =
        parse_filesystem_level(&filesystem).map_err(PyValueError::new_err)?;
    let allowed_level =
        parse_filesystem_level(&allowed_filesystem).map_err(PyValueError::new_err)?;

    let permissions = PluginPermissions {
        filesystem: requested_level,
        network,
        process_spawn,
        env_read,
    };
    let policy = PluginPolicy {
        allowed_filesystem: allowed_level,
        allow_network,
        allow_process_spawn,
        allowed_env_keys,
    };

    policy_check_plugin_permissions(&plugin_name, &permissions, &policy)
        .map_err(|err| map_policy_error(py, &err))
}

/// Detect the host's resource limits.
///
/// Returns a dict with three keys: `total_memory_mb`,
/// `total_cpu_cores`, and `max_processes`.
///
/// Raises `KernelError` if the host's limits cannot be detected. On
/// platforms without `/proc`, detection always fails.
#[pyfunction]
pub fn detect_host_limits(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let limits = HostLimits::detect().map_err(|err| {
        build_exception(
            py,
            "HOST_DETECTION_FAILED",
            &format!("host detection failed: {err}"),
            PyDict::new(py).into_any(),
        )
    })?;

    let dict = PyDict::new(py);
    dict.set_item("total_memory_mb", limits.total_memory_mb)?;
    dict.set_item("total_cpu_cores", limits.total_cpu_cores)?;
    dict.set_item("max_processes", limits.max_processes)?;
    Ok(dict)
}

/// Parse a filesystem level name into its enum variant.
fn parse_filesystem_level(name: &str) -> Result<FilesystemLevel, String> {
    match name {
        "none" => Ok(FilesystemLevel::None),
        "sandbox_only" => Ok(FilesystemLevel::SandboxOnly),
        "read_only" => Ok(FilesystemLevel::ReadOnly),
        "full" => Ok(FilesystemLevel::Full),
        other => Err(format!(
            "unknown filesystem level: {other} (expected one of \
             none, sandbox_only, read_only, full)"
        )),
    }
}

/// Convert a policy error into a `KernelError` exception.
fn map_policy_error(py: Python<'_>, err: &PolicyError) -> PyErr {
    match err {
        PolicyError::SandboxSpecRejected { reason } => {
            let details = PyDict::new(py);
            let _ = details.set_item("reason", reason);
            build_exception(
                py,
                "SANDBOX_QUOTA_EXCEEDED",
                &err.to_string(),
                details.into_any(),
            )
        }
        PolicyError::PluginPermissionDenied {
            plugin,
            permission,
        } => {
            let details = PyDict::new(py);
            let _ = details.set_item("plugin", plugin);
            let _ = details.set_item("permission", permission);
            build_exception(
                py,
                "PLUGIN_PERMISSION_DENIED",
                &err.to_string(),
                details.into_any(),
            )
        }
        PolicyError::ManifestInvalid { plugin, reason } => {
            let details = PyDict::new(py);
            let _ = details.set_item("plugin", plugin);
            let _ = details.set_item("reason", reason);
            build_exception(
                py,
                "PLUGIN_MANIFEST_INVALID",
                &err.to_string(),
                details.into_any(),
            )
        }
    }
}
