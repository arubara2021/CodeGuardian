//! CodeGuardian Code Intelligence Kernel.
//!
//! Owns Tree-sitter parsing, the AST arena, the symbol table, the call
//! graph, the per-function Control Flow Graph, the Program Dependence
//! Graph, the merged and serializable Code Property Graph, incremental
//! updates, and the query interface.
//!
//! The public surface is the eleven modules and the names re-exported
//! below. Callers write `codeguardian_kernel::Language` rather than
//! `codeguardian_kernel::langs::Language`.
//!
//! Eight languages are compiled in (Python, TypeScript, JavaScript,
//! Java, Go, Rust, C, C++). Each is implemented by a `LanguageModule`
//! in `langs/`. A language whose module is not present in the dispatch
//! table can be added at runtime by loading a compiled tree-sitter
//! grammar through `grammar_loader`. Runtime languages are registered
//! in a process-wide map and resolved through `langs::dispatch_by_name`.

pub mod ast;
pub mod call_graph;
pub mod cfg;
pub mod cpg;
pub mod grammar_loader;
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
pub use grammar_loader::{
    get_runtime_language, is_registered, list_runtime_languages, load_and_register,
    GrammarLoadError, GrammarLoader,
};
pub use langs::{dispatch, dispatch_by_name, GrammarSource, Language, LanguageModule};
pub use parser::{parse_file, parse_files, KernelError, ParseResult};
pub use pdg::{build_pdg, Pdg, PdgEdge, PdgEdgeKind};
pub use symbol_table::{extract_symbols, Symbol, SymbolKind, SymbolTable};
