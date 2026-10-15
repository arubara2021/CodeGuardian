//! Tree-sitter wrapper. The only module that knows tree-sitter exists.
//!
//! `parse_file` takes a language and a source string, runs the
//! appropriate tree-sitter grammar, and converts the resulting tree
//! into a flat `AstArena`. The conversion is a single preorder walk.
//! Byte offsets, rows, columns, and the tree structure are copied
//! across unchanged; the `NodeKind` is classified from the grammar's
//! kind string, with `NodeKind::Other(raw_id)` as the fallback for any
//! syntax the enum does not name.
//!
//! A malformed source file is not an error. Tree-sitter inserts ERROR
//! and MISSING nodes to repair the parse, and those nodes are preserved
//! in the arena with the corresponding flags set. `ParseResult::had_errors`
//! reports whether any repair happened, so the caller can decide how to
//! treat the file. Only catastrophic failures return `Err`.
//!
//! The invariant this module protects is that every byte offset in
//! every `AstNode` is within the source length. A grammar that produces
//! a node past EOF is a bug in the grammar or in tree-sitter, and this
//! module does not attempt to repair it; the resulting offsets are
//! checked before use elsewhere.

use thiserror::Error;
use tree_sitter::{Language as TsLanguage, Node as TsNode, Parser};

use crate::ast::{
    AstArena, AstNode, NodeKind, FLAG_ERROR, FLAG_EXTRA, FLAG_MISSING, FLAG_NAMED, NO_NODE,
};

/// Maximum AST depth accepted by the parser.
///
/// Real-world source is shallow. A Python file nested more than a
/// hundred levels deep is malformed. The limit exists so that a
/// pathological input cannot cause a stack overflow in the recursive
/// conversion walk.
pub const MAX_DEPTH: u32 = 500;

/// Every way parsing can fail catastrophically.
///
/// A parse that produces ERROR or MISSING nodes is not a failure; the
/// caller reads `ParseResult::had_errors`. The variants below are the
/// failures the caller cannot recover from without changing the input.
#[derive(Debug, Error)]
pub enum KernelError {
    /// The tree-sitter parser could not be initialized for a language.
    /// This is a build error: the grammar crate was compiled against a
    /// tree-sitter version the parser rejects.
    #[error("tree-sitter parser initialization failed for {language}")]
    ParserInit { language: &'static str },

    /// Tree-sitter returned no tree. The current code path cannot
    /// trigger this, but the error exists so the call site does not
    /// need an unwrap.
    #[error("tree-sitter returned no tree for {language}")]
    ParseFailed { language: &'static str },

    /// The source exceeds 4 GiB, which is the maximum byte offset the
    /// AST arena can represent in a u32.
    #[error("source length {len} exceeds the 4 GiB limit for u32 byte offsets")]
    SourceTooLarge { len: usize },

    /// The AST is deeper than `MAX_DEPTH`. The conversion walk refuses
    /// to recurse further so it cannot overflow the stack.
    #[error("AST depth {depth} exceeds the limit of {limit}")]
    TreeTooDeep { depth: u32, limit: u32 },
}

/// A source language supported by the kernel.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Language {
    /// Python, parsed by tree-sitter-python.
    Python,
    /// TypeScript, parsed by tree-sitter-typescript (non-TSX grammar).
    TypeScript,
}

impl Language {
    /// Return the human-readable name of the language.
    pub fn name(self) -> &'static str {
        match self {
            Language::Python => "python",
            Language::TypeScript => "typescript",
        }
    }

    /// Return the language for `name`, or `None` if it is not supported.
    pub fn from_name(name: &str) -> Option<Self> {
        match name {
            "python" => Some(Language::Python),
            "typescript" => Some(Language::TypeScript),
            _ => None,
        }
    }

    /// Return the tree-sitter grammar for this language.
    ///
    /// The grammar crates export their grammar as a `LanguageFn`
    /// constant, not as a function. The `Into` conversion produces the
    /// `tree_sitter::Language` the parser requires.
    fn tree_sitter(self) -> TsLanguage {
        match self {
            Language::Python => tree_sitter_python::LANGUAGE.into(),
            Language::TypeScript => tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into(),
        }
    }
}

/// The result of parsing one file.
#[derive(Debug)]
pub struct ParseResult {
    /// The arena holding every node of the parsed file.
    pub arena: AstArena,
    /// The index of the root node in `arena`.
    pub root: u32,
    /// Whether the parse produced ERROR or MISSING nodes.
    pub had_errors: bool,
}

/// Parse one source file with the given language.
///
/// Returns a `ParseResult` whose arena contains every node of the file,
/// including error-recovery nodes. The call fails only on catastrophic
/// errors listed on `KernelError`.
///
/// The conversion walk is recursive with a depth cap. The cap is
/// `MAX_DEPTH`; exceeding it returns `KernelError::TreeTooDeep` rather
/// than overflowing the stack.
pub fn parse_file(language: Language, source: &str) -> Result<ParseResult, KernelError> {
    if source.len() > u32::MAX as usize {
        return Err(KernelError::SourceTooLarge { len: source.len() });
    }

    let mut parser = Parser::new();
    parser
        .set_language(&language.tree_sitter())
        .map_err(|_| KernelError::ParserInit {
            language: language.name(),
        })?;

    let tree = parser
        .parse(source, None)
        .ok_or(KernelError::ParseFailed {
            language: language.name(),
        })?;

    let mut arena = AstArena::new();
    let mut had_errors = false;
    let root = convert_node(
        tree.root_node(),
        source,
        language,
        &mut arena,
        NO_NODE,
        0,
        &mut had_errors,
    )?;

    Ok(ParseResult {
        arena,
        root,
        had_errors,
    })
}

/// Parse a batch of files.
///
/// The batch succeeds only if every file parses. A single catastrophic
/// failure short-circuits the batch and returns that error. A file with
/// ERROR nodes still succeeds; the caller inspects `had_errors`.
pub fn parse_files(inputs: &[(Language, &str)]) -> Result<Vec<ParseResult>, KernelError> {
    inputs
        .iter()
        .map(|(language, source)| parse_file(*language, source))
        .collect()
}

/// Convert one tree-sitter node and its subtree into the arena.
///
/// The subtree is appended to `arena` in preorder. The returned u32 is
/// the arena index of the converted node. `parent` is the arena index
/// of the parent, or `NO_NODE` for the root. `depth` counts recursion
/// levels and is checked against `MAX_DEPTH`.
fn convert_node(
    ts_node: TsNode<'_>,
    source: &str,
    language: Language,
    arena: &mut AstArena,
    parent: u32,
    depth: u32,
    had_errors: &mut bool,
) -> Result<u32, KernelError> {
    if depth > MAX_DEPTH {
        return Err(KernelError::TreeTooDeep {
            depth,
            limit: MAX_DEPTH,
        });
    }

    if ts_node.is_error() || ts_node.is_missing() {
        *had_errors = true;
    }

    let kind = classify(language, ts_node.kind(), ts_node.kind_id());
    let name_id = extract_name_id(ts_node, source, arena);
    let start = ts_node.start_position();

    let id = arena.push(AstNode {
        kind,
        flags: flags_of(ts_node),
        start_byte: ts_node.start_byte() as u32,
        end_byte: ts_node.end_byte() as u32,
        start_row: start.row as u32,
        start_col: start.column as u32,
        parent,
        first_child: NO_NODE,
        next_sibling: NO_NODE,
        name: name_id,
    });

    let child_count = ts_node.child_count();
    let mut previous_child: u32 = NO_NODE;
    for index in 0..child_count {
        let child_ts = match ts_node.child(index) {
            Some(node) => node,
            None => continue,
        };
        let child_id = convert_node(
            child_ts,
            source,
            language,
            arena,
            id,
            depth + 1,
            had_errors,
        )?;
        if previous_child == NO_NODE {
            if let Some(node) = arena.get_mut(id) {
                node.first_child = child_id;
            }
        } else if let Some(node) = arena.get_mut(previous_child) {
            node.next_sibling = child_id;
        }
        previous_child = child_id;
    }

    Ok(id)
}

/// Build the flag word for a tree-sitter node.
fn flags_of(ts_node: TsNode<'_>) -> u16 {
    let mut flags = 0u16;
    if ts_node.is_named() {
        flags |= FLAG_NAMED;
    }
    if ts_node.is_error() {
        flags |= FLAG_ERROR;
    }
    if ts_node.is_missing() {
        flags |= FLAG_MISSING;
    }
    if ts_node.is_extra() {
        flags |= FLAG_EXTRA;
    }
    flags
}

/// Extract and intern the node's name, if it has one.
///
/// A node's name comes from one of two sources. Tree-sitter grammars
/// declare a `name` field on nodes such as function and class
/// definitions, in which case the child named by that field is the
/// name. Identifier leaves carry their name in their source text, which
/// is what lets the call graph resolve callees without holding the
/// source string.
fn extract_name_id(ts_node: TsNode<'_>, source: &str, arena: &mut AstArena) -> u32 {
    let text = if let Some(name_node) = ts_node.child_by_field_name("name") {
        name_node.utf8_text(source.as_bytes()).ok()
    } else if is_name_leaf(ts_node.kind()) {
        ts_node.utf8_text(source.as_bytes()).ok()
    } else {
        None
    };
    match text {
        Some(text) => arena.intern(text),
        None => NO_NODE,
    }
}

/// Return whether a tree-sitter kind string names a leaf whose text is
/// its own name.
fn is_name_leaf(kind: &str) -> bool {
    matches!(
        kind,
        "identifier" | "type_identifier" | "property_identifier"
    )
}

/// Classify a tree-sitter kind string into a `NodeKind`.
///
/// Kinds shared across grammars are matched first. Language-specific
/// kinds are matched in per-language helpers. Anything unmatched
/// becomes `NodeKind::Other(raw_id)`.
fn classify(language: Language, kind: &str, raw_id: u16) -> NodeKind {
    match kind {
        "identifier" | "type_identifier" | "property_identifier" => NodeKind::Identifier,
        "comment" | "line_comment" | "block_comment" => NodeKind::Comment,
        _ => match language {
            Language::Python => classify_python(kind, raw_id),
            Language::TypeScript => classify_typescript(kind, raw_id),
        },
    }
}

/// Classify a Python grammar kind string.
fn classify_python(kind: &str, raw_id: u16) -> NodeKind {
    match kind {
        "module" => NodeKind::Module,
        "block" => NodeKind::Block,
        "function_definition" => NodeKind::FunctionDef,
        "class_definition" => NodeKind::ClassDef,
        "lambda" => NodeKind::Lambda,
        "decorator" => NodeKind::Decorator,
        "parameters" | "lambda_parameters" => NodeKind::Parameter,
        "argument_list" => NodeKind::Argument,
        "attribute" => NodeKind::Attribute,
        "type" => NodeKind::TypeAnnotation,
        "assignment" => NodeKind::Assignment,
        "augmented_assignment" => NodeKind::AugmentedAssignment,
        "return_statement" => NodeKind::Return,
        "yield" => NodeKind::Yield,
        "if_statement" => NodeKind::If,
        "elif_clause" => NodeKind::Elif,
        "else_clause" => NodeKind::Else,
        "for_statement" => NodeKind::For,
        "while_statement" => NodeKind::While,
        "try_statement" => NodeKind::Try,
        "except_clause" => NodeKind::Except,
        "finally_clause" => NodeKind::Finally,
        "with_statement" => NodeKind::With,
        "raise_statement" => NodeKind::Raise,
        "pass_statement" => NodeKind::Pass,
        "break_statement" => NodeKind::Break,
        "continue_statement" => NodeKind::Continue,
        "global_statement" => NodeKind::Global,
        "nonlocal_statement" => NodeKind::Nonlocal,
        "assert_statement" => NodeKind::Assert,
        "delete_statement" => NodeKind::Del,
        "import_statement" => NodeKind::Import,
        "import_from_statement" => NodeKind::ImportFrom,
        "match_statement" => NodeKind::Match,
        "case_clause" => NodeKind::Case,
        "call" => NodeKind::Call,
        "binary_operator" => NodeKind::BinaryOp,
        "unary_operator" => NodeKind::UnaryOp,
        "comparison_operator" => NodeKind::ComparisonOp,
        "boolean_operator" => NodeKind::BoolOp,
        "conditional_expression" => NodeKind::ConditionalExpression,
        "integer" | "float" | "string" | "true" | "false" | "none" => NodeKind::Literal,
        "list" => NodeKind::List,
        "dictionary" => NodeKind::Dict,
        "set" => NodeKind::Set,
        "tuple" => NodeKind::Tuple,
        "subscript" => NodeKind::Subscript,
        "slice" => NodeKind::Slice,
        "await" => NodeKind::AwaitExpression,
        "ERROR" => NodeKind::Error,
        "MISSING" => NodeKind::Missing,
        _ => NodeKind::Other(raw_id),
    }
}

/// Classify a TypeScript grammar kind string.
fn classify_typescript(kind: &str, raw_id: u16) -> NodeKind {
    match kind {
        "program" => NodeKind::Module,
        "statement_block" | "class_body" => NodeKind::Block,
        "function_declaration" | "function" | "generator_function_declaration" => {
            NodeKind::FunctionDef
        }
        "class_declaration" | "abstract_class_declaration" => NodeKind::ClassDef,
        "method_definition" => NodeKind::MethodDef,
        "arrow_function" => NodeKind::Lambda,
        "decorator" => NodeKind::Decorator,
        "required_parameter" | "optional_parameter" | "rest_pattern" => NodeKind::Parameter,
        "arguments" => NodeKind::Argument,
        "member_expression" => NodeKind::Attribute,
        "type_annotation" => NodeKind::TypeAnnotation,
        "variable_declaration"
        | "lexical_declaration"
        | "variable_declarator"
        | "assignment_expression" => NodeKind::Assignment,
        "augmented_assignment_expression" => NodeKind::AugmentedAssignment,
        "return_statement" => NodeKind::Return,
        "yield_expression" => NodeKind::Yield,
        "if_statement" => NodeKind::If,
        "else_clause" => NodeKind::Else,
        "for_statement" | "for_in_statement" => NodeKind::For,
        "while_statement" | "do_statement" => NodeKind::While,
        "try_statement" => NodeKind::Try,
        "catch_clause" => NodeKind::Except,
        "finally_clause" => NodeKind::Finally,
        "throw_statement" => NodeKind::Raise,
        "break_statement" => NodeKind::Break,
        "continue_statement" => NodeKind::Continue,
        "import_statement" => NodeKind::Import,
        "export_statement" => NodeKind::Import,
        "switch_statement" | "switch_case" | "switch_default" => NodeKind::Match,
        "call_expression" | "new_expression" => NodeKind::Call,
        "binary_expression" => NodeKind::BinaryOp,
        "unary_expression" | "update_expression" => NodeKind::UnaryOp,
        "ternary_expression" => NodeKind::ConditionalExpression,
        "number" | "string" | "true" | "false" | "null" | "undefined" | "regex" => {
            NodeKind::Literal
        }
        "array" => NodeKind::List,
        "object" => NodeKind::Dict,
        "subscript_expression" => NodeKind::Subscript,
        "await_expression" => NodeKind::AwaitExpression,
        "ERROR" => NodeKind::Error,
        "MISSING" => NodeKind::Missing,
        _ => NodeKind::Other(raw_id),
    }
}
