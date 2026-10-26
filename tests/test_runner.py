"""Tests for the allowlist-driven runner in reprokit/runner.py."""

import subprocess

import pytest

from reprokit.runner import ALLOWED_TESTS, run_tests


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["pytest"], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.mark.parametrize(
    "bad_identifier",
    [
        "rm -rf /",
        "tests/test_buggy_app.py",
        "buggy",
        "buggy_input_validation; echo pwned",
        "python -m pytest",
        "",
        None,
        123,
    ],
)
def test_allowlist_rejects_unknown_identifiers(bad_identifier):
    with pytest.raises(ValueError):
        run_tests(bad_identifier)


@pytest.mark.parametrize("bad_timeout", [0, -1, "5", True])
def test_invalid_timeout_is_rejected(bad_timeout):
    with pytest.raises(ValueError):
        run_tests("fixed_input_validation", timeout=bad_timeout)


def test_successful_run_is_parsed_as_passed(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _completed(
            0,
            "tests/test_fixed_app.py::test_a PASSED\n"
            "tests/test_fixed_app.py::test_b PASSED\n"
            "2 passed in 0.05s\n",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_tests("fixed_input_validation", timeout=10)

    assert result.status == "passed"
    assert result.returncode == 0
    assert result.timed_out is False
    assert result.error is None
    assert isinstance(captured["args"], list)
    assert captured["args"][-1] == "tests/test_fixed_app.py"
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["timeout"] == 10.0
    assert "tests/test_fixed_app.py" in result.command_display


def test_failing_run_is_parsed_as_failed(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _completed(1, "1 failed, 2 passed in 0.07s\n"),
    )

    result = run_tests("buggy_input_validation")

    assert result.status == "failed"
    assert result.returncode == 1


def test_missing_summary_is_inconclusive(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _completed(0, "collected 0 items\n")
    )

    result = run_tests("fixed_input_validation")

    assert result.status == "inconclusive"


def test_ambiguous_output_is_inconclusive(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _completed(0, "0 passed in 0.01s\n")
    )
    assert run_tests("fixed_input_validation").status == "inconclusive"

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _completed(1, "3 passed in 0.02s\n")
    )
    assert run_tests("fixed_input_validation").status == "inconclusive"

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _completed(2, "1 passed, 1 failed in 0.02s\n")
    )
    assert run_tests("fixed_input_validation").status == "inconclusive"


def test_timeout_is_inconclusive(monkeypatch):
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_tests("fixed_input_validation", timeout=1)

    assert result.status == "inconclusive"
    assert result.timed_out is True
    assert result.returncode is None
    assert result.error is not None


def test_launch_error_is_inconclusive(monkeypatch):
    def fake_run(args, **kwargs):
        raise OSError("pytest executable unavailable")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_tests("fixed_input_validation")

    assert result.status == "inconclusive"
    assert result.returncode is None
    assert result.error is not None


def test_real_fixed_suite_passes():
    result = run_tests("fixed_input_validation", timeout=120)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.status == "passed", result.stdout + result.stderr
    assert result.duration_seconds > 0


def test_real_buggy_suite_reports_failure():
    result = run_tests("buggy_input_validation", timeout=120)

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.status == "failed", result.stdout + result.stderr
    assert "test_whitespace_only_input_is_rejected" in result.stdout


def test_allowlist_contains_only_expected_targets():
    assert set(ALLOWED_TESTS) == {"buggy_input_validation", "fixed_input_validation"}
