//! TypeScript language module.
//!
//! Implements `LanguageModule` for TypeScript by supplying the
//! language identity, the tree-sitter grammar, and the mapping from
//! TypeScript's tree-sitter kind strings onto `NodeKind`. Symbol
//! extraction and callee extraction use the shared defaults —
//! TypeScript's declarations and call syntax match the
//! language-agnostic implementation.
//!
//! Note on `function_declaration`: the tree-sitter-typescript grammar
//! produces a nested `function` node inside each `function_declaration`.
//! Both carry the same `name` field. Only `function_declaration` is
//! classified as `NodeKind::FunctionDef`. `function` is left to fall
//! through to `NodeKind::Other`. Classifying both would produce two
//! `FunctionDef` nodes per declaration with the same name, and blast's
//! target resolution rejects the ambiguity.

use tree_sitter::Language as TsLanguage;

use crate::ast::NodeKind;
use crate::langs::{classify_shared, Language, LanguageModule};

/// The TypeScript language module.
pub struct TypeScriptModule;

impl LanguageModule for TypeScriptModule {
    fn language(&self) -> Language {
        Language::TypeScript
    }

    fn grammar(&self) -> TsLanguage {
        tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into()
    }

    fn classify(&self, kind: &str, raw_id: u16) -> NodeKind {
        if let Some(shared) = classify_shared(kind) {
            return shared;
        }
        match kind {
            "program" => NodeKind::Module,
            "statement_block" | "class_body" => NodeKind::Block,
            "function_declaration" | "generator_function_declaration" => NodeKind::FunctionDef,
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
            _ => NodeKind::Other(raw_id),
        }
    }
}
