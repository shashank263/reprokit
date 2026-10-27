"""Streamlit workflow checks for the landing page and Magic Mode review panel."""

from reprokit import analysis, magic, patch, studio
from streamlit.testing.v1 import AppTest


_SOURCE = (
    b"def is_valid(value):\n"
    b"    return len(value) > 0\n"
    b"# token=gsk_abcdefghijklmnop1234\n"
)
_DIFF = (
    "--- a/demo_app.py\n"
    "+++ b/demo_app.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def is_valid(value):\n"
    "-    return len(value) > 0\n"
    "+    return len(value.strip()) > 0\n"
)


def _session_with_proposal():
    files = magic.ingest_files(
        [
            ("demo_app.py", _SOURCE),
            ("demo.log", b"2026-01-01T10:00:00Z ERROR database unavailable\n"),
        ]
    )
    [issue] = magic.issues_from_ai(
        [
            magic.MagicAIIssue(
                title="Whitespace-only input is accepted",
                severity="medium",
                evidence_refs=[("demo_app.py", 2)],
                hypothesis="Likely caused by checking length without trimming.",
                confidence=0.8,
            )
        ],
        files,
    )
    validation = patch.validate_diff(
        _DIFF, {item.name: item.text for item in files}
    )
    assert validation.ok, validation.error

    session = magic.MagicSession(files=files)
    session.set_issues([issue])
    proposal = magic.FixProposal(
        issue_id=issue.id,
        explanation="Reject whitespace-only input.",
        diff=validation.canonical_diff,
        files_changed=[("demo_app.py", [2])],
        expected_behavior="Reject blank input.",
        tests_needed=["edge: whitespace-only username is rejected"],
        limitations=["Uploaded code is not executed."],
        validation=validation,
    )
    session.fix_proposal = proposal
    session.set_step("proposal")
    return session, issue


def test_landing_page_and_logic_mode_navigation():
    app = AppTest.from_file("app.py").run()
    assert not app.exception
    assert any(button.label == "Enter the studio" for button in app.button)

    next(button for button in app.button if button.label == "Enter the studio").click().run()
    assert not app.exception
    assert any(button.label == "Run Both Examples" for button in app.button)

    app.button_group[0].set_value("Magic Mode").run()
    assert not app.exception
    assert app.get("file_uploader")


def test_magic_ui_explains_automatic_analysis_and_optional_questions():
    app = AppTest.from_file("app.py")
    app.session_state["rk_entered"] = True
    app.session_state["app_mode"] = "Magic Mode"
    app.run()

    assert not app.exception
    assert any("analyzed automatically" in item.value for item in app.markdown)
    assert any(
        "without requiring follow-up answers" in item.value for item in app.markdown
    )
    assert not any(button.label == "Detect issues" for button in app.button)


def test_corrected_copy_is_created_only_on_explicit_action():
    session, _issue = _session_with_proposal()
    app = AppTest.from_file("app.py")
    app.session_state["rk_entered"] = True
    app.session_state["app_mode"] = "Magic Mode"
    app.session_state["magic_session"] = session
    app.run()

    assert not app.exception
    downloads = [button.label for button in app.get("download_button")]
    assert "Download Evidence-to-Fix report (Markdown)" in downloads
    assert not any("demo_app." in label for label in downloads)

    app.radio[0].set_value(studio.COMMENT_MODE_EXPLAIN).run()
    next(button for button in app.button if button.label == "Generate corrected copies").click().run()

    assert not app.exception
    downloads = [button.label for button in app.get("download_button")]
    assert "Download demo_app.explained.py" in downloads
    assert "Download Evidence-to-Fix report (Markdown)" in downloads
    assert any("# fix: Reject whitespace-only input." in item.value for item in app.code)
    assert session.files[0].text.encode("utf-8") == _SOURCE


def test_mocked_ai_review_recommendation_and_corrected_download(monkeypatch):
    session, issue = _session_with_proposal()
    session.fix_proposal = None
    status = analysis.ProviderStatus(
        available=True,
        provider="test",
        model="offline-mock",
        reason="Mocked provider for the UI test.",
    )
    monkeypatch.setattr(analysis, "provider_status", lambda: status)
    monkeypatch.setattr(
        analysis,
        "propose_fix",
        lambda *args, **kwargs: analysis.MagicAnalysisResult(
            ok=True,
            data=magic.FixProposalDraft(
                explanation="Reject whitespace-only input.",
                diff=_DIFF,
                files_changed=[("demo_app.py", [2])],
                expected_behavior="Reject blank input.",
                tests_needed=["edge: whitespace-only username is rejected"],
                limitations=["The sample remains unexecuted."],
            ),
            error=None,
            status=status,
        ),
    )
    monkeypatch.setattr(
        analysis,
        "propose_regression_test",
        lambda *args, **kwargs: analysis.MagicAnalysisResult(
            ok=True,
            data=studio.RegressionDraft(
                feasible=True,
                language="python",
                framework="pytest",
                test_filename="test_username.py",
                code="def test_whitespace_is_rejected():\n"
                "    assert not is_valid('   ')\n",
                scenario="Submit a whitespace-only username.",
                expected_behavior="The username is rejected.",
                why_it_catches="The original implementation accepts spaces.",
            ),
            error=None,
            status=status,
        ),
    )

    session.fix_proposal = None
    app = AppTest.from_file("app.py")
    app.session_state["rk_entered"] = True
    app.session_state["app_mode"] = "Magic Mode"
    app.session_state["magic_session"] = session
    app.run()
    assert not app.exception

    next(button for button in app.button if button.label == "Propose fix").click().run()
    assert not app.exception
    assert session.fix_proposal is not None
    assert session.fix_proposal.validation.ok
    assert session.fix_proposal.diff.startswith("--- a/demo_app.py")
    assert any("Reviewable diff" in item.value for item in app.markdown)
    assert any("Explanation" in item.value for item in app.markdown)
    assert not any("gsk_abcdefghijklmnop1234" in item.value for item in app.code)

    next(
        button for button in app.button if button.label == "Recommend one regression test"
    ).click().run()
    assert not app.exception
    assert any("Proposed test" in item.value for item in app.markdown)
    assert any("Submit a whitespace-only username" in item.value for item in app.caption)
    assert any("The username is rejected." in item.value for item in app.caption)

    app.radio[0].set_value(studio.COMMENT_MODE_EXPLAIN).run()
    next(button for button in app.button if button.label == "Generate corrected copies").click().run()
    assert not app.exception
    downloads = [button.label for button in app.get("download_button")]
    assert "Download demo_app.explained.py" in downloads
    assert "Download Evidence-to-Fix report (Markdown)" in downloads
    assert not any("gsk_abcdefghijklmnop1234" in item.value for item in app.code)
    assert any("inspect it for secrets before sharing" in item.value for item in app.caption)
    assert session.files[0].text.encode("utf-8") == _SOURCE
