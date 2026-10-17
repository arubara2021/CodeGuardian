//! Handler implementations for the JSON-RPC daemon.
//!
//! Each handler receives the request's `params` as untyped JSON and
//! returns a JSON value or an `RpcError`. Parameter validation is
//! performed here; the sandbox and audit crates receive typed inputs
//! and never see unvalidated JSON.
//!
//! # Backend routing
//!
//! Every sandbox handler matches on the `backend` field and dispatches
//! to the corresponding implementation. The four backends — bubblewrap,
//! docker, seatbelt, firecracker — are all wired in. The two stubs
//! return a `CreateFailed` that names the missing implementation.
//!
//! # Language agnosticism
//!
//! `sandbox.create` does not check the language. A backend that can
//! run arbitrary Linux binaries (bubblewrap) will run any language; a
//! backend that runs container images (docker) uses whatever image the
//! caller supplies. The `language` field is a caller hint, not an
//! allow list.

use std::collections::HashMap;
use std::path::PathBuf;

use serde_json::{json, Value};

use codeguardian_audit::{export_json_array, export_jsonl, verify_chain, AuditWriter};
use codeguardian_rpc::RpcError;
use codeguardian_sandbox::{
    BubblewrapBackend, DockerBackend, ExecutionRequest, FirecrackerBackend, SandboxBackend,
    SandboxHandle, SandboxId, SandboxSpec, SeatbeltBackend,
};

/// The JSON-RPC error code for invalid method parameters.
const INVALID_PARAMS: i64 = -32602;

/// Return a report of the sandbox backends available on this host.
pub async fn sandbox_health(_params: Value) -> Result<Value, RpcError> {
    let bubblewrap = BubblewrapBackend::new();
    let docker = DockerBackend::new();
    let seatbelt = SeatbeltBackend::new();
    let firecracker = FirecrackerBackend::new();
    Ok(json!({
        "status": "ok",
        "backends": {
            "bubblewrap": {
                "supports_python": bubblewrap.supports_language("python"),
                "supports_typescript": bubblewrap.supports_language("typescript"),
            },
            "docker": {
                "supports_python": docker.supports_language("python"),
                "supports_typescript": docker.supports_language("typescript"),
            },
            "seatbelt": {
                "supports_python": seatbelt.supports_language("python"),
                "supports_typescript": seatbelt.supports_language("typescript"),
            },
            "firecracker": {
                "supports_python": firecracker.supports_language("python"),
                "supports_typescript": firecracker.supports_language("typescript"),
            },
        }
    }))
}

/// Create a sandbox handle.
///
/// Required parameters: `language` and `working_dir`. Optional:
/// `backend` (default `"bubblewrap"`), `memory_mb`, `cpu_cores`,
/// `process_limit`, `network_enabled`, `image`.
///
/// The `image` parameter is required when `backend` is `"docker"`.
/// Supplying an image to bubblewrap is allowed but ignored.
pub async fn sandbox_create(params: Value) -> Result<Value, RpcError> {
    let language = get_string(&params, "language")?;
    let working_dir = get_string(&params, "working_dir")?;
    let backend_name = params
        .get("backend")
        .and_then(Value::as_str)
        .unwrap_or("bubblewrap");

    let mut spec = SandboxSpec::new(language, PathBuf::from(working_dir));
    if let Some(memory) = params.get("memory_mb").and_then(Value::as_u64) {
        spec.quota.memory_mb = memory as u32;
    }
    if let Some(cores) = params.get("cpu_cores").and_then(Value::as_u64) {
        spec.quota.cpu_cores = cores as u32;
    }
    if let Some(pids) = params.get("process_limit").and_then(Value::as_u64) {
        spec.quota.process_limit = pids as u32;
    }
    if let Some(image) = params.get("image").and_then(Value::as_str) {
        spec.image = Some(image.to_owned());
    }
    if params
        .get("network_enabled")
        .and_then(Value::as_bool)
        .unwrap_or(false)
    {
        spec.network = codeguardian_sandbox::NetworkPolicy::allow_for_dependencies();
    }

    let handle = match backend_name {
        "bubblewrap" => BubblewrapBackend::new().create(&spec).await,
        "docker" => DockerBackend::new().create(&spec).await,
        "seatbelt" => SeatbeltBackend::new().create(&spec).await,
        "firecracker" => FirecrackerBackend::new().create(&spec).await,
        other => {
            return Err(RpcError {
                code: INVALID_PARAMS,
                message: format!("unknown backend: {other}"),
                data: None,
            });
        }
    }
    .map_err(|err| internal_error(err.to_string()))?;

    Ok(handle_to_json(&handle))
}

/// Destroy a sandbox handle.
pub async fn sandbox_destroy(params: Value) -> Result<Value, RpcError> {
    let handle = handle_from_json(&params)?;

    let result = match handle.backend.as_str() {
        "bubblewrap" => BubblewrapBackend::new().destroy(handle).await,
        "docker" => DockerBackend::new().destroy(handle).await,
        "seatbelt" => SeatbeltBackend::new().destroy(handle).await,
        "firecracker" => FirecrackerBackend::new().destroy(handle).await,
        other => {
            return Err(RpcError {
                code: INVALID_PARAMS,
                message: format!("unknown backend: {other}"),
                data: None,
            });
        }
    };

    result.map_err(|err| internal_error(err.to_string()))?;
    Ok(json!({"destroyed": true}))
}

/// Run a command inside a live sandbox.
pub async fn sandbox_execute(params: Value) -> Result<Value, RpcError> {
    let handle_value = params.get("handle").ok_or_else(|| RpcError {
        code: INVALID_PARAMS,
        message: "missing 'handle' parameter".to_owned(),
        data: None,
    })?;
    let handle = handle_from_json(handle_value)?;

    let command: Vec<String> = params
        .get("command")
        .and_then(Value::as_array)
        .map(|arr| {
            arr.iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .ok_or_else(|| RpcError {
            code: INVALID_PARAMS,
            message: "missing or invalid 'command' parameter".to_owned(),
            data: None,
        })?;

    let mut request = ExecutionRequest::new(command);
    if let Some(cwd) = params.get("cwd").and_then(Value::as_str) {
        request.cwd = Some(PathBuf::from(cwd));
    }
    if let Some(env_value) = params.get("env").and_then(Value::as_object) {
        let mut env: HashMap<String, String> = HashMap::new();
        for (key, value) in env_value {
            if let Some(text) = value.as_str() {
                env.insert(key.clone(), text.to_owned());
            }
        }
        request.env = env;
    }
    if let Some(timeout_ms) = params.get("timeout_ms").and_then(Value::as_u64) {
        request.timeout_ms = Some(timeout_ms);
    }
    if let Some(stdin_text) = params.get("stdin").and_then(Value::as_str) {
        request.stdin = Some(stdin_text.as_bytes().to_vec());
    }

    let result = match handle.backend.as_str() {
        "bubblewrap" => BubblewrapBackend::new().execute(&handle, &request).await,
        "docker" => DockerBackend::new().execute(&handle, &request).await,
        "seatbelt" => SeatbeltBackend::new().execute(&handle, &request).await,
        "firecracker" => FirecrackerBackend::new().execute(&handle, &request).await,
        other => {
            return Err(RpcError {
                code: INVALID_PARAMS,
                message: format!("unknown backend: {other}"),
                data: None,
            });
        }
    };

    let outcome = result.map_err(|err| internal_error(err.to_string()))?;
    Ok(json!({
        "exit_code": outcome.exit_code,
        "stdout": String::from_utf8_lossy(&outcome.stdout),
        "stderr": String::from_utf8_lossy(&outcome.stderr),
        "duration_ms": outcome.duration_ms,
        "timed_out": outcome.timed_out,
        "oom_killed": outcome.oom_killed,
        "output_truncated": outcome.output_truncated,
    }))
}

/// Append one entry to the audit log at `path`.
pub async fn audit_write(params: Value) -> Result<Value, RpcError> {
    let path = get_string(&params, "path")?;
    let payload = params.get("payload").cloned().unwrap_or(Value::Null);

    let writer = AuditWriter::new(path);
    let entry = writer
        .append(payload)
        .await
        .map_err(|err| internal_error(err.to_string()))?;

    serde_json::to_value(entry).map_err(|err| internal_error(err.to_string()))
}

/// Verify the audit chain at `path`.
pub async fn audit_verify(params: Value) -> Result<Value, RpcError> {
    let path = get_string(&params, "path")?;
    let summary = verify_chain(path)
        .await
        .map_err(|err| internal_error(err.to_string()))?;
    Ok(json!({
        "entries_checked": summary.entries_checked,
        "head_hash": summary.head_hash,
    }))
}

/// Export the audit log at `path`.
pub async fn audit_export(params: Value) -> Result<Value, RpcError> {
    let path = get_string(&params, "path")?;
    let format = get_string(&params, "format")?;

    let mut buf: Vec<u8> = Vec::new();
    let count = match format.as_str() {
        "jsonl" => export_jsonl(path, &mut buf).await,
        "json" => export_json_array(path, &mut buf).await,
        other => {
            return Err(RpcError {
                code: INVALID_PARAMS,
                message: format!("unknown export format: {other}"),
                data: None,
            });
        }
    }
    .map_err(|err| internal_error(err.to_string()))?;

    Ok(json!({
        "count": count,
        "bytes": buf.len(),
        "content": String::from_utf8_lossy(&buf),
    }))
}

/// Return the JSON representation of a sandbox handle.
fn handle_to_json(handle: &SandboxHandle) -> Value {
    json!({
        "id": handle.id.as_str(),
        "backend": handle.backend,
        "language": handle.language,
        "working_dir": handle.working_dir.display().to_string(),
        "network_enabled": handle.network_enabled,
        "memory_mb": handle.memory_mb,
        "cpu_cores": handle.cpu_cores,
        "process_limit": handle.process_limit,
        "image": handle.image,
    })
}

/// Parse a sandbox handle from a JSON object.
fn handle_from_json(value: &Value) -> Result<SandboxHandle, RpcError> {
    let id = get_string(value, "id")?;
    let backend = get_string(value, "backend")?;
    let language = get_string(value, "language")?;
    let working_dir = get_string(value, "working_dir")?;
    let network_enabled = value
        .get("network_enabled")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let memory_mb = value
        .get("memory_mb")
        .and_then(Value::as_u64)
        .unwrap_or(1024) as u32;
    let cpu_cores = value
        .get("cpu_cores")
        .and_then(Value::as_u64)
        .unwrap_or(2) as u32;
    let process_limit = value
        .get("process_limit")
        .and_then(Value::as_u64)
        .unwrap_or(256) as u32;
    let image = value
        .get("image")
        .and_then(Value::as_str)
        .map(str::to_owned);

    Ok(SandboxHandle {
        id: SandboxId::new(id),
        backend,
        language,
        working_dir: PathBuf::from(working_dir),
        network_enabled,
        memory_mb,
        cpu_cores,
        process_limit,
        image,
    })
}

/// Read a required string parameter, or return `INVALID_PARAMS`.
fn get_string(params: &Value, key: &str) -> Result<String, RpcError> {
    params
        .get(key)
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or_else(|| RpcError {
            code: INVALID_PARAMS,
            message: format!("missing or invalid '{key}' parameter"),
            data: None,
        })
}

/// Build an `INTERNAL_ERROR` response from a message.
fn internal_error(message: String) -> RpcError {
    RpcError {
        code: -32603,
        message,
        data: None,
    }
}
