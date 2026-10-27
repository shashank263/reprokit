"""Static verification of staged fix proposals.

Hard boundary: this module never executes uploaded code, and it never accepts
or builds arbitrary commands. The only executable path is the existing
allowlisted :func:`reprokit.runner.run_tests` with a fixed identifier.

Statuses are deliberately narrow:
- ``verified_static`` — every selected check completed successfully. This says
  nothing about runtime behavior.
- ``failed`` — at least one check failed (integrity, workspace, syntax, or the
  fixture regression).
- ``inconclusive`` — a check could not be completed (timeout, ambiguous runner
  output, missing staging metadata).

An empty selection is rejected rather than treated as success.
"""

from __future__ import annotations

import ast
import configparser
import json
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

from reprokit import patch
from reprokit.runner import TestRunResult, run_tests

STATUS_VERIFIED = "verified_static"
STATUS_FAILED = "failed"
STATUS_INCONCLUSIVE = "inconclusive"

CHECK_UPLOADS_UNCHANGED = "uploads_unchanged"
CHECK_PROPOSAL_INTEGRITY = "proposal_integrity"
CHECK_WORKSPACE_CONTENTS = "workspace_contents"
CHECK_PYTHON_SYNTAX = "python_syntax"
CHECK_FIXTURE_REGRESSION = "fixture_regression"

DEFAULT_CHECKS: tuple[str, ...] = (
    CHECK_UPLOADS_UNCHANGED,
    CHECK_PROPOSAL_INTEGRITY,
    CHECK_WORKSPACE_CONTENTS,
    CHECK_PYTHON_SYNTAX,
    CHECK_FIXTURE_REGRESSION,
)

CHECK_LABELS = {
    CHECK_UPLOADS_UNCHANGED: "Original uploads unchanged",
    CHECK_PROPOSAL_INTEGRITY: "Proposal and staged files match",
    CHECK_WORKSPACE_CONTENTS: "Workspace contents are exactly expected",
    CHECK_PYTHON_SYNTAX: "Supported formats parse (static only)",
    CHECK_FIXTURE_REGRESSION: "ReproKit fixture regression",
}

CHECK_SCOPES = {
    CHECK_UPLOADS_UNCHANGED: "uploaded snapshots",
    CHECK_PROPOSAL_INTEGRITY: "staged workspace",
    CHECK_WORKSPACE_CONTENTS: "staged workspace",
    CHECK_PYTHON_SYNTAX: "staged files (no execution)",
    CHECK_FIXTURE_REGRESSION: (
        "ReproKit's own fixtures — this does NOT verify the uploaded project"
    ),
}

MAX_OUTPUT_CHARS = 4_000
ALLOWED_TIMEOUTS = (30.0, 60.0, 120.0)
DEFAULT_TIMEOUT_SECONDS = 60.0
_TEMP_PREFIX = "reprokit-fix-"
_MAX_WORKSPACE_ENTRIES = 200

SCOPE_STATEMENT = (
    "Static verification only: integrity, workspace contents, syntax parsing, "
    "and ReproKit's own fixture regression. Uploaded code was NOT executed, and "
    "fixture results do not verify the uploaded project."
)


class VerificationInputError(ValueError):
    """Raised for unknown checks, unsafe workspaces, or missing staging data."""


@dataclass
class CheckResult:
    """Evidence for one approved check."""

    check_id: str
    label: str
    scope: str
    status: str  # "passed" | "failed" | "inconclusive"
    check: str  # exact command, or a precise description of the static check
    exit_status: Optional[int] = None
    duration_seconds: float = 0.0
    output: str = ""
    detail: str = ""


@dataclass
class VerificationReport:
    """Aggregated result for one verification run."""

    overall_status: str
    scope_statement: str
    checks: list[CheckResult]
    verified_at: str
    workspace: str
    proposal_digest: str

    @property
    def passed(self) -> bool:
        return self.overall_status == STATUS_VERIFIED

    def to_markdown(self) -> str:
        lines = [
            "# ReproKit Verification Report",
            "",
            f"- **Overall status:** `{self.overall_status}`",
            f"- **Scope:** {self.scope_statement}",
            f"- **Verified at (UTC):** {self.verified_at}",
            f"- **Workspace:** `{self.workspace}`",
            f"- **Proposal digest:** `{self.proposal_digest}`",
            "",
            "## Checks",
            "",
        ]
        for check in self.checks:
            lines += [
                f"### `{check.status}` — {check.label}",
                "",
                f"- Check ID: `{check.check_id}`",
                f"- Scope: {check.scope}",
                f"- Check: `{check.check}`",
                f"- Exit status: {check.exit_status if check.exit_status is not None else 'n/a'}",
                f"- Duration: {check.duration_seconds:.3f} s",
            ]
            if check.detail:
                lines.append(f"- Detail: {check.detail}")
            lines += ["", "```text", check.output or "(no output)", "```", ""]

        lines += [
            "---",
            "Static checks only. Uploaded code was not executed. A "
            "`verified_static` result does not mean the fix works at runtime.",
            "",
        ]
        return "\n".join(lines)


def _bounded(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n… [output truncated]"


def _approved_root() -> Path:
    return Path(tempfile.gettempdir()).resolve()


def _resolve_workspace(workspace: str) -> Path:
    if not workspace:
        raise VerificationInputError("No staged workspace; stage the proposal first.")
    base = Path(workspace).resolve()
    root = _approved_root()
    if base == root or root not in base.parents:
        raise VerificationInputError(
            "Workspace is outside the approved temporary root."
        )
    if not base.name.startswith(_TEMP_PREFIX):
        raise VerificationInputError(
            f"Workspace name must start with {_TEMP_PREFIX!r}."
        )
    if not base.is_dir():
        raise VerificationInputError("Workspace directory does not exist.")
    return base


def _validate_check_ids(check_ids: Optional[Sequence[str]]) -> tuple[str, ...]:
    if check_ids is None:
        return DEFAULT_CHECKS
    selected: list[str] = []
    for check_id in check_ids:
        if check_id not in CHECK_LABELS:
            raise VerificationInputError(f"Unknown verification check {check_id!r}.")
        if check_id not in selected:
            selected.append(check_id)
    if not selected:
        raise VerificationInputError("Select at least one approved check.")
    return tuple(selected)


def _validate_timeout(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise VerificationInputError(f"timeout must be a positive number, got {timeout!r}")
    if float(timeout) not in ALLOWED_TIMEOUTS:
        raise VerificationInputError(
            f"timeout must be one of {list(ALLOWED_TIMEOUTS)} seconds."
        )
    return float(timeout)


def _snapshot_hashes(session) -> dict[str, str]:
    return {item.name: patch.sha256_text(item.text) for item in session.files}


def _make_result(
    check_id: str,
    status: str,
    *,
    check: str,
    started: float,
    exit_status: Optional[int] = None,
    output: str = "",
    detail: str = "",
) -> CheckResult:
    return CheckResult(
        check_id=check_id,
        label=CHECK_LABELS[check_id],
        scope=CHECK_SCOPES[check_id],
        status=status,
        check=check,
        exit_status=exit_status,
        duration_seconds=time.perf_counter() - started,
        output=_bounded(output),
        detail=detail,
    )


def _check_uploads_unchanged(session, proposal) -> CheckResult:
    started = time.perf_counter()
    check = "SHA-256 comparison of uploaded snapshots"
    if not proposal.snapshot_hashes:
        return _make_result(
            CHECK_UPLOADS_UNCHANGED, STATUS_INCONCLUSIVE, check=check, started=started,
            detail="Proposal has no staging-time hashes; re-stage the proposal.",
        )
    actual = _snapshot_hashes(session)
    if set(actual) != set(proposal.snapshot_hashes):
        return _make_result(
            CHECK_UPLOADS_UNCHANGED, STATUS_FAILED, check=check, started=started,
            detail="Uploaded file set changed since staging.",
        )
    mismatched = sorted(
        name for name, digest in actual.items()
        if proposal.snapshot_hashes.get(name) != digest
    )
    if mismatched:
        return _make_result(
            CHECK_UPLOADS_UNCHANGED, STATUS_FAILED, check=check, started=started,
            output="\n".join(f"changed: {name}" for name in mismatched),
            detail="Original upload bytes changed since staging.",
        )
    return _make_result(
        CHECK_UPLOADS_UNCHANGED, "passed",
        check=check, started=started, output=f"{len(actual)} file(s) match staging hashes.",
    )


def _check_proposal_integrity(session, proposal, base: Path) -> CheckResult:
    started = time.perf_counter()
    check = "Re-validate unified diff against originals; compare staged bytes"
    if not proposal.snapshot_hashes or not proposal.staged_hashes:
        return _make_result(
            CHECK_PROPOSAL_INTEGRITY, STATUS_INCONCLUSIVE, check=check, started=started,
            detail="Proposal lacks staging-time hashes; re-stage the proposal.",
        )

    sources = {item.name: item.text for item in session.files}
    revalidation = patch.validate_diff(proposal.diff, sources)
    if not revalidation.ok:
        return _make_result(
            CHECK_PROPOSAL_INTEGRITY, STATUS_FAILED, check=check, started=started,
            detail=f"Proposal diff no longer validates: {revalidation.error}",
        )

    problems: list[str] = []
    expected_paths = {file_patch.path for file_patch in revalidation.files}
    if expected_paths != set(proposal.staged_hashes):
        problems.append("Staged hash manifest does not match the validated diff.")

    for file_patch in revalidation.files:
        target = base / file_patch.path
        if not target.is_file():
            problems.append(f"missing staged file: {file_patch.path}")
            continue
        if target.is_symlink():
            problems.append(f"staged file is a symlink: {file_patch.path}")
            continue
        actual = patch.sha256_text(target.read_text(encoding="utf-8"))
        recorded = proposal.staged_hashes.get(file_patch.path)
        if recorded is None:
            problems.append(f"no recorded hash for {file_patch.path}")
        elif actual != recorded:
            problems.append(f"staged bytes changed: {file_patch.path}")
        if target.read_text(encoding="utf-8") != file_patch.after:
            problems.append(f"staged content differs from proposal: {file_patch.path}")

    if problems:
        return _make_result(
            CHECK_PROPOSAL_INTEGRITY, STATUS_FAILED, check=check, started=started,
            output="\n".join(problems), detail="Staged files do not match the proposal.",
        )
    return _make_result(
        CHECK_PROPOSAL_INTEGRITY, "passed", check=check, started=started,
        output="\n".join(
            f"{file_patch.path}: {proposal.staged_hashes.get(file_patch.path, '')[:16]}…"
            for file_patch in revalidation.files
        ),
    )


def _walk_workspace(base: Path) -> tuple[list[str], str]:
    found: list[str] = []
    entries = 0
    for entry in base.rglob("*"):
        entries += 1
        if entries > _MAX_WORKSPACE_ENTRIES:
            return found, "Workspace contains too many entries."
        relative = entry.relative_to(base).as_posix()
        if entry.is_symlink():
            return found, f"symlink not allowed: {relative}"
        if entry.is_dir():
            continue
        if not entry.is_file():
            return found, f"unsupported workspace entry: {relative}"
        resolved = entry.resolve()
        if base not in resolved.parents:
            return found, f"path escapes workspace: {relative}"
        found.append(relative)
    return found, ""


def _check_workspace_contents(session, base: Path) -> CheckResult:
    started = time.perf_counter()
    check = "Enumerate staged files; reject extras, missing entries, symlinks"
    found, problem = _walk_workspace(base)
    if problem:
        return _make_result(
            CHECK_WORKSPACE_CONTENTS, STATUS_FAILED, check=check, started=started,
            detail=problem,
        )
    expected = {item.name for item in session.files}
    actual = set(found)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if extra:
            details.append("unexpected: " + ", ".join(extra))
        return _make_result(
            CHECK_WORKSPACE_CONTENTS, STATUS_FAILED, check=check, started=started,
            output="\n".join(details), detail="Workspace file set is not exact.",
        )
    return _make_result(
        CHECK_WORKSPACE_CONTENTS, "passed", check=check, started=started,
        output="\n".join(sorted(actual)),
    )


def _check_python_syntax(base: Path) -> CheckResult:
    started = time.perf_counter()
    check = "Parse Python, JSON, TOML, INI, and XML where validators are available (no execution)"
    problems: list[str] = []
    checked: list[str] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        suffix = path.suffix.lower()
        if suffix not in (".py", ".pyi", ".json", ".toml", ".ini", ".xml"):
            continue
        if suffix == ".toml" and tomllib is None:
            continue
        checked.append(path.name)
        text = path.read_text(encoding="utf-8")
        try:
            if suffix in (".py", ".pyi"):
                ast.parse(text, filename=path.name)
            elif suffix == ".json":
                json.loads(text)
            elif suffix == ".toml":
                tomllib.loads(text)
            elif suffix == ".ini":
                configparser.ConfigParser().read_string(text)
            else:
                ET.fromstring(text)
        except (
            SyntaxError,
            json.JSONDecodeError,
            ValueError,
            TypeError,
            ET.ParseError,
            configparser.Error,
        ) as exc:
            problems.append(f"{path.name}: {exc}")
    if problems:
        return _make_result(
            CHECK_PYTHON_SYNTAX, STATUS_FAILED, check=check, started=started,
            output="\n".join(problems), detail="Static parsing failed.",
        )
    if not checked:
        return _make_result(
            CHECK_PYTHON_SYNTAX, STATUS_INCONCLUSIVE, check=check, started=started,
            detail=(
                "No staged files had an available static parser. Supported checks "
                "here are Python, JSON, TOML (Python 3.11+), INI, and XML."
            ),
        )
    return _make_result(
        CHECK_PYTHON_SYNTAX, "passed", check=check, started=started,
        output="\n".join(checked),
    )


def _check_fixture_regression(timeout: float) -> CheckResult:
    started = time.perf_counter()
    result: TestRunResult = run_tests("fixed_input_validation", timeout=timeout)
    status = {
        "passed": "passed",
        "failed": STATUS_FAILED,
        "inconclusive": STATUS_INCONCLUSIVE,
    }.get(result.status, STATUS_INCONCLUSIVE)
    output = result.stdout + ("\n" + result.stderr if result.stderr else "")
    if result.error:
        output = (output + "\n" + result.error).strip()
    detail = "ReproKit fixture test only; it does not exercise the uploaded project."
    if status != "passed":
        detail += " The fixture check did not pass; see output."
    return _make_result(
        CHECK_FIXTURE_REGRESSION,
        status,
        check=result.command_display,
        started=started,
        exit_status=result.returncode,
        output=output,
        detail=detail,
    )


def _aggregate(checks: Sequence[CheckResult]) -> str:
    if any(check.status == STATUS_FAILED for check in checks):
        return STATUS_FAILED
    if any(check.status == STATUS_INCONCLUSIVE for check in checks):
        return STATUS_INCONCLUSIVE
    return STATUS_VERIFIED


def _digest(proposal) -> str:
    payload = proposal.diff + json.dumps(proposal.staged_hashes, sort_keys=True)
    return patch.sha256_text(payload)


def verify_proposal(
    session,
    proposal,
    *,
    check_ids: Optional[Sequence[str]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> VerificationReport:
    """Run approved checks against a staged proposal.

    Raises VerificationInputError for unknown checks, unsafe workspaces, or an
    empty selection. Never executes uploaded code or arbitrary commands.
    """
    selected = _validate_check_ids(check_ids)
    timeout = _validate_timeout(timeout)
    base = _resolve_workspace(proposal.workspace or "")

    if not session.files:
        raise VerificationInputError("No uploaded files are available to verify.")

    checks: list[CheckResult] = []
    for check_id in selected:
        if check_id == CHECK_UPLOADS_UNCHANGED:
            checks.append(_check_uploads_unchanged(session, proposal))
        elif check_id == CHECK_PROPOSAL_INTEGRITY:
            checks.append(_check_proposal_integrity(session, proposal, base))
        elif check_id == CHECK_WORKSPACE_CONTENTS:
            checks.append(_check_workspace_contents(session, base))
        elif check_id == CHECK_PYTHON_SYNTAX:
            checks.append(_check_python_syntax(base))
        elif check_id == CHECK_FIXTURE_REGRESSION:
            checks.append(_check_fixture_regression(timeout))
        else:  # pragma: no cover - guarded by _validate_check_ids
            raise VerificationInputError(f"Unknown verification check {check_id!r}.")

    return VerificationReport(
        overall_status=_aggregate(checks),
        scope_statement=SCOPE_STATEMENT,
        checks=checks,
        verified_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        workspace=str(base),
        proposal_digest=_digest(proposal),
    )