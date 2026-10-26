"""Log ingestion, secret redaction, grouping, and bounded AI excerpting.

This module is intentionally dependency-free (stdlib only) and has no
Streamlit or network coupling, so it can be unit-tested deterministically.

Safety properties:
- Hard size and line limits before any parsing.
- Secrets are redacted before grouping, display, or any AI call.
- AI excerpts are bounded by line and character counts.
- Nothing here executes log content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

MAX_FILE_BYTES = 2 * 1024 * 1024  # 2 MB
MAX_LINES = 20_000
MAX_LINE_CHARS = 2_000
MAX_AI_LINES = 40
MAX_AI_CHARS = 12_000
MAX_SUPPORTING_LINES = 5

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

CATEGORY_LABEL = {
    "exception": "Exception",
    "http_error": "HTTP error",
    "timeout": "Timeout",
    "error": "Error",
}

REDACTED = "[REDACTED]"


class LogInputError(ValueError):
    """Base class for unsafe or unusable log input."""


class LogTooLargeError(LogInputError):
    """Raised when the upload exceeds the configured byte limit."""


class EmptyLogError(LogInputError):
    """Raised when the upload contains no usable text."""


@dataclass(frozen=True)
class LogLine:
    number: int  # 1-based line number in the original file
    text: str  # redacted text


@dataclass
class ErrorGroup:
    """One grouped class of repeated log errors."""

    key: str
    category: str
    signature: str
    severity: str
    count: int = 1
    lines: list[LogLine] = field(default_factory=list)
    first_line: int = 0
    last_line: int = 0

    def supporting(self, limit: int = MAX_SUPPORTING_LINES) -> list[LogLine]:
        return self.lines[:limit]


@dataclass
class ParsedLog:
    """Result of parsing one uploaded log file."""

    filename: str
    total_bytes: int
    total_lines: int
    truncated: bool = False
    redaction_count: int = 0
    groups: list[ErrorGroup] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """Deterministic, fact-only incident summary."""
        if not self.groups:
            return (
                f"No known error patterns found in {self.total_lines} lines "
                f"from {self.filename or 'the uploaded file'}."
            )
        counts = {level: 0 for level in SEVERITY_ORDER}
        for group in self.groups:
            counts[group.severity] = counts.get(group.severity, 0) + 1
        top = sorted(
            self.groups, key=lambda g: (SEVERITY_ORDER[g.severity], -g.count, g.first_line)
        )[0]
        return (
            f"{len(self.groups)} error group(s) across {self.total_lines} lines: "
            f"{counts.get('critical', 0)} critical, {counts.get('high', 0)} high, "
            f"{counts.get('medium', 0)} medium, {counts.get('low', 0)} low. "
            f"Most significant: {CATEGORY_LABEL.get(top.category, top.category)} — "
            f"{top.signature} (x{top.count})."
        )


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.DOTALL
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_API_KEY_RES = (
    re.compile(r"\b(?:sk|gsk|pk|rk)[-_][A-Za-z0-9]{8,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{8,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{8,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
_AUTH_RE = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_SECRET_KV_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key)\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_WIN_HOME_RE = re.compile(r"(?i)([A-Z]:\\Users\\)([^\\\s]+)")
_NIX_HOME_RE = re.compile(r"(/home/)([^/\s]+)")
_HEX_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def redact(text: str) -> tuple[str, int]:
    """Redact common secrets and PII. Returns (redacted_text, replacement_count)."""
    if not text:
        return "", 0
    total = 0

    def apply(pattern: re.Pattern[str], replacement: str, source: str) -> str:
        nonlocal total
        result, count = pattern.subn(replacement, source)
        total += count
        return result

    out = apply(_PEM_RE, "[REDACTED_PRIVATE_KEY]", text)
    out = apply(_JWT_RE, "[REDACTED_JWT]", out)
    for pattern in _API_KEY_RES:
        out = apply(pattern, "[REDACTED_API_KEY]", out)
    out = apply(_AUTH_RE, r"\1 [REDACTED]", out)
    out = apply(_SECRET_KV_RE, r"\1=[REDACTED]", out)
    out = apply(_EMAIL_RE, "[REDACTED_EMAIL]", out)
    out = apply(_WIN_HOME_RE, r"\1[REDACTED]", out)
    out = apply(_NIX_HOME_RE, r"\1[REDACTED]", out)
    out = apply(_HEX_RE, "[REDACTED_HEX]", out)
    out = apply(_CARD_RE, "[REDACTED_NUMBER]", out)
    return out, total


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
_HTTP_RES = (
    re.compile(r'\bHTTP/\d(?:\.\d)?"?\s+([45]\d{2})\b'),
    re.compile(r"(?i)\bstatus(?:_?code)?\s*[:=]\s*([45]\d{2})\b"),
    re.compile(
        r"\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+\S+\s+"
        r'(?:HTTP/\d(?:\.\d)?"?\s+)?([45]\d{2})\b'
    ),
    re.compile(
        r"\b([45]\d{2})\s+(?:Bad Request|Unauthorized|Forbidden|Not Found|"
        r"Too Many Requests|Internal Server Error|Bad Gateway|"
        r"Service Unavailable|Gateway Timeout)\b"
    ),
)
_TIMEOUT_RE = re.compile(
    r"(?i)\b(timeout|timed out|deadline exceeded|etimedout|readtimeout|"
    r"connecttimeout|connection timed out)\b"
)
_EXCEPTION_RE = re.compile(r"\b([A-Z][A-Za-z0-9_.]*(?:Error|Exception))\b")
_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\)")
_LEVEL_RE = re.compile(r"\b(CRITICAL|FATAL|SEVERE|ERROR)\b")
_LEVEL_KV_RE = re.compile(r"(?i)\blevel\s*[:=]\s*(critical|fatal|severe|error)\b")

_TIMESTAMP_PREFIX_RE = re.compile(
    r"^\s*\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?\]?\s*"
)
_LEVEL_PREFIX_RE = re.compile(r"^\s*\[(?:TRACE|DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL|FATAL)\]?\s*", re.I)
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
_CONTINUATION_RE = re.compile(
    r"^(?:\s+|File \"|at\s|Caused by:|\.\.\. |During handling|The above exception)"
)


def _http_status(line: str) -> Optional[str]:
    for pattern in _HTTP_RES:
        match = pattern.search(line)
        if match:
            return match.group(1)
    return None


def _classify(line: str) -> Optional[tuple[str, str]]:
    """Return (category, signature) or None when the line is not an error."""
    status = _http_status(line)
    if status:
        return "http_error", f"HTTP {status}"
    if _TIMEOUT_RE.search(line):
        return "timeout", _normalize(line)
    if _EXCEPTION_RE.search(line) or _TRACEBACK_RE.search(line):
        return "exception", _normalize(line)
    if _LEVEL_RE.search(line) or _LEVEL_KV_RE.search(line):
        return "error", _normalize(line)
    return None


def _normalize(line: str) -> str:
    text = _TIMESTAMP_PREFIX_RE.sub("", line.strip())
    text = _LEVEL_PREFIX_RE.sub("", text).strip()
    text = _UUID_RE.sub("<id>", text)
    text = _QUOTED_RE.sub("<str>", text)
    text = re.sub(r"\b0x[0-9a-fA-F]+\b", "<hex>", text)
    text = re.sub(r"\b\d{2,}\b", "<n>", text)
    text = re.sub(r"\s+", " ", text)
    return text[:140] or "unclassified error"


def _severity(category: str, signature: str, line: str) -> str:
    if _TRACEBACK_RE.search(line) or re.search(r"\b(CRITICAL|FATAL|SEVERE)\b", line):
        return "critical"
    if category == "http_error":
        code = signature.split()[-1]
        return "medium" if code.startswith("4") else "high"
    if category in ("timeout", "exception"):
        return "high"
    if re.search(r"(?i)\blevel\s*[:=]\s*(critical|fatal|severe)\b", line):
        return "critical"
    return "medium"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def decode_log_bytes(raw: bytes, filename: str = "") -> tuple[str, list[str]]:
    """Decode upload bytes without raising on bad encodings."""
    warnings: list[str] = []
    if not raw:
        raise EmptyLogError("The uploaded file is empty.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        warnings.append(
            "The file is not valid UTF-8; undecodable bytes were replaced. "
            "Line numbers may be approximate."
        )
    if "\x00" in text:
        warnings.append("The file looks binary (contains NUL bytes); content was sanitized.")
        text = text.replace("\x00", "")
    if filename and not filename.lower().endswith((".log", ".txt")):
        warnings.append("Unexpected extension; only .log and .txt are accepted by the app.")
    return text, warnings


def parse_log(
    raw: bytes,
    filename: str = "",
    *,
    max_bytes: int = MAX_FILE_BYTES,
    max_lines: int = MAX_LINES,
) -> ParsedLog:
    """Parse and group an uploaded log. Raises LogInputError on unsafe input."""
    if max_bytes is not None and len(raw) > max_bytes:
        raise LogTooLargeError(
            f"File is {len(raw):,} bytes, above the {max_bytes:,}-byte limit."
        )

    text, warnings = decode_log_bytes(raw, filename)

    raw_lines = text.splitlines()
    truncated = len(raw_lines) > max_lines
    if truncated:
        raw_lines = raw_lines[:max_lines]
        warnings.append(
            f"Only the first {max_lines:,} lines were analyzed "
            f"({len(text.splitlines()):,} total)."
        )

    redacted_lines: list[LogLine] = []
    redaction_count = 0
    for index, line in enumerate(raw_lines, start=1):
        safe, count = redact(line)
        redaction_count += count
        redacted_lines.append(LogLine(index, safe[:MAX_LINE_CHARS]))

    groups: dict[tuple[str, str], ErrorGroup] = {}
    i = 0
    while i < len(redacted_lines):
        entry = redacted_lines[i]
        classified = _classify(entry.text)
        if classified is None:
            i += 1
            continue

        category, signature = classified
        key = f"{category}:{signature}"
        severity = _severity(category, signature, entry.text)
        group = groups.get((category, signature))
        if group is None:
            group = ErrorGroup(
                key=key,
                category=category,
                signature=signature,
                severity=severity,
                count=0,
                first_line=entry.number,
            )
            groups[(category, signature)] = group
        group.count += 1
        group.last_line = entry.number
        group.lines.append(entry)

        # Attach a bounded traceback continuation to the exception group.
        j = i + 1
        attached = 0
        while (
            j < len(redacted_lines)
            and attached < 12
            and _CONTINUATION_RE.match(redacted_lines[j].text)
        ):
            continuation = redacted_lines[j]
            group.lines.append(continuation)
            group.last_line = continuation.number
            j += 1
            attached += 1
        i = j

    ordered = sorted(
        groups.values(),
        key=lambda g: (SEVERITY_ORDER[g.severity], -g.count, g.first_line),
    )
    return ParsedLog(
        filename=filename,
        total_bytes=len(raw),
        total_lines=len(raw_lines),
        truncated=truncated,
        redaction_count=redaction_count,
        groups=ordered,
        warnings=warnings,
    )


def build_ai_excerpt(
    groups: Sequence[ErrorGroup],
    *,
    max_lines: int = MAX_AI_LINES,
    max_chars: int = MAX_AI_CHARS,
) -> str:
    """Build a bounded, already-redacted excerpt of the most significant groups."""
    if not groups:
        return ""
    ranked = sorted(
        groups, key=lambda g: (SEVERITY_ORDER[g.severity], -g.count, g.first_line)
    )
    out: list[str] = []
    for group in ranked:
        if len(out) >= max_lines:
            break
        out.append(
            f"Group {group.key} | severity={group.severity} | count={group.count}"
        )
        for line in group.supporting():
            if len(out) >= max_lines:
                break
            out.append(f"  {line.number}: {line.text}")
    excerpt = "\n".join(out)
    return excerpt[:max_chars]