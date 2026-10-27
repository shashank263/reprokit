"""Offline tests for the Stage 1 Magic Mode skeleton (reprokit/magic.py)."""

import hashlib

import pytest

from reprokit import magic


# ---------------------------------------------------------------------------
# File classification and ingestion
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("app.py", "code"),
        ("index.TSX", "code"),
        ("main.go", "code"),
        ("server.log", "log"),
        ("main.cpp", "code"),
        ("module.rs", "code"),
        ("deploy.sh", "code"),
        ("notes.txt", "text"),
        ("config.yaml", "text"),
        ("settings.toml", "text"),
        ("page.html", "text"),
        ("style.css", "text"),
        ("query.sql", "code"),
        ("trace.out", "log"),
    ],
)
def test_classify_extension(name, kind):
    assert magic.classify_extension(name) == kind


@pytest.mark.parametrize("name", ["archive.zip", "image.png", "noext", "run.exe"])
def test_unsupported_extensions_rejected(name):
    with pytest.raises(magic.UnsupportedFileTypeError):
        magic.classify_extension(name)


def test_ingest_preserves_original_and_hashes_content():
    raw = b"def f(x):\n    return x\n"
    [item] = magic.ingest_files([("app.py", raw)])

    assert item.name == "app.py"
    assert item.kind == "code"
    assert item.size_bytes == len(raw)
    assert item.sha256 == hashlib.sha256(raw).hexdigest()
    assert item.text == raw.decode()
    assert item.line_count == 2


def test_ingest_does_not_mutate_input_bytes():
    raw = b"secret = 'unchanged'\n"
    original = bytes(raw)
    magic.ingest_files([("config.py", raw)])
    assert raw == original


def test_ingest_rejects_empty_upload():
    with pytest.raises(magic.MagicInputError):
        magic.ingest_files([])
    with pytest.raises(magic.MagicInputError):
        magic.ingest_files([("empty.py", b"")])


def test_ingest_rejects_oversized_file(monkeypatch):
    monkeypatch.setattr(magic, "MAX_FILE_BYTES", 10)
    with pytest.raises(magic.FileTooLargeError):
        magic.ingest_files([("big.py", b"x" * 50)])


def test_ingest_rejects_too_many_files(monkeypatch):
    monkeypatch.setattr(magic, "MAX_FILES", 2)
    with pytest.raises(magic.TooManyFilesError):
        magic.ingest_files([("a.py", b"a"), ("b.py", b"b"), ("c.py", b"c")])


def test_ingest_rejects_total_size_over_limit(monkeypatch):
    monkeypatch.setattr(magic, "MAX_TOTAL_BYTES", 5)
    with pytest.raises(magic.FileTooLargeError):
        magic.ingest_files([("a.py", b"abc"), ("b.py", b"def")])


def test_duplicate_filenames_rejected_not_overwritten():
    with pytest.raises(magic.MagicInputError, match="Duplicate filename"):
        magic.ingest_files([("app.py", b"a"), ("app.py", b"b")])


def test_path_components_are_stripped():
    [item] = magic.ingest_files([("../../etc/app.py", b"x")])
    assert item.name == "app.py"


def test_binary_and_non_utf8_are_flagged_not_crashing():
    with pytest.raises(magic.BinaryFileError, match="was not analyzed"):
        magic.ingest_files([("blob.py", b"a\x00b")])
    with pytest.raises(magic.BinaryFileError, match="binary control bytes"):
        magic.ingest_files([("image.txt", b"\x89PNG\x01\x02\x03\x04")])

    [latin] = magic.ingest_files([("notes.txt", b"caf\xe9")])
    assert any("UTF-8" in w for w in latin.warnings)


def test_unsupported_content_is_identified_without_decoding():
    with pytest.raises(magic.UnsupportedFileTypeError, match="unsupported extension"):
        magic.ingest_files([("photo.png", b"\x89PNG\r\n\x1a\n")])


@pytest.mark.parametrize("extension", sorted(magic.CODE_EXTENSIONS))
def test_all_supported_source_extensions_are_classified(extension):
    assert magic.classify_extension(f"sample{extension}") == "code"
    [item] = magic.ingest_files([(f"sample{extension}", b"content\n")])
    assert item.format != "plain text"


@pytest.mark.parametrize("extension", sorted(magic.TEXT_EXTENSIONS))
def test_all_supported_document_and_configuration_extensions_are_classified(extension):
    assert magic.classify_extension(f"sample{extension}") == "text"
    [item] = magic.ingest_files([(f"sample{extension}", b"content\n")])
    assert item.format != "plain text" or extension == ".txt"


# ---------------------------------------------------------------------------
# Answer visibility / privacy filter
# ---------------------------------------------------------------------------
def _session_with_questions():
    session = magic.MagicSession()
    session.add_question(magic.Question(id="q1", text="Which environment?"))
    session.add_question(magic.Question(id="q2", text="Any recent deploy?"))
    session.add_question(magic.Question(id="q3", text="Expected behavior?"))
    return session


def test_private_and_skipped_answers_are_never_shareable():
    session = _session_with_questions()
    session.answer("q1", "production", "shared")
    session.answer("q2", "internal host db-prod-01", "private")
    session.answer("q3", "", "skipped")

    shareable = session.shareable_answers()

    assert [a.question_id for a in shareable] == ["q1"]
    assert session.private_answer_count() == 1


def test_shared_but_empty_answer_is_not_sent():
    session = _session_with_questions()
    session.answer("q1", "   ", "shared")
    assert session.shareable_answers() == []


def test_ai_context_excludes_private_answers():
    session = _session_with_questions()
    session.answer("q1", "production", "shared")
    session.answer("q2", "secret internal detail", "private")

    files = magic.ingest_files([("app.py", b"print('hi')\n")])
    context = magic.build_ai_context(files, session.shareable_answers())

    assert "production" in context
    assert "secret internal detail" not in context
    assert "=== SHARED DEVELOPER ANSWERS ===" in context


def test_ai_context_redacts_secrets_and_is_bounded():
    files = magic.ingest_files(
        [("app.py", b"API_KEY = 'gsk_abcdefghijklmnop1234'\n" * 50)]
    )
    context = magic.build_ai_context(files, [], max_chars=300)
    assert "gsk_abcdefghijklmnop1234" not in context
    assert len(context) <= 300


def test_unknown_question_or_visibility_rejected():
    session = _session_with_questions()
    with pytest.raises(magic.MagicInputError):
        session.answer("nope", "x", "shared")
    with pytest.raises(magic.MagicInputError):
        session.answer("q1", "x", "public")  # type: ignore[arg-type]


def test_duplicate_question_id_rejected():
    session = _session_with_questions()
    with pytest.raises(magic.MagicInputError):
        session.add_question(magic.Question(id="q1", text="again"))


# ---------------------------------------------------------------------------
# Workflow state
# ---------------------------------------------------------------------------
def test_workflow_steps_are_ordered_and_validated():
    keys = [key for key, _ in magic.WORKFLOW_STEPS]
    assert keys == ["upload", "analyze", "evidence", "proposal", "verify"]

    session = magic.MagicSession()
    assert session.step == "upload"
    session.set_step("evidence")
    assert session.step == "evidence"
    with pytest.raises(magic.MagicInputError):
        session.set_step("deploy")


def test_stage_one_has_no_proposal_or_verification():
    session = magic.MagicSession()
    assert session.proposal is None
    assert session.verification is None

# ---------------------------------------------------------------------------
# Stage 2: grounded issues
# ---------------------------------------------------------------------------
_LOG = b"""\
2024-10-26T10:00:00Z INFO start
2024-10-26T10:00:01Z ERROR database connection failed
2024-10-26T10:00:02Z GET /users 500
2024-10-26T10:00:03Z GET /users 500
2024-10-26T10:00:04Z WARN request timeout after 30s
"""


def test_issues_from_logs_have_real_evidence():
    files = magic.ingest_files([("server.log", _LOG)])
    issues = magic.issues_from_logs(files)

    assert issues
    for issue in issues:
        assert issue.evidence, "every log issue needs evidence"
        for item in issue.evidence:
            assert item.file == "server.log"
            assert 1 <= item.line <= files[0].line_count
            assert item.text == files[0].text.splitlines()[item.line - 1]

    http = next(i for i in issues if "HTTP 500" in i.title)
    assert http.severity == "high"
    assert http.severity_source == "observed"
    assert len(http.evidence) == 2  # two 500s


def test_issues_from_logs_ignores_code_and_text_files():
    files = magic.ingest_files([("app.py", b"print('hi')\n")])
    assert magic.issues_from_logs(files) == []


def test_resolve_evidence_discards_unknown_file_and_bad_lines():
    files = magic.ingest_files([("app.py", b"a\nb\nc\n")])
    refs = [
        ("app.py", 2),
        ("missing.py", 1),
        ("app.py", 0),
        ("app.py", 4),
        ("app.py", True),  # bool is not a valid line
        ("app.py", 2),  # duplicate
    ]
    resolved = magic.resolve_evidence(files, refs)
    assert [(e.file, e.line, e.text) for e in resolved] == [("app.py", 2, "b")]


def test_resolve_evidence_respects_max_refs():
    files = magic.ingest_files([("app.py", b"1\n2\n3\n4\n5\n6\n")])
    resolved = magic.resolve_evidence(files, [("app.py", n) for n in range(1, 7)], max_refs=2)
    assert len(resolved) == 2


def test_resolve_evidence_zero_limit_returns_no_evidence():
    files = magic.ingest_files([("app.py", b"line\n")])
    assert magic.resolve_evidence(files, [("app.py", 1)], max_refs=0) == []


def _ai_issue(title="Bug", refs=None, severity="high", hypothesis="Likely caused by X", questions=("Q1?",)):
    return magic.MagicAIIssue(
        title=title,
        severity=severity,
        evidence_refs=list(refs or []),
        hypothesis=hypothesis,
        questions=list(questions),
    )


def test_issues_from_ai_discards_ungrounded_issues():
    files = magic.ingest_files([("app.py", b"line1\nline2\n")])
    issues = magic.issues_from_ai(
        [_ai_issue(refs=[("missing.py", 1)]), _ai_issue(refs=[("app.py", 99)])],
        files,
    )
    assert issues == []


def test_issues_from_ai_grounds_and_attaches_questions():
    files = magic.ingest_files([("app.py", b"line1\nline2\n")])
    issues = magic.issues_from_ai([_ai_issue(refs=[("app.py", 2)])], files)

    assert len(issues) == 1
    issue = issues[0]
    assert issue.evidence[0].text == "line2"
    assert issue.hypothesis.startswith("Likely caused by")
    assert issue.severity_source == "ai"
    assert [q.text for q in issue.questions] == ["Q1?"]
    assert issue.questions[0].issue_id == issue.id


def test_issues_from_ai_caps_refs_questions_and_invalid_severity():
    files = magic.ingest_files([("app.py", b"a\nb\nc\nd\ne\nf\ng\n")])
    raw = _ai_issue(
        refs=[("app.py", n) for n in range(1, 8)],
        severity="catastrophic",
        questions=[f"Q{i}" for i in range(10)],
    )
    [issue] = magic.issues_from_ai([raw], files)
    assert len(issue.evidence) <= magic.MAX_EVIDENCE_REFS
    assert len(issue.questions) <= magic.MAX_QUESTIONS_PER_ISSUE
    assert issue.severity == "medium"


def test_issues_from_ai_enriches_matching_log_issue_instead_of_duplicating():
    files = magic.ingest_files([("server.log", _LOG)])
    log_issues = magic.issues_from_logs(files)
    target = next(i for i in log_issues if "HTTP 500" in i.title)
    first_ref = (target.evidence[0].file, target.evidence[0].line)

    ai_issues = magic.issues_from_ai(
        [_ai_issue(title="Repeated 500s", refs=[first_ref])], files, existing=log_issues
    )

    assert ai_issues == []
    assert target.hypothesis == "Likely caused by X"
    assert len(target.questions) == 1


# ---------------------------------------------------------------------------
# Stage 2: excerpt + session wiring
# ---------------------------------------------------------------------------
def test_issue_excerpt_is_numbered_bounded_and_redacted():
    code = ("API_KEY = 'gsk_abcdefghijklmnop1234'\n" + "x = 1\n" * 300).encode()
    files = magic.ingest_files([("app.py", code), ("server.log", _LOG)])
    excerpt = magic.build_issue_excerpt(files, max_lines=25, max_chars=800)

    assert "=== FILE app.py (code) ===" in excerpt
    assert "=== LOG FILE server.log (grouped errors) ===" in excerpt
    assert excerpt.count("\n") <= 26
    assert len(excerpt) <= 800
    assert "gsk_abcdefghijklmnop1234" not in excerpt
    assert "1: " in excerpt  # line numbers preserved


def test_set_issues_registers_questions_and_moves_step():
    files = magic.ingest_files([("app.py", b"a\nb\n")])
    issues = magic.issues_from_ai([_ai_issue(refs=[("app.py", 1)])], files)

    session = magic.MagicSession(files=files)
    session.set_issues(issues)

    assert session.selected_issue_id == issues[0].id
    assert session.step == "evidence"
    assert session.questions_for(issues[0].id) == issues[0].questions


def test_select_issue_and_unknown_issue():
    files = magic.ingest_files([("app.py", b"a\n")])
    issues = magic.issues_from_ai([_ai_issue(refs=[("app.py", 1)])], files)
    session = magic.MagicSession(files=files)
    session.set_issues(issues)

    assert session.selected_issue().id == issues[0].id
    with pytest.raises(magic.MagicInputError):
        session.select_issue("nope")


def test_issue_evidence_display_is_redacted():
    files = magic.ingest_files([("app.py", b"token=supersecret123\n")])
    [issue] = magic.issues_from_ai([_ai_issue(refs=[("app.py", 1)])], files)
    assert "supersecret123" not in issue.evidence[0].display_text
    assert "REDACTED" in issue.evidence[0].display_text


@pytest.mark.parametrize(
    ("name", "source", "expected"),
    [
        ("broken.py", b"def f(:\n    pass\n", "Python syntax error"),
        ("broken.json", b'{"ok": }\n', "Malformed JSON"),
        ("broken.xml", b"<root>\n<item>\n", "Malformed XML"),
    ],
)
def test_static_syntax_errors_have_exact_grounded_lines(name, source, expected):
    files = magic.ingest_files([(name, source)])
    issues, _notes = magic.analyze_static(files)

    [issue] = issues
    assert expected in issue.title
    assert issue.severity == "high"
    assert issue.confidence == 1.0
    assert issue.verification_status == "failed"
    assert issue.evidence
    assert issue.evidence[0].file == name
    assert 1 <= issue.evidence[0].line <= files[0].line_count


def test_static_checks_support_json_toml_xml_and_report_unknown_validators():
    files = magic.ingest_files(
        [
            ("settings.json", b'{"enabled": true}\n'),
            ("settings.toml", b"[app]\nenabled = true\n"),
            ("settings.ini", b"[app]\nenabled = true\n"),
            ("settings.xml", b"<app><enabled>true</enabled></app>\n"),
            ("script.js", b"function run() { return 1; }\n"),
        ]
    )
    issues, notes = magic.analyze_static(files)
    assert issues == []
    assert any("JSON syntax validation passed" in note for note in notes)
    if magic.tomllib is None:
        assert any("settings.toml" in note and "no installed" in note for note in notes)
    else:
        assert any("TOML syntax validation passed" in note for note in notes)
    assert any("XML syntax validation passed" in note for note in notes)
    assert any("INI syntax validation passed" in note for note in notes)
    assert any("no installed static validator" in note for note in notes)


def test_multiple_unrelated_files_are_reported_independently():
    files = magic.ingest_files(
        [
            ("input.py", b"def valid(value):\n    return len(value) > 0\n"),
            ("unrelated.js", b"function run() { return 1; }\n"),
        ]
    )
    issues, notes = magic.analyze_static(files)

    assert len(issues) == 1
    assert {item.file for item in issues[0].evidence} == {"input.py"}
    assert any("unrelated.js" in note for note in notes)
    assert not any("unrelated.js" in item.file for item in issues[0].evidence)


def test_static_analysis_parses_but_never_executes_uploaded_python():
    files = magic.ingest_files(
        [("do_not_run.py", b"raise RuntimeError('must not execute')\n")]
    )

    issues, _notes = magic.analyze_static(files)

    assert issues == []
    assert files[0].text.startswith("raise RuntimeError")


def test_logic_issue_proposes_narrow_fix_with_explicit_assumption():
    files = magic.ingest_files(
        [("input.py", b"def is_valid(value):\n    return len(value) > 0\n")]
    )
    [issue], _notes = magic.analyze_static(files)

    assert "Whitespace-only" in issue.title
    assert issue.evidence[0].label == "input.py:2"
    assert issue.confidence == 0.78
    assert issue.verification_status == "inconclusive"
    assert issue.assumptions
    from reprokit import patch, studio

    validation = patch.validate_diff(issue.fix_diff, {files[0].name: files[0].text})
    assert validation.ok, validation.error
    [corrected], error = studio.generate_corrected_files(files, validation)
    assert error == ""
    assert "len(value.strip()) > 0" in corrected.corrected
    assert files[0].text.endswith("return len(value) > 0\n")


def test_invalid_ini_reports_exact_parser_line():
    files = magic.ingest_files([("settings.ini", b"[app]\nenabled = true\ninvalid\n")])

    [issue], _notes = magic.analyze_static(files)

    assert issue.title.startswith("Malformed INI")
    assert issue.evidence[0].label == "settings.ini:3"
    assert issue.verification_status == "failed"


def test_log_only_upload_is_grounded_and_never_treated_as_source():
    files = magic.ingest_files([("service.log", b"ERROR database unavailable\n")])
    issues = magic.issues_from_logs(files)

    [issue] = issues
    assert issue.category == "log"
    assert issue.evidence[0].label == "service.log:1"
    assert issue.observed_problem.startswith("Observed error log event")
    assert issue.fix_diff == ""
    assert not [item for item in files if item.kind != "log"]

# ---------------------------------------------------------------------------
# Stage 3: fix context and staging
# ---------------------------------------------------------------------------
_SOURCE = b"def is_valid(v):\n    return len(v) > 0\n"
_FIX_DIFF = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def is_valid(v):\n"
    "-    return len(v) > 0\n"
    "+    return len(v.strip()) > 0\n"
)


def _issue_for(files):
    [issue] = magic.issues_from_ai(
        [
            magic.MagicAIIssue(
                title="Whitespace accepted",
                severity="high",
                evidence_refs=[("app.py", 2)],
                hypothesis="Likely missing strip().",
                questions=["Is whitespace-only input valid?"],
            )
        ],
        files,
    )
    return issue


def test_build_fix_context_includes_evidence_and_shared_only():
    files = magic.ingest_files([("app.py", _SOURCE)])
    issue = _issue_for(files)
    session = magic.MagicSession(files=files)
    session.add_question(issue.questions[0])
    shared = session.answer(issue.questions[0].id, "must stay Python 3 only", "shared")
    private = magic.Answer("secret-q", "db-prod-01", "private")
    skipped = magic.Answer("skip-q", "unused note", "skipped")

    context = magic.build_fix_context(files, issue, [shared, private, skipped])

    assert "ISSUE: Whitespace accepted" in context
    assert "app.py:2:     return len(v) > 0" in context
    assert "must stay Python 3 only" in context
    assert "db-prod-01" not in context
    assert "unused note" not in context


def test_build_fix_context_is_bounded_and_redacted():
    code = b"API_KEY = 'gsk_abcdefghijklmnop1234'\n" + b"x = 1\n" * 500
    files = magic.ingest_files([("app.py", code)])
    issue = magic.issues_from_ai(
        [magic.MagicAIIssue("T", "low", [("app.py", 1)])], files
    )[0]
    context = magic.build_fix_context(files, issue, [], max_chars=600)
    assert len(context) <= 600
    assert "gsk_abcdefghijklmnop1234" not in context
    assert "REDACTED" in context


def _validated_proposal(files, issue):
    from reprokit import patch

    validation = patch.validate_diff(_FIX_DIFF, {f.name: f.text for f in files})
    assert validation.ok, validation.error
    return magic.FixProposal(
        issue_id=issue.id,
        explanation="Strip before checking.",
        diff=validation.canonical_diff,
        files_changed=[("app.py", [2])],
        expected_behavior="Whitespace rejected.",
        tests_needed=["edge: rejects '   '"],
        limitations=["Assumes str input."],
        validation=validation,
    )


def test_stage_fix_writes_workspace_and_preserves_originals(tmp_path):
    files = magic.ingest_files([("app.py", _SOURCE)])
    issue = _issue_for(files)
    session = magic.MagicSession(files=files)
    session.set_issues([issue])
    before = files[0].sha256

    proposal = _validated_proposal(files, issue)
    magic.stage_fix(session, proposal, tmp_path / "ws")

    assert (tmp_path / "ws" / "app.py").read_text() == (
        "def is_valid(v):\n    return len(v.strip()) > 0\n"
    )
    # Original snapshot is untouched.
    assert files[0].sha256 == before
    assert files[0].text == _SOURCE.decode()
    assert session.fix_proposal is proposal
    assert proposal.status.startswith("Proposed")
    assert proposal.workspace is not None


def test_stage_fix_rejects_invalid_proposal(tmp_path):
    files = magic.ingest_files([("app.py", _SOURCE)])
    issue = _issue_for(files)
    session = magic.MagicSession(files=files)

    from reprokit import patch

    invalid = patch.validate_diff(
        "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-not here\n+x\n",
        {"app.py": _SOURCE.decode()},
    )
    proposal = magic.FixProposal(
        issue_id=issue.id,
        explanation="x",
        diff="",
        files_changed=[],
        expected_behavior="x",
        tests_needed=[],
        limitations=[],
        validation=invalid,
    )
    with pytest.raises(magic.MagicInputError):
        magic.stage_fix(session, proposal, tmp_path / "ws")
    assert not (tmp_path / "ws" / "app.py").exists()


# ---------------------------------------------------------------------------
# Regression: AI filename mismatch (questions + rejected proposals)
# ---------------------------------------------------------------------------
def test_ai_questions_referencing_unuploaded_files_are_dropped():
    files = magic.ingest_files(
        [("buggy_calculator.py", b"def divide(a, b):\n    return a / b\n")]
    )
    raw = magic.MagicAIIssue(
        title="Division by zero",
        severity="high",
        evidence_refs=[("buggy_calculator.py", 2)],
        questions=[
            "In basic.py, should divide by zero raise?",
            "Is buggy_calculator.py the only caller?",
        ],
    )

    [issue] = magic.issues_from_ai([raw], files)

    texts = [q.text for q in issue.questions]
    assert "Is buggy_calculator.py the only caller?" in texts
    assert all("basic.py" not in text for text in texts), texts
    # Related file is always derived from validated evidence, never AI text.
    assert all(q.related_file == "buggy_calculator.py" for q in issue.questions)


def test_filename_detection_helpers():
    allowed = ["buggy_calculator.py"]
    assert magic.has_unknown_filename("See basic.py for details", allowed)
    assert magic.has_unknown_filename("edit ./sub/other.py", allowed)
    assert not magic.has_unknown_filename("Check buggy_calculator.py", allowed)
    assert not magic.has_unknown_filename("No filenames here", allowed)
    assert magic.filename_mentions("a.py and B.PY") == {"a.py", "b.py"}


def test_build_fix_context_lists_exact_allowed_files():
    files = magic.ingest_files(
        [("buggy_calculator.py", b"def divide(a, b):\n    return a / b\n")]
    )
    issue = magic.issues_from_ai(
        [
            magic.MagicAIIssue(
                title="Division by zero",
                severity="high",
                evidence_refs=[("buggy_calculator.py", 2)],
            )
        ],
        files,
    )[0]
    context = magic.build_fix_context(files, issue, [])
    assert "ALLOWED FILES (use these exact filenames" in context
    assert "buggy_calculator.py" in context


def test_rejection_reason_is_never_blank():
    from reprokit import patch

    proposal = magic.FixProposal(
        issue_id="i",
        explanation="x",
        diff="",
        files_changed=[],
        expected_behavior="x",
        tests_needed=[],
        limitations=[],
        validation=patch.PatchValidation(ok=False, error=""),
    )
    reason = magic.rejection_reason_for(proposal)
    assert reason.strip()
    assert "could not be validated" in reason


def test_rejection_reason_preserves_validator_message():
    from reprokit import patch

    message = (
        "diff touches 'basic.py', which is not one of the uploaded files "
        "(buggy_calculator.py)"
    )
    proposal = magic.FixProposal(
        issue_id="i",
        explanation="x",
        diff="",
        files_changed=[],
        expected_behavior="x",
        tests_needed=[],
        limitations=[],
        validation=patch.PatchValidation(ok=False, error=message),
    )
    assert magic.rejection_reason_for(proposal) == message
