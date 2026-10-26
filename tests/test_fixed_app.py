"""Tests for the corrected implementation in fixtures/fixed_app.py."""

from fixtures.fixed_app import is_valid_input


def test_normal_input_is_accepted():
    assert is_valid_input("alice") is True


def test_empty_input_is_rejected():
    assert is_valid_input("") is False


def test_whitespace_only_input_is_rejected():
    assert is_valid_input("   ") is False
