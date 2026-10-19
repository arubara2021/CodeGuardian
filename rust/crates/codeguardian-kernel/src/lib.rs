//! CodeGuardian Code Intelligence Kernel.
//!
//! Owns Tree-sitter parsing, the AST arena, the symbol table, the call
//! graph, the per-function Control Flow Graph, the Program Dependence
//! Graph, the merged and serializable Code Property Graph, incremental
//! updates, and the query interface.
//!
//! The public surface is the ten modules and the names re-exported
//! below. Callers write `codeguardian_kernel::Language` rather than
//! `codeguardian_kernel::langs::Language`.
//!
//! Nine languages are supported (Python, TypeScript, JavaScript, Java,
//! Go, Rust, C#, C, C++). Each is implemented by a `LanguageModule` in
//! `langs/`. `dispatch` returns the module for a language; a language
//! whose module is not yet wired returns `None`, and the parser
//! converts that into a typed `KernelError::LanguageNotImplemented`.

pub mod ast;
pub mod call_graph;
pub mod cfg;
pub mod cpg;
pub mod incremental;
pub mod langs;
pub mod parser;
pub mod pdg;
pub mod query;
pub mod symbol_table;

pub use ast::{AstArena, AstNode, NodeKind, StringTable, NO_NODE};
pub use call_graph::{extract_calls, CallEdge};
pub use cfg::{build_cfg, Cfg, CfgEdge, CfgEdgeKind};
pub use cpg::{
    node_kind_from_wire, node_kind_wire_code, node_kind_wire_parts, CodePropertyGraph, CpgEdge,
    CpgError, CpgHeader, CpgNode, EdgeKind, SourceFile,
};
pub use langs::{dispatch, Language, LanguageModule};
pub use parser::{parse_file, parse_files, KernelError, ParseResult};
pub use pdg::{build_pdg, Pdg, PdgEdge, PdgEdgeKind};
pub use symbol_table::{extract_symbols, Symbol, SymbolKind, SymbolTable};
