//! Language module dispatch.
//!
//! Defines the `Language` enum, the `LanguageModule` trait, and the
//! `dispatch` function that returns the module for a given language.
//! Each supported language implements `LanguageModule` in its own
//! submodule (`python.rs`, `typescript.rs`, and so on).
//!
//! The trait has three required methods and two default methods. The
//! defaults delegate to the language-agnostic code in `symbol_table`
//! and `call_graph`. A language overrides a default only when its
//! syntax genuinely differs — for example, a future Ruby module would
//! override `extract_callee` to handle dynamic dispatch. No language
//! in the current set needs to override a default.
//!
//! `dispatch` returns `Option<&dyn LanguageModule>`. A language whose
//! module is not yet wired returns `None`; the caller (ultimately
//! `parser::parse_file`) converts `None` into a typed error. This
//! makes the "language declared but not implemented" case explicit
//! rather than a silent panic.
//!
//! C# was removed from the enum after `tree-sitter-c-sharp` 0.23.5 was
//! found to produce a grammar ABI that tree-sitter 0.24 refuses to
//! load. Re-adding it requires a compatible grammar release.

use tree_sitter::Language as TsLanguage;

use crate::ast::{AstArena, NodeKind};
use crate::call_graph;
use crate::symbol_table::Symbol;

pub mod c;
pub mod cpp;
pub mod go;
pub mod java;
pub mod javascript;
pub mod python;
pub mod rust;
pub mod typescript;

use c::CModule;
use cpp::CppModule;
use go::GoModule;
use java::JavaModule;
use javascript::JavaScriptModule;
use python::PythonModule;
use rust::RustModule;
use typescript::TypeScriptModule;

/// A source language supported by the kernel.
///
/// Variants are ordered so that the discriminant of `Python` is 0 and
/// `TypeScript` is 1, matching the values the pre-multi-language
/// `parser::Language` enum used. This is not load-bearing — the enum
/// is not serialized — but keeping the two lowest values stable
/// simplifies debugging when a CPG file predates the multi-language
/// extension.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Language {
    /// Python, parsed by tree-sitter-python.
    Python,
    /// TypeScript, parsed by tree-sitter-typescript.
    TypeScript,
    /// JavaScript, parsed by tree-sitter-javascript.
    JavaScript,
    /// Java, parsed by tree-sitter-java.
    Java,
    /// Go, parsed by tree-sitter-go.
    Go,
    /// Rust, parsed by tree-sitter-rust.
    Rust,
    /// C, parsed by tree-sitter-c.
    C,
    /// C++, parsed by tree-sitter-cpp.
    Cpp,
}

impl Language {
    /// Return the canonical human-readable name of the language.
    pub fn name(self) -> &'static str {
        match self {
            Language::Python => "python",
            Language::TypeScript => "typescript",
            Language::JavaScript => "javascript",
            Language::Java => "java",
            Language::Go => "go",
            Language::Rust => "rust",
            Language::C => "c",
            Language::Cpp => "cpp",
        }
    }

    /// Return the language for `name`, or `None` if unrecognized.
    ///
    /// Accepts the canonical name and the common short aliases a user
    /// is likely to write: `js` for JavaScript, `golang` for Go, `rs`
    /// for Rust, `c++` and `cxx` for C++.
    pub fn from_name(name: &str) -> Option<Self> {
        match name {
            "python" => Some(Language::Python),
            "typescript" => Some(Language::TypeScript),
            "javascript" | "js" => Some(Language::JavaScript),
            "java" => Some(Language::Java),
            "go" | "golang" => Some(Language::Go),
            "rust" | "rs" => Some(Language::Rust),
            "c" => Some(Language::C),
            "cpp" | "c++" | "cxx" => Some(Language::Cpp),
            _ => None,
        }
    }
}

/// The contract every language module satisfies.
///
/// The three required methods describe how the kernel learns about a
/// language: which language it is, which tree-sitter grammar it uses,
/// and how to translate the grammar's kind strings into `NodeKind`.
///
/// The two default methods describe how the kernel extracts structure
/// from a parse tree. Both defaults delegate to the shared code in
/// `symbol_table` and `call_graph`, which operates purely on
/// `NodeKind` and is therefore language-agnostic. A language that
/// needs different behavior overrides a default; none of the eight
/// currently supported languages does.
pub trait LanguageModule: Send + Sync {
    /// Return the language this module implements.
    fn language(&self) -> Language;

    /// Return the tree-sitter grammar for this language.
    fn grammar(&self) -> TsLanguage;

    /// Classify a tree-sitter kind string into a `NodeKind`.
    ///
    /// `raw_id` is the grammar-specific kind id, used as the payload
    /// for `NodeKind::Other` when no typed variant matches.
    fn classify(&self, kind: &str, raw_id: u16) -> NodeKind;

    /// Extract every named symbol from the subtree rooted at `root`.
    ///
    /// The default implementation delegates to
    /// `symbol_table::extract_symbols`, which operates on `NodeKind`
    /// alone and produces identical output for any language whose
    /// `classify` maps declarations to the appropriate variants.
    fn extract_symbols(&self, arena: &AstArena, root: u32, file_id: u32) -> Vec<Symbol> {
        crate::symbol_table::extract_symbols(arena, root, file_id)
    }

    /// Return the textual name of the callee at a call site.
    ///
    /// The default implementation delegates to the shared callee-name
    /// extractor in `call_graph`, which walks the callee subtree and
    /// returns the last identifier. A language whose call syntax
    /// cannot be reduced to "last identifier" would override this.
    fn extract_callee(&self, arena: &AstArena, call_node: u32) -> Option<String> {
        call_graph::default_extract_callee_name(arena, call_node)
    }
}

/// Classify a grammar kind string that is shared across every
/// supported grammar.
///
/// Every language module's `classify` should call this first, then
/// fall through to language-specific kinds, then return
/// `NodeKind::Other(raw_id)` as the final fallback. The variants this
/// function handles are the tree-sitter built-ins that appear in every
/// grammar with the same meaning.
pub fn classify_shared(kind: &str) -> Option<NodeKind> {
    match kind {
        "identifier" | "type_identifier" | "property_identifier" | "field_identifier" => {
            Some(NodeKind::Identifier)
        }
        "comment" | "line_comment" | "block_comment" => Some(NodeKind::Comment),
        "ERROR" => Some(NodeKind::Error),
        "MISSING" => Some(NodeKind::Missing),
        _ => None,
    }
}

static C_MODULE: CModule = CModule;
static CPP_MODULE: CppModule = CppModule;
static GO_MODULE: GoModule = GoModule;
static JAVA_MODULE: JavaModule = JavaModule;
static JAVASCRIPT_MODULE: JavaScriptModule = JavaScriptModule;
static PYTHON_MODULE: PythonModule = PythonModule;
static RUST_MODULE: RustModule = RustModule;
static TYPESCRIPT_MODULE: TypeScriptModule = TypeScriptModule;

/// Return the module that implements `language`, or `None` if the
/// language is declared but its module is not yet wired.
///
/// After C# removal every variant in `Language` has a module, so the
/// `None` arm is unreachable from `Language`. It remains for forward
/// compatibility if a variant is added before its module lands.
pub fn dispatch(language: Language) -> Option<&'static dyn LanguageModule> {
    match language {
        Language::Python => Some(&PYTHON_MODULE),
        Language::TypeScript => Some(&TYPESCRIPT_MODULE),
        Language::JavaScript => Some(&JAVASCRIPT_MODULE),
        Language::Java => Some(&JAVA_MODULE),
        Language::Go => Some(&GO_MODULE),
        Language::Rust => Some(&RUST_MODULE),
        Language::C => Some(&C_MODULE),
        Language::Cpp => Some(&CPP_MODULE),
    }
}