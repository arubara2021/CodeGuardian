//! C language module.
//!
//! Implements `LanguageModule` for C by supplying the language
//! identity, the tree-sitter grammar, and the mapping from C's
//! tree-sitter kind strings onto `NodeKind`. Symbol extraction and
//! callee extraction use the shared defaults.
//!
//! # Known gap
//!
//! tree-sitter-c's `function_definition` node does not carry a `name`
//! field. The function name lives inside a nested `function_declarator`
//! whose `declarator` field is the identifier. The current parser
//! (`parser.rs::extract_name_id`) does not walk the declarator chain,
//! so C `function_definition` nodes have no `name`, are skipped by
//! `symbol_table::extract_symbols`, and cannot be the target of a
//! resolved call. C therefore does not support same-file blast radius
//! today. Closing the gap requires extending `parser.rs::extract_name_id`
//! to descend into the declarator field for languages whose grammar
//! nests the name one level down. That extension is out of scope for
//! this batch; C ships at `best-effort` reliability tier until it lands.
//!
//! Call sites are still classified correctly; a call to `target()` in a
//! C file produces a `Call` node and an unresolved `CallEdge`. The edge
//! is dropped by `push_call_edge` because the callee name does not
//! resolve, so the caller simply does not appear in any blast report.

use tree_sitter::Language as TsLanguage;

use crate::ast::NodeKind;
use crate::langs::{classify_shared, Language, LanguageModule};

/// The C language module.
pub struct CModule;

impl LanguageModule for CModule {
    fn language(&self) -> Language {
        Language::C
    }

    fn grammar(&self) -> TsLanguage {
        tree_sitter_c::LANGUAGE.into()
    }

    fn classify(&self, kind: &str, raw_id: u16) -> NodeKind {
        if let Some(shared) = classify_shared(kind) {
            return shared;
        }
        match kind {
            "translation_unit" => NodeKind::Module,
            "preproc_include"
            | "preproc_def"
            | "preproc_function_def"
            | "preproc_ifdef"
            | "preproc_if"
            | "preproc_else"
            | "preproc_elif"
            | "preproc_defined"
            | "preproc_call" => NodeKind::Preprocessor,
            "function_definition" => NodeKind::FunctionDef,
            "declaration" => NodeKind::Assignment,
            "struct_specifier" => NodeKind::Struct,
            "union_specifier" => NodeKind::Union,
            "enum_specifier" => NodeKind::Enum,
            "type_definition" => NodeKind::TypeAlias,
            "parameter_declaration" | "variadic_parameter" => NodeKind::Parameter,
            "argument_list" => NodeKind::Argument,
            "compound_statement" => NodeKind::Block,
            "if_statement" => NodeKind::If,
            "else_clause" => NodeKind::Else,
            "for_statement" => NodeKind::For,
            "while_statement" | "do_statement" => NodeKind::While,
            "switch_statement" => NodeKind::Match,
            "case_statement" => NodeKind::Case,
            "return_statement" => NodeKind::Return,
            "break_statement" => NodeKind::Break,
            "continue_statement" => NodeKind::Continue,
            "goto_statement" => NodeKind::Goto,
            "call_expression" => NodeKind::Call,
            "field_expression" => NodeKind::Attribute,
            "subscript_expression" => NodeKind::Subscript,
            "binary_expression" => NodeKind::BinaryOp,
            "unary_expression" => NodeKind::UnaryOp,
            "conditional_expression" => NodeKind::ConditionalExpression,
            "number_literal"
            | "string_literal"
            | "char_literal"
            | "concatenated_string" => NodeKind::Literal,
            "initializer_list" => NodeKind::List,
            "cast_expression" => NodeKind::TypeCast,
            _ => NodeKind::Other(raw_id),
        }
    }
}