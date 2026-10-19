//! Sample Rust fixture for the multi-language repo.
//!
//! Same shape as `main.py`: one `target` function and one `caller`
//! function that calls it.

fn target(value: i32) -> i32 {
    value * 2
}

fn caller(value: i32) -> i32 {
    target(value) + 1
}
