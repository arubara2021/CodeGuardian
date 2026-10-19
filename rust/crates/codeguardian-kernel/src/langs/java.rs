//! Java language module.
//!
//! Implements `LanguageModule` for Java by supplying the language
//! identity, the tree-sitter grammar, and the mapping from Java's
//! tree-sitter kind strings onto `NodeKind`. Symbol extraction and
//! callee extraction use the shared defaults — Java's method
//! declarations and call syntax match the language-agnostic
//! implementation once `class_declaration` and `method_declaration`
//! are classified correctly.

use tree_sitter::Language as TsLanguage;

use crate::ast::NodeKind;
use crate::langs::{classify_shared, Language, LanguageModule};

/// The Java language module.
pub struct JavaModule;

impl LanguageModule for JavaModule {
    fn language(&self) -> Language {
        Language::Java
    }

    fn grammar(&self) -> TsLanguage {
        tree_sitter_java::LANGUAGE.into()
    }

    fn classify(&self, kind: &str, raw_id: u16) -> NodeKind {
        if let Some(shared) = classify_shared(kind) {
            return shared;
        }
        match kind {
            "program" => NodeKind::Module,
            "package_declaration" => NodeKind::Package,
            "import_declaration" => NodeKind::Import,
            "class_declaration" => NodeKind::ClassDef,
            "interface_declaration" => NodeKind::Interface,
            "enum_declaration" => NodeKind::Enum,
            "method_declaration" | "constructor_declaration" => NodeKind::MethodDef,
            "field_declaration" => NodeKind::Assignment,
            "formal_parameter" | "spread_parameter" => NodeKind::Parameter,
            "argument_list" => NodeKind::Argument,
            "block"
            | "constructor_body"
            | "class_body"
            | "interface_body"
            | "enum_body"
            | "static_initializer" => NodeKind::Block,
            "if_statement" => NodeKind::If,
            "for_statement" | "enhanced_for_statement" => NodeKind::For,
            "while_statement" | "do_statement" => NodeKind::While,
            "switch_expression" => NodeKind::Match,
            "try_statement" => NodeKind::Try,
            "catch_clause" => NodeKind::Except,
            "finally_clause" => NodeKind::Finally,
            "synchronized_statement" => NodeKind::Synchronized,
            "return_statement" => NodeKind::Return,
            "throw_statement" => NodeKind::Raise,
            "break_statement" => NodeKind::Break,
            "continue_statement" => NodeKind::Continue,
            "yield_statement" => NodeKind::Yield,
            "assert_statement" => NodeKind::Assert,
            "method_invocation" => NodeKind::Call,
            "object_creation_expression" => NodeKind::NewExpression,
            "binary_expression" => NodeKind::BinaryOp,
            "unary_expression" | "update_expression" => NodeKind::UnaryOp,
            "ternary_expression" => NodeKind::ConditionalExpression,
            "string_literal"
            | "character_literal"
            | "decimal_integer_literal"
            | "hex_integer_literal"
            | "octal_integer_literal"
            | "binary_integer_literal"
            | "decimal_floating_point_literal"
            | "hex_floating_point_literal"
            | "true"
            | "false"
            | "null_literal" => NodeKind::Literal,
            "array_creation_expression" | "array_initializer" => NodeKind::List,
            "array_access" => NodeKind::Subscript,
            "cast_expression" => NodeKind::TypeCast,
            "annotation" => NodeKind::Annotation,
            "modifiers" => NodeKind::Modifier,
            "lambda_expression" => NodeKind::Lambda,
            "type_parameters" | "type_parameter" => NodeKind::TypeParameter,
            _ => NodeKind::Other(raw_id),
        }
    }
}
