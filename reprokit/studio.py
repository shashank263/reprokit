"""Evidence-to-Fix Review Studio.

Presentation-agnostic helpers that turn a validated fix proposal into:

- corrected file copies (clean or with explanatory comments), never touching
  the original uploads and never executed;
- a reviewable regression-test recommendation (Python/pytest when the evidence
  is sufficient, otherwise an explicit limitation);
- a downloadable Markdown fix report that separates completed checks from
  proposed, unexecuted tests.

Everything here is offline and deterministic except :func:`regression_from_ai`,
which only adapts an already-parsed AI draft.
"""

from __future__ import annotations

import ast
import configparser
import difflib
import json
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence
import xml.etree.ElementTree as ET

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

from reprokit import patch
from reprokit.logs import redact

COMMENT_MODE_CLEAN = "clean"
COMMENT_MODE_EXPLAIN = "explain"
COMMENT_MODES = (COMMENT_MODE_CLEAN, COMMENT_MODE_EXPLAIN)

TEST_STATUS_PROPOSED = "proposed_unexecuted"
TEST_STATUS_NOT_GENERATED = "not_generated"

MAX_COMMENT_REASON = 90
MAX_COMMENT_BLOCK = 40

_HASH_COMMENT = frozenset(
    {
        ".py", ".pyi", ".rb", ".sh", ".ps1", ".yaml", ".yml", ".toml", ".ini",
        ".cfg", ".conf", ".env", ".properties", ".pl", ".r", ".ex", ".exs",
    }
)
_SLASH_COMMENT = frozenset(
    {
        ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".kt", ".kts",
        ".go", ".rs", ".cs", ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp",
        ".hxx", ".php", ".swift", ".scala", ".dart",
    }
)
_DASH_COMMENT = frozenset({".lua", ".hs"})
_NO_COMMENT = frozenset({".json", ".txt", ".log", ".csv"})
_BLOCK_COMMENT = frozenset({".xml", ".html", ".htm", ".css", ".scss", ".less"})

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass
class CorrectedFile:
    """One generated corrected copy. The original snapshot is never changed."""

    name: str
    mode: str
    original: str
    corrected: str
    added: int = 0
    removed: int = 0
    comments_added: int = 0
    redactions: int = 0
    syntax_ok: Optional[bool] = None
    syntax_note: str = ""

    @property
    def changed(self) -> bool:
        return self.original != self.corrected


@dataclass
class RegressionTest:
    """A recommended regression test. Never executed by the application."""

    status: str = TEST_STATUS_NOT_GENERATED
    language: str = ""
    framework: str = ""
    filename: str = ""
    code: str = ""
    scenario: str = ""
    preconditions: list[str] = field(default_factory=list)
    expected_behavior: str = ""
    why_it_catches: str = ""
    limitations: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    issue_id: str = ""
    raw: str = ""
    parse_error: str = ""

    @property
    def is_proposed(self) -> bool:
        return self.status == TEST_STATUS_PROPOSED


@dataclass
class RegressionDraft:
    """Parsed AI regression recommendation, before grounding."""

    feasible: bool
    language: str = ""
    framework: str = ""
    test_filename: str = ""
    code: str = ""
    scenario: str = ""
    preconditions: list[str] = field(default_factory=list)
    expected_behavior: str = ""
    why_it_catches: str = ""
    limitations: list[str] = field(default_factory=list)
    raw: str = ""
    provider: str = ""
    model: str = ""
    parse_error: str = ""


def safe_filename(name: str, *, fallback: str = "file") -> str:
    """Reduce a name to a safe download token (no paths, no traversal)."""
    base = (name or "").replace("\\", "/").split("/")[-1].strip()
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", base).strip(".-")
    return cleaned or fallback


def comment_token(name: str) -> Optional[str]:
    """Line-comment token for an extension, or None when unsupported."""
    lowered = (name or "").lower()
    dot = lowered.rfind(".")
    extension = lowered[dot:] if dot != -1 else ""
    if extension in _HASH_COMMENT:
        return "#"
    if extension in _SLASH_COMMENT:
        return "//"
    if extension == ".sql":
        return "--"
    if extension in _DASH_COMMENT:
        return "--"
    if extension == ".md":
        return "<!--"
    if extension in (".xml", ".html", ".htm"):
        return "<!--"
    if extension in _BLOCK_COMMENT:
        return "/*"
    return None


def _reason_line(reason: str) -> str:
    safe_reason, _ = redact(reason or "")
    text = re.sub(r"\s+", " ", safe_reason.strip())
    text = text.replace("\n", " ")
    return text[:MAX_COMMENT_REASON] or "verified fix"


def _with_comments(before: str, after: str, token: str, reason: str) -> tuple[str, int]:
    """Insert one short comment above each added/replaced block."""
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    out: list[str] = []
    comments = 0
    text = _reason_line(reason)
    if token == "/*":
        text = text.replace("*/", "* /")
    elif token == "<!--":
        text = text.replace("--", "- -").rstrip("-")

    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        block = after_lines[j1:j2]
        if tag == "equal":
            out.extend(block)
            continue
        if tag == "delete":
            continue
        if block and len(block) <= MAX_COMMENT_BLOCK and token:
            indent = re.match(r"[ \t]*", block[0]).group(0)
            if token == "/*":
                out.append(f"{indent}/* fix: {text} */")
            elif token == "<!--":
                out.append(f"{indent}<!-- fix: {text} -->")
            else:
                out.append(f"{indent}{token} fix: {text}")
            comments += 1
        out.extend(block)

    rendered = "\n".join(out)
    if after.endswith("\n"):
        rendered += "\n"
    return rendered, comments


def _static_parse(name: str, text: str) -> tuple[Optional[bool], str]:
    """Re-parse the generated source where a parser exists. Never executes it."""
    lowered = name.lower()
    if lowered.endswith(".py") or lowered.endswith(".pyi"):
        try:
            ast.parse(text, filename=name)
            return True, "ast.parse succeeded (syntax only, not execution)."
        except SyntaxError as exc:
            return False, f"syntax error: {exc.msg} (line {exc.lineno})"
    if lowered.endswith(".json"):
        try:
            json.loads(text)
            return True, "json.loads succeeded (syntax only)."
        except json.JSONDecodeError as exc:
            return False, f"invalid JSON: {exc.msg}"
    if lowered.endswith(".toml") and tomllib is not None:
        try:
            tomllib.loads(text)
            return True, "TOML parser succeeded (syntax only)."
        except (ValueError, TypeError) as exc:
            return False, f"invalid TOML: {exc}"
    if lowered.endswith(".ini"):
        try:
            configparser.ConfigParser().read_string(text)
            return True, "INI parser succeeded (syntax only)."
        except configparser.Error as exc:
            return False, f"invalid INI: {exc}"
    if lowered.endswith(".xml"):
        try:
            ET.fromstring(text)
            return True, "XML parser succeeded (syntax only)."
        except ET.ParseError as exc:
            return False, f"invalid XML: {exc}"
    return None, "No static parser for this file type; not validated."


def generate_corrected_files(
    files: Sequence[object],
    validation,
    *,
    mode: str = COMMENT_MODE_CLEAN,
    reason: str = "",
) -> tuple[list[CorrectedFile], str]:
    """Build corrected copies from a validated proposal.

    Returns (corrected_files, error). The uploads themselves are untouched.
    """
    if mode not in COMMENT_MODES:
        return [], f"Unknown comment mode {mode!r}."
    if validation is None or not validation.ok:
        return [], "A validated proposal is required before generating corrected files."

    by_name = {item.name: item for item in files}
    corrected: list[CorrectedFile] = []

    for file_patch in validation.files:
        item = by_name.get(file_patch.path)
        if item is None:
            return [], f"{file_patch.path!r} is not one of the uploaded files."
        original = item.text
        text = file_patch.after
        comments = 0
        note = ""

        if mode == COMMENT_MODE_EXPLAIN:
            token = comment_token(file_patch.path)
            if token is None:
                note = "Comments are not supported for this file type; kept clean."
            else:
                text, comments = _with_comments(original, text, token, reason)

        text, redactions = redact(text)
        syntax_ok, syntax_note = _static_parse(file_patch.path, text)
        if syntax_ok is False:
            return [], (
                f"Corrected copy for {file_patch.path!r} failed static syntax "
                f"validation: {syntax_note}"
            )
        if redactions:
            syntax_note += f" {redactions} secret(s) redacted from the artifact."
        corrected.append(
            CorrectedFile(
                name=safe_filename(file_patch.path),
                mode=mode,
                original=original,
                corrected=text,
                added=file_patch.added,
                removed=file_patch.removed,
                comments_added=comments,
                redactions=redactions,
                syntax_ok=syntax_ok,
                syntax_note=syntax_note + (f" {note}" if note else ""),
            )
        )

    return corrected, ""


def corrected_download_name(name: str, mode: str) -> str:
    """buggy_calculator.py -> buggy_calculator.corrected.py."""
    base = safe_filename(name)
    dot = base.rfind(".")
    stem, extension = (base[:dot], base[dot:]) if dot > 0 else (base, "")
    suffix = "explained" if mode == COMMENT_MODE_EXPLAIN else "corrected"
    return f"{stem}.{suffix}{extension}"


def _looks_like_python_test(code: str) -> bool:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    return any(
        isinstance(node, ast.FunctionDef) and node.name.startswith("test")
        for node in tree.body
    )


def regression_from_ai(
    draft: RegressionDraft,
    *,
    issue=None,
    evidence: Iterable[str] = (),
    files: Sequence[object] = (),
) -> RegressionTest:
    """Ground an AI regression draft. Invalid or unsafe drafts are downgraded."""
    evidence_list = list(evidence)
    placed = RegressionTest(
        evidence=evidence_list,
        issue_id=getattr(issue, "id", "") or "",
        raw=draft.raw,
    )

    if draft.parse_error:
        placed.parse_error = draft.parse_error
        placed.limitations = ["The AI response could not be parsed."]
        return placed

    if not draft.feasible:
        placed.limitations = draft.limitations or [
            "The AI reported that the evidence was insufficient for a concrete test."
        ]
        return placed

    language = (draft.language or "").strip().lower()
    code = draft.code or ""
    raw_filename = (draft.test_filename or "").strip()
    filename = safe_filename(raw_filename, fallback="")
    limitations = list(draft.limitations)
    if raw_filename and ("/" in raw_filename or "\\" in raw_filename or ".." in raw_filename):
        limitations.append("The suggested test path was unsafe; a plain filename was used.")
        filename = ""

    uploaded = {item.name for item in files}
    if language not in ("python",):
        limitations.append(
            f"Only Python/pytest test code is generated today; reported language "
            f"was {draft.language or 'unspecified'!r}."
        )
        placed.limitations = limitations
        return placed

    if not code.strip():
        limitations.append("The AI returned no test code.")
        placed.limitations = limitations
        return placed

    if not _looks_like_python_test(code):
        limitations.append(
            "The returned code does not define a test function; treated as a "
            "specification rather than runnable code."
        )
        returned_code = ""
    else:
        returned_code = code

    if filename and not _SAFE_NAME_RE.match(filename):
        limitations.append("The suggested test filename was unsafe; ignored.")
        filename = ""
    if filename and filename in uploaded:
        limitations.append(
            f"The suggested test filename {filename!r} matches an uploaded file; "
            "reprokit_regression_test.py was used instead."
        )
        filename = ""
    if not filename:
        filename = "reprokit_regression_test.py"

    placed.status = TEST_STATUS_PROPOSED
    placed.language = "python"
    placed.framework = (draft.framework or "pytest").strip() or "pytest"
    placed.filename = filename
    placed.code = returned_code
    placed.scenario = draft.scenario.strip()
    placed.preconditions = [p.strip() for p in draft.preconditions if p.strip()]
    placed.expected_behavior = draft.expected_behavior.strip()
    placed.why_it_catches = draft.why_it_catches.strip()
    placed.limitations = limitations
    return placed


def regression_not_generated(reason: str, *, issue=None, evidence: Iterable[str] = ()) -> RegressionTest:
    """Explicit 'no test generated' result — never a silent empty state."""
    return RegressionTest(
        status=TEST_STATUS_NOT_GENERATED,
        limitations=[reason],
        evidence=list(evidence),
        issue_id=getattr(issue, "id", "") or "",
    )


def _status_line(verification) -> tuple[str, str]:
    if verification is None:
        return "not_run", "No verification report exists for this proposal yet."
    return (
        verification.overall_status,
        "Static checks only; uploaded code was not executed, so runtime "
        "behavior is not verified.",
    )


def build_fix_report(
    *,
    issue,
    files: Sequence[object],
    log_excerpts: Sequence[str] = (),
    proposal=None,
    regression: Optional[RegressionTest] = None,
    verification=None,
    corrected: Sequence[CorrectedFile] = (),
    comment_mode: str = COMMENT_MODE_CLEAN,
) -> str:
    """Markdown fix report. Secrets are redacted on the way out."""
    evidence = list(getattr(issue, "evidence", []) or [])
    file_names = sorted({item.name for item in files})
    status, status_note = _status_line(verification)

    lines: list[str] = [
        "# ReproKit — Evidence-to-Fix Report",
        "",
        f"- **Issue:** {getattr(issue, 'title', 'unknown')}",
        f"- **Severity:** {getattr(issue, 'severity', 'n/a')} "
        f"({getattr(issue, 'severity_source', 'observed')})",
        f"- **Uploaded files:** {', '.join(file_names) or 'none'}",
        f"- **Verification status:** `{status}` — {status_note}",
        "",
        "## 1. Finding and evidence (observed)",
        "",
    ]

    if evidence:
        for item in evidence:
            first_line = (item.text or "").strip().splitlines()[0] if item.text else ""
            lines.append(f"- `{item.label}` — `{first_line[:160]}`")
    else:
        lines.append("- No evidence lines are attached to this finding.")

    lines += ["", "### Relevant log evidence", ""]
    if log_excerpts:
        lines += ["```text"] + [text for text in log_excerpts] + ["```"]
    else:
        lines.append("No error log was uploaded for this review.")

    hypothesis = (getattr(issue, "hypothesis", "") or "").strip()
    lines += [
        "",
        "## 2. Root-cause hypothesis (AI, unconfirmed)",
        "",
        hypothesis or "No hypothesis was produced for this finding.",
        "",
        "## 3. Proposed fix (reviewable, not applied)",
        "",
    ]

    if proposal is not None and getattr(proposal, "validation", None) is not None and proposal.validation.ok:
        lines += [
            f"**Rationale:** {proposal.explanation or '(none provided)'}",
            "",
            f"**Expected behavior after the fix (unverified):** "
            f"{proposal.expected_behavior or '(none provided)'}",
            "",
        ]
        if proposal.files_changed:
            for path, numbers in proposal.files_changed:
                rendered = ", ".join(str(n) for n in numbers[:20]) or "(not specified)"
                lines.append(f"- `{path}` — lines {rendered}")
        lines += ["", "```diff", proposal.diff.rstrip("\n"), "```"]
        if proposal.limitations:
            lines += ["", "**Limitations / assumptions (AI):**"]
            lines += [f"- {item}" for item in proposal.limitations]
    else:
        reason = ""
        if proposal is not None:
            reason = getattr(proposal, "rejection_reason", "") or ""
        lines.append(f"No validated fix is available. {reason}".strip())

    lines += ["", "## 4. Regression-test recommendation", ""]
    if regression is not None and regression.is_proposed:
        lines += [
            f"- **Status:** `{regression.status}` — proposed, **not executed**",
            f"- **Scenario:** {regression.scenario or '(not described)'}",
            f"- **Framework:** {regression.framework or 'pytest'} ({regression.language})",
            f"- **Suggested file:** `{regression.filename}`",
            f"- **Expected behavior:** {regression.expected_behavior or '(not described)'}",
            f"- **Why it catches a recurrence:** {regression.why_it_catches or '(not described)'}",
        ]
        if regression.preconditions:
            lines += ["- **Preconditions / input:**"]
            lines += [f"  - {item}" for item in regression.preconditions]
        if regression.code:
            lines += ["", "```python", regression.code.rstrip("\n"), "```"]
        if regression.limitations:
            lines += ["", "**Limitations:**"]
            lines += [f"- {item}" for item in regression.limitations]
    elif regression is not None:
        lines.append("No runnable test was generated.")
        lines += [f"- {item}" for item in regression.limitations]
    else:
        lines.append("No regression test has been requested yet.")

    lines += ["", "## 5. Verification", ""]
    if verification is None:
        lines.append("No verification run yet. Nothing here is claimed as verified.")
    else:
        lines.append(f"Overall status: `{verification.overall_status}`")
        lines.append("")
        lines.append("| Check | Status | Exit | Duration |")
        lines.append("| --- | --- | --- | --- |")
        for check in verification.checks:
            exit_status = check.exit_status if check.exit_status is not None else "n/a"
            lines.append(
                f"| {check.label} | `{check.status}` | {exit_status} | "
                f"{check.duration_seconds:.3f}s |"
            )
        lines += ["", f"Scope: {verification.scope_statement}"]

    lines += ["", "### Completed vs proposed", ""]
    lines.append(
        "- **Completed:** static checks that actually ran (see table above)."
        if verification is not None
        else "- **Completed:** none."
    )
    lines.append(
        "- **Proposed, not executed:** the regression test above. ReproKit never "
        "executes uploaded or generated code."
    )

    if corrected:
        lines += ["", "## 6. Corrected files", ""]
        lines.append(
            f"Generated copies ({comment_mode} mode). Originals remain unchanged."
        )
        for item in corrected:
            state = (
                "parses"
                if item.syntax_ok is True
                else "parse failed"
                if item.syntax_ok is False
                else "unparsed"
            )
            lines.append(
                f"- `{item.name}` -> `{corrected_download_name(item.name, item.mode)}` "
                f"(+{item.added}/-{item.removed}, {item.comments_added} comment(s), {state})"
            )

    lines += [
        "",
        "---",
        "Uploaded code was not executed. AI output is advisory: only executed "
        "checks are evidence; proposed tests have not been run, and a file that "
        "parses is not a proven fix. Runtime behavior was not verified.",
        "",
    ]

    report, _ = redact("\n".join(lines))
    return report


def build_findings_report(
    *,
    files: Sequence[object],
    issues: Sequence[object],
    notes: Sequence[str] = (),
    proposal=None,
    corrected: Sequence[CorrectedFile] = (),
    verification=None,
) -> str:
    """Create an evidence-led report for all findings, including log-only runs."""
    names = sorted(item.name for item in files)
    lines = [
        "# ReproKit — File Analysis Report",
        "",
        f"- **Files analyzed:** {', '.join(names) or 'none'}",
        "- **Uploaded code execution:** never",
        "- **Verification status:** "
        + (
            f"`{verification.overall_status}`"
            if verification is not None
            else "`inconclusive` — no approved verification run"
        ),
        "",
        "## Findings",
        "",
    ]
    if not issues:
        lines.append("No actionable findings were detected.")
    for index, issue in enumerate(issues, start=1):
        lines += [
            f"### {index}. {getattr(issue, 'title', 'Finding')}",
            "",
            f"- **Severity:** {getattr(issue, 'severity', 'unknown')}",
            f"- **Confidence:** {getattr(issue, 'confidence', 0.0):.0%}",
            f"- **Classification:** {getattr(issue, 'severity_source', 'unknown')}",
            f"- **Verification:** `{getattr(issue, 'verification_status', 'inconclusive')}`",
            f"- **Observed problem:** "
            f"{getattr(issue, 'observed_problem', '') or 'Not independently established.'}",
            f"- **Why it matters:** "
            f"{getattr(issue, 'why_it_matters', '') or 'See the evidence and hypothesis below.'}",
            "",
            "**Evidence (redacted):**",
        ]
        evidence = list(getattr(issue, "evidence", []) or [])
        if evidence:
            for item in evidence:
                lines.append(f"- `{item.label}` — `{item.display_text}`")
        else:
            lines.append("- No grounded evidence is available.")
        hypothesis = (getattr(issue, "hypothesis", "") or "").strip()
        if hypothesis:
            lines += ["", f"**Hypothesis / explanation:** {hypothesis}"]
        recommendations = list(getattr(issue, "recommendations", []) or [])
        if recommendations:
            recommendation_label = (
                "AI-recommended next steps (advisory):"
                if getattr(issue, "recommendations_source", "deterministic") == "ai"
                else "Source-independent remediation guidance:"
            )
            lines += ["", f"**{recommendation_label}**"]
            lines.extend(f"- {step}" for step in recommendations)
        correction = (getattr(issue, "proposed_correction", "") or "").strip()
        if correction:
            lines += ["", f"**Proposed correction:** `{correction}`"]
        assumptions = list(getattr(issue, "assumptions", []) or [])
        if assumptions:
            lines += ["", "**Assumptions / uncertainty:**"]
            lines.extend(f"- {assumption}" for assumption in assumptions)
        lines.append("")

    if proposal is not None and getattr(proposal, "validation", None) is not None:
        lines += ["## Proposed change", ""]
        if proposal.validation.ok:
            lines += [
                proposal.explanation or "Validated against the uploaded snapshots.",
                "",
                "```diff",
                proposal.diff.rstrip("\n"),
                "```",
            ]
            for file_patch in proposal.validation.files:
                lines += [
                    "",
                    f"### Before and after: `{file_patch.path}`",
                    "",
                    "**Before:**",
                    "```text",
                    file_patch.before.rstrip("\n"),
                    "```",
                    "",
                    "**After:**",
                    "```text",
                    file_patch.after.rstrip("\n"),
                    "```",
                ]
        else:
            lines.append(
                "No change was accepted. "
                + (proposal.validation.error or "Validation failed.")
            )
    else:
        lines += ["## Proposed change", "", "No safe corrected copy was generated."]

    if corrected:
        lines += ["", "## Corrected copies", ""]
        for item in corrected:
            lines.append(
                f"- `{corrected_download_name(item.name, item.mode)}` — "
                f"static validation: {item.syntax_note or 'not available'}"
            )
    lines += ["", "## Verification and limitations", ""]
    lines.extend(f"- {note}" for note in notes)
    if verification is None:
        lines.append("- No approved verification was run. No runtime correctness is claimed.")
    else:
        for check in verification.checks:
            lines.append(f"- `{check.status}` — {check.label}: {check.detail or check.check}")
        lines.append(verification.scope_statement)
    lines += [
        "",
        "Static parsing validates syntax only where a parser is available. "
        "Uploaded files and generated tests were not executed.",
        "",
    ]
    report, _ = redact("\n".join(lines))
    return report


def report_download_name(issue_title: str = "") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (issue_title or "").lower()).strip("-")
    return f"reprokit-fix-report{'-' + slug[:40] if slug else ''}.md"