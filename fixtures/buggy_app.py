"""Fixture module for ReproKit.

This module intentionally contains a small, deterministic bug so that
downstream tooling can reproduce and analyse a known failure.
"""


def is_valid_username(value: str) -> bool:
    """Validate a username submitted through a form.

    Intended behavior: a username is valid only when it contains at least
    one non-whitespace character. Empty strings and whitespace-only strings
    (for example ``"   "`` or ``"\\t\\n"``) must be rejected.

    Known bug: this implementation only rejects empty strings, so
    whitespace-only input is incorrectly accepted as valid.
    """
    if value is None:
        return False

    # BUG: length is checked without stripping whitespace, so "   " passes.
    return len(value) > 0


if __name__ == "__main__":
    # Deterministic demonstration of the bug.
    print("''    ->", is_valid_username(""))        # False (correct)
    print("'   ' ->", is_valid_username("   "))     # True  (incorrect)
    print("'abc' ->", is_valid_username("abc"))     # True  (correct)
