"""Magic Mode: uploads, grounded issues, privacy-filtered answers, workflow state.

Stage 2 scope:
- deterministic log issue grouping (reusing :mod:`reprokit.logs`)
- bounded, redacted code/text excerpts for AI issue detection
- every evidence reference is resolved against the uploaded file (file + line)
  so the UI can show the exact supporting lines; unknown refs are discarded
- follow-up questions with explicit Share / Private / Skip visibility

Nothing here runs uploaded code, writes to originals, or applies patches.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal, Optional, Sequence

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
    r"\b[\w./\\-]+\.(?:py|pyi|js|jsx|ts|tsx|java|kt|go|rb|rs|cs|c|h|cpp|hpp|"
    r"php|swift|scala|sh|ps1|sql|log|txt|md|json|ya?ml|toml|ini|cfg|conf|csv)\b",
    re.IGNORECASE,
)

CODE_EXTENSIONS = frozenset(
    {
        ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go",
        ".rb", ".rs", ".cs", ".c", ".h", ".cpp", ".hpp", ".php", ".swift",
        ".scala", ".sh", ".ps1", ".sql",
    }
)
LOG_EXTENSIONS = frozenset({".log"})
TEXT_EXTENSIONS = frozenset(
    {".txt", ".md", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".csv"}
)
ALLOWED_EXTENSIONS = CODE_EXTENSIONS | LOG_EXTENSIONS | TEXT_EXTENSIONS

FileKind = Literal["code", "log", "text"]
Visibility = Literal["shared", "private", "skipped"]

WORKFLOW_STEPS: tuple[tuple[str, str], ...] = (
    ("upload", "Upload code, log, and text files"),
    ("group", "Group issues found across the uploads"),
    ("questions", "Answer targeted follow-up questions"),
    ("proposal", "Generate a reviewable diff proposal"),
    ("verify", "Verify the proposal with approved tests"),
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


def classify_extension(filename: str) -> FileKind:
    """Map a filename to code/log/text, or raise UnsupportedFileTypeError."""
    lowered = (filename or "").lower()
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
    """All Magic Mode state. Stage 2 stores inputs, issues, and answers."""

    files: list[MagicFile] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    answers: dict[str, Answer] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    selected_issue_id: Optional[str] = None
    analysis_note: str = ""
    fix_proposal: Optional["FixProposal"] = None
    step: str = "upload"
    proposal: Optional[str] = None  # reserved; not implemented yet
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
        if self.issues and self.step in ("upload", "group"):
            self.step = "questions"

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
        if safe_name in seen:
            raise MagicInputError(
                f"Duplicate filename {safe_name!r}; rename files instead of "
                "overwriting."
            )
        seen.add(safe_name)

        kind = classify_extension(safe_name)
        size = len(raw or b"")
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
        if b"\x00" in raw:
            warnings.append("Binary content detected; treated as text with NUL bytes removed.")
            raw = raw.replace(b"\x00", b"")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
            warnings.append("Not valid UTF-8; undecodable bytes were replaced.")

        results.append(
            MagicFile(
                name=safe_name,
                kind=kind,
                size_bytes=size,
                sha256=hashlib.sha256(raw).hexdigest(),
                text=text,
                line_count=len(text.splitlines()),
                warnings=tuple(warnings),
            )
        )

    return results


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

        issue_id = f"ai:{index}:{_slug(raw.title)}"
        produced.append(
            Issue(
                id=issue_id,
                title=raw.title.strip() or "Untitled AI finding",
                category="code" if any(f.name == evidence[0].file and f.kind == "code" for f in files) else "text",
                severity=severity,
                evidence=evidence,
                hypothesis=raw.hypothesis.strip(),
                questions=_make_questions(issue_id, raw.questions, evidence[0].file, [f.name for f in files]),
                severity_source="ai",
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
        cleaned = (text or "").strip()
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
