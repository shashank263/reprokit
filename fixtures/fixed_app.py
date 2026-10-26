"""Fixed reference implementation for ReproKit.

This module mirrors fixtures/buggy_app.py but corrects the input-validation
bug. The buggy fixture is kept unchanged so the original failure remains
reproducible.
"""


def is_valid_input(value: str) -> bool:
    """Validate a form input string.

    Intended behavior: input is valid only when it contains at least one
    non-whitespace character. Empty strings and whitespace-only strings
    (for example ``"   "`` or ``"\\t\\n"``) are rejected.

    This is the corrected version: whitespace is stripped before the
    emptiness check, so whitespace-only input is rejected.
    """
    if value is None:
        return False

    return len(value.strip()) > 0
