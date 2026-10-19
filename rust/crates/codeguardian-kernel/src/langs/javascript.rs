//! JavaScript language module.
//!
//! Implements `LanguageModule` for JavaScript by supplying the
//! language identity, the tree-sitter grammar, and the mapping from
//! JavaScript's tree-sitter kind strings onto `NodeKind`. Symbol
//! extraction and callee extraction use the shared defaults —
//! JavaScript's declarations and call syntax match the
//! language-agnostic implementation.
//!
//! The classification is nearly identical to TypeScript's because the
//! two grammars expose the same node names for the syntax they share.
//! The TypeScript-only kinds — `abstract_class_declaration` and
//! `type_annotation` — are omitted because they cannot appear in a
//! JavaScript file.
//!
//! Note on `function_declaration`: the tree-sitter-javascript grammar
//! produces a nested `function` node inside each `function_declaration`.
//! Both carry the same `name` field. Only `function_declaration` is
//! classified as `NodeKind::FunctionDef`. `function` is left to fall
//! through to `NodeKind::Other`. Classifying both would produce two
//! `FunctionDef` nodes per declaration with the same name, and blast's
//! target resolution rejects the ambiguity.

use tree_sitter::Language as TsLanguage;

use crate::ast::NodeKind;
use crate::langs::{classify_shared, Language, LanguageModule};

/// The JavaScript language module.
pub struct JavaScriptModule;

impl LanguageModule for JavaScriptModule {
    fn language(&self) -> Language {
        Language::JavaScript
    }

    fn grammar(&self) -> TsLanguage {
        tree_sitter_javascript::LANGUAGE.into()
    }

    fn classify(&self, kind: &str, raw_id: u16) -> NodeKind {
        if let Some(shared) = classify_shared(kind) {
            return shared;
        }
        match kind {
            "program" => NodeKind::Module,
            "statement_block" | "class_body" => NodeKind::Block,
            "function_declaration" | "generator_function_declaration" => NodeKind::FunctionDef,
            "class_declaration" => NodeKind::ClassDef,
            "method_definition" => NodeKind::MethodDef,
            "arrow_function" => NodeKind::Lambda,
            "decorator" => NodeKind::Decorator,
            "required_parameter" | "optional_parameter" | "rest_pattern" => NodeKind::Parameter,
            "arguments" => NodeKind::Argument,
            "member_expression" => NodeKind::Attribute,
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
            "template_string" => NodeKind::TemplateLiteral,
            _ => NodeKind::Other(raw_id),
        }
    }
}
