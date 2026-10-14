//! JSON-RPC 2.0 client over a byte stream.
//!
//! Sends requests and reads responses. Requests carry a monotonic id;
//! responses are matched by id. Notifications carry no id and expect
//! no response.

use std::io::{self, BufRead, Write};

use serde_json::{json, Value};

use crate::protocol::{RpcError, JSONRPC_VERSION};

/// A JSON-RPC 2.0 client.
pub struct RpcClient<R: BufRead, W: Write> {
    reader: R,
    writer: W,
    next_id: u64,
}

impl<R: BufRead, W: Write> RpcClient<R, W> {
    /// Create a client over the given reader and writer.
    pub fn new(reader: R, writer: W) -> Self {
        Self {
            reader,
            writer,
            next_id: 1,
        }
    }

    /// Send a request and wait for its response.
    pub fn call(
        &mut self,
        method: &str,
        params: Value,
    ) -> io::Result<Result<Value, RpcError>> {
        let id = self.next_id;
        self.next_id = self.next_id.saturating_add(1);
        let request = json!({
            "jsonrpc": JSONRPC_VERSION,
            "id": id,
            "method": method,
            "params": params,
        });
        self.writer.write_all(request.to_string().as_bytes())?;
        self.writer.write_all(b"\n")?;
        self.writer.flush()?;
        loop {
            let mut line = String::new();
            let read = self.reader.read_line(&mut line)?;
            if read == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "peer closed before responding",
                ));
            }
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }
            let parsed: Value = match serde_json::from_str(trimmed) {
                Ok(value) => value,
                Err(exc) => return Err(io::Error::new(io::ErrorKind::InvalidData, exc)),
            };
            if parsed.get("id").and_then(Value::as_u64) != Some(id) {
                continue;
            }
            if let Some(error) = parsed.get("error") {
                let code = error.get("code").and_then(Value::as_i64).unwrap_or(0);
                let message = error
                    .get("message")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                let data = error.get("data").cloned();
                return Ok(Err(RpcError {
                    code,
                    message,
                    data,
                }));
            }
            return Ok(Ok(parsed.get("result").cloned().unwrap_or(Value::Null)));
        }
    }

    /// Send a notification. No response is expected.
    pub fn notify(&mut self, method: &str, params: Value) -> io::Result<()> {
        let message = json!({
            "jsonrpc": JSONRPC_VERSION,
            "method": method,
            "params": params,
        });
        self.writer.write_all(message.to_string().as_bytes())?;
        self.writer.write_all(b"\n")?;
        self.writer.flush()?;
        Ok(())
    }
}
