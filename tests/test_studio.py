"""Offline tests for the Evidence-to-Fix Review Studio (reprokit/studio.py)."""

import json

import pytest

from reprokit import magic, patch, studio, verify


SOURCE = b"def divide(a, b):\n    return a / b\n"
DIFF = (
    "--- a/buggy_calculator.py\n"
    "+++ b/buggy_calculator.py\n"
    "@@ -1,2 +1,4 @@\n"
    " def divide(a, b):\n"
    "+    if b == 0:\n"
    "+        return None\n"
    "     return a / b\n"
)


def _setup(source=SOURCE, diff=DIFF, name="buggy_calculator.py"):
    files = magic.ingest_files([(name, source)])
    issue = magic.issues_from_ai(
        [
            magic.MagicAIIssue(
                title="Division by zero",
                severity="high",
                evidence_refs=[(name, 2)],
                hypothesis="Likely missing a zero guard.",
                questions=["Should zero return None?"],
            )
        ],
        files,
    )[0]
    validation = patch.validate_diff(diff, {f.name: f.text for f in files})
    assert validation.ok, validation.error
    proposal = magic.FixProposal(
        issue_id=issue.id,
        explanation="Guard against b == 0.",
        diff=validation.canonical_diff,
        files_changed=[(name, [2])],
        expected_behavior="Zero returns None.",
        tests_needed=["edge: b == 0"],
        limitations=["Numeric input assumed."],
        validation=validation,
    )
    return files, issue, proposal


# ---------------------------------------------------------------------------
# Corrected-file generation
# ---------------------------------------------------------------------------
def test_clean_corrected_file_matches_patch_and_preserves_original():
    files, issue, proposal = _setup()
    original = files[0].text

    corrected, error = studio.generate_corrected_files(
        files, proposal.validation, mode=studio.COMMENT_MODE_CLEAN
    )

    assert error == ""
    [item] = corrected
    assert item.name == "buggy_calculator.py"
    assert item.corrected == proposal.validation.files[0].after
    assert item.comments_added == 0
    assert item.syntax_ok is True
    # Originals are untouched.
    assert files[0].text == original


def test_explain_mode_adds_concise_comments_and_still_parses():
    files, issue, proposal = _setup()

    corrected, error = studio.generate_corrected_files(
        files, proposal.validation, mode=studio.COMMENT_MODE_EXPLAIN,
        reason="Division by zero",
    )

    assert error == ""
    [item] = corrected
    assert item.comments_added >= 1
    assert "# fix: Division by zero" in item.corrected
    assert "if b == 0" in item.corrected
    assert item.syntax_ok is True
    assert files[0].text == SOURCE.decode()


def test_explain_mode_falls_back_for_json_without_comments():
    files = magic.ingest_files([("config.json", b'{\n  "limit": 1\n}\n')])
    diff = (
        "--- a/config.json\n"
        "+++ b/config.json\n"
        "@@ -1,3 +1,3 @@\n"
        " {\n"
        '-  "limit": 1\n'
        '+  "limit": 2\n'
        " }\n"
    )
    validation = patch.validate_diff(diff, {f.name: f.text for f in files})
    assert validation.ok, validation.error

    [item], error = studio.generate_corrected_files(
        files, validation, mode=studio.COMMENT_MODE_EXPLAIN, reason="limit"
    )
    assert error == ""
    assert item.comments_added == 0
    assert "not supported" in item.syntax_note
    assert item.syntax_ok is True


def test_explained_xml_copy_uses_valid_xml_comments_and_static_parse():
    files = magic.ingest_files([("config.xml", b"<app>\n  <limit>1</limit>\n</app>\n")])
    diff = (
        "--- a/config.xml\n"
        "+++ b/config.xml\n"
        "@@ -1,3 +1,3 @@\n"
        " <app>\n"
        "-  <limit>1</limit>\n"
        "+  <limit>2</limit>\n"
        " </app>\n"
    )
    validation = patch.validate_diff(diff, {item.name: item.text for item in files})
    assert validation.ok, validation.error

    [copy], error = studio.generate_corrected_files(
        files,
        validation,
        mode=studio.COMMENT_MODE_EXPLAIN,
        reason="Corrected configured limit",
    )

    assert error == ""
    assert "<!-- fix: Corrected configured limit -->" in copy.corrected
    assert copy.syntax_ok is True
    assert files[0].text == "<app>\n  <limit>1</limit>\n</app>\n"


def test_invalid_static_corrected_copy_is_not_offered():
    files = magic.ingest_files([("config.json", b'{"value": 1}\n')])
    diff = (
        "--- a/config.json\n"
        "+++ b/config.json\n"
        "@@ -1 +1 @@\n"
        '-{"value": 1}\n'
        '+{"value": }\n'
    )
    validation = patch.validate_diff(diff, {item.name: item.text for item in files})
    assert validation.ok, validation.error

    copies, error = studio.generate_corrected_files(files, validation)

    assert copies == []
    assert "failed static syntax validation" in error
    assert files[0].text == '{"value": 1}\n'


def test_multiple_files_generate_multiple_copies():
    files = magic.ingest_files(
        [("a.py", b"x = 1\n"), ("b.py", b"y = 1\n")]
    )
    diff = (
        "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-y = 1\n+y = 2\n"
    )
    validation = patch.validate_diff(diff, {f.name: f.text for f in files})
    assert validation.ok, validation.error

    corrected, error = studio.generate_corrected_files(files, validation)
    assert error == ""
    assert {item.name for item in corrected} == {"a.py", "b.py"}
    assert all(item.syntax_ok for item in corrected)


def test_invalid_proposal_refuses_generation():
    files, _issue, _proposal = _setup()
    validation = patch.validate_diff("not a diff", {f.name: f.text for f in files})

    corrected, error = studio.generate_corrected_files(files, validation)
    assert corrected == []
    assert "validated proposal" in error


def test_unknown_comment_mode_rejected():
    files, _issue, proposal = _setup()
    corrected, error = studio.generate_corrected_files(
        files, proposal.validation, mode="shouty"
    )
    assert corrected == []
    assert "Unknown comment mode" in error


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("buggy_calculator.py", "buggy_calculator.corrected.py"),
        ("notes", "notes.corrected"),
        ("../../evil.py", "evil.corrected.py"),
    ],
)
def test_corrected_download_names_are_safe(name, expected):
    assert studio.corrected_download_name(name, studio.COMMENT_MODE_CLEAN) == expected


def test_safe_filename_strips_paths():
    assert studio.safe_filename("../../etc/passwd") == "passwd"
    assert studio.safe_filename("a b!.py") == "a-b-.py"
    assert studio.safe_filename("///") == "file"


# ---------------------------------------------------------------------------
# Regression-test recommendation
# ---------------------------------------------------------------------------
def _draft(**overrides):
    base = dict(
        feasible=True,
        language="python",
        framework="pytest",
        test_filename="test_division.py",
        code='def test_zero_division():\n    assert divide(1, 0) is None\n',
        scenario="Divide by zero",
        preconditions=["b == 0"],
        expected_behavior="Returns None",
        why_it_catches="The buggy version raises ZeroDivisionError.",
        limitations=[],
    )
    base.update(overrides)
    return studio.RegressionDraft(**base)


def test_valid_python_regression_is_proposed_not_executed():
    files, issue, _proposal = _setup()
    test = studio.regression_from_ai(
        _draft(), issue=issue, evidence=["buggy_calculator.py:2"], files=files
    )

    assert test.status == studio.TEST_STATUS_PROPOSED
    assert test.is_proposed
    assert test.filename == "test_division.py"
    assert "def test_zero_division" in test.code
    assert test.evidence == ["buggy_calculator.py:2"]


def test_unsafe_test_filename_is_replaced():
    files, issue, _proposal = _setup()
    test = studio.regression_from_ai(
        _draft(test_filename="../evil.py"), issue=issue, files=files
    )
    assert test.filename == "reprokit_regression_test.py"
    assert any("unsafe" in item.lower() for item in test.limitations)


def test_test_filename_colliding_with_upload_is_replaced():
    files, issue, _proposal = _setup()
    test = studio.regression_from_ai(
        _draft(test_filename="buggy_calculator.py"), issue=issue, files=files
    )
    assert test.filename == "reprokit_regression_test.py"
    assert any("matches an uploaded file" in item for item in test.limitations)


def test_non_python_or_infeasible_is_not_generated():
    files, issue, _proposal = _setup()
    other = studio.regression_from_ai(
        _draft(language="javascript"), issue=issue, files=files
    )
    assert other.status == studio.TEST_STATUS_NOT_GENERATED

    infeasible = studio.regression_from_ai(
        _draft(feasible=False, code="", limitations=["No API details were provided."]),
        issue=issue,
        files=files,
    )
    assert infeasible.status == studio.TEST_STATUS_NOT_GENERATED
    assert "No API details" in infeasible.limitations[0]


def test_non_test_code_is_kept_as_specification_only():
    files, issue, _proposal = _setup()
    test = studio.regression_from_ai(
        _draft(code="print('not a test')\n"), issue=issue, files=files
    )
    assert test.status == studio.TEST_STATUS_PROPOSED
    assert test.code == ""
    assert any("does not define a test function" in item for item in test.limitations)


def test_parse_error_marks_not_generated():
    files, issue, _proposal = _setup()
    test = studio.regression_from_ai(
        studio.RegressionDraft(feasible=False, parse_error="bad json"),
        issue=issue,
        files=files,
    )
    assert test.status == studio.TEST_STATUS_NOT_GENERATED
    assert test.parse_error == "bad json"


# ---------------------------------------------------------------------------
# Fix report
# ---------------------------------------------------------------------------
def _verification_report(files, proposal):
    checks = [
        verify.CheckResult(
            check_id=verify.CHECK_UPLOADS_UNCHANGED,
            label="Original uploads unchanged",
            scope="uploaded snapshots",
            status="passed",
            check="SHA-256 comparison",
            duration_seconds=0.01,
            output="1 file(s) match staging hashes.",
        ),
        verify.CheckResult(
            check_id=verify.CHECK_FIXTURE_REGRESSION,
            label="ReproKit fixture regression",
            scope="ReproKit's own fixtures",
            status="passed",
            check="pytest -v tests/test_fixed_app.py",
            exit_status=0,
            duration_seconds=0.9,
            output="3 passed in 0.05s",
        ),
    ]
    return verify.VerificationReport(
        overall_status=verify.STATUS_VERIFIED,
        scope_statement=verify.SCOPE_STATEMENT,
        checks=checks,
        verified_at="2026-01-01T00:00:00+00:00",
        workspace="C:/temp/reprokit-fix-x",
        proposal_digest="abc",
    )


def test_report_contains_evidence_diff_test_and_status():
    files, issue, proposal = _setup()
    regression = studio.regression_from_ai(
        _draft(), issue=issue, evidence=["buggy_calculator.py:2"], files=files
    )
    verification = _verification_report(files, proposal)
    corrected, _ = studio.generate_corrected_files(files, proposal.validation)

    report = studio.build_fix_report(
        issue=issue,
        files=files,
        log_excerpts=["2026 ERROR zero division at buggy_calculator.py:2"],
        proposal=proposal,
        regression=regression,
        verification=verification,
        corrected=corrected,
    )

    assert "## 1. Finding and evidence" in report
    assert "buggy_calculator.py:2" in report
    assert "```diff" in report and "if b == 0" in report
    assert studio.TEST_STATUS_PROPOSED in report
    assert "not executed" in report.lower()
    assert verify.STATUS_VERIFIED in report
    assert "Scope:" in report
    assert "## 6. Corrected files" in report
    assert "buggy_calculator.corrected.py" in report


def test_report_marks_unverified_and_no_test_honestly():
    files, issue, proposal = _setup()
    report = studio.build_fix_report(
        issue=issue, files=files, proposal=proposal, regression=None, verification=None
    )
    assert "not_run" in report
    assert "No regression test has been requested yet." in report
    assert "uploaded code was not executed" in report.lower()


def test_report_redacts_secrets():
    files, issue, proposal = _setup()
    report = studio.build_fix_report(
        issue=issue,
        files=files,
        log_excerpts=["token=gsk_abcdefghijklmnop1234 leaked"],
        proposal=proposal,
    )
    assert "gsk_abcdefghijklmnop1234" not in report
    assert "REDACTED" in report


def test_findings_report_supports_log_only_and_all_uploaded_files():
    files = magic.ingest_files(
        [("server.log", b"ERROR request failed\n"), ("notes.md", b"Not related.\n")]
    )
    [issue] = magic.issues_from_logs(files)

    report = studio.build_findings_report(
        files=files,
        issues=[issue],
        notes=["Source code was not uploaded; the application cause is unknown."],
    )

    assert "server.log:1" in report
    assert "notes.md" in report
    assert "Severity" in report
    assert "Confidence" in report
    assert "inconclusive" in report
    assert "Source code was not uploaded" in report
    assert "Uploaded code execution:** never" in report


def test_report_download_name_is_safe():
    assert studio.report_download_name("Division by zero!") == "reprokit-fix-report-division-by-zero.md"
    assert studio.report_download_name("../../etc") == "reprokit-fix-report-etc.md"
    assert studio.report_download_name("") == "reprokit-fix-report.md"