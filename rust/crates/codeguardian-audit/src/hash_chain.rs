//! Canonical JSON and hashing primitives.
//!
//! Canonical JSON is the byte representation used as the input to the
//! payload hash. It differs from `serde_json::to_string` in one way:
//! object keys are emitted in lexicographic order. Without that
//! ordering, two structurally equal payloads would hash differently
//! depending on the order their keys were inserted.
//!
//! The encoder is written by hand rather than assembled from a
//! `BTreeMap` rebuild of the input. The tree is walked once, and each
//! value's bytes are appended directly to the output buffer.

use serde_json::Value;
use sha2::{Digest, Sha256};

/// The hexadecimal alphabet used by `hex_encode`.
const HEX_DIGITS: &[u8; 16] = b"0123456789abcdef";

/// Serialize a JSON value into canonical bytes.
///
/// Object keys are sorted lexicographically. No whitespace is emitted.
/// Strings are escaped using the JSON standard. Two values that are
/// structurally equal produce identical bytes.
pub fn canonical_json_bytes(value: &Value) -> Vec<u8> {
    let mut out: Vec<u8> = Vec::new();
    write_canonical(&mut out, value);
    out
}

/// Append the canonical encoding of `value` to `out`.
fn write_canonical(out: &mut Vec<u8>, value: &Value) {
    match value {
        Value::Null => out.extend_from_slice(b"null"),
        Value::Bool(true) => out.extend_from_slice(b"true"),
        Value::Bool(false) => out.extend_from_slice(b"false"),
        Value::Number(n) => {
            let text = n.to_string();
            out.extend_from_slice(text.as_bytes());
        }
        Value::String(s) => write_json_string(out, s),
        Value::Array(items) => {
            out.push(b'[');
            let mut first = true;
            for item in items {
                if !first {
                    out.push(b',');
                }
                first = false;
                write_canonical(out, item);
            }
            out.push(b']');
        }
        Value::Object(map) => {
            out.push(b'{');
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            let mut first = true;
            for key in keys {
                if !first {
                    out.push(b',');
                }
                first = false;
                write_json_string(out, key);
                out.push(b':');
                if let Some(inner) = map.get(key.as_str()) {
                    write_canonical(out, inner);
                }
            }
            out.push(b'}');
        }
    }
}

/// Append a JSON-escaped, quoted string to `out`.
///
/// String escaping is delegated to `serde_json` so the rules match the
/// JSON specification exactly. Falling back to an empty string on
/// serialization failure is safe: a `str` cannot contain anything that
/// fails to serialize.
fn write_json_string(out: &mut Vec<u8>, s: &str) {
    let quoted = serde_json::to_string(s).unwrap_or_else(|_| String::from("\"\""));
    out.extend_from_slice(quoted.as_bytes());
}

/// Return the hex-encoded SHA-256 digest of `bytes`.
pub fn sha256_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    let digest = hasher.finalize();
    hex_encode(&digest)
}

/// Return the lower-case hex encoding of `bytes`.
pub fn hex_encode(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for &byte in bytes {
        out.push(HEX_DIGITS[(byte >> 4) as usize] as char);
        out.push(HEX_DIGITS[(byte & 0x0f) as usize] as char);
    }
    out
}

/// Return the hex-encoded SHA-256 of the payload's canonical bytes.
pub fn compute_payload_hash(payload: &Value) -> String {
    let bytes = canonical_json_bytes(payload);
    sha256_hex(&bytes)
}

/// Return the hex-encoded SHA-256 of `prev_hash` concatenated with
/// `payload_hash`.
///
/// Both arguments are hex strings, not raw bytes. The concatenation is
/// a string concatenation, matching the design document's formula:
/// `entry_hash = sha256(prev_hash + payload_hash)`.
pub fn compute_entry_hash(prev_hash: &str, payload_hash: &str) -> String {
    let mut input = String::with_capacity(prev_hash.len() + payload_hash.len());
    input.push_str(prev_hash);
    input.push_str(payload_hash);
    sha256_hex(input.as_bytes())
}
