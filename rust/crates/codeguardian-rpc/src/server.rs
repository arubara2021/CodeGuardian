//! JSON-RPC 2.0 server over a byte stream.
//!
//! Reads line-delimited JSON requests, dispatches them to registered
//! handlers by method name, and writes line-delimited JSON responses.
//! Notifications produce no response. The dispatch registry is
//! populated by callers before ``serve`` runs, so later phases can
//! attach method handlers without editing this file.

use std::collections::HashMap;
use std::io::{self, BufRead, Write};
use std::sync::Arc;

use serde_json::{json, Map, Value};

use crate::protocol::{RpcError, JSONRPC_VERSION};

/// JSON-RPC standard code: the JSON could not be parsed.
pub const PARSE_ERROR: i64 = -32700;
/// JSON-RPC standard code: the JSON was not a valid request object.
pub const INVALID_REQUEST: i64 = -32600;
/// JSON-RPC standard code: no handler is registered for the method.
pub const METHOD_NOT_FOUND: i64 = -32601;
/// JSON-RPC standard code: the handler returned an error.
pub const INTERNAL_ERROR: i64 = -32603;

/// A function invoked for a registered RPC method.
///
/// Receives the request's ``params`` value and returns either a result
/// value or a typed error.
pub type Handler = Arc<dyn Fn(Value) -> Result<Value, RpcError> + Send + Sync + 'static>;

/// A JSON-RPC 2.0 server.
pub struct RpcServer {
    handlers: HashMap<String, Handler>,
}

impl Default for RpcServer {
    fn default() -> Self {
        Self::new()
    }
}

impl RpcServer {
    /// Create a server with no registered handlers.
    pub fn new() -> Self {
        Self {
            handlers: HashMap::new(),
        }
    }

    /// Register ``handler`` for ``method``.
    ///
    /// Registering the same method twice replaces the first handler.
    pub fn register<F>(&mut self, method: impl Into<String>, handler: F)
    where
        F: Fn(Value) -> Result<Value, RpcError> + Send + Sync + 'static,
    {
        self.handlers.insert(method.into(), Arc::new(handler));
    }

    /// Return the number of registered handlers.
    pub fn handler_count(&self) -> usize {
        self.handlers.len()
    }

    /// Process one line of input and return the response, if any.
    ///
    /// Empty lines and notifications produce ``None``. Requests and
    /// malformed lines produce a JSON-RPC response string.
    pub fn handle_line(&self, line: &str) -> Option<String> {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            return None;
        }
        let parsed: Value = match serde_json::from_str(trimmed) {
            Ok(value) => value,
            Err(_) => {
                return Some(error_response(Value::Null, PARSE_ERROR, "Parse error", None));
            }
        };
        let id = parsed.get("id").cloned();
        let method = match parsed.get("method").and_then(Value::as_str) {
            Some(name) => name,
            None => {
                return Some(error_response(
                    id.unwrap_or(Value::Null),
                    INVALID_REQUEST,
                    "Missing method",
                    None,
                ));
            }
        };
        let response_id = match id {
            Some(value) => value,
            None => return None,
        };
        let params = parsed.get("params").cloned().unwrap_or(Value::Null);
        let handler = match self.handlers.get(method) {
            Some(found) => Arc::clone(found),
            None => {
                return Some(error_response(
                    response_id,
                    METHOD_NOT_FOUND,
                    "Method not found",
                    None,
                ));
            }
        };
        match handler(params) {
            Ok(result) => Some(success_response(response_id, result)),
            Err(err) => Some(error_response(
                response_id,
                err.code,
                &err.message,
                err.data,
            )),
        }
    }

    /// Serve requests from ``reader``, writing responses to ``writer``.
    ///
    /// Returns when ``reader`` reaches EOF. IO errors are propagated.
    pub fn serve<R: BufRead, W: Write>(
        &self,
        reader: R,
        mut writer: W,
    ) -> io::Result<()> {
        for line in reader.lines() {
            let line = line?;
            if let Some(response) = self.handle_line(&line) {
                writer.write_all(response.as_bytes())?;
                writer.write_all(b"\n")?;
                writer.flush()?;
            }
        }
        Ok(())
    }
}

fn success_response(id: Value, result: Value) -> String {
    json!({
        "jsonrpc": JSONRPC_VERSION,
        "id": id,
        "result": result,
    })
    .to_string()
}

fn error_response(id: Value, code: i64, message: &str, data: Option<Value>) -> String {
    let mut error = Map::new();
    error.insert("code".to_string(), Value::from(code));
    error.insert("message".to_string(), Value::from(message));
    if let Some(payload) = data {
        error.insert("data".to_string(), payload);
    }
    json!({
        "jsonrpc": JSONRPC_VERSION,
        "id": id,
        "error": Value::Object(error),
    })
    .to_string()
}
