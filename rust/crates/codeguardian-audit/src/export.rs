//! Audit log export.
//!
//! Two export formats. `export_jsonl` writes one serialized entry per
//! line, matching the on-disk format. `export_json_array` writes a
//! single JSON array containing every entry. Both read the log from
//! its path and stream the output to a caller-supplied writer.
//!
//! Export validates every entry during the read. A line that is not
//! valid JSON fails the export before any output is produced, so a
//! partially-corrupt log cannot produce a partially-correct export.
//! The reading side is `AuditWriter::read_all_entries`, which returns
//! entries in file order.

use std::path::PathBuf;

use tokio::io::{AsyncWrite, AsyncWriteExt};

use crate::writer::AuditWriter;
use crate::AuditError;

/// Write the audit log at `audit_path` to `writer` as line-delimited
/// JSON.
///
/// Each entry occupies exactly one line, in file order. A trailing
/// newline is written after the last entry. Returns the number of
/// entries written.
pub async fn export_jsonl<W>(
    audit_path: impl Into<PathBuf>,
    writer: &mut W,
) -> Result<usize, AuditError>
where
    W: AsyncWrite + Unpin,
{
    let entries = AuditWriter::new(audit_path).read_all_entries().await?;
    let mut count: usize = 0;
    for entry in &entries {
        let mut line = serde_json::to_vec(entry)?;
        line.push(b'\n');
        writer.write_all(&line).await?;
        count += 1;
    }
    writer.flush().await?;
    Ok(count)
}

/// Write the audit log at `audit_path` to `writer` as a single JSON
/// array.
///
/// The array is compact — no whitespace between elements. A trailing
/// newline is written after the closing bracket. Returns the number of
/// entries written.
pub async fn export_json_array<W>(
    audit_path: impl Into<PathBuf>,
    writer: &mut W,
) -> Result<usize, AuditError>
where
    W: AsyncWrite + Unpin,
{
    let entries = AuditWriter::new(audit_path).read_all_entries().await?;
    let bytes = serde_json::to_vec(&entries)?;
    writer.write_all(&bytes).await?;
    writer.write_all(b"\n").await?;
    writer.flush().await?;
    Ok(entries.len())
}
