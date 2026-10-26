"""Tests for the intentionally buggy fixture in fixtures/buggy_app.py."""

from fixtures.buggy_app import is_valid_username


def test_non_empty_input_is_accepted():
    assert is_valid_username("alice") is True


def test_empty_input_is_rejected():
    assert is_valid_username("") is False


def test_whitespace_only_input_is_rejected():
    # Fails by design: the fixture only checks length, not stripped content.
    assert is_valid_username("   ") is False
