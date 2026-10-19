// Sample C++ fixture for the multi-language repo.
//
// Same shape as `main.py`: one `target` function and one `caller`
// function that calls it.
//
// Note: like C, C++ free-function names are not currently resolved by
// the kernel, because tree-sitter-cpp nests the function name inside
// a declarator chain rather than in a `name` field, and qualified
// calls (`Foo::bar()`) are not handled by the shared callee
// extractor. See `rust/crates/codeguardian-kernel/src/langs/cpp.rs`
// for the details.

int target(int value)
{
    return value * 2;
}

int caller(int value)
{
    return target(value) + 1;
}
