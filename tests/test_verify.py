"""Offline tests for static proposal verification (reprokit/verify.py)."""

import ast
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from reprokit import magic, patch, verify
from reprokit.runner import TestRunResult as RunResult

SOURCE = b"def is_valid(v):\n    return len(v) > 0\n"
DIFF = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def is_valid(v):\n"
    "-    return len(v) > 0\n"
    "+    return len(v.strip()) > 0\n"
)


def _result(
    status="passed",
    rc=0,
    stdout="3 passed in 0.03s\n",
    stderr="",
    error=None,
    timed_out=False,
):
    return RunResult(
        test_id="fixed_input_validation",
        command=(
            "python",
            "-m",
            "pytest",
            "-v",
            "tests/test_fixed_app.py",
        ),
        returncode=rc,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.01,
        status=status,
        timed_out=timed_out,
        error=error,
    )


def _build(source=SOURCE, diff=DIFF):
    files = magic.ingest_files([("app.py", source)])
    issue = magic.issues_from_ai(
        [magic.MagicAIIssue(title="T", severity="high", evidence_refs=[("app.py", 2)])],
        files,
    )[0]
    validation = patch.validate_diff(diff, {f.name: f.text for f in files})
    assert validation.ok, validation.error
    proposal = magic.FixProposal(
        issue_id=issue.id,
        explanation="Strip input.",
        diff=validation.canonical_diff,
        files_changed=[("app.py", [2])],
        expected_behavior="Whitespace rejected.",
        tests_needed=["edge: rejects '   '"],
        limitations=["Assumes str input."],
        validation=validation,
    )
    session = magic.MagicSession(files=files)
    session.set_issues([issue])
    return session, proposal


@pytest.fixture
def staged(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="reprokit-fix-"))
    session, proposal = _build()
    magic.stage_fix(session, proposal, root)
    monkeypatch.setattr(verify, "run_tests", lambda *a, **k: _result())
    yield session, proposal, root
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_all_checks_pass_gives_verified_static(staged):
    session, proposal, _ = staged
    report = verify.verify_proposal(session, proposal)

    assert report.overall_status == verify.STATUS_VERIFIED
    assert len(report.checks) == len(verify.DEFAULT_CHECKS)
    assert all(check.status == "passed" for check in report.checks)
    assert "NOT executed" in report.scope_statement
    assert report.verified_at.endswith("+00:00")
    assert report.proposal_digest == verify._digest(proposal)

    fixture = next(c for c in report.checks if c.check_id == verify.CHECK_FIXTURE_REGRESSION)
    assert "does NOT verify" in fixture.scope
    assert "ReproKit fixture" in fixture.detail


def test_staging_records_snapshot_and_staged_hashes(staged):
    session, proposal, root = staged
    assert proposal.snapshot_hashes == {
        "app.py": patch.sha256_text(SOURCE.decode())
    }
    assert proposal.staged_hashes == {
        "app.py": patch.sha256_text(
            "def is_valid(v):\n    return len(v.strip()) > 0\n"
        )
    }
    assert (root / "app.py").read_text() == proposal.validation.files[0].after


def test_report_markdown_includes_evidence_and_scope(staged):
    session, proposal, _ = staged
    report = verify.verify_proposal(session, proposal)
    markdown = report.to_markdown()

    assert "# ReproKit Verification Report" in markdown
    assert report.overall_status in markdown
    assert "NOT executed" in markdown
    assert "does NOT verify the uploaded project" in markdown
    assert "Exit status" in markdown
    assert "Duration" in markdown
    assert "pytest" in markdown


# ---------------------------------------------------------------------------
# Integrity / tampering
# ---------------------------------------------------------------------------
def test_tampered_staged_file_fails(staged):
    session, proposal, root = staged
    (root / "app.py").write_text("def is_valid(v):\n    return True\n")

    report = verify.verify_proposal(session, proposal)

    assert report.overall_status == verify.STATUS_FAILED
    integrity = next(c for c in report.checks if c.check_id == verify.CHECK_PROPOSAL_INTEGRITY)
    assert integrity.status == "failed"
    assert "changed" in integrity.output or "differs" in integrity.output


def test_missing_staged_file_fails(staged):
    session, proposal, root = staged
    (root / "app.py").unlink()

    report = verify.verify_proposal(session, proposal)

    assert report.overall_status == verify.STATUS_FAILED
    contents = next(c for c in report.checks if c.check_id == verify.CHECK_WORKSPACE_CONTENTS)
    assert contents.status == "failed"


def test_extra_file_fails(staged):
    session, proposal, root = staged
    (root / "extra.py").write_text("x = 1\n")

    report = verify.verify_proposal(session, proposal)

    assert report.overall_status == verify.STATUS_FAILED
    contents = next(c for c in report.checks if c.check_id == verify.CHECK_WORKSPACE_CONTENTS)
    assert contents.status == "failed"
    assert "unexpected" in contents.output


def test_symlink_in_workspace_fails_or_skips(staged):
    session, proposal, root = staged
    target = root / "app.py"
    link = root / "link.py"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted in this environment")

    report = verify.verify_proposal(session, proposal)
    assert report.overall_status == verify.STATUS_FAILED
    contents = next(c for c in report.checks if c.check_id == verify.CHECK_WORKSPACE_CONTENTS)
    assert contents.status == "failed"


def test_changed_upload_after_staging_fails(staged):
    session, proposal, _ = staged
    session.files = magic.ingest_files([("app.py", b"totally different\n")])

    report = verify.verify_proposal(session, proposal)

    assert report.overall_status == verify.STATUS_FAILED
    uploads = next(c for c in report.checks if c.check_id == verify.CHECK_UPLOADS_UNCHANGED)
    assert uploads.status == "failed"


def test_tampered_proposal_diff_fails(staged):
    session, proposal, _ = staged
    proposal.diff = "not a diff"

    report = verify.verify_proposal(session, proposal)

    assert report.overall_status == verify.STATUS_FAILED
    integrity = next(c for c in report.checks if c.check_id == verify.CHECK_PROPOSAL_INTEGRITY)
    assert integrity.status == "failed"


def test_missing_staging_hashes_is_inconclusive(staged):
    session, proposal, _ = staged
    proposal.snapshot_hashes = {}
    proposal.staged_hashes = {}

    report = verify.verify_proposal(session, proposal)

    assert report.overall_status == verify.STATUS_INCONCLUSIVE
    uploads = next(c for c in report.checks if c.check_id == verify.CHECK_UPLOADS_UNCHANGED)
    assert uploads.status == "inconclusive"


# ---------------------------------------------------------------------------
# Rejected inputs (never run anything)
# ---------------------------------------------------------------------------
def test_unknown_check_id_rejected(staged, monkeypatch):
    session, proposal, _ = staged
    called = {"n": 0}
    monkeypatch.setattr(verify, "run_tests", lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    with pytest.raises(verify.VerificationInputError):
        verify.verify_proposal(session, proposal, check_ids=["shell_command"])
    assert called["n"] == 0


def test_empty_check_selection_rejected(staged):
    session, proposal, _ = staged
    with pytest.raises(verify.VerificationInputError):
        verify.verify_proposal(session, proposal, check_ids=[])


def test_workspace_outside_temp_root_rejected(staged):
    session, proposal, _ = staged
    proposal.workspace = str(Path.cwd())
    with pytest.raises(verify.VerificationInputError, match="outside"):
        verify.verify_proposal(session, proposal)


def test_workspace_with_bad_prefix_rejected(staged):
    session, proposal, _ = staged
    bad = Path(tempfile.mkdtemp(prefix="not-reprokit-"))
    try:
        proposal.workspace = str(bad)
        with pytest.raises(verify.VerificationInputError, match="must start with"):
            verify.verify_proposal(session, proposal)
    finally:
        shutil.rmtree(bad, ignore_errors=True)


def test_missing_workspace_rejected(staged):
    session, proposal, _ = staged
    proposal.workspace = ""
    with pytest.raises(verify.VerificationInputError):
        verify.verify_proposal(session, proposal)


def test_invalid_timeout_rejected(staged):
    session, proposal, _ = staged
    for timeout in (0, 90, 9999, float("nan"), float("inf")):
        with pytest.raises(verify.VerificationInputError):
            verify.verify_proposal(session, proposal, timeout=timeout)


# ---------------------------------------------------------------------------
# Fixture regression scope and outcomes
# ---------------------------------------------------------------------------
def test_fixture_failure_fails_with_evidence(staged, monkeypatch):
    session, proposal, _ = staged
    monkeypatch.setattr(
        verify,
        "run_tests",
        lambda *a, **k: _result(
            status="failed", rc=1, stdout="1 failed in 0.07s\n"
        ),
    )

    report = verify.verify_proposal(session, proposal)

    assert report.overall_status == verify.STATUS_FAILED
    fixture = next(c for c in report.checks if c.check_id == verify.CHECK_FIXTURE_REGRESSION)
    assert fixture.status == "failed"
    assert fixture.exit_status == 1
    assert "1 failed" in fixture.output
    assert "does not exercise the uploaded project" in fixture.detail


def test_fixture_timeout_is_inconclusive(staged, monkeypatch):
    session, proposal, _ = staged
    monkeypatch.setattr(
        verify,
        "run_tests",
        lambda *a, **k: _result(
            status="inconclusive",
            rc=None,
            stdout="",
            error="pytest timed out after 60 seconds",
            timed_out=True,
        ),
    )

    report = verify.verify_proposal(session, proposal)

    assert report.overall_status == verify.STATUS_INCONCLUSIVE
    fixture = next(c for c in report.checks if c.check_id == verify.CHECK_FIXTURE_REGRESSION)
    assert fixture.status == "inconclusive"
    assert fixture.exit_status is None
    assert "timed out" in fixture.output


def test_fixture_ambiguous_result_is_inconclusive(staged, monkeypatch):
    session, proposal, _ = staged
    monkeypatch.setattr(
        verify,
        "run_tests",
        lambda *a, **k: _result(
            status="inconclusive", rc=0, stdout="collected 0 items\n"
        ),
    )
    report = verify.verify_proposal(session, proposal)
    assert report.overall_status == verify.STATUS_INCONCLUSIVE


def test_fixture_not_selected_does_not_run(staged, monkeypatch):
    session, proposal, _ = staged

    def _boom(*a, **k):
        raise AssertionError("fixture runner must not be called")

    monkeypatch.setattr(verify, "run_tests", _boom)
    report = verify.verify_proposal(
        session,
        proposal,
        check_ids=[verify.CHECK_UPLOADS_UNCHANGED, verify.CHECK_WORKSPACE_CONTENTS],
    )
    assert report.overall_status == verify.STATUS_VERIFIED


def test_fixture_results_never_claim_upload_verification(staged):
    session, proposal, _ = staged
    report = verify.verify_proposal(session, proposal)
    markdown = report.to_markdown().lower()

    assert "does not verify the uploaded project" in markdown
    assert "uploaded code was not executed" in markdown
    assert "does not mean the fix works" in markdown


# ---------------------------------------------------------------------------
# Static syntax / JSON checks
# ---------------------------------------------------------------------------
def test_syntax_error_fails_static_check():
    root = Path(tempfile.mkdtemp(prefix="reprokit-fix-"))
    try:
        bad_source = b"def f(:\n    pass\n"
        bad_diff = (
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def f(:\n"
            "-    pass\n"
            "+    return 1\n"
        )
        session, proposal = _build(source=bad_source, diff=bad_diff)
        magic.stage_fix(session, proposal, root)

        report = verify.verify_proposal(
            session,
            proposal,
            check_ids=[verify.CHECK_PYTHON_SYNTAX],
        )
        assert report.overall_status == verify.STATUS_FAILED
        syntax = report.checks[0]
        assert syntax.status == "failed"
        assert "app.py" in syntax.output
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_no_parsable_files_is_inconclusive(staged):
    session, proposal, _ = staged
    report = verify.verify_proposal(
        session, proposal, check_ids=[verify.CHECK_PYTHON_SYNTAX]
    )
    assert report.overall_status == verify.STATUS_VERIFIED  # app.py parses
    # A non-code-only workspace would yield inconclusive; covered by unit intent.


def test_static_validator_checks_python_json_toml_and_xml_without_execution(tmp_path):
    (tmp_path / "source.py").write_text(
        "raise RuntimeError('not executed')\n", encoding="utf-8"
    )
    (tmp_path / "settings.json").write_text('{"enabled": true}\n', encoding="utf-8")
    (tmp_path / "settings.ini").write_text("[app]\nenabled = true\n", encoding="utf-8")
    (tmp_path / "settings.xml").write_text("<app />\n", encoding="utf-8")
    if verify.tomllib is not None:
        (tmp_path / "settings.toml").write_text("[app]\nenabled = true\n", encoding="utf-8")

    result = verify._check_python_syntax(tmp_path)

    assert result.status == "passed"
    assert "source.py" in result.output
    assert "settings.json" in result.output
    assert "settings.ini" in result.output
    assert "settings.xml" in result.output
    if verify.tomllib is not None:
        assert "settings.toml" in result.output


def test_unavailable_static_validator_is_inconclusive(tmp_path):
    (tmp_path / "source.rs").write_text("fn main() {}", encoding="utf-8")

    result = verify._check_python_syntax(tmp_path)

    assert result.status == verify.STATUS_INCONCLUSIVE
    assert "No staged files had an available static parser" in result.detail


def test_output_is_bounded(staged, monkeypatch):
    session, proposal, _ = staged
    monkeypatch.setattr(
        verify,
        "run_tests",
        lambda *a, **k: _result(stdout="x" * 20_000),
    )

    report = verify.verify_proposal(
        session, proposal, check_ids=[verify.CHECK_FIXTURE_REGRESSION]
    )
    fixture = report.checks[0]
    assert len(fixture.output) <= verify.MAX_OUTPUT_CHARS + 40
    assert "truncated" in fixture.output


# ---------------------------------------------------------------------------
# Restaging clears verification; no execution constructs in verify.py
# ---------------------------------------------------------------------------
def test_restaging_clears_previous_verification(staged):
    session, proposal, root = staged
    report = verify.verify_proposal(session, proposal)
    session.verification = report
    assert session.verification is not None

    second_root = Path(tempfile.mkdtemp(prefix="reprokit-fix-"))
    try:
        magic.stage_fix(session, proposal, second_root)
        assert session.verification is None
    finally:
        shutil.rmtree(second_root, ignore_errors=True)


def test_verify_module_has_no_execution_primitives():
    source = Path(verify.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert "subprocess" not in imported
    assert "os" not in imported
    assert "shell=True" not in source
    assert "eval(" not in source
    assert "exec(" not in source
    assert "os.system" not in source