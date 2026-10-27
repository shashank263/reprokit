"""Magic Mode: uploads, grounded issues, privacy-filtered answers, workflow state.

Workflow features:
- deterministic log issue grouping (reusing :mod:`reprokit.logs`)
- bounded, redacted code/text excerpts for AI issue detection
- every evidence reference is resolved against the uploaded file (file + line)
  so the UI can show the exact supporting lines; unknown refs are discarded
- follow-up questions with explicit Share / Private / Skip visibility
- validated proposals are staged as copies in a temporary workspace

Uploaded code is never executed, and original snapshots are never modified.
"""

from __future__ import annotations

import hashlib
import ast
import configparser
import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal, Optional, Sequence
import xml.etree.ElementTree as ET

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

from reprokit import logs
from reprokit.logs import MAX_FILE_BYTES, MAX_LINE_CHARS, redact

MAX_FILES = 8
MAX_TOTAL_BYTES = 4 * 1024 * 1024

MAX_AI_LINES = 200
MAX_AI_CHARS = 12_000
MAX_EVIDENCE_REFS = 5
MAX_AI_ISSUES = 12
MAX_QUESTIONS_PER_ISSUE = 3

SEVERITIES = ("critical", "high", "medium", "low")

# Filenames mentioned by AI text, used to catch references to files that were
# never uploaded (the model sometimes invents names).
_FILENAME_RE = re.compile(
    r"\b[\w./\\-]+\.(?:py|pyi|js|jsx|ts|tsx|mjs|cjs|java|kt|go|rb|rs|cs|c|h|"
    r"cpp|cc|cxx|hpp|hxx|php|swift|scala|sh|ps1|sql|dart|lua|pl|r|ex|exs|hs|"
    r"log|out|trace|txt|md|json|ya?ml|toml|ini|cfg|conf|csv|tsv|xml|html?|css|"
    r"scss|less|properties|env)\b",
    re.IGNORECASE,
)

CODE_EXTENSIONS = frozenset(
    {
        ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go",
        ".rb", ".rs", ".cs", ".c", ".h", ".cpp", ".hpp", ".php", ".swift",
        ".scala", ".sh", ".ps1", ".sql", ".mjs", ".cjs", ".cc", ".cxx",
        ".hxx", ".dart", ".lua", ".pl", ".r", ".ex", ".exs", ".hs",
    }
)
LOG_EXTENSIONS = frozenset({".log", ".out", ".trace"})
TEXT_EXTENSIONS = frozenset(
    {
        ".txt", ".md", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
        ".conf", ".csv", ".tsv", ".xml", ".html", ".htm", ".css", ".scss",
        ".less", ".properties", ".env",
    }
)
ALLOWED_EXTENSIONS = CODE_EXTENSIONS | LOG_EXTENSIONS | TEXT_EXTENSIONS

FileKind = Literal["code", "log", "text"]
Visibility = Literal["shared", "private", "skipped"]

WORKFLOW_STEPS: tuple[tuple[str, str], ...] = (
    ("upload", "Upload supported files"),
    ("analyze", "Analyze automatically"),
    ("evidence", "Review evidence and findings"),
    ("proposal", "Generate safe corrected copies"),
    ("verify", "Verify and download report"),
)

_KNOWN_STEPS = {key for key, _ in WORKFLOW_STEPS}


class MagicInputError(ValueError):
    """Raised for unusable or unsafe Magic Mode input."""


class TooManyFilesError(MagicInputError):
    """Raised when the upload count exceeds the limit."""


class FileTooLargeError(MagicInputError):
    """Raised when one upload or the total exceeds the size limit."""


class UnsupportedFileTypeError(MagicInputError):
    """Raised when an upload's extension is not allowed."""


class BinaryFileError(MagicInputError):
    """Raised when an upload is binary and cannot safely be analyzed as text."""


def classify_extension(filename: str) -> FileKind:
    """Map a filename to code/log/text, or raise UnsupportedFileTypeError."""
    lowered = (filename or "").lower()
    if lowered in {"dockerfile", "makefile"}:
        return "code"
    dot = lowered.rfind(".")
    extension = lowered[dot:] if dot != -1 else ""
    if extension in CODE_EXTENSIONS:
        return "code"
    if extension in LOG_EXTENSIONS:
        return "log"
    if extension in TEXT_EXTENSIONS:
        return "text"
    raise UnsupportedFileTypeError(
        f"{filename or '(unnamed file)'} has an unsupported extension "
        f"{extension or '(none)'}. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
    )


@dataclass(frozen=True)
class MagicFile:
    """One uploaded file, preserved as-is. Never modified in place."""

    name: str
    kind: FileKind
    size_bytes: int
    sha256: str
    text: str
    line_count: int
    warnings: tuple[str, ...] = ()
    format: str = "plain text"

    def preview(self, max_lines: int = 20, max_chars: int = 2_000) -> str:
        """Redacted, bounded preview for display only."""
        safe, _ = redact(self.text)
        lines = safe.splitlines()[:max_lines]
        return "\n".join(line[:MAX_LINE_CHARS] for line in lines)[:max_chars]


@dataclass(frozen=True)
class Evidence:
    """One supporting line, resolved against the uploaded file.

    ``text`` is the exact line from the uploaded snapshot; ``display_text`` is
    redacted for presentation.
    """

    file: str
    line: int
    text: str

    @property
    def display_text(self) -> str:
        safe, _ = redact(self.text)
        return safe[:MAX_LINE_CHARS]

    @property
    def label(self) -> str:
        return f"{self.file}:{self.line}"


@dataclass
class Question:
    """A follow-up question, always AI-originated in Magic Mode."""

    id: str
    text: str
    related_file: Optional[str] = None
    issue_id: Optional[str] = None


@dataclass
class Issue:
    """A grounded issue: every evidence item points at a real file line."""

    id: str
    title: str
    category: str  # "log" | "code" | "text"
    severity: str
    evidence: list[Evidence] = field(default_factory=list)
    hypothesis: str = ""  # AI only; never observed fact
    questions: list[Question] = field(default_factory=list)
    severity_source: str = "observed"  # "observed" | "ai"
    confidence: float = 0.5
    observed_problem: str = ""
    why_it_matters: str = ""
    proposed_correction: str = ""
    fix_diff: str = ""
    assumptions: list[str] = field(default_factory=list)
    verification_status: str = "inconclusive"
    recommendations: list[str] = field(default_factory=list)
    recommendations_source: str = "deterministic"
    source_key: str = ""

    @property
    def is_ai_hypothesis(self) -> bool:
        return bool(self.hypothesis.strip())


@dataclass
class Answer:
    """A developer answer with explicit visibility."""

    question_id: str
    text: str = ""
    visibility: Visibility = "skipped"

    @property
    def is_shareable(self) -> bool:
        return self.visibility == "shared" and bool(self.text.strip())


@dataclass
class MagicSession:
    """All per-session Magic Mode inputs, findings, proposals, and answers."""

    files: list[MagicFile] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    answers: dict[str, Answer] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    selected_issue_id: Optional[str] = None
    analysis_note: str = ""
    fix_proposal: Optional["FixProposal"] = None
    regression: Optional[object] = None
    corrected_files: list = field(default_factory=list)
    comment_mode: str = "clean"
    step: str = "upload"
    proposal: Optional[str] = None  # canonical proposal diff after staging
    verification: Optional[object] = None  # verify.VerificationReport when present
    notes: list[str] = field(default_factory=list)

    def set_step(self, step: str) -> None:
        if step not in _KNOWN_STEPS:
            raise MagicInputError(f"Unknown workflow step {step!r}.")
        self.step = step

    def add_question(self, question: Question) -> None:
        if any(q.id == question.id for q in self.questions):
            raise MagicInputError(f"Duplicate question id {question.id!r}.")
        self.questions.append(question)

    def set_issues(self, issues: Sequence[Issue]) -> None:
        """Replace issues and re-register their questions (answers are kept)."""
        self.issues = list(issues)
        self.questions = [q for issue in self.issues for q in issue.questions]
        if self.selected_issue_id not in {i.id for i in self.issues}:
            self.selected_issue_id = self.issues[0].id if self.issues else None
        if self.issues and self.step in ("upload", "analyze"):
            self.step = "evidence"

    def select_issue(self, issue_id: str) -> Issue:
        for issue in self.issues:
            if issue.id == issue_id:
                self.selected_issue_id = issue_id
                return issue
        raise MagicInputError(f"Unknown issue {issue_id!r}.")

    def selected_issue(self) -> Optional[Issue]:
        for issue in self.issues:
            if issue.id == self.selected_issue_id:
                return issue
        return None

    def questions_for(self, issue_id: str) -> list[Question]:
        return [q for q in self.questions if q.issue_id == issue_id]

    def answer(
        self, question_id: str, text: str = "", visibility: Visibility = "skipped"
    ) -> Answer:
        if visibility not in ("shared", "private", "skipped"):
            raise MagicInputError(f"Unknown answer visibility {visibility!r}.")
        if not any(q.id == question_id for q in self.questions):
            raise MagicInputError(f"Unknown question {question_id!r}.")
        answer = Answer(question_id=question_id, text=text or "", visibility=visibility)
        self.answers[question_id] = answer
        return answer

    def shareable_answers(self) -> list[Answer]:
        """Only answers explicitly shared with a non-empty body. The AI filter."""
        return [a for a in self.answers.values() if a.is_shareable]

    def private_answer_count(self) -> int:
        return sum(1 for a in self.answers.values() if a.visibility == "private")


def ingest_files(uploads: Iterable[tuple[str, bytes]]) -> list[MagicFile]:
    """Validate uploads and describe them. Does not write or execute anything.

    Raises MagicInputError subclasses on unsafe input.
    """
    items = list(uploads)
    if not items:
        raise MagicInputError("No files uploaded.")
    if len(items) > MAX_FILES:
        raise TooManyFilesError(f"Too many files: {len(items)} (max {MAX_FILES}).")

    total = 0
    seen: set[str] = set()
    results: list[MagicFile] = []

    for name, raw in items:
        safe_name = (name or "unnamed").replace("\\", "/").split("/")[-1]
        if not safe_name:
            raise MagicInputError("A file has an empty name.")
        name_key = safe_name.casefold()
        if name_key in seen:
            raise MagicInputError(
                f"Duplicate filename {safe_name!r}; rename files instead of "
                "overwriting."
            )
        seen.add(name_key)

        kind = classify_extension(safe_name)
        uploaded_bytes = raw or b""
        size = len(uploaded_bytes)
        if size == 0:
            raise MagicInputError(f"{safe_name} is empty.")
        if size > MAX_FILE_BYTES:
            raise FileTooLargeError(
                f"{safe_name} is {size:,} bytes (max {MAX_FILE_BYTES:,})."
            )
        total += size
        if total > MAX_TOTAL_BYTES:
            raise FileTooLargeError(
                f"Total upload is {total:,} bytes (max {MAX_TOTAL_BYTES:,})."
            )

        warnings: list[str] = []
        if b"\x00" in uploaded_bytes:
            raise BinaryFileError(
                f"{safe_name} contains NUL bytes and appears binary; it was not analyzed."
            )
        control_bytes = sum(
            byte < 9 or 13 < byte < 32 for byte in uploaded_bytes
        )
        if uploaded_bytes and control_bytes / len(uploaded_bytes) > 0.05:
            raise BinaryFileError(
                f"{safe_name} contains binary control bytes; it was not analyzed."
            )
        content = uploaded_bytes
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = content.decode("utf-8", errors="replace")
            if text.count("\ufffd") / max(len(text), 1) > 0.30:
                raise BinaryFileError(
                    f"{safe_name} contains mostly non-text bytes; it was not analyzed."
                )
            warnings.append("Not valid UTF-8; undecodable bytes were replaced.")

        results.append(
            MagicFile(
                name=safe_name,
                kind=kind,
                size_bytes=size,
                sha256=hashlib.sha256(uploaded_bytes).hexdigest(),
                text=text,
                line_count=len(text.splitlines()),
                warnings=tuple(warnings),
                format=_format_for_name(safe_name),
            )
        )

    return results


_LANGUAGES = {
    ".py": "Python", ".pyi": "Python", ".js": "JavaScript",
    ".jsx": "JavaScript/JSX", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript/JSX", ".java": "Java",
    ".kt": "Kotlin", ".go": "Go", ".rb": "Ruby", ".rs": "Rust",
    ".cs": "C#", ".c": "C", ".h": "C/C++ header", ".cpp": "C++",
    ".cc": "C++", ".cxx": "C++", ".hpp": "C++ header",
    ".hxx": "C++ header", ".php": "PHP", ".swift": "Swift",
    ".scala": "Scala", ".sh": "Shell", ".ps1": "PowerShell",
    ".sql": "SQL", ".dart": "Dart", ".lua": "Lua", ".pl": "Perl",
    ".r": "R", ".ex": "Elixir", ".exs": "Elixir", ".hs": "Haskell",
    ".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML",
    ".xml": "XML", ".html": "HTML", ".htm": "HTML", ".css": "CSS",
    ".scss": "SCSS", ".less": "Less", ".ini": "INI", ".cfg": "INI",
    ".conf": "configuration text", ".properties": "Java properties",
    ".env": "environment configuration", ".csv": "CSV", ".tsv": "TSV",
    ".log": "application log", ".out": "text log", ".trace": "trace log",
    ".md": "Markdown", ".txt": "plain text",
}


def _format_for_name(filename: str) -> str:
    lowered = filename.lower()
    if lowered in {"dockerfile", "makefile"}:
        return "Dockerfile" if lowered == "dockerfile" else "Makefile"
    if lowered == ".env":
        return "environment configuration"
    return _LANGUAGES.get(Path(lowered).suffix, "plain text")


def analyze_static(files: Sequence[MagicFile]) -> tuple[list[Issue], list[str]]:
    """Run safe, deterministic parsers and narrowly-scoped source heuristics."""
    issues: list[Issue] = []
    notes: list[str] = []
    for item in files:
        if item.kind == "log":
            continue
        suffix = Path(item.name).suffix.lower()
        text = item.text
        try:
            if suffix in (".py", ".pyi"):
                ast.parse(text, filename=item.name)
            elif suffix == ".json":
                json.loads(text)
            elif suffix == ".toml" and tomllib is not None:
                tomllib.loads(text)
            elif suffix == ".ini":
                configparser.ConfigParser().read_string(text)
            elif suffix == ".xml":
                ET.fromstring(text)
            else:
                notes.append(
                    f"{item.name}: identified as {item.format}; no installed "
                    "static validator is available for this format."
                )
                continue
        except json.JSONDecodeError as exc:
            issues.append(
                _static_error_issue(
                    item, exc.lineno, f"Malformed JSON: {exc.msg}", 1.0
                )
            )
            continue
        except ET.ParseError as exc:
            line = exc.position[0] if exc.position else 1
            issues.append(
                _static_error_issue(item, line, f"Malformed XML: {exc}", 1.0)
            )
            continue
        except configparser.Error as exc:
            parse_errors = getattr(exc, "errors", [])
            line = parse_errors[0][0] if parse_errors else getattr(exc, "lineno", 1)
            issues.append(
                _static_error_issue(item, line, f"Malformed INI: {exc}", 1.0)
            )
            continue
        except SyntaxError as exc:
            line = exc.lineno or 1
            message = f"Python syntax error: {exc.msg}"
            issues.append(_static_error_issue(item, line, message, 1.0))
            continue
        except RecursionError:
            notes.append(
                f"{item.name}: the static parser exceeded its nesting limit; "
                "validation is inconclusive."
            )
            continue
        except (ValueError, TypeError) as exc:
            # TOMLDecodeError is a ValueError; lineno is unavailable on some
            # Python releases, so fall back to the first line there.
            if suffix == ".toml":
                line = getattr(exc, "lineno", 1)
                issues.append(
                    _static_error_issue(item, line, f"Malformed TOML: {exc}", 1.0)
                )
                continue
            raise

        if suffix in (".py", ".pyi"):
            issue = _whitespace_length_issue(item)
            if issue is not None:
                issues.append(issue)
            else:
                notes.append(
                    f"{item.name}: Python AST parsing passed; this checks syntax "
                    "only, not runtime behavior or general logic correctness."
                )
        elif suffix in (".json", ".toml", ".xml", ".ini"):
            notes.append(f"{item.name}: {item.format} syntax validation passed.")

    return issues, notes


def _static_error_issue(
    item: MagicFile, line: int, message: str, confidence: float
) -> Issue:
    line = min(max(line, 1), max(item.line_count, 1))
    evidence = resolve_evidence([(item)], [(item.name, line)])
    return Issue(
        id=f"static:{item.name}:{line}",
        title=message,
        category="text" if item.kind == "text" else "code",
        severity="high",
        evidence=evidence,
        hypothesis=f"The {item.format} parser reported a malformed or invalid construct.",
        severity_source="observed",
        confidence=confidence,
        observed_problem=message,
        why_it_matters="The file cannot be parsed reliably by tools that expect valid syntax.",
        verification_status="failed",
    )


def _whitespace_length_issue(item: MagicFile) -> Optional[Issue]:
    """Flag one concrete Python validation pitfall with its explicit assumption."""
    lines = item.text.splitlines()
    pattern = re.compile(r"^(\s*)return\s+len\(([A-Za-z_]\w*)\)\s*>\s*0\s*$")
    for number, line in enumerate(lines, start=1):
        match = pattern.match(line)
        if not match:
            continue
        indent, variable = match.groups()
        changed_line = f"{indent}return len({variable}.strip()) > 0"
        after = list(lines)
        after[number - 1] = changed_line
        before_text = item.text
        after_text = "\n".join(after) + ("\n" if item.text.endswith("\n") else "")
        diff_lines = list(
            difflib.unified_diff(
                before_text.splitlines(keepends=True),
                after_text.splitlines(keepends=True),
                fromfile=f"a/{item.name}",
                tofile=f"b/{item.name}",
            )
        )
        diff = "".join(diff_lines)
        if diff.startswith("--- "):
            diff = diff.replace("--- a/", "--- a/", 1)
        return Issue(
            id=f"static:logic:{item.name}:{number}",
            title="Whitespace-only input passes a non-empty check",
            category="code",
            severity="medium",
            evidence=resolve_evidence([(item)], [(item.name, number)]),
            hypothesis=(
                "This check returns true for a string containing only spaces. "
                "Whether that is a bug depends on the intended input policy."
            ),
            severity_source="observed",
            confidence=0.78,
            observed_problem=(
                f"len({variable}) > 0 accepts any non-empty string, including whitespace."
            ),
            why_it_matters=(
                "If whitespace-only input should be rejected, this validation "
                "allows a blank-looking value through."
            ),
            proposed_correction=changed_line.strip(),
            fix_diff=diff,
            assumptions=[
                "Assumes this function validates human-entered text and should "
                "reject whitespace-only values."
            ],
            verification_status="inconclusive",
        )
    return None


def build_ai_context(
    files: Sequence[MagicFile],
    answers: Sequence[Answer],
    *,
    max_chars: int = 12_000,
) -> str:
    """Bounded, redacted context containing only explicitly shared answers."""
    blocks: list[str] = []
    for item in files:
        safe, _ = redact(item.text)
        blocks.append(f"=== FILE {item.name} ({item.kind}) ===\n{safe}")

    shared = [a for a in answers if a.is_shareable]
    if shared:
        lines = ["=== SHARED DEVELOPER ANSWERS ==="]
        for answer in shared:
            safe_text, _ = redact(answer.text)
            lines.append(f"[{answer.question_id}] {safe_text}")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)[:max_chars]

# ---------------------------------------------------------------------------
# Stage 2: grounded issues
# ---------------------------------------------------------------------------
@dataclass
class MagicAIIssue:
    """One AI-proposed issue, validated for shape but not yet grounded."""

    title: str
    severity: str
    evidence_refs: list[tuple[str, int]]
    hypothesis: str = ""
    questions: list[str] = field(default_factory=list)
    confidence: float = 0.5


@dataclass
class MagicAIAnalysis:
    issues: list[MagicAIIssue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    raw: str = ""
    provider: str = ""
    model: str = ""
    parse_error: Optional[str] = None


def _slug(text: str, limit: int = 40) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return cleaned[:limit] or "issue"


def resolve_evidence(
    files: Sequence[MagicFile],
    refs: Iterable[tuple[str, int]],
    *,
    max_refs: int = MAX_EVIDENCE_REFS,
) -> list[Evidence]:
    """Resolve (file, line) refs against uploads; discard anything invalid."""
    if max_refs <= 0:
        return []

    by_name = {item.name: item for item in files}
    resolved: list[Evidence] = []
    seen: set[tuple[str, int]] = set()

    for name, line in refs:
        item = by_name.get(name)
        if item is None:
            continue
        if isinstance(line, bool) or not isinstance(line, int):
            continue
        if line < 1 or line > item.line_count:
            continue
        key = (name, line)
        if key in seen:
            continue
        seen.add(key)
        text = item.text.splitlines()[line - 1]
        resolved.append(Evidence(file=name, line=line, text=text))
        if len(resolved) >= max_refs:
            break

    return resolved


def issues_from_logs(files: Sequence[MagicFile]) -> list[Issue]:
    """Deterministic log issues. No AI, no network."""
    issues: list[Issue] = []
    for item in files:
        if item.kind != "log":
            continue
        parsed = logs.parse_log(item.text.encode("utf-8"), item.name)
        for index, group in enumerate(parsed.groups[:MAX_AI_ISSUES]):
            refs = [(item.name, line.number) for line in group.supporting()]
            evidence = resolve_evidence(files, refs)
            if not evidence:
                continue
            issues.append(
                Issue(
                    id=f"log:{item.name}:{index}",
                    title=(
                        f"{logs.CATEGORY_LABEL.get(group.category, group.category)}: "
                        f"{group.signature}"
                    ),
                    category="log",
                    severity=group.severity,
                    evidence=evidence,
                    severity_source="observed",
                    confidence=1.0,
                    observed_problem=(
                        f"Observed {group.category} log event: {group.signature} "
                        f"({group.count} occurrence(s))."
                    ),
                    why_it_matters=(
                        "This is direct evidence of an error or service symptom, "
                        "but does not by itself establish the underlying cause."
                    ),
                    recommendations=_log_remediation(group.category),
                    source_key=group.key,
                )
            )
    return issues


def build_issue_excerpt(
    files: Sequence[MagicFile],
    *,
    max_lines: int = MAX_AI_LINES,
    max_chars: int = MAX_AI_CHARS,
) -> str:
    """Bounded, redacted, line-numbered excerpt of code, text, and log groups.

    The budget is shared fairly so one large file cannot crowd out the others.
    """
    files = list(files)
    if not files:
        return ""

    per_file_lines = max(1, max_lines // len(files))
    per_file_chars = max(200, max_chars // len(files))
    blocks: list[str] = []

    for item in files:
        budget = min(per_file_lines, max(0, max_lines))
        if budget <= 0:
            break

        if item.kind == "log":
            parsed = logs.parse_log(item.text.encode("utf-8"), item.name)
            lines = [f"=== LOG FILE {item.name} (grouped errors) ==="]
            used = 0
            for group in parsed.groups[:MAX_AI_ISSUES]:
                if used >= budget:
                    break
                lines.append(
                    f"group {group.category}:{group.signature} | "
                    f"severity={group.severity} | count={group.count}"
                )
                for line in group.supporting():
                    if used >= budget:
                        break
                    lines.append(f"{line.number}: {line.text}")
                    used += 1
            blocks.append("\n".join(lines)[:per_file_chars])
            continue

        lines = [f"=== FILE {item.name} ({item.kind}) ==="]
        used = 0
        for number, raw_line in enumerate(item.text.splitlines(), start=1):
            if used >= budget:
                break
            safe, _ = redact(raw_line)  # per line keeps line numbers exact
            lines.append(f"{number}: {safe[:MAX_LINE_CHARS]}")
            used += 1
        blocks.append("\n".join(lines)[:per_file_chars])

    return "\n\n".join(blocks)[:max_chars]


def issues_from_ai(
    ai_issues: Sequence[MagicAIIssue],
    files: Sequence[MagicFile],
    existing: Sequence[Issue] = (),
) -> list[Issue]:
    """Ground AI issues; discard ungrounded ones and enrich matching log issues."""
    existing = list(existing)
    ref_owner: dict[tuple[str, int], Issue] = {
        (e.file, e.line): issue for issue in existing for e in issue.evidence
    }
    produced: list[Issue] = []
    enriched_ids: set[str] = set()

    for index, raw in enumerate(ai_issues[:MAX_AI_ISSUES]):
        evidence = resolve_evidence(files, raw.evidence_refs)
        if not evidence:
            continue  # never display an issue without real evidence

        severity = raw.severity if raw.severity in SEVERITIES else "medium"
        owners = {id(ref_owner[(e.file, e.line)]) for e in evidence if (e.file, e.line) in ref_owner}

        if len(owners) == 1 and len(evidence) <= len(ref_owner[(evidence[0].file, evidence[0].line)].evidence):
            target = ref_owner[(evidence[0].file, evidence[0].line)]
            if target.id not in enriched_ids and not target.hypothesis:
                target.hypothesis = raw.hypothesis.strip()
                target.questions = _make_questions(target.id, raw.questions, evidence[0].file, [f.name for f in files])
                enriched_ids.add(target.id)
            continue

        issue_id = f"ai:{_slug(evidence[0].file)}:{index}:{_slug(raw.title)}"
        safe_title, _ = logs.redact(raw.title.strip())
        safe_hypothesis, _ = logs.redact(raw.hypothesis.strip())
        allowed_files = [item.name for item in files]
        if has_unknown_filename(safe_title, allowed_files):
            safe_title = f"Finding in {evidence[0].file}"
        if has_unknown_filename(safe_hypothesis, allowed_files):
            safe_hypothesis = ""
        produced.append(
            Issue(
                id=issue_id,
                title=safe_title or "Untitled AI finding",
                category="code" if any(f.name == evidence[0].file and f.kind == "code" for f in files) else "text",
                severity=severity,
                evidence=evidence,
                hypothesis=safe_hypothesis,
                questions=_make_questions(issue_id, raw.questions, evidence[0].file, [f.name for f in files]),
                severity_source="ai",
                confidence=min(max(raw.confidence, 0.0), 1.0),
            )
        )

    return produced


def filename_mentions(text: str) -> set[str]:
    """Filenames referenced in AI text (normalized to basenames)."""
    names: set[str] = set()
    for raw in _FILENAME_RE.findall(text or ""):
        names.add(raw.replace("\\", "/").split("/")[-1].lower())
    return names


def has_unknown_filename(text: str, allowed_files: Sequence[str]) -> bool:
    """True when AI text mentions a filename that was not uploaded."""
    names = filename_mentions(text)
    if not names:
        return False
    allowed = {name.lower() for name in allowed_files}
    return not names.issubset(allowed)


def _make_questions(
    issue_id: str,
    texts: Sequence[str],
    related_file: Optional[str],
    allowed_files: Sequence[str] = (),
) -> list[Question]:
    questions: list[Question] = []
    for index, text in enumerate(texts[:MAX_QUESTIONS_PER_ISSUE], start=1):
        cleaned, _ = logs.redact((text or "").strip())
        if not cleaned:
            continue
        if allowed_files and has_unknown_filename(cleaned, allowed_files):
            # Never show a question grounded in a file the user did not upload.
            continue
        questions.append(
            Question(
                id=f"{issue_id}:q{index}",
                text=cleaned[:400],
                related_file=related_file,
                issue_id=issue_id,
            )
        )
    return questions


def _log_remediation(category: str) -> list[str]:
    """Offer source-independent next steps without guessing the root cause."""
    options = {
        "exception": [
            "Review the captured stack-trace frames and adjacent log lines.",
            "Compare the failure time with recent application changes and dependency health.",
        ],
        "http_error": [
            "Check server-side logs for the same request and status-code interval.",
            "Review the request context and service health before changing application behavior.",
        ],
        "timeout": [
            "Check dependency health, observed request duration, and configured timeouts.",
            "Confirm expected latency before changing timeout settings.",
        ],
        "error": [
            "Review adjacent log lines and service metrics around the recorded event.",
            "Inspect the corresponding application or dependency health before remediation.",
        ],
    }
    return options.get(
        category,
        ["Review surrounding log context; the underlying application source was not provided."],
    )


# ---------------------------------------------------------------------------
# Stage 3: fix proposals (never applied to originals)
# ---------------------------------------------------------------------------
@dataclass
class FixProposalDraft:
    """Parsed AI fix proposal, not yet validated against the snapshots."""

    explanation: str
    diff: str
    files_changed: list[tuple[str, list[int]]] = field(default_factory=list)
    expected_behavior: str = ""
    tests_needed: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    raw: str = ""
    provider: str = ""
    model: str = ""
    parse_error: Optional[str] = None


@dataclass
class FixProposal:
    """A validated proposal. Always unverified until tests actually run."""

    issue_id: str
    explanation: str
    diff: str  # canonical, validated diff
    files_changed: list[tuple[str, list[int]]]
    expected_behavior: str
    tests_needed: list[str]
    limitations: list[str]
    validation: "patch.PatchValidation"
    status: str = "Proposed — not verified"
    provider: str = ""
    model: str = ""
    workspace: Optional[str] = None
    snapshot_hashes: dict[str, str] = field(default_factory=dict)
    staged_hashes: dict[str, str] = field(default_factory=dict)
    rejection_reason: str = ""


def build_fix_context(
    files: Sequence[MagicFile],
    issue: Issue,
    answers: Sequence[Answer],
    *,
    max_chars: int = 12_000,
) -> str:
    """Bounded, redacted context for a fix proposal.

    Evidence files are shown first; only explicitly shared answers are added.
    """
    by_name = {item.name: item for item in files}
    evidence_files: list[MagicFile] = []
    for evidence in issue.evidence:
        item = by_name.get(evidence.file)
        if item is not None and item.kind != "log" and item not in evidence_files:
            evidence_files.append(item)
    others = [f for f in files if f.kind != "log" and f not in evidence_files]

    header = [
        f"ISSUE: {issue.title}",
        "ALLOWED FILES (use these exact filenames; do not invent others): "
        + ", ".join(sorted(by_name)),
        "OBSERVED EVIDENCE:",
    ]
    for evidence in issue.evidence:
        safe, _ = redact(evidence.text)
        header.append(f"  {evidence.file}:{evidence.line}: {safe[:MAX_LINE_CHARS]}")

    excerpt = build_issue_excerpt(
        evidence_files + others, max_lines=MAX_AI_LINES, max_chars=max_chars
    )
    parts = ["\n".join(header), excerpt]

    shared = [a for a in answers if a.is_shareable]
    if shared:
        lines = ["=== SHARED DEVELOPER ANSWERS ==="]
        for answer in shared:
            safe_text, _ = redact(answer.text)
            lines.append(f"[{answer.question_id}] {safe_text}")
        parts.append("\n".join(lines))

    return "\n\n".join(parts)[:max_chars]


def rejection_reason_for(proposal: FixProposal) -> str:
    """Guaranteed non-empty, sanitized reason for a rejected proposal."""
    reason = (proposal.validation.error or "").strip()
    if reason:
        return reason
    return (
        "The proposal could not be validated against your uploaded files, and no "
        "specific reason was reported. Nothing was staged."
    )


def stage_fix(
    session: MagicSession,
    proposal: FixProposal,
    root: str | Path,
) -> MagicSession:
    """Apply a validated proposal to a fresh temp workspace only."""
    from reprokit import patch

    if not proposal.validation.ok:
        raise MagicInputError("Cannot stage an invalid proposal.")

    sources = {item.name: item.text for item in session.files}
    patch.write_workspace(sources, proposal.validation.files, root)
    proposal.snapshot_hashes = {
        name: patch.sha256_text(text) for name, text in sources.items()
    }
    proposal.staged_hashes = {
        file_patch.path: patch.sha256_text(file_patch.after)
        for file_patch in proposal.validation.files
    }
    proposal.workspace = str(Path(root))
    session.fix_proposal = proposal
    session.proposal = proposal.diff
    session.verification = None
    session.set_step("proposal")
    return session
