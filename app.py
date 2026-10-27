"""ReproKit dashboard (Streamlit entry point).

Launch with:
    streamlit run app.py
"""

import hashlib
import re
import tempfile

import streamlit as st

from reprokit import analysis, logs, magic, patch, studio, ui, verify
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
            col_kind.markdown(f"**{item.format}** · {item.kind}")
            col_size.caption(f"{item.size_bytes:,} bytes")
            col_lines.caption(f"{item.line_count:,} lines")
            st.caption(f"SHA-256 `{item.sha256[:16]}…` · read-only snapshot")
            for warning in item.warnings:
                st.warning(warning)
            with st.expander("Preview (redacted, bounded)"):
                st.code(item.preview() or "(empty)", language="text")


def render_magic_mode() -> None:
    """Magic Mode: automatic analysis, grounded fixes, and safe verification."""
    ui.hero(
        "Magic Mode — Assist",
        "Magic Mode",
        "AI-Assisted Issue Review",
        "Upload supported source, configuration, logs, or text. ReproKit analyzes "
        "automatically, grounds findings in real file lines, and proceeds with "
        "best-effort fixes without requiring follow-up answers.",
        chips=("Grounding required", "Automatic analysis", "Nothing executed"),
        terminal='upload → analyze → evidence → <span class="rk-ok">fix</span> → verify',
    )

    session = _magic_session()
    _render_workflow_steps(session.step)

    st.divider()
    ui.section(
        "01 · Intake",
        "1 · Upload files",
        "Snapshots are read-only and hashed; supported text files are analyzed automatically.",
    )
    st.caption(
        f"Accepted: {', '.join(sorted(magic.ALLOWED_EXTENSIONS))} · "
        f"max {magic.MAX_FILES} files, {magic.MAX_FILE_BYTES // 1024:,} KB each, "
        f"{magic.MAX_TOTAL_BYTES // (1024 * 1024)} MB total. Uploaded code is "
        "**never executed** and files are kept as read-only snapshots."
    )

    uploads = st.file_uploader(
        "Source, configuration, log, and text files",
        accept_multiple_files=True,
        key="magic_upload",
    )

    if uploads:
        try:
            files = magic.ingest_files([(u.name, u.getvalue()) for u in uploads])
        except magic.MagicInputError as exc:
            st.error(f"Upload rejected: {exc}")
            return
        files_changed = files != session.files
        if files_changed:
            session.files = files
            session.set_issues([])
            session.analysis_note = ""
            _clear_magic_proposal_state(session)
            _run_magic_analysis(session)
        if session.step == "upload":
            session.set_step("analyze")
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
        "Analysis runs on upload; findings cite uploaded lines. Follow-up questions are optional.",
    )
    status = analysis.provider_status()
    col_detect, col_status = st.columns([1, 3])
    if col_detect.button("Re-analyze files", type="primary", use_container_width=True):
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
            "No actionable findings were detected. Static checks do not prove "
            "general logic correctness; AI review is used when configured."
        )
        report = studio.build_findings_report(
            files=session.files,
            issues=[],
            notes=session.notes + ([session.analysis_note] if session.analysis_note else []),
        )
        st.download_button(
            "Download findings report (Markdown)",
            data=report,
            file_name="reprokit-analysis-report.md",
            mime="text/markdown",
            key="download_magic_empty_findings_report",
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

    col_sev, col_src, col_ev, col_conf = st.columns(4)
    col_sev.metric("Severity", issue.severity)
    col_src.metric("Severity source", issue.severity_source)
    col_ev.metric("Evidence lines", len(issue.evidence))
    col_conf.metric("Confidence", f"{issue.confidence:.0%}")
    st.caption(f"Finding verification status: `{issue.verification_status}`")

    st.markdown("**Observed evidence (exact uploaded lines, redacted for display)**")
    st.code(
        "\n".join(f"{item.label}: {item.display_text}" for item in issue.evidence),
        language="text",
    )
    if issue.observed_problem:
        st.markdown("**Observed problem**")
        st.write(issue.observed_problem)
    if issue.why_it_matters:
        st.markdown("**Why it matters**")
        st.write(issue.why_it_matters)
    if issue.recommendations:
        recommendation_heading = (
            "Actionable next steps (AI suggestions)"
            if issue.recommendations_source == "ai"
            else "Remediation guidance (not a confirmed cause)"
        )
        st.markdown(f"**{recommendation_heading}**")
        for recommendation in issue.recommendations:
            st.write(f"- {recommendation}")
    if issue.proposed_correction:
        st.markdown("**Proposed correction**")
        st.code(issue.proposed_correction, language="text")
    for assumption in issue.assumptions:
        st.caption(f"Assumption: {assumption}")

    report = studio.build_findings_report(
        files=session.files,
        issues=session.issues,
        notes=session.notes + ([session.analysis_note] if session.analysis_note else []),
        proposal=session.fix_proposal,
        corrected=session.corrected_files,
        verification=session.verification,
    )
    st.download_button(
        "Download findings report (Markdown)",
        data=report,
        file_name="reprokit-analysis-report.md",
        mime="text/markdown",
        key="download_magic_findings_report",
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
        "A proposed unified diff is validated against your exact uploaded "
        "snapshots before anything is shown; nothing is applied to your files. "
        "Uploaded code is **not executed**, and the allowlisted fixture tests cannot "
        "verify arbitrary uploaded projects."
    )
    if issue.category == "log":
        st.info(
            "This finding is based on logs. ReproKit will not invent or modify "
            "application source that was not directly implicated by evidence."
        )
    elif st.button("Propose fix", type="primary", use_container_width=False):
        _run_propose_fix(session, issue)

    if session.fix_proposal is not None and session.fix_proposal.issue_id == issue.id:
        _render_fix_proposal(session, issue, session.fix_proposal)

    _render_magic_privacy(session)


def _run_propose_fix(session: magic.MagicSession, issue: magic.Issue) -> None:
    """Request, parse, and validate a fix proposal. Never applies anything."""
    session.fix_proposal = None
    session.verification = None
    session.regression = None
    session.corrected_files = []
    sources = {item.name: item.text for item in session.files if item.kind != "log"}
    if not sources or not any(
        evidence.file in sources for evidence in issue.evidence
    ):
        session.analysis_note = (
            "No source or configuration file supports a safe correction. "
            "Log-only findings receive remediation guidance, not invented source fixes."
        )
        return

    if issue.fix_diff:
        validation = patch.validate_diff(issue.fix_diff, sources)
        proposal = magic.FixProposal(
            issue_id=issue.id,
            explanation=(
                "Trim surrounding whitespace before testing for an empty value. "
                "This deterministic correction assumes whitespace-only input is invalid."
            ),
            diff=validation.canonical_diff if validation.ok else "",
            files_changed=[(item.path, [issue.evidence[0].line]) for item in validation.files],
            expected_behavior="Whitespace-only strings are rejected.",
            tests_needed=["Check that ordinary text is accepted and whitespace-only text is rejected."],
            limitations=[
                "The intended input policy is not established by the source alone.",
                "Uploaded application code was not executed.",
            ],
            validation=validation,
        )
        if not validation.ok:
            proposal.rejection_reason = magic.rejection_reason_for(proposal)
        session.fix_proposal = proposal
        session.analysis_note = (
            "A minimal static correction was validated against the immutable upload. "
            "Its whitespace-policy assumption is shown with the finding."
            if validation.ok
            else f"Static fix rejected: {proposal.rejection_reason}"
        )
        if validation.ok:
            session.set_step("proposal")
            session.corrected_files, error = studio.generate_corrected_files(
                session.files, validation, reason=proposal.explanation
            )
            if error:
                session.analysis_note += f" Corrected copy withheld: {error}"
        return

    status = analysis.provider_status()
    if not status.available:
        session.analysis_note += (
            " "
            f"AI fix proposal unavailable: {status.reason} "
            "The static finding and its assumptions remain available for review."
        )
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
        session.set_step("proposal")
        session.analysis_note = (
            "Fix proposal validated against your snapshots. It is not verified by tests."
        )
    else:
        proposal.rejection_reason = magic.rejection_reason_for(proposal)
        session.analysis_note = f"Fix proposal rejected: {proposal.rejection_reason}"
    session.fix_proposal = proposal
    if validation.ok:
        corrected, error = studio.generate_corrected_files(
            session.files, validation, reason=proposal.explanation
        )
        if not error:
            session.corrected_files = corrected
        else:
            session.analysis_note += f" Corrected copy withheld: {error}"


def _render_fix_proposal(session: magic.MagicSession, issue, proposal: magic.FixProposal) -> None:
    """Evidence-to-Fix Review Studio: evidence, fix, test, downloads."""
    report = session.verification
    if report is None:
        st.warning(
            "**Proposed - not verified.** This change was validated for "
            "safety/application only. No tests have run against it, "
            "and uploaded code is never executed.",
            icon="⚠️",
        )
    elif report.overall_status == verify.STATUS_VERIFIED:
        st.success(
            "**Statically verified - runtime behavior NOT verified.** Integrity, "
            "workspace, and syntax checks passed, and ReproKit's own fixtures "
            "passed. Uploaded code was not executed.",
            icon="✅",
        )
    elif report.overall_status == verify.STATUS_FAILED:
        st.error(
            "**Verification failed.** One or more approved checks did not pass; "
            "see the Verification tab.",
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

    evidence_tab, fix_tab, test_tab, download_tab = st.tabs(
        ["1 · Evidence", "2 · Fix & verification", "3 · Regression test", "4 · Downloads"]
    )
    with evidence_tab:
        _render_studio_evidence(session, proposal, issue)
    with fix_tab:
        _render_studio_fix(session, proposal)
    with test_tab:
        _render_studio_regression(session, proposal, issue)
    with download_tab:
        _render_studio_downloads(session, proposal, issue)


def _render_studio_evidence(session, proposal, issue) -> None:
    """A. Source files and evidence, linked to real uploaded lines."""
    if issue is None:
        st.info("Select an issue to see its evidence.")
        return

    st.markdown(f"### {issue.title}")
    st.caption(
        f"Severity `{issue.severity}` · source `{issue.severity_source}` · "
        f"category `{issue.category}`"
    )

    st.markdown("**Observed evidence (exact uploaded lines, redacted)**")
    if issue.evidence:
        st.code(
            "\n".join(f"{item.label}: {item.display_text}" for item in issue.evidence),
            language="text",
        )
    else:
        st.warning("This finding has no grounded evidence lines.")

    st.markdown("**Why the evidence suggests this issue**")
    if issue.hypothesis:
        st.write(issue.hypothesis)
        st.caption(
            "AI hypothesis — plausible, but not confirmed by execution. Only the "
            "lines above are observed."
        )
    else:
        st.caption("No AI hypothesis for this finding; treat only the lines as evidence.")

    st.markdown("**Uploaded files**")
    for item in session.files:
        st.write(
            f"- `{item.name}` · {item.kind} · {item.line_count:,} lines · "
            f"`{item.sha256[:12]}…`"
        )

    log_files = [item for item in session.files if item.kind == "log"]
    if log_files:
        st.markdown("**Supporting error-log excerpts**")
        for log_file in log_files:
            safe, _ = logs.redact(log_file.text)
            excerpt = "\n".join(safe.splitlines()[:15])
            with st.expander(f"`{log_file.name}` — first 15 redacted lines"):
                st.code(excerpt or "(empty)", language="text")
    else:
        st.caption("No error log was uploaded for this review.")


def _render_studio_fix(session, proposal) -> None:
    """B. Fix proposal, reviewable diff, staging, and verification."""
    st.markdown("**Explanation**")
    st.write(proposal.explanation or "(none provided)")

    st.markdown("**Files and lines intended to change**")
    if proposal.files_changed:
        for path, lines in proposal.files_changed:
            rendered = ", ".join(str(n) for n in lines[:20]) or "(not specified)"
            st.write(f"- `{path}` — lines {rendered}")
    else:
        st.write("(not specified)")

    changed_paths = {file_patch.path for file_patch in proposal.validation.files}
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
    st.code(logs.redact(proposal.diff)[0], language="diff")

    st.markdown("**Before / after**")
    for file_patch in proposal.validation.files:
        with st.expander(
            f"`{file_patch.path}` · +{file_patch.added} / -{file_patch.removed}"
        ):
            before_col, after_col = st.columns(2)
            before_col.markdown("**Before (uploaded snapshot)**")
            before_col.code(logs.redact(file_patch.before)[0] or "(empty)", language="text")
            after_col.markdown("**After (proposed)**")
            after_col.code(logs.redact(file_patch.after)[0] or "(empty)", language="text")

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


def _render_studio_regression(session, proposal, issue) -> None:
    """C. Regression-test recommendation. Proposed, never executed."""
    status = analysis.provider_status()
    if issue is None:
        st.info("Select an issue first.")
        return

    reg = session.regression
    if reg is not None:
        if reg.is_proposed:
            st.success(
                "**Proposed regression test — not executed.** ReproKit never runs "
                "uploaded or generated code.",
                icon="🧪",
            )
            col_a, col_b, col_c = st.columns(3)
            col_a.metric("Status", reg.status)
            col_b.metric("Framework", reg.framework or "pytest")
            col_c.metric("Language", reg.language or "python")
            st.markdown("**Proposed test** — not executed")
            st.caption(reg.scenario or "(scenario not described)")
            if reg.preconditions:
                st.markdown("**Input / preconditions**")
                for item in reg.preconditions:
                    st.write(f"- {item}")
            st.caption(reg.expected_behavior or "(expected behavior not described)")
            st.markdown(f"**Why it catches a recurrence:** {reg.why_it_catches or '(not described)'}")
            st.caption(f"Linked finding: `{issue.id}` · evidence: "
                       + ", ".join(item.label for item in issue.evidence))
            if reg.code:
                st.code(reg.code, language="python")
            if reg.limitations:
                st.markdown("**Limitations**")
                for item in reg.limitations:
                    st.write(f"- {item}")
        else:
            st.warning(
                "**No runnable regression test was generated.** " + (
                    reg.limitations[0] if reg.limitations else ""
                )
            )
            for item in reg.limitations[1:]:
                st.write(f"- {item}")

    if not status.available:
        st.caption(f"AI test recommendation unavailable: {status.reason}")
    if st.button("Recommend one regression test", type="primary", use_container_width=False):
        if not status.available:
            session.regression = studio.regression_not_generated(
                "AI provider is not configured, so no test could be recommended. "
                "Add GROQ_API_KEY and try again.",
                issue=issue,
                evidence=[item.label for item in issue.evidence],
            )
        else:
            context = (
                magic.build_fix_context(
                    session.files, issue, session.shareable_answers()
                )
                + "\n\n=== PROPOSED FIX ===\n"
                + proposal.diff
            )
            result = analysis.propose_regression_test(
                context, shared_answers=session.shareable_answers()
            )
            if result.data is not None:
                session.regression = studio.regression_from_ai(
                    result.data,
                    issue=issue,
                    evidence=[item.label for item in issue.evidence],
                    files=session.files,
                )
                if not result.ok and result.error:
                    session.regression.limitations.append(result.error)
            else:
                session.regression = studio.regression_not_generated(
                    result.error or "The AI request failed.",
                    issue=issue,
                    evidence=[item.label for item in issue.evidence],
                )
        st.rerun()


def _render_studio_downloads(session, proposal, issue) -> None:
    """D/E. Corrected-file generation and the downloadable fix report."""
    st.markdown("**Generate corrected copies**")
    st.caption(
        "Creates separate files from the validated diff. Your uploads are never "
        "modified, and the generated code is not executed."
    )
    mode = st.radio(
        "Comment style",
        options=[studio.COMMENT_MODE_CLEAN, studio.COMMENT_MODE_EXPLAIN],
        index=0,
        horizontal=True,
        key="studio_comment_mode",
        format_func=lambda value: (
            "Explain the fix — concise comments"
            if value == studio.COMMENT_MODE_EXPLAIN
            else "Clean fix — minimal code"
        ),
    )

    if st.button("Generate corrected copies", type="primary", use_container_width=False):
        corrected, error = studio.generate_corrected_files(
            session.files,
            proposal.validation,
            mode=mode,
            reason=(proposal.explanation or (issue.title if issue else "verified fix")),
        )
        if error:
            st.error(f"Could not generate corrected files: {error}")
        else:
            session.corrected_files = corrected
            session.comment_mode = mode
            st.rerun()

    if session.corrected_files:
        st.markdown(f"**Corrected copies ({session.comment_mode} mode)**")
        for item in session.corrected_files:
            state = (
                "parses"
                if item.syntax_ok is True
                else "parse failed"
                if item.syntax_ok is False
                else "unparsed"
            )
            with st.expander(
                f"`{item.name}` → `{studio.corrected_download_name(item.name, item.mode)}` "
                f"· +{item.added}/-{item.removed} · {state}"
            ):
                st.caption(
                    f"{item.syntax_note} Comments added: {item.comments_added}; "
                    f"redactions: {item.redactions}. Parsing is not proof of "
                    "correctness; inspect it for secrets before sharing."
                )
                st.code(item.corrected or "(empty)", language="text")
                st.download_button(
                    f"Download {studio.corrected_download_name(item.name, item.mode)}",
                    data=item.corrected,
                    file_name=studio.corrected_download_name(item.name, item.mode),
                    mime="text/plain",
                    key=f"download_corrected_{item.name}_{item.mode}",
                )
    else:
        st.caption("No corrected copies generated yet.")

    st.divider()
    st.markdown("**Fix report**")
    log_excerpts: list[str] = []
    for item in session.files:
        if item.kind == "log":
            safe, _ = logs.redact(item.text)
            log_excerpts.append("\n".join(safe.splitlines()[:10]))
    report_markdown = studio.build_fix_report(
        issue=issue,
        files=session.files,
        log_excerpts=log_excerpts,
        proposal=proposal,
        regression=session.regression,
        verification=session.verification,
        corrected=session.corrected_files,
        comment_mode=session.comment_mode,
    )
    st.caption(
        "Report separates observed evidence, AI hypotheses, completed checks, and "
        "proposed-but-unexecuted tests. Secrets are redacted."
    )
    st.download_button(
        "Download Evidence-to-Fix report (Markdown)",
        data=report_markdown,
        file_name=studio.report_download_name(issue.title if issue else ""),
        mime="text/markdown",
        use_container_width=False,
    )


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
            session.set_step("verify")
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
    """Analyze each upload independently; correlate nothing without evidence."""
    _clear_magic_proposal_state(session)
    log_issues = magic.issues_from_logs(session.files)
    static_issues, static_notes = magic.analyze_static(session.files)
    ai_issues: list[magic.Issue] = []
    status = analysis.provider_status()

    if status.available:
        for item in (file for file in session.files if file.kind == "log"):
            parsed = logs.parse_log(item.text.encode("utf-8"), item.name)
            if not parsed.groups:
                continue
            result = analysis.analyze_log_excerpt(logs.build_ai_excerpt(parsed.groups))
            if not result.ok or result.data is None:
                if result.error:
                    static_notes.append(f"{item.name}: AI log analysis unavailable: {result.error}")
                continue
            by_key = {
                issue.source_key: issue
                for issue in log_issues
                if issue.evidence and issue.evidence[0].file == item.name
            }
            for finding in result.data.findings:
                target = by_key.get(finding.group_key)
                if target is None:
                    continue
                hypothesis, _ = logs.redact(finding.likely_cause)
                if not magic.has_unknown_filename(hypothesis, [item.name]):
                    target.hypothesis = hypothesis
                else:
                    target.hypothesis = ""
                safe_steps = []
                for step in finding.recommended_steps:
                    safe_step, _ = logs.redact(step)
                    if not magic.has_unknown_filename(safe_step, [item.name]):
                        safe_steps.append(safe_step)
                target.recommendations = safe_steps or target.recommendations
                if safe_steps:
                    target.recommendations_source = "ai"
            static_notes.append(
                f"{item.name}: AI log causes and steps are hypotheses matched to exact observed groups."
            )

    ai_targets = [item for item in session.files if item.kind != "log"]
    if status.available:
        for item in ai_targets:
            excerpt = magic.build_issue_excerpt([item])
            result = analysis.analyze_magic_issues(
                excerpt, shared_answers=session.shareable_answers()
            )
            if result.ok and result.data is not None:
                ai_issues.extend(
                    magic.issues_from_ai(result.data.issues, [item])
                )
                static_notes.append(
                    f"{item.name}: AI review completed; ungrounded references were discarded."
                )
            elif result.error:
                static_notes.append(f"{item.name}: AI review unavailable: {result.error}")
    elif ai_targets:
        static_notes.append(f"AI review unavailable: {status.reason}")

    session.set_issues(list(log_issues) + static_issues + ai_issues)
    if session.issues:
        session.set_step("evidence")
    session.analysis_note = " ".join(static_notes) or (
        "Analysis completed. Syntax checks do not prove general logic correctness."
    )
    source_issue = next(
        (issue for issue in session.issues if issue.category != "log"),
        None,
    )
    if source_issue is not None:
        session.select_issue(source_issue.id)
        _run_propose_fix(session, source_issue)


def _clear_magic_proposal_state(session: magic.MagicSession) -> None:
    """Discard proposals and verification tied to prior inputs or findings."""
    session.fix_proposal = None
    session.proposal = None
    session.verification = None
    session.corrected_files = []
    st.session_state.pop("studio_regression_key", None)
    st.session_state.pop("studio_regression", None)


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
            "- **Skip** — not used at all. Answers are optional; analysis and fixes proceed without them.\n"
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
            "- Originals are immutable; proposed changes are applied only to separate copies.\n"
            "- Secrets are redacted before display and before any AI call."
        )
    ui.footer_quote()


def render_intro() -> None:
    """Landing page: what ReproKit is for, then straight into the studio."""
    ui.landing_hero(
        kicker="Welcome to the studio",
        name="Repro",
        emphasis="Kit",
        subname="The Bug Reproduction Studio",
        purpose_html=(
            "A <strong>bug-reproduction studio</strong>: reproduce a failure with real "
            "test evidence, then review AI suggestions without mistaking a good story "
            "for a proven result."
        ),
        chips=("Reproduce", "Review", "Decide"),
    )

    ui.section(
        "The swarm",
        "A mess, until you settle it",
        "One click tidies the bubbles — the way a real bug report tidies a symptom.",
    )
    ui.swarm(bool(st.session_state.get("rk_swarm_open")))

    _, centre, _ = st.columns([1, 1.35, 1])
    with centre:
        release_label = (
            "Settle it again"
            if st.session_state.get("rk_swarm_open")
            else "Settle the swarm"
        )
        if st.button(release_label, use_container_width=True):
            st.session_state["rk_swarm_open"] = not st.session_state.get("rk_swarm_open")
            st.rerun()
        if st.button("Enter the studio", type="primary", use_container_width=True):
            st.session_state["rk_entered"] = True
            st.rerun()

    ui.cta_note(
        [
            ("Start here:", "the proven demo, end to end."),
            ("No setup:", "works without an AI key."),
            ("Safe:", "uploaded code is never executed."),
        ]
    )

    ui.section("Why it exists", "Three ways bugs waste a day", "")
    ui.pillars(
        [
            (
                "01 · REPRODUCE",
                "It worked on my machine",
                "An un-runnable failure is a rumour. Here it becomes a command, an exit "
                "code, and captured output.",
            ),
            (
                "02 · REVIEW",
                "The log says everything",
                "A thousand lines, one buried exception. Evidence gets grouped and "
                "redacted before anyone reads it.",
            ),
            (
                "03 · DECIDE",
                "The fix that wasn't",
                "AI can patch the wrong file with total confidence. Proposals are "
                "validated, never auto-applied, and never called verified.",
            ),
        ]
    )

    ui.quote(
        "Programs must be written for people to read, and only incidentally for "
        "machines to execute.",
        "Abelson & Sussman, Structure and Interpretation of Computer Programs",
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
