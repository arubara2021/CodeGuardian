//! Kernel bindings for the CodeGuardian native extension.
//!
//! Five functions call into `codeguardian_kernel` and translate its
//! errors into `_native.KernelError` exceptions. Every failure the
//! kernel can produce is classified into a string code that the Python
//! side maps onto an existing `CodeGuardianError` code.
//!
//! `map_cpg_error`, `map_core_kernel_error`, `map_grammar_load_error`,
//! and `build_exception` are `pub(crate)` so other binding modules —
//! blast and policy — can reuse the same error mapping and the same
//! exception class. There is one exception type for every binding in
//! this crate.
//!
//! The binding surface is deliberately small. `build_graph` returns
//! serialized bytes rather than an in-memory handle, and
//! `graph_summary` reads bytes rather than a path, so the kernel never
//! holds Python-visible state.
//!
//! # Language resolution
//!
//! `build_graph` accepts any language name that resolves through
//! `langs::dispatch_by_name`. That is the eight built-in languages
//! (with their aliases) plus any runtime grammar registered by a
//! previous `load_grammar` call. An unrecognized name raises
//! `ValueError` so the caller sees a user mistake rather than a typed
//! kernel error.
//!
//! # Runtime grammar loading
//!
//! `load_grammar` delegates to `codeguardian_kernel::load_and_register`
//! and translates the four `GrammarLoadError` variants into
//! `_native.KernelError` exceptions with stable string codes. The
//! caller is responsible for keeping the loaded grammar's path stable
//! for the life of the process; the kernel never unloads it.

use std::io::Cursor;
use std::path::PathBuf;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use codeguardian_kernel::{
    dispatch_by_name, list_runtime_languages, load_and_register, CodePropertyGraph, CpgError,
    GrammarLoadError, KernelError as CoreKernelError, SourceFile,
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
/// language is resolved by `langs::dispatch_by_name`: the eight
/// built-in languages (with aliases), or any runtime grammar registered
/// by a previous `load_grammar` call.
///
/// A name that resolves to no module raises `ValueError` with the
/// message `unrecognized language: <name>`.
///
/// Raises `KernelError` if any source fails to parse catastrophically
/// or if serialization fails.
#[pyfunction]
pub fn build_graph(
    py: Python<'_>,
    sources: Vec<(String, String, String)>,
) -> PyResult<Vec<u8>> {
    let mut parsed: Vec<SourceFile> = Vec::with_capacity(sources.len());
    for (path, language_name, content) in sources {
        if dispatch_by_name(&language_name).is_none() {
            return Err(PyValueError::new_err(format!(
                "unrecognized language: {language_name}"
            )));
        }
        parsed.push(SourceFile {
            path,
            language: language_name,
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

/// Load a runtime tree-sitter grammar from a shared library.
///
/// `language_name` is the canonical name the grammar registers under.
/// It must not collide with a built-in language name (or alias) and
/// must not be already registered by a previous call.
///
/// `library_path` is the platform-specific shared library:
/// `libtree_sitter_<name>.so` on Linux, `.dylib` on macOS, and
/// `tree_sitter_<name>.dll` on Windows.
///
/// Raises `KernelError` with a stable `code` attribute for each of the
/// four failure modes: `GRAMMAR_LIBRARY_NOT_FOUND`,
/// `GRAMMAR_SYMBOL_NOT_FOUND`, `GRAMMAR_ABI_INCOMPATIBLE`,
/// `GRAMMAR_ALREADY_LOADED`.
#[pyfunction]
pub fn load_grammar(
    py: Python<'_>,
    language_name: String,
    library_path: String,
) -> PyResult<()> {
    let path_buf = PathBuf::from(&library_path);
    py.allow_threads(move || load_and_register(&language_name, &path_buf))
        .map_err(|err| map_grammar_load_error(py, &err))
}

/// Return the sorted list of every language name the kernel can parse.
///
/// The result contains the eight built-in canonical names plus any
/// runtime grammar name registered by a `load_grammar` call. Aliases
/// such as `js`, `golang`, `rs`, `c++`, `cxx` are not listed because
/// they resolve through the same built-in module.
#[pyfunction]
pub fn list_languages(_py: Python<'_>) -> PyResult<Vec<String>> {
    let mut names: Vec<String> = vec![
        "c".to_string(),
        "cpp".to_string(),
        "go".to_string(),
        "java".to_string(),
        "javascript".to_string(),
        "python".to_string(),
        "rust".to_string(),
        "typescript".to_string(),
    ];
    names.extend(list_runtime_languages());
    names.sort();
    names.dedup();
    Ok(names)
}

/// Convert a core kernel error into a `KernelError` exception.
pub(crate) fn map_core_kernel_error(py: Python<'_>, err: &CoreKernelError) -> PyErr {
    match err {
        CoreKernelError::ParserInit { language } => {
            let details = PyDict::new(py);
            let _ = details.set_item("language", language.as_str());
            build_exception(py, "PARSER_INIT", &err.to_string(), details.into_any())
        }
        CoreKernelError::ParseFailed { language } => {
            let details = PyDict::new(py);
            let _ = details.set_item("language", language.as_str());
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
        CoreKernelError::LanguageNotImplemented { language } => {
            let details = PyDict::new(py);
            let _ = details.set_item("language", language.as_str());
            build_exception(
                py,
                "LANGUAGE_NOT_IMPLEMENTED",
                &err.to_string(),
                details.into_any(),
            )
        }
    }
}

/// Convert a grammar load error into a `KernelError` exception.
pub(crate) fn map_grammar_load_error(py: Python<'_>, err: &GrammarLoadError) -> PyErr {
    match err {
        GrammarLoadError::LibraryNotFound { path } => {
            let details = PyDict::new(py);
            let _ = details.set_item("path", path.display().to_string());
            build_exception(
                py,
                "GRAMMAR_LIBRARY_NOT_FOUND",
                &err.to_string(),
                details.into_any(),
            )
        }
        GrammarLoadError::SymbolNotFound { symbol } => {
            let details = PyDict::new(py);
            let _ = details.set_item("symbol", symbol.as_str());
            build_exception(
                py,
                "GRAMMAR_SYMBOL_NOT_FOUND",
                &err.to_string(),
                details.into_any(),
            )
        }
        GrammarLoadError::AbiIncompatible {
            plugin_abi,
            kernel_abi,
        } => {
            let details = PyDict::new(py);
            let _ = details.set_item("plugin_abi", *plugin_abi);
            let _ = details.set_item("kernel_abi", *kernel_abi);
            build_exception(
                py,
                "GRAMMAR_ABI_INCOMPATIBLE",
                &err.to_string(),
                details.into_any(),
            )
        }
        GrammarLoadError::AlreadyLoaded { language } => {
            let details = PyDict::new(py);
            let _ = details.set_item("language", language.as_str());
            build_exception(
                py,
                "GRAMMAR_ALREADY_LOADED",
                &err.to_string(),
                details.into_any(),
            )
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
