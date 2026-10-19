//! Symbol extraction and lookup.
//!
//! The symbol table records the named declarations a caller needs to
//! resolve a call site: functions, classes, methods, and parameters.
//! It does not record imports, local variables, or references. A call
//! to `foo.bar()` where `foo` is an imported module resolves to nothing
//! in this file; cross-file resolution is a later concern.
//!
//! The invariant this module protects is that `lookup_by_simple_name`
//! never fails to report ambiguity. A simple name that appears in more
//! than one place returns all matches; the caller decides whether the
//! ambiguity is resolvable. A qualified name that appears more than
//! once is a definition error and the table records the shadowing by
//! returning the last definition.
//!
//! The set of "class-like" scope kinds includes every construct that
//! groups methods across the nine supported languages: Python and
//! TypeScript classes, Rust structs, interfaces, and traits, Java
//! classes and interfaces, Go structs, C# classes and structs, and C
//! and C++ structs and unions. A method nested inside any of these
//! receives a dotted qualified name that includes the containing
//! type.

use std::collections::HashMap;

use crate::ast::{AstArena, NodeKind};

/// The role a symbol plays in a file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SymbolKind {
    /// A top-level function.
    Function,
    /// A class, struct, interface, trait, or enum.
    Class,
    /// A method defined inside a class-like scope.
    Method,
    /// A parameter of a function or method.
    Parameter,
}

/// One named declaration.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Symbol {
    /// The dot-joined path from the outermost enclosing named scope to
    /// this symbol. For a top-level function `parse_config`, this is
    /// `parse_config`. For a method `parse` inside class `Parser`, this
    /// is `Parser.parse`.
    pub qualified_name: String,
    /// The symbol's own name, without any enclosing scope.
    pub simple_name: String,
    /// The role this symbol plays.
    pub kind: SymbolKind,
    /// The arena index of the file the symbol lives in.
    pub file_id: u32,
    /// The arena index of the definition node.
    pub node_id: u32,
    /// Byte offset where the definition begins.
    pub start_byte: u32,
    /// Byte offset where the definition ends.
    pub end_byte: u32,
}

/// Index of symbols by qualified name, simple name, and file.
#[derive(Debug, Default)]
pub struct SymbolTable {
    symbols: Vec<Symbol>,
    by_qualified: HashMap<String, Vec<usize>>,
    by_simple: HashMap<String, Vec<usize>>,
    by_file: HashMap<u32, Vec<usize>>,
}

impl SymbolTable {
    /// Construct an empty table.
    pub fn new() -> Self {
        Self::default()
    }

    /// Insert a symbol and return its index.
    pub fn insert(&mut self, symbol: Symbol) -> usize {
        let index = self.symbols.len();
        self.by_qualified
            .entry(symbol.qualified_name.clone())
            .or_default()
            .push(index);
        self.by_simple
            .entry(symbol.simple_name.clone())
            .or_default()
            .push(index);
        self.by_file
            .entry(symbol.file_id)
            .or_default()
            .push(index);
        self.symbols.push(symbol);
        index
    }

    /// Return the number of symbols in the table.
    pub fn len(&self) -> usize {
        self.symbols.len()
    }

    /// Return whether the table is empty.
    pub fn is_empty(&self) -> bool {
        self.symbols.is_empty()
    }

    /// Return every symbol in insertion order.
    pub fn all(&self) -> &[Symbol] {
        &self.symbols
    }

    /// Return the last symbol with the given qualified name, or `None`.
    ///
    /// A qualified name that appears more than once is a definition
    /// error; the last definition shadows the earlier ones and is the
    /// one returned.
    pub fn lookup_by_qualified_name(&self, name: &str) -> Option<&Symbol> {
        self.by_qualified
            .get(name)
            .and_then(|ids| ids.last())
            .and_then(|&idx| self.symbols.get(idx))
    }

    /// Return every symbol with the given simple name.
    pub fn lookup_by_simple_name(&self, name: &str) -> Vec<&Symbol> {
        self.by_simple
            .get(name)
            .map(|ids| {
                ids.iter()
                    .filter_map(|&idx| self.symbols.get(idx))
                    .collect()
            })
            .unwrap_or_default()
    }

    /// Return every symbol defined in `file_id`.
    pub fn symbols_in_file(&self, file_id: u32) -> Vec<&Symbol> {
        self.by_file
            .get(&file_id)
            .map(|ids| {
                ids.iter()
                    .filter_map(|&idx| self.symbols.get(idx))
                    .collect()
            })
            .unwrap_or_default()
    }
}

/// Extract every symbol from the subtree rooted at `root`.
///
/// Walks the AST once in preorder. Each function, class-like declaration,
/// method, or parameter node with a name produces one symbol. Anonymous
/// lambdas and unnamed parameters are skipped.
pub fn extract_symbols(arena: &AstArena, root: u32, file_id: u32) -> Vec<Symbol> {
    let mut symbols = Vec::new();
    for node_id in arena.walk_preorder(root) {
        let node = match arena.get(node_id) {
            Some(n) => n,
            None => continue,
        };
        let kind = match classify_symbol(arena, node_id, node.kind) {
            Some(k) => k,
            None => continue,
        };
        let simple_name = match arena.string(node.name) {
            Some(s) => s.to_owned(),
            None => continue,
        };
        let qualified_name = qualified_name_of(arena, node_id, &simple_name);
        symbols.push(Symbol {
            qualified_name,
            simple_name,
            kind,
            file_id,
            node_id,
            start_byte: node.start_byte,
            end_byte: node.end_byte,
        });
    }
    symbols
}

/// Map a syntax kind to a symbol kind, or `None` if the node does not
/// declare a symbol.
///
/// A function nested inside a class-like scope is a method.
/// `NodeKind::Impl` is intentionally not classified as a symbol: an
/// `impl Foo` block names the type it implements, not a new symbol, so
/// the impl node contributes a scope but no entry. Its methods are
/// classified as `Method` because `is_inside_class` walks through
/// `Impl` when computing the enclosing scope.
fn classify_symbol(arena: &AstArena, node_id: u32, kind: NodeKind) -> Option<SymbolKind> {
    match kind {
        NodeKind::FunctionDef => {
            if is_inside_class(arena, node_id) {
                Some(SymbolKind::Method)
            } else {
                Some(SymbolKind::Function)
            }
        }
        NodeKind::ClassDef
        | NodeKind::Struct
        | NodeKind::Interface
        | NodeKind::Trait
        | NodeKind::Enum
        | NodeKind::Union => Some(SymbolKind::Class),
        NodeKind::MethodDef => Some(SymbolKind::Method),
        NodeKind::Parameter => Some(SymbolKind::Parameter),
        _ => None,
    }
}

/// Return whether `node_id` is lexically enclosed by a class-like body.
///
/// The walk stops at the first function or method boundary, so a
/// function nested inside a method inside a class is a function, not a
/// method. `Impl` blocks count as class-like for the purpose of this
/// check so that Rust methods inside `impl Foo { ... }` are classified
/// as methods rather than top-level functions.
fn is_inside_class(arena: &AstArena, node_id: u32) -> bool {
    let mut current = arena.parent_of(node_id);
    while let Some(id) = current {
        let node = match arena.get(id) {
            Some(n) => n,
            None => break,
        };
        match node.kind {
            NodeKind::ClassDef
            | NodeKind::Struct
            | NodeKind::Interface
            | NodeKind::Trait
            | NodeKind::Enum
            | NodeKind::Union
            | NodeKind::Impl => return true,
            NodeKind::FunctionDef | NodeKind::MethodDef | NodeKind::Lambda => return false,
            _ => {}
        }
        current = arena.parent_of(id);
    }
    false
}

/// Build the qualified name by walking up the parent chain.
fn qualified_name_of(arena: &AstArena, node_id: u32, simple_name: &str) -> String {
    let mut parts: Vec<&str> = vec![simple_name];
    let mut current = arena.parent_of(node_id);
    while let Some(id) = current {
        let parent = match arena.get(id) {
            Some(n) => n,
            None => break,
        };
        if is_named_scope(parent.kind) {
            if let Some(name) = arena.string(parent.name) {
                parts.push(name);
            }
        }
        current = arena.parent_of(id);
    }
    parts.reverse();
    parts.join(".")
}

/// Return whether a syntax kind introduces a named lexical scope.
///
/// `NodeKind::Impl` is omitted because tree-sitter's Rust grammar does
/// not expose a `name` field on `impl_item`. Its methods still qualify
/// under `is_inside_class`, but the impl block itself contributes no
/// name segment. A future refinement that extracts the impl's target
/// type would add `Impl` here.
fn is_named_scope(kind: NodeKind) -> bool {
    matches!(
        kind,
        NodeKind::FunctionDef
            | NodeKind::ClassDef
            | NodeKind::MethodDef
            | NodeKind::Struct
            | NodeKind::Interface
            | NodeKind::Trait
            | NodeKind::Enum
            | NodeKind::Union
    )
}