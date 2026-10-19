//! C++ language module.
//!
//! Implements `LanguageModule` for C++ by supplying the language
//! identity, the tree-sitter grammar, and the mapping from C++'s
//! tree-sitter kind strings onto `NodeKind`. Symbol extraction and
//! callee extraction use the shared defaults.
//!
//! # Known gap
//!
//! The same limitation described in `langs/c.rs` applies to C++. The
//! tree-sitter-cpp `function_definition` node carries the function
//! name inside a nested declarator chain rather than in a `name` field,
//! so C++ free functions do not produce symbols and do not resolve as
//! callees. Additionally, C++ member-function calls use a
//! `qualified_identifier` node (`Foo::bar()`), which the shared callee
//! extractor does not handle because it only recognises `Identifier`,
//! `Attribute`, and `Subscript`. C++ therefore ships at `best-effort`
//! reliability tier until `parser.rs::extract_name_id` walks
//! declarators and `call_graph::default_extract_callee_name` handles
//! qualified identifiers.
//!
//! Class, struct, enum, namespace, and template declarations all parse
//! correctly and contribute their nodes to the graph. The gap is
//! strictly about resolving call edges.

use tree_sitter::Language as TsLanguage;

use crate::ast::NodeKind;
use crate::langs::{classify_shared, Language, LanguageModule};

/// The C++ language module.
pub struct CppModule;

impl LanguageModule for CppModule {
    fn language(&self) -> Language {
        Language::Cpp
    }

    fn grammar(&self) -> TsLanguage {
        tree_sitter_cpp::LANGUAGE.into()
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
            | "preproc_elif" => NodeKind::Preprocessor,
            "namespace_definition" => NodeKind::Namespace,
            "using_declaration" => NodeKind::Import,
            "function_definition" => NodeKind::FunctionDef,
            "declaration" => NodeKind::Assignment,
            "class_specifier" => NodeKind::ClassDef,
            "struct_specifier" => NodeKind::Struct,
            "union_specifier" => NodeKind::Union,
            "enum_specifier" => NodeKind::Enum,
            "type_definition" | "alias_declaration" => NodeKind::TypeAlias,
            "template_declaration" => NodeKind::TemplateDeclaration,
            "template_parameter_list" | "template_parameter" => NodeKind::TypeParameter,
            "template_type" | "template_function" => NodeKind::Identifier,
            "parameter_declaration" | "variadic_parameter_declaration" => NodeKind::Parameter,
            "argument_list" => NodeKind::Argument,
            "compound_statement" | "field_declaration_list" => NodeKind::Block,
            "if_statement" => NodeKind::If,
            "else_clause" => NodeKind::Else,
            "for_statement" | "for_range_loop" => NodeKind::For,
            "while_statement" | "do_statement" => NodeKind::While,
            "switch_statement" => NodeKind::Match,
            "case_statement" => NodeKind::Case,
            "try_statement" => NodeKind::Try,
            "catch_clause" => NodeKind::Except,
            "return_statement" => NodeKind::Return,
            "throw_statement" => NodeKind::Raise,
            "break_statement" => NodeKind::Break,
            "continue_statement" => NodeKind::Continue,
            "goto_statement" => NodeKind::Goto,
            "call_expression" => NodeKind::Call,
            "field_expression" | "qualified_identifier" => NodeKind::Attribute,
            "subscript_expression" => NodeKind::Subscript,
            "binary_expression" => NodeKind::BinaryOp,
            "unary_expression" | "delete_expression" => NodeKind::UnaryOp,
            "conditional_expression" => NodeKind::ConditionalExpression,
            "new_expression" => NodeKind::NewExpression,
            "number_literal"
            | "string_literal"
            | "char_literal"
            | "concatenated_string"
            | "true"
            | "false"
            | "null"
            | "nullptr" => NodeKind::Literal,
            "initializer_list" => NodeKind::List,
            "cast_expression"
            | "static_cast_expression"
            | "dynamic_cast_expression"
            | "const_cast_expression"
            | "reinterpret_cast_expression" => NodeKind::TypeCast,
            "lambda_expression" => NodeKind::Lambda,
            _ => NodeKind::Other(raw_id),
        }
    }
}
