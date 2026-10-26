"""Safe, allowlist-driven pytest runner for ReproKit.

Only the predefined test identifiers in ``ALLOWED_TESTS`` can be executed.
Callers never supply commands, file paths, shell strings, or test names;
identifiers are mapped internally to fixed pytest targets and pytest is
launched with an argument list and ``shell=False``.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Strict allowlist: identifier -> pytest target paths (relative to PROJECT_ROOT).
ALLOWED_TESTS: Mapping[str, Sequence[str]] = {
    "buggy_input_validation": ("tests/test_buggy_app.py",),
    "fixed_input_validation": ("tests/test_fixed_app.py",),
}

DEFAULT_TIMEOUT_SECONDS = 60.0

_COUNT_RE = re.compile(
    r"(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed|warnings?)\b"
)
_DURATION_RE = re.compile(r"\bin\s+\d+(?:\.\d+)?s\b")


@dataclass(frozen=True)
class TestRunResult:
    """Outcome of one allowlisted pytest run."""

    test_id: str
    command: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    duration_seconds: float
    status: str  # "passed" | "failed" | "inconclusive"
    timed_out: bool = False
    error: str | None = None

    @property
    def command_display(self) -> str:
        """Human-readable form of the exact command that was executed."""
        return subprocess.list2cmdline(list(self.command))


def _resolve_targets(test_id: str) -> tuple[str, ...]:
    if not isinstance(test_id, str) or test_id not in ALLOWED_TESTS:
        raise ValueError(
            f"Unknown test identifier {test_id!r}. "
            f"Allowed identifiers: {sorted(ALLOWED_TESTS)}"
        )
    return tuple(ALLOWED_TESTS[test_id])


def _parse_summary(stdout: str) -> dict[str, int] | None:
    """Extract pytest's final counts line, or None when no result is present."""
    summary_line: str | None = None
    for line in stdout.splitlines():
        candidate = line.strip("= ")
        if _DURATION_RE.search(candidate) and _COUNT_RE.search(candidate):
            summary_line = candidate

    if summary_line is None:
        return None

    counts: dict[str, int] = {}
    for raw_number, raw_name in _COUNT_RE.findall(summary_line):
        name = "error" if raw_name.startswith("error") else raw_name
        counts[name] = counts.get(name, 0) + int(raw_number)
    return counts


def _normalize_status(
    *,
    returncode: int | None,
    stdout: str,
    timed_out: bool,
    error: str | None,
) -> str:
    """Map a completed run to passed/failed/inconclusive.

    Anything ambiguous, missing, or abnormal is reported as "inconclusive"
    so that it can never be mistaken for a pass.
    """
    if timed_out or error is not None or returncode is None:
        return "inconclusive"

    counts = _parse_summary(stdout)
    if counts is None:
        return "inconclusive"

    passed = counts.get("passed", 0)
    failed = counts.get("failed", 0)
    errors = counts.get("error", 0)

    if returncode == 0:
        return "passed" if passed > 0 and failed == 0 and errors == 0 else "inconclusive"
    if returncode == 1:
        return "failed" if failed + errors > 0 else "inconclusive"
    return "inconclusive"


def run_tests(
    test_id: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    cwd: str | Path | None = None,
) -> TestRunResult:
    """Run one allowlisted pytest target and return a normalized result.

    Args:
        test_id: One of the keys in ``ALLOWED_TESTS``.
        timeout: Seconds before the run is aborted (must be positive).
        cwd: Optional working directory; defaults to the project root.

    Raises:
        ValueError: If ``test_id`` is not allowlisted or ``timeout`` is invalid.
    """
    targets = _resolve_targets(test_id)

    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError(f"timeout must be a positive number, got {timeout!r}")

    command = (sys.executable, "-m", "pytest", "-v", *targets)
    workdir = Path(cwd) if cwd is not None else PROJECT_ROOT

    started = time.perf_counter()
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(workdir),
            shell=False,
            capture_output=True,
            text=True,
            timeout=float(timeout),
            check=False,
        )
        duration = time.perf_counter() - started
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        status = _normalize_status(
            returncode=completed.returncode,
            stdout=stdout,
            timed_out=False,
            error=None,
        )
        return TestRunResult(
            test_id=test_id,
            command=command,
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            status=status,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.perf_counter() - started
        return TestRunResult(
            test_id=test_id,
            command=command,
            returncode=None,
            stdout=_as_text(exc.stdout),
            stderr=_as_text(exc.stderr),
            duration_seconds=duration,
            status="inconclusive",
            timed_out=True,
            error=f"pytest timed out after {timeout} seconds",
        )
    except OSError as exc:
        duration = time.perf_counter() - started
        return TestRunResult(
            test_id=test_id,
            command=command,
            returncode=None,
            stdout="",
            stderr="",
            duration_seconds=duration,
            status="inconclusive",
            error=f"failed to launch pytest: {exc}",
        )


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)
