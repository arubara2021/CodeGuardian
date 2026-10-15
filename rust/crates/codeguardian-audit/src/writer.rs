//! Append-only audit writer.
//!
//! The writer appends one line to a JSONL file per entry. Each line is
//! a serialized `AuditEntry`. The writer reads the file's tail to find
//! the current head hash before computing the new entry's hash, so a
//! writer that starts after a crash continues the existing chain.
//!
//! No file locking. The design assumes a single writer; a second
//! writer would corrupt the chain silently. Multi-writer coordination
//! is a known gap.

use std::path::{Path, PathBuf};

use serde_json::Value;
use tokio::fs::OpenOptions;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};

use crate::hash_chain::{compute_entry_hash, compute_payload_hash};
use crate::{AuditEntry, AuditError, GENESIS};

/// An append-only writer for one audit log file.
#[derive(Debug, Clone)]
pub struct AuditWriter {
    path: PathBuf,
}

impl AuditWriter {
    /// Construct a writer for the given path. No I/O happens until
    /// `append` or `head_hash` is called.
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    /// Return the path this writer appends to.
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Return the `entry_hash` of the last entry in the file, or `None`
    /// if the file does not exist or is empty.
    ///
    /// Reads the file from the beginning and tracks the last non-empty
    /// line. The audit log is expected to be small enough that a full
    /// scan is cheaper than maintaining a side index.
    pub async fn head_hash(&self) -> Result<Option<String>, AuditError> {
        let file = match OpenOptions::new().read(true).open(&self.path).await {
            Ok(file) => file,
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(err) => return Err(AuditError::Io(err)),
        };
        let reader = BufReader::new(file);
        let mut lines = reader.lines();
        let mut last: Option<String> = None;
        while let Some(line) = lines.next_line().await? {
            if line.trim().is_empty() {
                continue;
            }
            let entry: AuditEntry = serde_json::from_str(&line)?;
            last = Some(entry.entry_hash);
        }
        Ok(last)
    }

    /// Append one entry with the given payload and return it.
    ///
    /// The payload is serialized canonically, hashed, and chained onto
    /// the current head. The returned entry is what was written.
    pub async fn append(&self, payload: Value) -> Result<AuditEntry, AuditError> {
        let prev_hash = self
            .head_hash()
            .await?
            .unwrap_or_else(|| GENESIS.to_owned());
        let payload_hash = compute_payload_hash(&payload);
        let entry_hash = compute_entry_hash(&prev_hash, &payload_hash);
        let entry = AuditEntry {
            payload,
            payload_hash,
            prev_hash,
            entry_hash,
        };

        let mut line = serde_json::to_string(&entry)?;
        line.push('\n');

        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
            .await?;
        file.write_all(line.as_bytes()).await?;
        file.flush().await?;
        Ok(entry)
    }

    /// Read every entry in the file, in order.
    ///
    /// Returns an empty vector if the file does not exist. Blank lines
    /// are skipped; a line that is not valid JSON is an error.
    pub async fn read_all_entries(&self) -> Result<Vec<AuditEntry>, AuditError> {
        let file = match OpenOptions::new().read(true).open(&self.path).await {
            Ok(file) => file,
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(err) => return Err(AuditError::Io(err)),
        };
        let reader = BufReader::new(file);
        let mut lines = reader.lines();
        let mut entries: Vec<AuditEntry> = Vec::new();
        while let Some(line) = lines.next_line().await? {
            if line.trim().is_empty() {
                continue;
            }
            entries.push(serde_json::from_str(&line)?);
        }
        Ok(entries)
    }
}
