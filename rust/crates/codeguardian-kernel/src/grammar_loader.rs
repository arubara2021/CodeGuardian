//! Runtime grammar loading for tree-sitter.
//!
//! A plugin that ships its own compiled tree-sitter grammar calls
//! `GrammarLoader::load` with the language name and the path to the
//! platform-specific shared library. The loader opens the library,
//! locates the exported grammar symbol (`tree_sitter_<name>`),
//! verifies the grammar's ABI version against the kernel's tree-sitter
//! runtime, and registers a `RuntimeLanguageModule` in a process-wide
//! registry.
//!
//! Registration is one-way. Grammars are never unloaded, because
//! tree-sitter holds a raw pointer into the grammar's static data for
//! the lifetime of every `TsLanguage` value derived from it. Leaking
//! both the library handle and the module is the only safe option, and
//! it matches the fact that grammars are loaded at most once per
//! process.
//!
//! Built-in languages (Python, TypeScript, JavaScript, Java, Go, Rust,
//! C, C++) are compiled into the kernel. The loader refuses to
//! register a grammar whose name resolves to a built-in, because the
//! built-in dispatch already has a definition for it and a second
//! definition would silently shadow the first.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};

use libloading::{Library, Symbol};
use thiserror::Error;
use tree_sitter::{ffi, Language as TsLanguage};

use crate::ast::NodeKind;
use crate::langs::{classify_shared, GrammarSource, Language};

/// Every way a runtime grammar load can fail.
#[derive(Debug, Error)]
pub enum GrammarLoadError {
    /// The grammar library file does not exist at the given path.
    #[error("grammar library not found at {path}")]
    LibraryNotFound {
        /// Path the caller passed.
        path: PathBuf,
    },

    /// The shared library loaded but does not export the symbol the
    /// loader looks for. Tree-sitter grammar crates conventionally
    /// export `tree_sitter_<name>`, where `<name>` has dashes replaced
    /// with underscores.
    #[error("grammar symbol {symbol} not found in library")]
    SymbolNotFound {
        /// Symbol the loader tried to resolve.
        symbol: String,
    },

    /// The grammar was compiled against an ABI version incompatible
    /// with the kernel's tree-sitter runtime.
    #[error("grammar ABI {plugin_abi} is incompatible with kernel ABI {kernel_abi}")]
    AbiIncompatible {
        /// ABI version the grammar reports.
        plugin_abi: u32,
        /// ABI version the kernel supports.
        kernel_abi: u32,
    },

    /// The language name is already registered, either as a built-in
    /// or by a previous `load` call.
    #[error("grammar for language {language} is already loaded")]
    AlreadyLoaded {
        /// The language name.
        language: String,
    },
}

/// A `GrammarSource` implementation backed by a runtime-loaded
/// tree-sitter grammar.
///
/// The kernel cannot know the language's syntax kinds, so `classify`
/// uses a heuristic map of the kind names that are most common across
/// tree-sitter grammars. A node whose kind is not in the map becomes
/// `NodeKind::Other(raw_id)` and is preserved in the graph without
/// further interpretation. A plugin that needs richer classification
/// ships a Python wrapper that overrides `LanguagePlugin.classify`.
struct RuntimeLanguageModule {
    name: String,
    language: TsLanguage,
}

impl GrammarSource for RuntimeLanguageModule {
    fn grammar(&self) -> TsLanguage {
        self.language.clone()
    }

    fn classify(&self, kind: &str, raw_id: u16) -> NodeKind {
        if let Some(shared) = classify_shared(kind) {
            return shared;
        }
        match kind {
            "function_declaration"
            | "function_definition"
            | "function_item"
            | "func_declaration" => NodeKind::FunctionDef,
            "class_declaration"
            | "class_definition"
            | "class_specifier"
            | "struct_item"
            | "struct_declaration" => NodeKind::ClassDef,
            "method_declaration" | "method_definition" => NodeKind::MethodDef,
            "module" | "program" | "source_file" | "translation_unit" => NodeKind::Module,
            "block" | "compound_statement" | "statement_block" => NodeKind::Block,
            "if_statement" | "if_expression" => NodeKind::If,
            "for_statement" | "for_expression" | "for_in_statement" => NodeKind::For,
            "while_statement" | "while_expression" => NodeKind::While,
            "return_statement" | "return_expression" => NodeKind::Return,
            "call_expression" | "function_call" => NodeKind::Call,
            "parameter" | "parameter_declaration" | "formal_parameter" => NodeKind::Parameter,
            "argument_list" | "arguments" => NodeKind::Argument,
            "import_statement" | "import_declaration" | "use_declaration" => NodeKind::Import,
            _ => NodeKind::Other(raw_id),
        }
    }

    fn name(&self) -> &str {
        &self.name
    }
}

/// A process-wide handle to the runtime grammar registry.
///
/// The loader holds no per-instance state; the registry itself is a
/// global because the parser resolves grammars through the top-level
/// `langs::dispatch_by_name` function rather than through a loader
/// instance. The facade exists so the pyo3 binding has an object to
/// construct and so later phases can add per-loader scoping without
/// changing the call surface.
pub struct GrammarLoader {
    _private: (),
}

impl GrammarLoader {
    /// Construct a loader. Equivalent to accessing the global registry
    /// directly, but kept as a type so callers do not depend on the
    /// singleton.
    pub fn new() -> Self {
        Self { _private: () }
    }

    /// Load a grammar from `library_path` and register it under
    /// `language_name`.
    ///
    /// Fails if the file is missing, the symbol is missing, the
    /// grammar's ABI version is incompatible with the kernel's, or the
    /// language name is already registered (built-in or runtime).
    pub fn load(
        &self,
        language_name: &str,
        library_path: &Path,
    ) -> Result<(), GrammarLoadError> {
        load_and_register(language_name, library_path)
    }

    /// Return whether a grammar with this name is loaded.
    pub fn is_loaded(&self, language_name: &str) -> bool {
        is_registered(language_name)
    }

    /// Return every runtime-loaded language name, sorted for
    /// determinism.
    pub fn list_loaded(&self) -> Vec<String> {
        list_runtime_languages()
    }
}

impl Default for GrammarLoader {
    fn default() -> Self {
        Self::new()
    }
}

/// The process-wide runtime grammar registry. Keys are canonical
/// language names (the same strings the plugin manifest declares);
/// values are leaked `GrammarSource` trait objects.
static REGISTRY: OnceLock<Mutex<HashMap<String, &'static dyn GrammarSource>>> = OnceLock::new();

fn registry() -> &'static Mutex<HashMap<String, &'static dyn GrammarSource>> {
    REGISTRY.get_or_init(|| Mutex::new(HashMap::new()))
}

/// Load a grammar and register it under `language_name`.
///
/// Not re-entrant with respect to the same name. The mutex is held
/// only for the final insert, never during the shared-library load, so
/// a slow filesystem does not block other loaders.
pub fn load_and_register(
    language_name: &str,
    library_path: &Path,
) -> Result<(), GrammarLoadError> {
    if Language::from_name(language_name).is_some() {
        return Err(GrammarLoadError::AlreadyLoaded {
            language: language_name.to_string(),
        });
    }
    if is_registered(language_name) {
        return Err(GrammarLoadError::AlreadyLoaded {
            language: language_name.to_string(),
        });
    }
    if !library_path.exists() {
        return Err(GrammarLoadError::LibraryNotFound {
            path: library_path.to_path_buf(),
        });
    }

    let language = unsafe { load_tree_sitter_language(language_name, library_path) }?;

    let module = Box::new(RuntimeLanguageModule {
        name: language_name.to_string(),
        language,
    });
    let leaked: &'static RuntimeLanguageModule = Box::leak(module);
    let trait_obj: &'static dyn GrammarSource = leaked;

    let mut map = registry()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    map.insert(language_name.to_string(), trait_obj);
    Ok(())
}

/// Return the runtime-loaded grammar for `language_name`, if any.
pub fn get_runtime_language(language_name: &str) -> Option<&'static dyn GrammarSource> {
    let map = registry()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    map.get(language_name).copied()
}

/// Return whether a runtime grammar is registered under this name.
pub fn is_registered(language_name: &str) -> bool {
    registry()
        .lock()
        .map(|map| map.contains_key(language_name))
        .unwrap_or(false)
}

/// Return every runtime-loaded language name, sorted for determinism.
pub fn list_runtime_languages() -> Vec<String> {
    let mut names: Vec<String> = registry()
        .lock()
        .map(|map| map.keys().cloned().collect())
        .unwrap_or_default();
    names.sort();
    names
}

// -- internals --------------------------------------------------------

/// Symbol type for a tree-sitter grammar entry point. Every grammar
/// crate exports `tree_sitter_<name>` as an `extern "C" fn` returning
/// a pointer to the language's static data.
type TsLanguageFn = unsafe extern "C" fn() -> *const ffi::TSLanguage;

unsafe fn load_tree_sitter_language(
    language_name: &str,
    library_path: &Path,
) -> Result<TsLanguage, GrammarLoadError> {
    let library = Library::new(library_path).map_err(|_| GrammarLoadError::LibraryNotFound {
        path: library_path.to_path_buf(),
    })?;

    let symbol_name = format!("tree_sitter_{}\0", language_name.replace('-', "_"));
    let symbol: Symbol<TsLanguageFn> =
        library
            .get(symbol_name.as_bytes())
            .map_err(|_| GrammarLoadError::SymbolNotFound {
                symbol: symbol_name.trim_end_matches('\0').to_string(),
            })?;

    let raw_ptr = symbol();
    if raw_ptr.is_null() {
        return Err(GrammarLoadError::SymbolNotFound {
            symbol: symbol_name.trim_end_matches('\0').to_string(),
        });
    }

    // `Language::from_raw` wraps a `*const TSLanguage` in the safe
    // handle type. The pointer remains valid because the library is
    // leaked at the end of this function, so the loaded code is never
    // unmapped while any `TsLanguage` derived from it is alive.
    let language = TsLanguage::from_raw(raw_ptr);

    let plugin_abi = language.version() as u32;
    let kernel_abi = tree_sitter::LANGUAGE_VERSION as u32;
    let min_compat = tree_sitter::MIN_COMPATIBLE_LANGUAGE_VERSION as u32;
    if plugin_abi < min_compat || plugin_abi > kernel_abi {
        return Err(GrammarLoadError::AbiIncompatible {
            plugin_abi,
            kernel_abi,
        });
    }

    // Leak the library handle so the loaded code stays mapped for the
    // life of the process. A grammar loaded once and never unloaded
    // matches tree-sitter's own lifetime model.
    Box::leak(Box::new(library));

    Ok(language)
}
