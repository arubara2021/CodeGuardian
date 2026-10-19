//! Symbol extraction and lookup.
//!
//! The symbol table records the named declarations a caller needs to
//! resolve a call site: functions, classes, methods, and parameters.
//! It does not record imports, local variables, or references.
//!
//! Resolution is scoped. A call site can ask for a symbol by simple
//! name, by qualified name, by class scope, or by file scope. The
//! simple-name lookup reports every match so the caller decides
//! whether the ambiguity is resolvable; the scoped lookups return at
//! most one symbol because they narrow by enclosing class or file.
//!
//! The set of "class-like" scope kinds includes every construct that
//! groups methods across the supported languages: classes, structs,
//! interfaces, traits, and enums.

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
    /// this symbol.
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

    /// Return the number of symbols with the given simple name.
    ///
    /// Used by call-site resolution to decide whether a simple-name
    /// lookup is unambiguous without materialising the full vector.
    pub fn count_simple_name(&self, name: &str) -> usize {
        self.by_simple.get(name).map(|ids| ids.len()).unwrap_or(0)
    }

    /// Return the method named `method_name` defined inside the
    /// class-like scope whose simple name is `class_name`, or `None`.
    ///
    /// The lookup is by qualified name (`<class_name>.<method_name>`),
    /// so it does not confuse a method on one class with a method of
    /// the same name on another class. This is what makes
    /// `self.foo()` resolvable inside class `Foo`.
    pub fn lookup_in_class(&self, class_name: &str, method_name: &str) -> Option<&Symbol> {
        let qualified = format!("{class_name}.{method_name}");
        self.lookup_by_qualified_name(&qualified)
    }

    /// Return the symbol with `simple_name` defined in `file_id`, or
    /// `None`.
    ///
    /// When more than one definition of `simple_name` exists in the
    /// file, the last one wins, matching Python's shadowing semantics.
    pub fn lookup_in_file(&self, file_id: u32, simple_name: &str) -> Option<&Symbol> {
        let ids = self.by_file.get(&file_id)?;
        let mut result: Option<&Symbol> = None;
        for &idx in ids {
            let symbol = self.symbols.get(idx)?;
            if symbol.simple_name == simple_name {
                result = Some(symbol);
            }
        }
        result
    }
}

/// Extract every symbol from the subtree rooted at `root`.
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
