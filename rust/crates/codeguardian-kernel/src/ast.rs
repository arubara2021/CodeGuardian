//! AST arena and node kind vocabulary for the Code Intelligence Kernel.
//!
//! The AST is stored as a flat, contiguous vector of fixed-size records.
//! Nodes reference each other by `u32` index, never by pointer. This
//! layout is what makes the graph cheap to serialize later: a straight
//! byte copy of the arena is a valid on-disk representation, with no
//! pointer chasing, no allocation per node, and no dependence on the
//! Rust memory model.
//!
//! The tree structure is expressed through three fields on every node:
//! `parent`, `first_child`, and `next_sibling`. `NO_NODE` (`u32::MAX`)
//! is the sentinel for "no link". The invariant this module protects is
//! that every non-sentinel link index points at a live node. An
//! accessor that returns a dangling index is a bug; an accessor that
//! returns `None` for a dangling index is correct defensive behavior.
//!
//! `NodeKind` is a typed lens, not an exhaustive enum. Variants carry
//! the syntax kinds the kernel distinguishes across all nine supported
//! languages (Python, TypeScript, JavaScript, Java, Go, Rust, C#, C,
//! C++). Everything else is `Other(raw_kind_id)`, where `raw_kind_id`
//! is the tree-sitter kind identifier for the language that produced
//! the file. The graph remains lossless; the enum remains stable
//! across grammar upgrades.
//!
//! New variants are appended at the end of the enum, after
//! `Other(u16)`, so that the discriminant of every existing variant —
//! and therefore the wire code used by `cpg.rs` — is unchanged. The
//! wire-code mapping for the new variants is defined in `cpg.rs`
//! alongside the existing mapping.

use std::collections::HashMap;

/// Sentinel index meaning "no node". Used for `parent`, `first_child`,
/// `next_sibling`, and `name` when the corresponding link is absent.
pub const NO_NODE: u32 = u32::MAX;

/// Flag bit: the node is a named node in tree-sitter's sense.
pub const FLAG_NAMED: u16 = 1 << 0;
/// Flag bit: the node is an error-recovery node produced by tree-sitter.
pub const FLAG_ERROR: u16 = 1 << 1;
/// Flag bit: the node is a missing node inserted by tree-sitter to
/// repair a parse.
pub const FLAG_MISSING: u16 = 1 << 2;
/// Flag bit: the node is an extra node (comments, whitespace) that
/// tree-sitter attaches outside the normal grammar.
pub const FLAG_EXTRA: u16 = 1 << 3;

/// The typed vocabulary of syntax kinds the kernel distinguishes.
///
/// Variants are grouped by role. The grouping is a reading aid; the
/// discriminant values carry no meaning and must not be relied on.
/// `Other` carries the raw tree-sitter kind id for any syntax the enum
/// does not name. The kernel preserves these nodes without interpreting
/// them, so the graph is complete even for grammar constructs the enum
/// does not cover.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NodeKind {
    // Structural
    Module,
    Block,
    FunctionDef,
    ClassDef,
    MethodDef,
    Lambda,
    Decorator,
    Parameter,
    Argument,
    Identifier,
    Attribute,
    Comment,
    TypeAnnotation,

    // Statements
    Assignment,
    AugmentedAssignment,
    Return,
    Yield,
    If,
    Elif,
    Else,
    For,
    While,
    Try,
    Except,
    Finally,
    With,
    Raise,
    Pass,
    Break,
    Continue,
    Global,
    Nonlocal,
    Assert,
    Del,
    Import,
    ImportFrom,
    Match,
    Case,

    // Expressions
    Call,
    BinaryOp,
    UnaryOp,
    ComparisonOp,
    BoolOp,
    ConditionalExpression,
    Literal,
    List,
    Dict,
    Set,
    Tuple,
    Subscript,
    Slice,
    NewExpression,
    AwaitExpression,

    // Error recovery
    Error,
    Missing,

    // Catch-all for syntax kinds the enum does not name. The payload is
    // the raw tree-sitter kind id for the language that produced the
    // node.
    Other(u16),

    // The variants below were added when the kernel was extended from
    // two languages (Python, TypeScript) to nine (adding JavaScript,
    // Java, Go, Rust, C#, C, C++). They are appended after `Other(u16)`
    // so that the discriminant — and therefore the wire code — of
    // every earlier variant is unchanged. The wire-code mapping for
    // these variants lives in `cpg.rs`.

    // Structural types (Go, Rust, Java, C#, C, C++, TypeScript)
    Struct,
    Interface,
    Trait,
    Impl,
    Enum,
    Union,
    Namespace,
    Package,

    // Type system (Rust, Java, C#, TypeScript)
    TypeParameter,
    TypeAlias,
    WhereClause,
    Lifetime,

    // Metadata and modifiers (Java, C#)
    Annotation,
    Modifier,

    // Go-specific control flow
    GoStatement,
    DeferStatement,
    SelectStatement,

    // Rust-specific
    Loop,
    MacroDefinition,
    MacroCall,

    // C and C++ preprocessor
    Preprocessor,
    TemplateDeclaration,

    // C-family control flow (Java, C#)
    Synchronized,
    Lock,
    Goto,

    // JavaScript and TypeScript expression form
    TemplateLiteral,

    // C# property and JavaScript accessor
    Property,

    // Explicit type cast (Java, C#, Go, Rust, C)
    TypeCast,
}

/// A single node in the flat AST arena.
///
/// All byte offsets are `u32`. A source file larger than 4 GiB is
/// rejected by `parser::parse_file` before any node is constructed, so
/// every offset in the arena is guaranteed representable.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct AstNode {
    /// The typed syntax kind of the node.
    pub kind: NodeKind,
    /// A bit set of `FLAG_*` constants.
    pub flags: u16,
    /// Byte offset of the first byte of the node's source span.
    pub start_byte: u32,
    /// Byte offset one past the last byte of the node's source span.
    pub end_byte: u32,
    /// Zero-based line of the node's start.
    pub start_row: u32,
    /// Zero-based column of the node's start.
    pub start_col: u32,
    /// Index of the parent node, or `NO_NODE` for the root.
    pub parent: u32,
    /// Index of the first child node, or `NO_NODE` if the node is a leaf.
    pub first_child: u32,
    /// Index of the next sibling node, or `NO_NODE` if the node is the
    /// last child of its parent.
    pub next_sibling: u32,
    /// String-table index of the node's name, or `NO_NODE` if unnamed.
    /// Set for identifiers, function definitions, class definitions, and
    /// any other node whose grammar exposes a name field.
    pub name: u32,
}

impl AstNode {
    /// Return whether this node is named in tree-sitter's sense.
    #[inline]
    pub fn is_named(&self) -> bool {
        self.flags & FLAG_NAMED != 0
    }

    /// Return whether this node was produced by tree-sitter's error
    /// recovery.
    #[inline]
    pub fn is_error(&self) -> bool {
        self.flags & FLAG_ERROR != 0
    }

    /// Return whether this node was inserted by tree-sitter to repair a
    /// parse.
    #[inline]
    pub fn is_missing(&self) -> bool {
        self.flags & FLAG_MISSING != 0
    }

    /// Return whether this node is an extra node (comment, whitespace).
    #[inline]
    pub fn is_extra(&self) -> bool {
        self.flags & FLAG_EXTRA != 0
    }

    /// Return the length in bytes of the node's source span.
    #[inline]
    pub fn byte_len(&self) -> u32 {
        self.end_byte.saturating_sub(self.start_byte)
    }

    /// Return whether the node carries a name string.
    #[inline]
    pub fn has_name(&self) -> bool {
        self.name != NO_NODE
    }
}

/// Interned string table.
///
/// Names are stored once and referenced by `u32` id. The table is
/// append-only: no string is ever removed. Two calls to `intern` with
/// the same text return the same id.
#[derive(Debug, Default)]
pub struct StringTable {
    strings: Vec<String>,
    lookup: HashMap<String, u32>,
}

impl StringTable {
    /// Construct an empty string table.
    pub fn new() -> Self {
        Self::default()
    }

    /// Return the id for `text`, inserting it if absent.
    pub fn intern(&mut self, text: &str) -> u32 {
        if let Some(&id) = self.lookup.get(text) {
            return id;
        }
        let id = self.strings.len() as u32;
        let owned = text.to_owned();
        self.strings.push(owned.clone());
        self.lookup.insert(owned, id);
        id
    }

    /// Return the string for `id`, or `None` if `id` is out of range.
    pub fn get(&self, id: u32) -> Option<&str> {
        self.strings.get(id as usize).map(String::as_str)
    }

    /// Return the number of interned strings.
    pub fn len(&self) -> usize {
        self.strings.len()
    }

    /// Return whether the table is empty.
    pub fn is_empty(&self) -> bool {
        self.strings.is_empty()
    }
}

/// The AST arena. Nodes are stored contiguously and referenced by
/// `u32` id. The arena is append-only for the lifetime of a parse; the
/// only mutation a later block performs is in-place link repair, which
/// is what `get_mut` exists for.
#[derive(Debug, Default)]
pub struct AstArena {
    nodes: Vec<AstNode>,
    strings: StringTable,
}

impl AstArena {
    /// Construct an empty arena.
    pub fn new() -> Self {
        Self::default()
    }

    /// Construct an empty arena with capacity for `cap` nodes.
    pub fn with_capacity(cap: usize) -> Self {
        Self {
            nodes: Vec::with_capacity(cap),
            strings: StringTable::new(),
        }
    }

    /// Return the number of nodes in the arena.
    pub fn len(&self) -> usize {
        self.nodes.len()
    }

    /// Return whether the arena is empty.
    pub fn is_empty(&self) -> bool {
        self.nodes.is_empty()
    }

    /// Append a node and return its index.
    pub fn push(&mut self, node: AstNode) -> u32 {
        let id = self.nodes.len() as u32;
        self.nodes.push(node);
        id
    }

    /// Return a reference to the node at `id`, or `None` if out of
    /// range.
    pub fn get(&self, id: u32) -> Option<&AstNode> {
        self.nodes.get(id as usize)
    }

    /// Return a mutable reference to the node at `id`, or `None` if out
    /// of range.
    pub fn get_mut(&mut self, id: u32) -> Option<&mut AstNode> {
        self.nodes.get_mut(id as usize)
    }

    /// Intern a string and return its id.
    pub fn intern(&mut self, text: &str) -> u32 {
        self.strings.intern(text)
    }

    /// Return the string for `id`, or `None` if `id` is out of range or
    /// is the no-name sentinel.
    pub fn string(&self, id: u32) -> Option<&str> {
        if id == NO_NODE {
            return None;
        }
        self.strings.get(id)
    }

    /// Return a reference to the string table.
    pub fn string_table(&self) -> &StringTable {
        &self.strings
    }

    /// Return the parent id of `id`, or `None` if `id` has no parent or
    /// is out of range.
    pub fn parent_of(&self, id: u32) -> Option<u32> {
        let node = self.get(id)?;
        if node.parent == NO_NODE {
            None
        } else {
            Some(node.parent)
        }
    }

    /// Return an iterator over the direct children of `id`.
    pub fn children_of(&self, id: u32) -> ChildrenIter<'_> {
        let next = self.get(id).map(|n| n.first_child).unwrap_or(NO_NODE);
        ChildrenIter { arena: self, next }
    }

    /// Return a preorder iterator over the subtree rooted at `root`.
    /// The root itself is the first item.
    pub fn walk_preorder(&self, root: u32) -> PreorderIter<'_> {
        PreorderIter {
            arena: self,
            root,
            next: root,
        }
    }
}

/// Iterator over the direct children of a node.
pub struct ChildrenIter<'a> {
    arena: &'a AstArena,
    next: u32,
}

impl<'a> Iterator for ChildrenIter<'a> {
    type Item = u32;

    fn next(&mut self) -> Option<u32> {
        let current = self.next;
        if current == NO_NODE {
            return None;
        }
        let node = self.arena.get(current)?;
        self.next = node.next_sibling;
        Some(current)
    }
}

/// Preorder iterator over a subtree of the arena.
///
/// Walks in the order the tree-sitter grammar produced the nodes: a
/// node, then its first child, then the next sibling, then ascending.
/// The ascent stops at `root` so the iterator never leaves the subtree
/// it was asked to walk, even if `root` has a parent in the arena.
pub struct PreorderIter<'a> {
    arena: &'a AstArena,
    root: u32,
    next: u32,
}

impl<'a> Iterator for PreorderIter<'a> {
    type Item = u32;

    fn next(&mut self) -> Option<u32> {
        let current = self.next;
        if current == NO_NODE {
            return None;
        }
        let node = match self.arena.get(current) {
            Some(n) => n,
            None => {
                self.next = NO_NODE;
                return None;
            }
        };

        if node.first_child != NO_NODE {
            self.next = node.first_child;
            return Some(current);
        }

        let mut cursor = current;
        loop {
            if cursor == self.root {
                self.next = NO_NODE;
                break;
            }
            let cursor_node = match self.arena.get(cursor) {
                Some(n) => n,
                None => {
                    self.next = NO_NODE;
                    break;
                }
            };
            if cursor_node.next_sibling != NO_NODE {
                self.next = cursor_node.next_sibling;
                break;
            }
            if cursor_node.parent == NO_NODE {
                self.next = NO_NODE;
                break;
            }
            cursor = cursor_node.parent;
        }
        Some(current)
    }
}
