/*
 * Sample C fixture for the multi-language repo.
 *
 * Same shape as `main.py`: one `target` function and one `caller`
 * function that calls it.
 *
 * Note: the kernel does not currently resolve C function names,
 * because tree-sitter-c places the name inside a nested declarator
 * chain rather than in a `name` field. The file still parses and
 * produces a valid CPG with the correct node structure; only
 * symbol extraction and blast resolution are affected. See
 * `rust/crates/codeguardian-kernel/src/langs/c.rs` for the details.
 */

int target(int value)
{
    return value * 2;
}

int caller(int value)
{
    return target(value) + 1;
}
