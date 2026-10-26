"""ReproKit dashboard (Streamlit entry point).

Launch with:
    streamlit run app.py
"""

import hashlib
import re
import tempfile

import streamlit as st

from reprokit import analysis, logs, magic, patch, ui, verify
from reprokit.runner import TestRunResult, run_tests

# Approved identifiers only — see reprokit/runner.py.
BUGGY_TEST_ID = "buggy_input_validation"
FIXED_TEST_ID = "fixed_input_validation"

_EXPECTED_STATUS = {
    BUGGY_TEST_ID: "failed",
    FIXED_TEST_ID: "passed",
}

_EXPECTED_LABEL = {
    BUGGY_TEST_ID: (
        "The buggy example is **expected to fail**: its whitespace-only input "
        "validation bug is intentional and reproducible. A failing run here is "
        "the correct, expected outcome — it is not a success."
    ),
    FIXED_TEST_ID: (
        "The fixed example is **expected to pass**: the corrected "
        "implementation rejects empty and whitespace-only input."
    ),
}


def _render_result(result: TestRunResult, expected_status: str) -> None:
    """Display one TestRunResult without altering or fabricating its outcome."""
    st.subheader(f"Result: `{result.status}`")

    if result.status == expected_status:
        if expected_status == "failed":
            st.warning(
                "This run failed **as expected**. The failure is the "
                "reproducible, intentional bug — do not read it as a pass."
            )
        else:
            st.success("All tests passed, as expected.")
    elif result.status == "inconclusive":
        st.warning("The result is inconclusive and must not be read as a pass.")
        if result.timed_out:
            st.warning(
                f"The run timed out after the configured limit "
                f"({result.error}). Try again or raise the runner timeout."
            )
        elif result.error:
            st.warning(f"Runner error: {result.error}")
        else:
            st.warning(
                "pytest produced no unambiguous test summary. Check the "
                "stdout/stderr below and re-run."
            )
    else:
        st.error(
            f"Unexpected result: expected `{expected_status}` but got "
            f"`{result.status}`. Investigate the output below before drawing "
            "any conclusion."
        )

    exit_code = (
        result.returncode
        if result.returncode is not None
        else "n/a (no process result)"
    )
    col_status, col_code, col_duration = st.columns(3)
    col_status.metric("Normalized status", result.status)
    col_code.metric("Exit code", exit_code)
    col_duration.metric(
        "Duration", f"{result.duration_seconds:.2f} s"
    )

    st.markdown("**Command executed**")
    st.code(result.command_display, language="text")

    with st.expander("stdout", expanded=True):
        st.code(result.stdout if result.stdout else "(empty)", language="text")

    with st.expander("stderr", expanded=result.stderr != ""):
        st.code(result.stderr if result.stderr else "(empty)", language="text")


def _run_and_show(test_id: str) -> None:
    """Execute the allowlisted test and store the result for rendering."""
    result = run_tests(test_id)
    st.session_state[f"result_{test_id}"] = result


def _exit_code_display(result: TestRunResult) -> str:
    if result.returncode is None:
        return "n/a (no process result)"
    return str(result.returncode)


def _relevant_output_lines(result: TestRunResult) -> list[str]:
    """Pick per-test pass/fail lines plus pytest's summary line from stdout."""
    lines = [
        line
        for line in result.stdout.splitlines()
        if re.search(r"\b(PASSED|FAILED|ERROR)\b", line)
    ]
    for line in reversed(result.stdout.splitlines()):
        if re.search(r"\bin\s+\d+(?:\.\d+)?s\b", line):
            lines.append(line.strip("=").strip())
            break
    if not lines:
        tail = result.stdout.strip()
        lines = [tail[-400:] if len(tail) > 400 else tail] if tail else ["(empty)"]
    return lines


def _render_comparison_side(
    container, heading: str, result: TestRunResult, expected_status: str
) -> None:
    """One side of the before/after panel, built only from the returned result."""
    container.markdown(f"### {heading}")
    container.metric("Status", result.status)
    container.metric("Exit code", _exit_code_display(result))
    container.metric("Duration", f"{result.duration_seconds:.2f} s")
    matches = result.status == expected_status
    container.caption(
        f"Expected: `{expected_status}` — "
        + ("**matches**" if matches else "**does not match**")
    )
    with container.expander("Relevant captured output", expanded=True):
        container.code("\n".join(_relevant_output_lines(result)), language="text")


def _render_comparison(buggy_result: TestRunResult, fixed_result: TestRunResult) -> None:
    """Side-by-side before/after comparison plus a status-derived verdict."""
    ui.section(
        "03 · Compare",
        "Before-and-After Comparison",
        "Same harness, same tests, same rules. The only variable is the implementation.",
    )

    col_buggy, col_fixed = st.columns(2)
    _render_comparison_side(
        col_buggy, "Before — Buggy example", buggy_result, _EXPECTED_STATUS[BUGGY_TEST_ID]
    )
    _render_comparison_side(
        col_fixed, "After — Fixed example", fixed_result, _EXPECTED_STATUS[FIXED_TEST_ID]
    )

    st.markdown("**Comparison summary**")

    problems = []
    for label, result in (("buggy", buggy_result), ("fixed", fixed_result)):
        if result.status == "inconclusive" or result.error is not None:
            detail = f": {result.error}" if result.error else ""
            problems.append(f"{label} run is inconclusive{detail}")

    if problems:
        st.warning(
            "**Comparison is INCONCLUSIVE** — at least one run did not produce a "
            "reliable outcome: " + "; ".join(problems) + ". Re-run the examples."
        )
        return

    buggy_ok = buggy_result.status == _EXPECTED_STATUS[BUGGY_TEST_ID]
    fixed_ok = fixed_result.status == _EXPECTED_STATUS[FIXED_TEST_ID]
    if buggy_ok and fixed_ok:
        st.success(
            "Observed outcomes **match expectations**: the buggy example failed "
            "(the intentional bug was reproduced) and the fixed example passed. "
            "This verdict is derived from the runner-returned statuses."
        )
    else:
        details = []
        if not buggy_ok:
            details.append(
                f"buggy example reported `{buggy_result.status}` "
                f"(expected `{_EXPECTED_STATUS[BUGGY_TEST_ID]}`)"
            )
        if not fixed_ok:
            details.append(
                f"fixed example reported `{fixed_result.status}` "
                f"(expected `{_EXPECTED_STATUS[FIXED_TEST_ID]}`)"
            )
        st.error(
            "Observed outcomes **do not match expectations**: " + "; ".join(details)
            + ". Investigate before drawing any conclusion."
        )


def _render_analysis(result: analysis.AnalysisResult) -> None:
    """Render an AnalysisResult, clearly separating AI advice from test evidence."""
    if not result.ok:
        if result.analysis is not None and result.analysis.parse_error:
            st.warning("The AI response could not be parsed into a structured analysis.")
            if result.error:
                st.caption(result.error)
            with st.expander("Raw AI output (for inspection only)", expanded=False):
                st.code(result.analysis.raw or "(empty)", language="text")
        else:
            st.warning(result.error or "Analysis failed.")
        return

    a = result.analysis
    st.success(
        f"Structured analysis produced by **{a.provider}** (model `{a.model}`). "
        "This is **AI-generated advice (hypotheses and suggestions)** — it is not "
        "observed test evidence. The test-run sections above remain the "
        "authoritative record."
    )

    st.markdown("**Problem summary**")
    st.write(a.problem_summary)

    st.markdown("**Likely cause (hypothesis — not confirmed)**")
    st.write(a.likely_cause)

    st.markdown("**Reproduction steps**")
    if a.reproduction_steps:
        st.write("\n".join(f"{i}. {step}" for i, step in enumerate(a.reproduction_steps, 1)))
    else:
        st.write("(none provided)")

    st.markdown("**Suggested test cases**")
    if a.suggested_test_cases:
        for case in a.suggested_test_cases:
            kind = "edge" if case.lower().startswith("edge") else "normal"
            st.write(f"- [{kind}] {case}")
    else:
        st.write("(none provided)")

    st.markdown("**Missing information / clarifying questions**")
    if a.missing_information:
        st.write("\n".join(f"- {q}" for q in a.missing_information))
    else:
        st.write("(none provided)")


def _log_ai_by_key(ai_result):
    """Map observed group keys to AI enrichment objects."""
    if ai_result is None or not ai_result.ok or ai_result.data is None:
        return {}
    return {finding.group_key: finding for finding in ai_result.data.findings}


def _get_log_ai(excerpt: str):
    """Call the AI once per distinct excerpt and reuse it across reruns."""
    digest = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
    cached = st.session_state.get("log_ai_cache")
    if cached is not None and cached.get("digest") == digest:
        return cached["result"]

    with st.spinner("Requesting advisory AI analysis…"):
        result = analysis.analyze_log_excerpt(excerpt)
    st.session_state["log_ai_cache"] = {"digest": digest, "result": result}
    return result


def _render_log_findings(parsed: logs.ParsedLog, ai_result) -> None:
    """Render deterministic findings, with clearly separated AI enrichment."""
    ai_by_key = _log_ai_by_key(ai_result)

    if ai_result is not None:
        if ai_result.ok and ai_result.data is not None:
            st.caption(
                f"AI enrichment by **{ai_result.data.provider}** "
                f"(`{ai_result.data.model}`). Causes and steps below are "
                "**AI hypotheses/suggestions**, not observed log evidence."
            )
        elif ai_result.error:
            st.caption(f"AI enrichment unavailable: {ai_result.error}")

    for group in parsed.groups:
        heading = (
            f"{group.severity.upper()} · {logs.CATEGORY_LABEL.get(group.category, group.category)}"
            f" · {group.signature} (×{group.count})"
        )
        with st.expander(heading, expanded=group.severity in ("critical", "high")):
            col_sev, col_count, col_lines = st.columns(3)
            col_sev.metric("Severity", group.severity)
            col_count.metric("Occurrences", group.count)
            col_lines.metric("Line range", f"{group.first_line}–{group.last_line}")

            st.markdown("**Observed in the log (fact)**")
            st.code(
                "\n".join(f"{line.number}: {line.text}" for line in group.supporting()),
                language="text",
            )
            if group.count > len(group.supporting()):
                st.caption(
                    f"Showing {len(group.supporting())} of {group.count} occurrences."
                )

            finding = ai_by_key.get(group.key)
            if finding is not None:
                st.markdown("**Likely cause (AI hypothesis — not observed fact)**")
                st.write(finding.likely_cause or "(not provided)")
                st.markdown("**Recommended troubleshooting steps (AI suggestions)**")
                if finding.recommended_steps:
                    for step in finding.recommended_steps:
                        st.write(f"- {step}")
                else:
                    st.write("(none provided)")
            elif ai_result is not None and ai_result.ok:
                st.caption(
                    "No AI enrichment matched this group. Treat only the observed "
                    "log lines as evidence."
                )


def _build_log_report(parsed: logs.ParsedLog, ai_result) -> str:
    """Build a downloadable Markdown report from facts and advisory AI content."""
    ai_by_key = _log_ai_by_key(ai_result)
    out: list[str] = [
        "# ReproKit Log Analysis Report",
        "",
        f"- File: `{parsed.filename or 'uploaded log'}`",
        f"- Size: {parsed.total_bytes:,} bytes",
        f"- Lines analyzed: {parsed.total_lines:,}",
        f"- Redactions applied: {parsed.redaction_count}",
        f"- Truncated: {'yes' if parsed.truncated else 'no'}",
        "",
        "## Incident summary (observed facts)",
        "",
        parsed.summary(),
        "",
    ]

    if parsed.groups:
        out += ["## Findings (observed)", ""]
    for group in parsed.groups:
        out += [
            f"### {group.severity.upper()} — "
            f"{logs.CATEGORY_LABEL.get(group.category, group.category)}: {group.signature}",
            "",
            f"- Occurrences: {group.count}",
            f"- Lines: {group.first_line}–{group.last_line}",
            "",
            "```text",
            *[f"{line.number}: {line.text}" for line in group.supporting()],
            "```",
            "",
        ]
        finding = ai_by_key.get(group.key)
        if finding is not None:
            out += [
                "**Likely cause (AI hypothesis — not observed fact):** "
                + (finding.likely_cause or "(not provided)"),
                "",
                "**Recommended troubleshooting steps (AI suggestions):**",
                *[f"- {step}" for step in finding.recommended_steps],
                "",
            ]

    if ai_result is not None and ai_result.ok and ai_result.data is not None:
        out += [
            "## AI incident summary (advisory, not evidence)",
            "",
            ai_result.data.incident_summary or "(none provided)",
            "",
        ]
        if ai_result.data.missing_information:
            out += ["### Missing information (AI)", ""]
            out += [f"- {item}" for item in ai_result.data.missing_information]
            out.append("")

    out += [
        "---",
        "AI-generated content is advisory. Only the captured log lines are "
        "observed evidence.",
        "",
    ]
    return "\n".join(out)


def _render_log_analysis() -> None:
    """Upload → redact/group → optional AI enrichment → download."""
    ui.section(
        "04 · Logs",
        "AI Log Analysis",
        "Deterministic grouping first. AI contributes hypotheses, never evidence.",
    )
    st.caption(
        f"Upload a `.log` or `.txt` file (max {logs.MAX_FILE_BYTES // (1024 * 1024)} MB, "
        f"first {logs.MAX_LINES:,} lines). Errors, exceptions, HTTP 4xx/5xx, and "
        "timeouts are grouped deterministically; secrets are redacted before "
        "display or any AI call."
    )

    status = analysis.provider_status()
    uploaded = st.file_uploader(
        "Log file", type=["log", "txt"], accept_multiple_files=False, key="log_upload"
    )

    if uploaded is None:
        st.info(
            "Upload a log file to begin. Deterministic grouping works without "
            "an AI key."
        )
        return

    try:
        parsed = logs.parse_log(uploaded.getvalue(), uploaded.name)
    except logs.LogInputError as exc:
        st.error(f"Could not analyze this file: {exc}")
        return

    for warning in parsed.warnings:
        st.warning(warning)

    st.success(
        f"Parsed {parsed.total_lines:,} lines · "
        f"{len(parsed.groups)} error group(s) · "
        f"{parsed.redaction_count} redaction(s)"
    )

    st.markdown("**Incident summary (observed facts)**")
    st.write(parsed.summary())

    if not parsed.groups:
        st.info(
            "No known error, exception, HTTP 4xx/5xx, or timeout patterns were "
            "found. Nothing is inferred beyond the uploaded lines."
        )
        return

    excerpt = logs.build_ai_excerpt(parsed.groups)
    if status.available:
        ai_result = _get_log_ai(excerpt)
    else:
        ai_result = None
        st.caption(f"AI enrichment unavailable: {status.reason}")

    st.divider()
    _render_log_findings(parsed, ai_result)

    if ai_result is not None and ai_result.ok and ai_result.data is not None:
        st.markdown("**AI incident summary (advisory — not observed evidence)**")
        st.write(ai_result.data.incident_summary or "(none provided)")
        if ai_result.data.missing_information:
            with st.expander("Missing information / clarifying questions (AI)"):
                for item in ai_result.data.missing_information:
                    st.write(f"- {item}")

    st.divider()
    st.download_button(
        "Download report (Markdown)",
        data=_build_log_report(parsed, ai_result),
        file_name="reprokit-log-report.md",
        mime="text/markdown",
        use_container_width=False,
    )


def render_logic_mode() -> None:
    """Logic Mode: the existing evidence-driven dashboard, unchanged."""
    ui.hero(
        "Logic Mode — Evidence",
        "ReproKit",
        "Evidence-Driven Bug Testing",
        "Controlled, allowlisted pytest runs against a known-buggy fixture and a "
        "fixed reference implementation. Every result below is real captured "
        "evidence — command, exit code, duration, stdout, stderr — never a "
        "hard-coded claim.",
        chips=("Allowlisted test IDs", "Real pytest evidence", "No arbitrary input"),
        terminal='pytest -q <span class="rk-bad">1 failed</span>, '
        '<span class="rk-ok">2 passed</span> in 0.07s',
    )

    ui.section(
        "01 · The proof",
        "Run the proof",
        "Only two predefined identifiers exist. Nothing here accepts a pasted "
        "command or an arbitrary file path.",
    )

    col_buggy, col_fixed, col_both = st.columns(3)
    if col_buggy.button("Run Buggy Example", type="primary", use_container_width=True):
        _run_and_show(BUGGY_TEST_ID)
    if col_fixed.button("Run Fixed Example", type="primary", use_container_width=True):
        _run_and_show(FIXED_TEST_ID)
    if col_both.button("Run Both Examples", type="primary", use_container_width=True):
        # Sequential before/after order: buggy first, then fixed.
        _run_and_show(BUGGY_TEST_ID)
        _run_and_show(FIXED_TEST_ID)

    for test_id in (BUGGY_TEST_ID, FIXED_TEST_ID):
        result = st.session_state.get(f"result_{test_id}")
        if result is not None and result.test_id == test_id:
            st.divider()
            st.markdown(_EXPECTED_LABEL[test_id])
            _render_result(result, _EXPECTED_STATUS[test_id])

    buggy_result = st.session_state.get(f"result_{BUGGY_TEST_ID}")
    fixed_result = st.session_state.get(f"result_{FIXED_TEST_ID}")
    if (
        buggy_result is not None
        and buggy_result.test_id == BUGGY_TEST_ID
        and fixed_result is not None
        and fixed_result.test_id == FIXED_TEST_ID
    ):
        st.divider()
        _render_comparison(buggy_result, fixed_result)

    # --- AI Log Analysis ----------------------------------------------------
    st.divider()
    _render_log_analysis()

    # --- Bug Report Analysis (Step 6A) --------------------------------------
    st.divider()
    ui.section(
        "05 · Reports",
        "Bug Report Analysis",
        "Structured advice, clearly separated from what the tests actually observed.",
    )
    st.markdown(
        "Paste a bug report below. If an AI provider is configured, ReproKit "
        "asks it for a **structured analysis**. AI output is **advisory** — "
        "it is *not* observed test evidence, and it never selects test IDs, "
        "builds commands, executes code, or runs the tests. The captured "
        "test-run evidence above is the authoritative record."
    )

    status = analysis.provider_status()
    if not status.available:
        st.info("AI analysis is currently **unavailable**: " + status.reason)
    else:
        st.caption(f"Active provider: **{status.provider}** (model: `{status.model}`)")

    report = st.text_area(
        "Bug report",
        height=200,
        placeholder="Describe the bug you observed (symptoms, where it happens, expected vs actual)…",
    )
    if st.button("Analyze Bug Report", type="primary"):
        st.session_state["analysis_result"] = analysis.analyze_bug_report(report)

    analysis_result = st.session_state.get("analysis_result")
    if analysis_result is not None:
        st.divider()
        _render_analysis(analysis_result)

    ui.footer_quote()


def _render_workflow_steps(current: str) -> None:
    """Compact, polished progress display for the Magic Mode workflow."""
    done_until = [key for key, _ in magic.WORKFLOW_STEPS]
    current_index = done_until.index(current) if current in done_until else 0

    ui.workflow_steps(magic.WORKFLOW_STEPS, current_index)


def _magic_session() -> magic.MagicSession:
    session = st.session_state.get("magic_session")
    if not isinstance(session, magic.MagicSession):
        session = magic.MagicSession()
        st.session_state["magic_session"] = session
    return session


def _render_magic_files(files: list[magic.MagicFile]) -> None:
    st.markdown(f"**Uploaded files ({len(files)})** — originals preserved, never modified")
    for item in files:
        with st.container(border=True):
            col_name, col_kind, col_size, col_lines = st.columns([3, 1, 1, 1])
            col_name.markdown(f"`{item.name}`")
            col_kind.markdown(f"**{item.kind}**")
            col_size.caption(f"{item.size_bytes:,} bytes")
            col_lines.caption(f"{item.line_count:,} lines")
            st.caption(f"SHA-256 `{item.sha256[:16]}…` · read-only snapshot")
            for warning in item.warnings:
                st.warning(warning)
            with st.expander("Preview (redacted, bounded)"):
                st.code(item.preview() or "(empty)", language="text")


def render_magic_mode() -> None:
    """Magic Mode (Stage 2): grounded issues + private-by-default follow-ups."""
    ui.hero(
        "Magic Mode — Assist",
        "Magic Mode",
        "AI-Assisted Issue Review",
        "Upload code, logs, and notes. Deterministic grouping comes first, every "
        "finding is grounded in real file lines, and the model asks you targeted "
        "questions before proposing anything.",
        chips=("Grounding required", "Private by default", "Nothing executed"),
        terminal='upload → detect → ask → <span class="rk-ok">propose</span> → verify',
    )

    session = _magic_session()
    _render_workflow_steps(session.step)

    st.divider()
    ui.section(
        "01 · Intake",
        "1 · Upload files",
        "Snapshots are read-only, hashed, and never executed.",
    )
    st.caption(
        f"Accepted: {', '.join(sorted(magic.ALLOWED_EXTENSIONS))} · "
        f"max {magic.MAX_FILES} files, {magic.MAX_FILE_BYTES // 1024:,} KB each, "
        f"{magic.MAX_TOTAL_BYTES // (1024 * 1024)} MB total. Uploaded code is "
        "**never executed** and files are kept as read-only snapshots."
    )

    uploads = st.file_uploader(
        "Code, log, and text files",
        type=[ext.lstrip(".") for ext in sorted(magic.ALLOWED_EXTENSIONS)],
        accept_multiple_files=True,
        key="magic_upload",
    )

    if uploads:
        try:
            files = magic.ingest_files([(u.name, u.getvalue()) for u in uploads])
        except magic.MagicInputError as exc:
            st.error(f"Upload rejected: {exc}")
            return
        if files != session.files:
            session.files = files
            session.set_issues([])
            session.analysis_note = ""
        if session.step == "upload":
            session.set_step("group")
    else:
        files = session.files

    if files:
        _render_magic_files(files)
    else:
        st.info("Upload at least one file to begin.")
        return

    st.divider()
    ui.section(
        "02 · Grounding",
        "2 · Detected issues",
        "Every evidence reference is resolved against an uploaded line.",
    )
    status = analysis.provider_status()
    col_detect, col_status = st.columns([1, 3])
    if col_detect.button("Detect issues", type="primary", use_container_width=True):
        _run_magic_analysis(session)
    if status.available:
        col_status.caption(
            f"AI provider: **{status.provider}** (`{status.model}`) · "
            "AI explanations are hypotheses, not observed facts."
        )
    else:
        col_status.caption(f"AI review unavailable: {status.reason}")

    if session.analysis_note:
        st.caption(session.analysis_note)

    if not session.issues:
        st.info(
            "No issues yet. Click **Detect issues**. Log files are grouped "
            "deterministically; code and text files are reviewed by AI when a key "
            "is configured."
        )
        _render_magic_privacy(session)
        return

    issue_labels = {
        issue.id: (
            f"[{issue.severity.upper()}] {issue.title} · {len(issue.evidence)} evidence line(s)"
        )
        for issue in session.issues
    }
    selected_id = st.selectbox(
        "Select an issue",
        options=[issue.id for issue in session.issues],
        index=[issue.id for issue in session.issues].index(
            session.selected_issue_id or session.issues[0].id
        ),
        format_func=lambda issue_id: issue_labels[issue_id],
        key="magic_issue_select",
    )
    issue = session.select_issue(selected_id)

    col_sev, col_src, col_ev = st.columns(3)
    col_sev.metric("Severity", issue.severity)
    col_src.metric("Severity source", issue.severity_source)
    col_ev.metric("Evidence lines", len(issue.evidence))

    st.markdown("**Observed evidence (exact uploaded lines, redacted for display)**")
    st.code(
        "\n".join(f"{item.label}: {item.display_text}" for item in issue.evidence),
        language="text",
    )

    if issue.is_ai_hypothesis:
        st.markdown("**Likely cause (AI hypothesis — not observed fact)**")
        st.write(issue.hypothesis)
    else:
        st.caption(
            "No AI hypothesis for this issue. Only the evidence lines above are "
            "observed."
        )

    questions = session.questions_for(issue.id)
    st.markdown(f"**Follow-up questions ({len(questions)})**")
    if not questions:
        st.caption("No follow-up questions for this issue.")
    for question in questions:
        _render_magic_question(session, question)

    if session.questions:
        shareable = session.shareable_answers()
        st.caption(
            f"{len(shareable)} answer(s) shared with AI · "
            f"{session.private_answer_count()} kept private · "
            "private and skipped answers are never included in AI prompts."
        )
        if st.button("Refresh analysis with shared answers", use_container_width=False):
            _run_magic_analysis(session)
            st.rerun()

    st.divider()
    ui.section(
        "03 · Proposal",
        "3 · Fix proposal",
        "Reviewable diff only. Originals stay untouched.",
    )
    st.caption(
        "The AI proposes a unified diff. It is validated against your exact uploaded "
        "snapshots before anything is shown; nothing is applied to your files. "
        "Uploaded code is **not executed**, and the allowlisted fixture tests cannot "
        "verify arbitrary uploaded projects."
    )
    if st.button("Propose fix", type="primary", use_container_width=False):
        _run_propose_fix(session, issue)

    if session.fix_proposal is not None and session.fix_proposal.issue_id == issue.id:
        _render_fix_proposal(session, session.fix_proposal)

    _render_magic_privacy(session)


def _run_propose_fix(session: magic.MagicSession, issue: magic.Issue) -> None:
    """Request, parse, and validate a fix proposal. Never applies anything."""
    session.fix_proposal = None
    session.verification = None
    status = analysis.provider_status()
    if not status.available:
        session.analysis_note = f"Fix proposal unavailable: {status.reason}"
        return

    context = magic.build_fix_context(
        session.files, issue, session.shareable_answers()
    )
    result = analysis.propose_fix(
        context, shared_answers=session.shareable_answers()
    )
    if not result.ok or result.data is None:
        session.analysis_note = f"Fix proposal failed: {result.error}"
        return

    draft = result.data
    sources = {item.name: item.text for item in session.files if item.kind != "log"}
    validation = patch.validate_diff(draft.diff, sources)
    proposal = magic.FixProposal(
        issue_id=issue.id,
        explanation=draft.explanation,
        diff=validation.canonical_diff if validation.ok else "",
        files_changed=draft.files_changed,
        expected_behavior=draft.expected_behavior,
        tests_needed=draft.tests_needed,
        limitations=draft.limitations,
        validation=validation,
        provider=draft.provider,
        model=draft.model,
    )
    if validation.ok:
        session.analysis_note = (
            "Fix proposal validated against your snapshots. It is not verified by tests."
        )
    else:
        proposal.rejection_reason = magic.rejection_reason_for(proposal)
        session.analysis_note = f"Fix proposal rejected: {proposal.rejection_reason}"
    session.fix_proposal = proposal


def _render_fix_proposal(session: magic.MagicSession, proposal: magic.FixProposal) -> None:
    report = session.verification
    if report is None:
        st.warning(
            "**Proposed — not verified.** This diff was generated by AI and validated "
            "for safety/application only. No tests have run against it, and uploaded "
            "code is never executed.",
            icon="⚠️",
        )
    elif report.overall_status == verify.STATUS_VERIFIED:
        st.success(
            "**Statically verified — runtime behavior NOT verified.** Integrity, "
            "workspace, and syntax checks passed, and ReproKit's own fixtures passed. "
            "Uploaded code was not executed.",
            icon="✅",
        )
    elif report.overall_status == verify.STATUS_FAILED:
        st.error(
            "**Verification failed.** One or more approved checks did not pass; "
            "see the evidence below.",
            icon="❌",
        )
    else:
        st.warning(
            "**Verification inconclusive.** A check could not complete, so this "
            "proposal is not verified.",
            icon="⚠️",
        )

    if not proposal.validation.ok:
        reason = proposal.rejection_reason or magic.rejection_reason_for(proposal)
        uploaded = ", ".join(sorted(item.name for item in session.files)) or "(none)"
        st.error(f"Proposal rejected before display: {reason}")
        st.caption(
            f"Files that a diff may modify: `{uploaded}`. The AI diff referenced a "
            "different filename or an unsafe change; nothing was staged."
        )
        st.caption(
            "Re-run Propose fix. Diffs must use the exact uploaded filename; "
            "ReproKit never renames files to make a proposal fit."
        )
        return

    st.markdown("**Explanation**")
    st.write(proposal.explanation or "(none provided)")

    st.markdown("**Files and lines intended to change**")
    if proposal.files_changed:
        for path, lines in proposal.files_changed:
            rendered = ", ".join(str(n) for n in lines[:20]) or "(not specified)"
            st.write(f"- `{path}` — lines {rendered}")
    else:
        st.write("(not specified)")

    changed_paths = {patch_file.path for patch_file in proposal.validation.files}
    for path, _ in proposal.files_changed:
        if path not in changed_paths:
            st.caption(
                f"Note: the AI listed `{path}`, but the validated diff does not "
                "change it. The diff is authoritative."
            )

    st.markdown("**Expected behavior after the fix (AI claim — unverified)**")
    st.write(proposal.expected_behavior or "(none provided)")

    st.markdown("**Tests needed (AI suggestions — not run)**")
    if proposal.tests_needed:
        for test in proposal.tests_needed:
            st.write(f"- {test}")
    else:
        st.write("(none provided)")

    st.markdown("**Limitations / uncertainty (AI)**")
    if proposal.limitations:
        for item in proposal.limitations:
            st.write(f"- {item}")
    else:
        st.write("(none provided)")

    st.markdown("**Reviewable diff (validated, canonical)**")
    st.code(proposal.diff, language="diff")

    st.markdown("**Before / after**")
    for file_patch in proposal.validation.files:
        with st.expander(f"`{file_patch.path}` · +{file_patch.added} / -{file_patch.removed}"):
            before_col, after_col = st.columns(2)
            before_col.markdown("**Before (uploaded snapshot)**")
            before_col.code(file_patch.before or "(empty)", language="text")
            after_col.markdown("**After (proposed)**")
            after_col.code(file_patch.after or "(empty)", language="text")

    if proposal.workspace:
        current = {item.name: item.sha256 for item in session.files}
        original = {
            item.name: hashlib.sha256(item.text.encode("utf-8")).hexdigest()
            for item in session.files
        }
        st.success(
            f"Staged in a temporary workspace: `{proposal.workspace}`. "
            f"Originals unchanged: {'verified' if current == original else 'MISMATCH'}. "
            "Nothing was executed."
        )
        _render_verification(session, proposal)
    else:
        if st.button(
            "Stage in temporary workspace (no execution)",
            use_container_width=False,
        ):
            try:
                root = tempfile.mkdtemp(prefix="reprokit-fix-")
                magic.stage_fix(session, proposal, root)
                st.rerun()
            except magic.MagicInputError as exc:
                st.error(f"Could not stage the proposal: {exc}")


def _render_verification(session: magic.MagicSession, proposal: magic.FixProposal) -> None:
    """Approved-check controls, evidence, and report download."""
    st.divider()
    st.markdown("**Verify staged proposal (approved checks)**")
    st.caption(
        "Only fixed checks from ReproKit's registry run. Uploaded code is never "
        "executed, and the fixture regression validates ReproKit's own fixtures — "
        "not your uploaded project."
    )

    selected: list[str] = []
    for check_id in verify.DEFAULT_CHECKS:
        if st.checkbox(
            verify.CHECK_LABELS[check_id],
            value=True,
            key=f"verify_check_{check_id}",
            help=verify.CHECK_SCOPES[check_id],
        ):
            selected.append(check_id)

    timeout = st.selectbox(
        "Fixture check timeout",
        options=list(verify.ALLOWED_TIMEOUTS),
        index=1,
        format_func=lambda value: f"{int(value)} seconds",
        key="verify_timeout",
    )

    if st.button("Run approved checks", type="primary", use_container_width=False):
        try:
            session.verification = verify.verify_proposal(
                session, proposal, check_ids=selected, timeout=timeout
            )
        except verify.VerificationInputError as exc:
            st.error(f"Verification blocked: {exc}")
        else:
            st.rerun()

    report = session.verification
    if report is None:
        return

    checks = report.checks
    passed = sum(1 for check in checks if check.status == "passed")
    col_a, col_b, col_c = st.columns(3)
    col_a.metric("Overall status", report.overall_status)
    col_b.metric("Checks passed", f"{passed}/{len(checks)}")
    col_c.metric("Verified at (UTC)", report.verified_at)

    st.caption(report.scope_statement)

    for check in checks:
        icon = {"passed": "✅", "failed": "❌"}.get(check.status, "⚠️")
        with st.expander(
            f"{icon} {check.label} — `{check.status}`",
            expanded=check.status != "passed",
        ):
            st.markdown(f"**Check:** `{check.check}`")
            st.caption(f"Scope: {check.scope}")
            exit_status = (
                check.exit_status if check.exit_status is not None else "n/a"
            )
            st.markdown(
                f"**Exit status:** `{exit_status}` · "
                f"**Duration:** `{check.duration_seconds:.3f} s`"
            )
            if check.detail:
                st.caption(check.detail)
            st.code(check.output or "(no output)", language="text")

    st.download_button(
        "Download verification report (Markdown)",
        data=report.to_markdown(),
        file_name="reprokit-verification.md",
        mime="text/markdown",
        use_container_width=False,
    )


def _run_magic_analysis(session: magic.MagicSession) -> None:
    """Deterministic log issues first, then bounded AI review of code/text."""
    log_issues = magic.issues_from_logs(session.files)
    ai_issues: list[magic.Issue] = []
    note = ""

    has_ai_targets = any(item.kind in ("code", "text") for item in session.files)
    status = analysis.provider_status()

    if not has_ai_targets:
        note = (
            "Log files were grouped deterministically. Upload code or text files "
            "for AI-assisted review."
        )
    elif not status.available:
        note = f"AI review unavailable: {status.reason}"
    else:
        excerpt = magic.build_issue_excerpt(session.files)
        result = analysis.analyze_magic_issues(
            excerpt, shared_answers=session.shareable_answers()
        )
        if result.ok and result.data is not None:
            ai_issues = magic.issues_from_ai(
                result.data.issues, session.files, existing=log_issues
            )
            note = (
                f"AI review by {result.data.provider} (`{result.data.model}`) — "
                "hypotheses only. Ungrounded references were discarded."
            )
        else:
            note = f"AI review failed: {result.error}"

    session.set_issues(list(log_issues) + ai_issues)
    session.analysis_note = note


def _render_magic_question(session: magic.MagicSession, question: magic.Question) -> None:
    """Per-question Share / Private / Skip form (private by default)."""
    existing = session.answers.get(question.id)
    options = ["Share with AI", "Keep private", "Skip"]
    reverse = {"shared": "Share with AI", "private": "Keep private", "skipped": "Skip"}
    current = reverse.get(existing.visibility if existing else "skipped", "Skip")

    with st.form(key=f"magic_answer_{question.id}"):
        st.markdown(f"**{question.text}**")
        if question.related_file:
            st.caption(f"Related file: `{question.related_file}`")
        choice = st.radio(
            "Visibility",
            options=options,
            index=options.index(current),
            horizontal=True,
            key=f"magic_vis_{question.id}",
        )
        text = st.text_area(
            "Your answer",
            value=existing.text if existing else "",
            height=80,
            key=f"magic_text_{question.id}",
        )
        if st.form_submit_button("Save answer"):
            visibility = {
                "Share with AI": "shared",
                "Keep private": "private",
                "Skip": "skipped",
            }[choice]
            session.answer(question.id, text, visibility)
            st.success(
                "Saved. "
                + (
                    "This answer may be sent to the AI."
                    if visibility == "shared" and text.strip()
                    else "This answer will not be sent to the AI."
                )
            )


def _render_magic_privacy(session: magic.MagicSession) -> None:
    st.divider()
    ui.section(
        "04 · Trust",
        "Privacy & safety",
        "Share, keep private, or skip — the AI only ever sees shared answers.",
    )
    col_priv, col_safe = st.columns(2)
    with col_priv:
        st.markdown("**Your answers stay yours**")
        st.markdown(
            "- **Share** — may be included in the AI prompt.\n"
            "- **Private** — stored locally for this session, **never sent to AI**.\n"
            "- **Skip** — not used at all.\n"
        )
        st.caption(
            f"Private answers this session: {session.private_answer_count()} · "
            f"Shareable answers: {len(session.shareable_answers())}"
        )
    with col_safe:
        st.markdown("**Hard boundaries**")
        st.markdown(
            "- Uploaded code is **never executed** and no shell commands are run.\n"
            "- Every evidence reference is checked against a real uploaded line; "
            "invalid references are discarded.\n"
            "- Originals are preserved; no patches are generated in this stage.\n"
            "- Secrets are redacted before display and before any AI call."
        )
    ui.footer_quote()


def render_intro() -> None:
    """Landing page: what ReproKit is for, who feels the pain, and why it is different."""
    ui.landing_hero(
        kicker="Welcome to the studio",
        name="Repro",
        emphasis="Kit",
        subname="The Bug Reproduction Studio",
        purpose_html=(
            "ReproKit is a <strong>bug-reproduction studio</strong> for people who are "
            "tired of guessing. Upload code and logs, reproduce the failure with real "
            "test evidence, and review AI suggestions without ever confusing a "
            "plausible story with a proven result."
        ),
        chips=("Reproduce", "Review", "Decide"),
    )

    ui.section(
        "The swarm",
        "Every bug starts as a blur",
        "Twelve scattered symptoms, one shape hiding underneath. Release them and "
        "watch them settle into the name.",
    )
    ui.swarm(bool(st.session_state.get("rk_swarm_open")))
    release_label = (
        "Release the swarm again"
        if st.session_state.get("rk_swarm_open")
        else "Release the swarm"
    )
    if st.button(release_label, type="primary", use_container_width=False):
        st.session_state["rk_swarm_open"] = not st.session_state.get("rk_swarm_open")
        st.rerun()

    ui.section(
        "Why it exists",
        "Three ways bugs waste your day",
        "Every one of these starts with the same mistake: treating a hunch as proof.",
    )
    ui.pillars(
        [
            (
                "01 · REPRODUCE",
                "It worked on my machine",
                "A failure you cannot re-run is a rumour. ReproKit turns it into a "
                "command, an exit code, and captured output you can hand to someone.",
            ),
            (
                "02 · REVIEW",
                "The log says everything and nothing",
                "Thousands of lines, one buried exception. Evidence gets grouped, "
                "redacted, and shown next to the exact line that produced it.",
            ),
            (
                "03 · DECIDE",
                "The fix that wasn't",
                "AI can write a confident patch for the wrong file. Here proposals are "
                "validated against your real snapshot and labelled unverified until "
                "checks actually run.",
            ),
        ]
    )

    ui.section(
        "From the trenches",
        "What developers actually say",
        "Anonymous voices from everyday debugging. No heroics, just the loop we all know.",
    )
    ui.struggles(
        [
            (
                "Spent two days on a bug that only appeared because one config line "
                "differed between staging and production.",
                "Backend developer · 6 years in",
            ),
            (
                "The AI gave me a beautiful patch. It renamed a file, broke the import "
                "graph, and was completely confident about it.",
                "Platform engineer · 4 years in",
            ),
            (
                "Best debugging tool I ever had was a test that failed the same way "
                "twice in a row.",
                "QA engineer · 9 years in",
            ),
        ]
    )

    ui.voices(
        (
            "Programs must be written for people to read, and only incidentally for "
            "machines to execute.",
            "Harold Abelson & Gerald Jay Sussman, Structure and Interpretation of "
            "Computer Programs",
        ),
        (
            "A hypothesis is a question with good posture. Evidence is the answer.",
            "ReproKit principle",
        ),
    )

    ui.section(
        "How it works",
        "Two modes, one rule",
        "The rule is simple: observed evidence outranks any opinion — human or model.",
    )
    ui.pillars(
        [
            (
                "LOGIC MODE",
                "Prove it",
                "Allowlisted pytest runs, before/after comparison, and log analysis. "
                "Real commands, real exit codes, nothing hard-coded.",
            ),
            (
                "MAGIC MODE",
                "Investigate it",
                "Upload your own files, get grounded findings, answer only what you "
                "choose to share, then review a diff — never auto-applied.",
            ),
            (
                "THE RULE",
                "Never pretend",
                "Inconclusive stays inconclusive. Static verification never claims "
                "runtime success. Secrets are redacted before anything is sent.",
            ),
        ]
    )

    st.write("")
    if st.button("Enter the studio", type="primary", use_container_width=False):
        st.session_state["rk_entered"] = True
        st.rerun()
    ui.cta_note(
        [
            ("Start here:", "Logic Mode runs the proven demo end to end."),
            ("No setup:", "works without an AI key; add one for analysis."),
            ("Safe by default:", "uploaded code is never executed."),
        ]
    )
    ui.footer_quote()


def main() -> None:
    st.set_page_config(
        page_title="ReproKit — The Bug Reproduction Studio",
        page_icon="◈",
        layout="wide",
    )

    dark = bool(st.session_state.get("rk_dark", False))
    ui.inject(dark)

    header_left, header_right = st.columns([4.2, 1.5])
    with header_left:
        st.markdown(
            '<div style="font-family:Iowan Old Style,Palatino,Georgia,serif;'
            'font-size:1.05rem;color:var(--rk-muted);">ReproKit '
            '<span style="color:var(--rk-accent);">·</span> '
            "The Bug Reproduction Studio</div>",
            unsafe_allow_html=True,
        )
    with header_right:
        new_dark = ui.lamp_control(dark, key="lamp_toggle")
    if new_dark != dark:
        st.session_state["rk_dark"] = new_dark
        st.rerun()

    if not st.session_state.get("rk_entered"):
        render_intro()
        return

    nav_left, nav_right = st.columns([5.4, 1])
    if nav_right.button("Home", use_container_width=True):
        st.session_state["rk_entered"] = False
        st.rerun()

    mode = st.segmented_control(
        "Mode",
        options=["Logic Mode", "Magic Mode"],
        default="Logic Mode",
        key="app_mode",
        label_visibility="collapsed",
        width="stretch",
    )
    st.caption(
        "**Logic Mode** — real pytest evidence, before/after comparison, AI log "
        "analysis. **Magic Mode** — AI-assisted workflow for your own files."
    )

    if mode == "Magic Mode":
        render_magic_mode()
    else:
        render_logic_mode()


if __name__ == "__main__":
    main()
