//! Go language module.
//!
//! Implements `LanguageModule` for Go by supplying the language
//! identity, the tree-sitter grammar, and the mapping from Go's
//! tree-sitter kind strings onto `NodeKind`. Symbol extraction and
//! callee extraction use the shared defaults — Go's `function_declaration`
//! and `method_declaration` produce the same symbol kinds as the other
//! languages once classified.
//!
//! Go's `type S struct { ... }` parses as a `type_declaration` wrapping
//! a `type_spec` whose `type` field is a `struct_type`. The name lives
//! on `type_spec`, not on `struct_type`, so a struct symbol is only
//! recorded if the containing `type_spec` is named. This module
//! classifies `struct_type` as `NodeKind::Struct` regardless; the
//! symbol-table layer decides whether a struct node with no name field
//! contributes a symbol.

use tree_sitter::Language as TsLanguage;

use crate::ast::NodeKind;
use crate::langs::{classify_shared, Language, LanguageModule};

/// The Go language module.
pub struct GoModule;

impl LanguageModule for GoModule {
    fn language(&self) -> Language {
        Language::Go
    }

    fn grammar(&self) -> TsLanguage {
        tree_sitter_go::LANGUAGE.into()
    }

    fn classify(&self, kind: &str, raw_id: u16) -> NodeKind {
        if let Some(shared) = classify_shared(kind) {
            return shared;
        }
        match kind {
            "source_file" => NodeKind::Module,
            "package_clause" => NodeKind::Package,
            "import_declaration" | "import_spec" => NodeKind::Import,
            "function_declaration" => NodeKind::FunctionDef,
            "method_declaration" => NodeKind::MethodDef,
            "struct_type" => NodeKind::Struct,
            "interface_type" => NodeKind::Interface,
            "type_alias" => NodeKind::TypeAlias,
            "type_parameter_list" | "type_parameter_declaration" => NodeKind::TypeParameter,
            "block" | "statement_list" => NodeKind::Block,
            "if_statement" => NodeKind::If,
            "for_statement" => NodeKind::For,
            "expression_switch_statement" | "type_switch_statement" => NodeKind::Match,
            "select_statement" => NodeKind::SelectStatement,
            "go_statement" => NodeKind::GoStatement,
            "defer_statement" => NodeKind::DeferStatement,
            "return_statement" => NodeKind::Return,
            "break_statement" => NodeKind::Break,
            "continue_statement" => NodeKind::Continue,
            "goto_statement" => NodeKind::Goto,
            "call_expression" => NodeKind::Call,
            "selector_expression" => NodeKind::Attribute,
            "index_expression" => NodeKind::Subscript,
            "slice_expression" => NodeKind::Slice,
            "binary_expression" => NodeKind::BinaryOp,
            "unary_expression" => NodeKind::UnaryOp,
            "type_assertion_expression" => NodeKind::TypeCast,
            "func_literal" => NodeKind::Lambda,
            "parameter_declaration" | "variadic_parameter_declaration" => NodeKind::Parameter,
            "argument_list" => NodeKind::Argument,
            "literal_value"
            | "raw_string_literal"
            | "interpreted_string_literal"
            | "rune_literal"
            | "int_literal"
            | "float_literal"
            | "imaginary_literal"
            | "nil"
            | "true"
            | "false" => NodeKind::Literal,
            "array_type" | "slice_type" => NodeKind::List,
            "map_type" => NodeKind::Dict,
            _ => NodeKind::Other(raw_id),
        }
    }
}
