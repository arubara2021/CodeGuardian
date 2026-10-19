"""Sample Python fixture for the multi-language repo.

The fixture shape is: one `target` function and one `caller` function
that calls it. Every file in this directory follows the same shape so
a test that walks the directory sees a consistent story across
languages.
"""


def target(value: int) -> int:
    return value * 2


def caller(value: int) -> int:
    return target(value) + 1