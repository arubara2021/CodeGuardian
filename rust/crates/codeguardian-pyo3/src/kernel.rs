//! Kernel bindings for the CodeGuardian native extension.
//!
//! Three functions call into `codeguardian_kernel` and translate its
//! errors into `_native.KernelError` exceptions. Every failure the
//! kernel can produce is classified into a string code that the Python
//! side maps onto an existing `CodeGuardianError` code.
//!
//! `map_cpg_error` and `build_exception` are `pub(crate)` so other
//! binding modules — blast and policy — can reuse the same error
//! mapping and the same exception class. There is one exception type
//! for every binding in this crate.
//!
//! The binding surface is deliberately small. `build_graph` returns
//! serialized bytes rather than an in-memory handle, and
//! `graph_summary` reads bytes rather than a path, so the kernel never
//! holds Python-visible state.

use std::io::Cursor;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use codeguardian_kernel::{
    CodePropertyGraph, CpgError, KernelError as CoreKernelError, Language, SourceFile,
};

use crate::KernelError;

/// Return the kernel crate's version string.
#[pyfunction]
pub fn kernel_version() -> String {
    env!("CARGO_PKG_VERSION").to_owned()
}

/// Build a Code Property Graph from source files and return it in the
/// serialized wire format.
///
/// `sources` is a list of `(path, language, content)` tuples. The
/// language must be one of `"python"` or `"typescript"`. Every file in
/// the list is parsed; the resulting graph is serialized to bytes in
/// the format `codeguardian.core.storage.GraphStore` reads.
///
/// Raises `ValueError` if a language name is unknown.
/// Raises `KernelError` if any source fails to parse catastrophically
/// or if serialization fails.
#[pyfunction]
pub fn build_graph(
    py: Python<'_>,
    sources: Vec<(String, String, String)>,
) -> PyResult<Vec<u8>> {
    let mut parsed: Vec<SourceFile> = Vec::with_capacity(sources.len());
    for (path, language_name, content) in sources {
        let language = match Language::from_name(&language_name) {
            Some(lang) => lang,
            None => {
                return Err(PyValueError::new_err(format!(
                    "unsupported language: {language_name}"
                )));
            }
        };
        parsed.push(SourceFile {
            path,
            language,
            content,
        });
    }

    let result = py.allow_threads(move || -> Result<Vec<u8>, CpgError> {
        let graph = CodePropertyGraph::build(&parsed)?;
        let mut buf: Vec<u8> = Vec::new();
        graph.write_to(&mut buf)?;
        Ok(buf)
    });

    result.map_err(|err| map_cpg_error(py, &err))
}

/// Read a serialized Code Property Graph and return its summary.
///
/// The returned dictionary has four keys: `node_count` and
/// `edge_count` from the header, `incremental` from the header's flag
/// word, and `file_count` from the payload's file table.
///
/// Raises `KernelError` if the bytes are not a valid CPG file or if the
/// payload checksum does not match the header.
#[pyfunction]
pub fn graph_summary<'py>(py: Python<'py>, data: &[u8]) -> PyResult<Bound<'py, PyDict>> {
    let data_owned = data.to_vec();
    let result = py.allow_threads(move || -> Result<(u64, u64, bool, usize), CpgError> {
        let mut header_cursor = Cursor::new(&data_owned[..]);
        let header = CodePropertyGraph::read_header(&mut header_cursor)?;

        let mut graph_cursor = Cursor::new(&data_owned[..]);
        let graph = CodePropertyGraph::read_from(&mut graph_cursor)?;

        Ok((
            header.node_count,
            header.edge_count,
            header.incremental(),
            graph.file_count(),
        ))
    });

    let (node_count, edge_count, incremental, file_count) =
        result.map_err(|err| map_cpg_error(py, &err))?;

    let dict = PyDict::new(py);
    dict.set_item("node_count", node_count)?;
    dict.set_item("edge_count", edge_count)?;
    dict.set_item("file_count", file_count)?;
    dict.set_item("incremental", incremental)?;
    Ok(dict)
}

/// Convert a core kernel error into a `KernelError` exception.
pub(crate) fn map_core_kernel_error(py: Python<'_>, err: &CoreKernelError) -> PyErr {
    match err {
        CoreKernelError::ParserInit { language } => {
            let details = PyDict::new(py);
            let _ = details.set_item("language", *language);
            build_exception(py, "PARSER_INIT", &err.to_string(), details.into_any())
        }
        CoreKernelError::ParseFailed { language } => {
            let details = PyDict::new(py);
            let _ = details.set_item("language", *language);
            build_exception(py, "PARSE_FAILED", &err.to_string(), details.into_any())
        }
        CoreKernelError::SourceTooLarge { len } => {
            let details = PyDict::new(py);
            let _ = details.set_item("len", *len);
            build_exception(py, "SOURCE_TOO_LARGE", &err.to_string(), details.into_any())
        }
        CoreKernelError::TreeTooDeep { depth, limit } => {
            let details = PyDict::new(py);
            let _ = details.set_item("depth", *depth);
            let _ = details.set_item("limit", *limit);
            build_exception(py, "TREE_TOO_DEEP", &err.to_string(), details.into_any())
        }
    }
}

/// Convert a CPG error into a `KernelError` exception.
pub(crate) fn map_cpg_error(py: Python<'_>, err: &CpgError) -> PyErr {
    match err {
        CpgError::Io(io) => {
            let details = PyDict::new(py);
            let _ = details.set_item("io_kind", io.kind().to_string());
            build_exception(py, "GRAPH_IO", &err.to_string(), details.into_any())
        }
        CpgError::InvalidMagic(magic) => {
            let details = PyDict::new(py);
            let _ = details.set_item("magic", magic.to_vec());
            build_exception(py, "INVALID_MAGIC", &err.to_string(), details.into_any())
        }
        CpgError::UnsupportedVersion(version) => {
            let details = PyDict::new(py);
            let _ = details.set_item("version", *version);
            build_exception(
                py,
                "UNSUPPORTED_FORMAT_VERSION",
                &err.to_string(),
                details.into_any(),
            )
        }
        CpgError::InvalidHeaderSize(size) => {
            let details = PyDict::new(py);
            let _ = details.set_item("size", *size);
            build_exception(py, "INVALID_HEADER_SIZE", &err.to_string(), details.into_any())
        }
        CpgError::ChecksumMismatch { expected, actual } => {
            let details = PyDict::new(py);
            let _ = details.set_item("expected", *expected);
            let _ = details.set_item("actual", *actual);
            build_exception(py, "CHECKSUM_MISMATCH", &err.to_string(), details.into_any())
        }
        CpgError::Truncated { expected, actual } => {
            let details = PyDict::new(py);
            let _ = details.set_item("expected", *expected);
            let _ = details.set_item("actual", *actual);
            build_exception(py, "GRAPH_TRUNCATED", &err.to_string(), details.into_any())
        }
        CpgError::UnknownNodeKind(code) => {
            let details = PyDict::new(py);
            let _ = details.set_item("code", *code);
            build_exception(py, "UNKNOWN_NODE_KIND", &err.to_string(), details.into_any())
        }
        CpgError::UnknownEdgeKind(code) => {
            let details = PyDict::new(py);
            let _ = details.set_item("code", *code);
            build_exception(py, "UNKNOWN_EDGE_KIND", &err.to_string(), details.into_any())
        }
        CpgError::Corrupt { reason } => {
            let details = PyDict::new(py);
            let _ = details.set_item("reason", reason.clone());
            build_exception(py, "GRAPH_CORRUPTED", &err.to_string(), details.into_any())
        }
        CpgError::Parse(inner) => map_core_kernel_error(py, inner),
    }
}

/// Construct a `KernelError` instance with the given attributes and
/// wrap it in a `PyErr`.
///
/// If the exception cannot be constructed for any reason, the function
/// falls back to a bare `KernelError` with only a message, so the
/// caller always receives a typed exception rather than a panic.
pub(crate) fn build_exception(
    py: Python<'_>,
    code: &str,
    message: &str,
    details: Bound<'_, PyAny>,
) -> PyErr {
    let exc_type = py.get_type::<KernelError>();
    let instance = match exc_type.call1((message,)) {
        Ok(value) => value,
        Err(_) => return PyErr::new::<KernelError, _>(message.to_owned()),
    };
    let _ = instance.setattr("code", code);
    let _ = instance.setattr("message", message);
    let _ = instance.setattr("details", details);
    PyErr::from_value(instance)
}
