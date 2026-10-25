//! Kernel bindings for the CodeGuardian native extension.
//!
//! Six functions and one class call into `codeguardian_kernel` and
//! translate its errors into `_native.KernelError` exceptions.
//!
//! The stateless functions (`build_graph`, `graph_summary`,
//! `load_grammar`, `list_languages`, `kernel_version`) are unchanged
//! from the previous version. The `PyCPG` class is new: it wraps a
//! deserialized `CodePropertyGraph` so that a caller can run many
//! queries against one graph without paying the deserialization cost
//! on every call.
//!
//! # Why `PyCPG` exists
//!
//! The previous design held no Python-visible kernel state. Every call
//! deserialized the graph from bytes. That works for one-shot queries
//! but is expensive when a caller issues five to ten queries in a
//! phase: at 4.4 MB per `psf/requests` graph, ten deserializations
//! cost roughly 1.5 seconds of pure overhead.
//!
//! `PyCPG` is a per-instance handle. Rust owns the graph; Python holds
//! a reference. When Python drops the reference, Rust frees the graph.
//! The handle is immutable after construction and exposes six
//! read-only methods. It is not shared mutable state; it is an owned
//! object with a narrow surface. The "no Python-visible state" rule
//! from Phase 6 is relaxed deliberately for this case, not abandoned.
//!
//! # Language resolution
//!
//! `build_graph` accepts any language name that resolves through
//! `langs::dispatch_by_name`. That is the eight built-in languages
//! (with their aliases) plus any runtime grammar registered by a
//! previous `load_grammar` call.

use std::io::Cursor;
use std::path::{Path, PathBuf};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use codeguardian_kernel::query::{DeclarationInfo, is_declaration_kind};
use codeguardian_kernel::{
    dispatch_by_name, list_runtime_languages, load_and_register, CodePropertyGraph, CpgError,
    GrammarLoadError, KernelError as CoreKernelError, NodeKind, SourceFile,
};

use crate::KernelError;

/// Return the kernel crate's version string.
#[pyfunction]
pub fn kernel_version() -> String {
    env!("CARGO_PKG_VERSION").to_owned()
}

/// Build a Code Property Graph from source files and return it in the
/// serialized wire format.
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

/// A Python-side handle to a deserialized Code Property Graph.
///
/// Construct once with the bytes returned by `build_graph`, then call
/// any of the six query methods as many times as needed without
/// re-deserializing.
///
/// The handle is immutable. All query methods are read-only. When
/// Python drops the last reference, Rust frees the graph.
#[pyclass(module = "codeguardian._native", name = "PyCPG")]
pub struct PyCPG {
    graph: CodePropertyGraph,
}

#[pymethods]
impl PyCPG {
    /// Deserialize a Code Property Graph from bytes.
    ///
    /// Raises `KernelError` with the same codes as `graph_summary` if
    /// the bytes are not a valid CPG file.
    #[new]
    fn new(py: Python<'_>, data: &[u8]) -> PyResult<Self> {
        let mut cursor = Cursor::new(data);
        let graph =
            CodePropertyGraph::read_from(&mut cursor).map_err(|e| map_cpg_error(py, &e))?;
        Ok(PyCPG { graph })
    }

    /// Return every file path recorded in the graph.
    fn files(&self) -> Vec<String> {
        (0..self.graph.file_count() as u32)
            .filter_map(|id| self.graph.file_path(id).map(String::from))
            .collect()
    }

    /// Return the declaration whose qualified name matches, as a dict.
    ///
    /// Returns `None` if the file is not in the graph, or if no
    /// declaration in the file has the requested qualified name. The
    /// dict has keys: `node_id`, `name`, `qualified_name`, `kind`,
    /// `file_path`, `line`, `start_byte`, `end_byte`.
    fn find_function<'py>(
        &self,
        py: Python<'py>,
        file_path: String,
        qualified_name: String,
    ) -> PyResult<Option<Bound<'py, PyDict>>> {
        let file_id = match self.graph.file_id(&file_path) {
            Some(id) => id,
            None => return Ok(None),
        };
        let node_id = match self.graph.find_declaration(file_id, &qualified_name) {
            Some(id) => id,
            None => return Ok(None),
        };
        let info = match self.graph.declaration_info(node_id) {
            Some(info) => info,
            None => return Ok(None),
        };
        Ok(Some(info_to_dict(py, &info)?))
    }

    /// Return callers of the declaration whose qualified name matches.
    ///
    /// The result is a list of dicts, one per caller. Only callers that
    /// are themselves declarations are included; a call from a top-level
    /// expression has no dict. Returns an empty list if the file or the
    /// target is not found.
    fn callers_of<'py>(
        &self,
        py: Python<'py>,
        file_path: String,
        qualified_name: String,
    ) -> PyResult<Bound<'py, PyList>> {
        self.neighbours_of(py, file_path, qualified_name, true)
    }

    /// Return callees of the declaration whose qualified name matches.
    fn callees_of<'py>(
        &self,
        py: Python<'py>,
        file_path: String,
        qualified_name: String,
    ) -> PyResult<Bound<'py, PyList>> {
        self.neighbours_of(py, file_path, qualified_name, false)
    }

    /// Return every declaration in a file, sorted by source position.
    fn list_symbols<'py>(
        &self,
        py: Python<'py>,
        file_path: String,
    ) -> PyResult<Bound<'py, PyList>> {
        let list = PyList::empty(py);
        let file_id = match self.graph.file_id(&file_path) {
            Some(id) => id,
            None => return Ok(list),
        };
        let mut infos: Vec<DeclarationInfo> = self
            .graph
            .nodes_in_file(file_id)
            .iter()
            .filter_map(|&node_id| self.graph.declaration_info(node_id))
            .collect();
        infos.sort_by_key(|info| (info.start_byte, info.node_id));
        for info in infos {
            let dict = PyDict::new(py);
            dict.set_item("node_id", info.node_id)?;
            dict.set_item("name", &info.simple_name)?;
            dict.set_item("qualified_name", &info.qualified_name)?;
            dict.set_item("kind", kind_label(info.kind))?;
            dict.set_item("line", info.line)?;
            list.append(dict)?;
        }
        Ok(list)
    }

    /// Return the full source of a declaration (function, method, or
    /// class), read from the file on disk.
    ///
    /// The `clone_root` is the workspace root; the resolved path must
    /// remain under it, and symlinks are resolved before the check.
    ///
    /// Raises `KernelError` with code `FILE_NOT_FOUND` if the file path
    /// is not in the graph, `TARGET_NOT_FOUND` if the qualified name is
    /// not a declaration in that file, `PATH_ESCAPES_ROOT` if the
    /// resolved path leaves the workspace, `FILE_UNREADABLE` if the
    /// file cannot be read, or `STALE_OFFSETS` if the graph's byte
    /// offsets do not match the file on disk.
    fn function_source(
        &self,
        py: Python<'_>,
        file_path: String,
        qualified_name: String,
        clone_root: String,
    ) -> PyResult<String> {
        let file_id = self.graph.file_id(&file_path).ok_or_else(|| {
            py_error(py, "FILE_NOT_FOUND", format!("file not in graph: {file_path}"))
        })?;
        let node_id = self
            .graph
            .find_declaration(file_id, &qualified_name)
            .ok_or_else(|| {
                py_error(
                    py,
                    "TARGET_NOT_FOUND",
                    format!("no declaration {qualified_name} in {file_path}"),
                )
            })?;
        let info = self.graph.declaration_info(node_id).ok_or_else(|| {
            py_error(
                py,
                "TARGET_NOT_FOUND",
                format!("declaration {qualified_name} not resolvable"),
            )
        })?;

        let resolved = resolve_under_root(Path::new(&clone_root), &file_path)
            .map_err(|reason| py_error(py, "PATH_ESCAPES_ROOT", reason))?;

        let source = std::fs::read_to_string(&resolved).map_err(|e| {
            py_error(
                py,
                "FILE_UNREADABLE",
                format!("{}: {e}", resolved.display()),
            )
        })?;

        let start = info.start_byte as usize;
        let end = info.end_byte as usize;
        source.get(start..end).map(str::to_owned).ok_or_else(|| {
            py_error(
                py,
                "STALE_OFFSETS",
                format!(
                    "graph offsets {start}..{end} do not fit {}-byte file",
                    source.len()
                ),
            )
        })
    }
}

impl PyCPG {
    fn neighbours_of<'py>(
        &self,
        py: Python<'py>,
        file_path: String,
        qualified_name: String,
        want_callers: bool,
    ) -> PyResult<Bound<'py, PyList>> {
        let list = PyList::empty(py);
        let file_id = match self.graph.file_id(&file_path) {
            Some(id) => id,
            None => return Ok(list),
        };
        let node_id = match self.graph.find_declaration(file_id, &qualified_name) {
            Some(id) => id,
            None => return Ok(list),
        };
        let ids = if want_callers {
            self.graph.callers_of(node_id)
        } else {
            self.graph.callees_of(node_id)
        };
        for neighbour_id in ids {
            let node = match self.graph.node(neighbour_id) {
                Some(n) => n,
                None => continue,
            };
            if !is_declaration_kind(node.kind) {
                continue;
            }
            let info = match self.graph.declaration_info(neighbour_id) {
                Some(info) => info,
                None => continue,
            };
            list.append(info_to_dict(py, &info)?)?;
        }
        Ok(list)
    }
}

/// Convert a `DeclarationInfo` into a Python dict.
fn info_to_dict<'py>(py: Python<'py>, info: &DeclarationInfo) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("node_id", info.node_id)?;
    dict.set_item("name", &info.simple_name)?;
    dict.set_item("qualified_name", &info.qualified_name)?;
    dict.set_item("kind", kind_label(info.kind))?;
    dict.set_item("file_path", &info.file_path)?;
    dict.set_item("line", info.line)?;
    dict.set_item("start_byte", info.start_byte)?;
    dict.set_item("end_byte", info.end_byte)?;
    Ok(dict)
}

/// Return a short string label for a declaration kind.
fn kind_label(kind: NodeKind) -> &'static str {
    match kind {
        NodeKind::FunctionDef => "function",
        NodeKind::MethodDef => "method",
        NodeKind::ClassDef => "class",
        NodeKind::Struct => "struct",
        NodeKind::Interface => "interface",
        NodeKind::Trait => "trait",
        NodeKind::Enum => "enum",
        NodeKind::Union => "union",
        _ => "other",
    }
}

/// Resolve `file_path` under `root`, rejecting symlinks and relative
/// paths that escape the workspace.
fn resolve_under_root(root: &Path, file_path: &str) -> Result<PathBuf, String> {
    let canonical_root = root
        .canonicalize()
        .map_err(|e| format!("clone_root not resolvable: {e}"))?;
    let raw = Path::new(file_path);
    let candidate = if raw.is_absolute() {
        raw.to_path_buf()
    } else {
        root.join(raw)
    };
    let canonical = candidate
        .canonicalize()
        .map_err(|e| format!("file not resolvable: {e}"))?;
    if !canonical.starts_with(&canonical_root) {
        return Err(format!(
            "file path escapes clone root: {file_path}"
        ));
    }
    Ok(canonical)
}

/// Build a `KernelError` with the given code and message.
fn py_error(py: Python<'_>, code: &str, message: String) -> PyErr {
    build_exception(py, code, &message, PyDict::new(py).into_any())
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
