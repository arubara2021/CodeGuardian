//! Handler implementations for the JSON-RPC daemon.
//!
//! Each handler receives the request's `params` as untyped JSON and
//! returns a JSON value or an `RpcError`. Parameter validation is
//! performed here; the sandbox and audit crates receive typed inputs
//! and never see unvalidated JSON.
//!
//! Every error that comes from a lower crate is wrapped as
//! `INTERNAL_ERROR` with the crate's message. The Python bridge
//! translates any response with an `error` field into a
//! `CodeGuardianError` with `SYS_INTERNAL`; the details carry the
//! original message for diagnostics.

use std::path::PathBuf;

use serde_json::{json, Value};

use codeguardian_audit::{export_json_array, export_jsonl, verify_chain, AuditWriter};
use codeguardian_rpc::RpcError;
use codeguardian_sandbox::{
    BubblewrapBackend, DockerBackend, SandboxBackend, SandboxHandle, SandboxId, SandboxSpec,
};

/// The JSON-RPC error code for invalid method parameters.
const INVALID_PARAMS: i64 = -32602;

/// Return a report of the sandbox backends available on this host.
///
/// The report is a static description of what the binary knows. It
/// does not probe the host for `bwrap` or `docker` binaries; that
/// probing happens when a sandbox is actually created.
pub async fn sandbox_health(_params: Value) -> Result<Value, RpcError> {
    let bubblewrap = BubblewrapBackend::new();
    let docker = DockerBackend::new();
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
        }
    }))
}

/// Create a sandbox handle.
///
/// Required parameters: `language` and `working_dir`. Optional:
/// `backend`, defaulting to `"bubblewrap"`.
pub async fn sandbox_create(params: Value) -> Result<Value, RpcError> {
    let language = get_string(&params, "language")?;
    let working_dir = get_string(&params, "working_dir")?;
    let backend_name = params
        .get("backend")
        .and_then(Value::as_str)
        .unwrap_or("bubblewrap");

    let spec = SandboxSpec::new(language, PathBuf::from(working_dir));

    let handle_result = match backend_name {
        "bubblewrap" => BubblewrapBackend::new().create(&spec).await,
        "docker" => DockerBackend::new().create(&spec).await,
        other => {
            return Err(RpcError {
                code: INVALID_PARAMS,
                message: format!("unknown backend: {other}"),
                data: None,
            });
        }
    };

    let handle = handle_result.map_err(|err| internal_error(err.to_string()))?;
    Ok(handle_to_json(&handle))
}

/// Destroy a sandbox handle.
///
/// The parameters are the fields of the handle returned by
/// `sandbox.create`: `id`, `backend`, `language`, `working_dir`, and
/// `network_enabled`.
pub async fn sandbox_destroy(params: Value) -> Result<Value, RpcError> {
    let id = get_string(&params, "id")?;
    let backend_name = get_string(&params, "backend")?;
    let language = get_string(&params, "language")?;
    let working_dir = get_string(&params, "working_dir")?;
    let network_enabled = params
        .get("network_enabled")
        .and_then(Value::as_bool)
        .unwrap_or(false);

    let handle = SandboxHandle {
        id: SandboxId::new(id),
        backend: backend_name.clone(),
        language,
        working_dir: PathBuf::from(working_dir),
        network_enabled,
    };

    let result = match backend_name.as_str() {
        "bubblewrap" => BubblewrapBackend::new().destroy(handle).await,
        "docker" => DockerBackend::new().destroy(handle).await,
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

/// Append one entry to the audit log at `path`.
///
/// Required parameters: `path` and `payload`. Returns the entry that
/// was written, including both hashes.
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
///
/// Required parameter: `path`. Returns the number of entries that
/// passed and the head hash.
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
///
/// Required parameters: `path` and `format`. Format is `"jsonl"` or
/// `"json"`. Returns the entry count, the byte count, and the exported
/// content as a UTF-8 string.
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
