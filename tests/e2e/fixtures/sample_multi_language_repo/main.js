/**
 * Sample JavaScript fixture for the multi-language repo.
 *
 * Same shape as `main.py`: one `target` function and one `caller`
 * function that calls it.
 */

function target(value) {
    return value * 2;
}

function caller(value) {
    return target(value) + 1;
}