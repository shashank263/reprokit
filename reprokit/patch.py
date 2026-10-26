"""Strict unified-diff validation and application for Magic Mode.

Security posture:
- Only files that were uploaded may be patched; paths are normalized and
  checked for absolute paths, ``..``, backslashes, NUL, and duplicates.
- Binary patches, new/deleted files, mode changes, malformed hunks, and
  oversized diffs are rejected.
- Hunks must apply exactly (no fuzz) to the original snapshot. Any mismatch is
  treated as a stale snapshot and rejected.
- :func:`write_workspace` copies *all* sources into a fresh directory and writes
  patched content only there. Originals are never touched.

This module does not execute code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

MAX_DIFF_BYTES = 64 * 1024
MAX_DIFF_LINES = 400
MAX_FILES_CHANGED = 3
MAX_HUNKS_PER_FILE = 40
MAX_PATCHED_LINES = 5_000

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:")

_BINARY_MARKERS = (
    "GIT binary patch",
    "Binary files ",
    "Binary file ",
    "literal 0",
)


class PatchError(ValueError):
    """Raised when a diff is unsafe, malformed, or does not apply cleanly."""


def sha256_text(text: str) -> str:
    """Stable SHA-256 of UTF-8 text, used for snapshot/staging integrity."""
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class HunkLine:
    op: str  # " ", "-", "+"
    text: str


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[HunkLine] = field(default_factory=list)


@dataclass
class FilePatch:
    path: str
    hunks: list[Hunk] = field(default_factory=list)
    before: str = ""
    after: str = ""
    added: int = 0
    removed: int = 0


@dataclass
class PatchValidation:
    ok: bool
    error: str = ""
    files: list[FilePatch] = field(default_factory=list)
    canonical_diff: str = ""


def normalize_path(raw: str) -> str:
    """Normalize a diff path to a safe relative POSIX path or raise PatchError."""
    if not isinstance(raw, str):
        raise PatchError("diff path must be a string")
    path = raw.strip()
    if not path:
        raise PatchError("diff contains an empty path")
    if "\x00" in path:
        raise PatchError("diff path contains a NUL byte")
    if "\\" in path:
        raise PatchError(f"diff path uses backslashes, which are not allowed: {raw!r}")
    if path.startswith("/") or _WINDOWS_ABS_RE.match(path):
        raise PatchError(f"absolute paths are not allowed: {raw!r}")

    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            path = path[2:]
            break

    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise PatchError(f"unsafe path component in {raw!r}")
    return path


def _parse(diff_text: str) -> list[FilePatch]:
    if not isinstance(diff_text, str) or not diff_text.strip():
        raise PatchError("diff is empty")
    if "\x00" in diff_text:
        raise PatchError("diff contains binary data")
    if len(diff_text.encode("utf-8", errors="replace")) > MAX_DIFF_BYTES:
        raise PatchError(f"diff exceeds the {MAX_DIFF_BYTES:,}-byte limit")
    lines = diff_text.splitlines()
    if len(lines) > MAX_DIFF_LINES:
        raise PatchError(f"diff exceeds the {MAX_DIFF_LINES}-line limit")

    for marker in _BINARY_MARKERS:
        if any(marker in line for line in lines):
            raise PatchError("binary patches are not supported")

    patches: list[FilePatch] = []
    index = 0
    total_lines = 0

    while index < len(lines):
        line = lines[index]
        if line.startswith("diff ") or line.startswith("index "):
            raise PatchError("extended git headers are not supported")
        if not line.startswith("--- "):
            raise PatchError("malformed diff: expected '--- ' file header")
        old_raw = line[4:].strip()
        if old_raw == "/dev/null":
            raise PatchError("creating new files is not supported")
        old_path = normalize_path(old_raw)
        if old_path == "dev/null":
            raise PatchError("creating new files is not supported")
        index += 1

        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise PatchError("malformed diff: expected '+++ ' file header")
        new_raw = lines[index][4:].strip()
        if new_raw == "/dev/null":
            raise PatchError("deleting files is not supported")
        new_path = normalize_path(new_raw)
        if new_path == "dev/null":
            raise PatchError("deleting files is not supported")
        if new_path != old_path:
            raise PatchError("diff renames files, which is not supported")
        index += 1

        hunks: list[Hunk] = []
        while index < len(lines) and lines[index].startswith("@@"):
            match = _HUNK_RE.match(lines[index])
            if not match:
                raise PatchError(f"malformed hunk header: {lines[index]!r}")
            old_start = int(match.group(1))
            old_count = int(match.group(2)) if match.group(2) is not None else 1
            new_start = int(match.group(3))
            new_count = int(match.group(4)) if match.group(4) is not None else 1
            index += 1

            body: list[HunkLine] = []
            seen_old = seen_new = 0
            while index < len(lines) and not lines[index].startswith(("@@", "--- ")):
                body_line = lines[index]
                if body_line.startswith("\\"):
                    index += 1  # "\ No newline at end of file" marker
                    continue
                if body_line.startswith("+"):
                    op, text = "+", body_line[1:]
                    seen_new += 1
                elif body_line.startswith("-"):
                    op, text = "-", body_line[1:]
                    seen_old += 1
                elif body_line.startswith(" ") or body_line == "":
                    op, text = " ", body_line[1:] if body_line else ""
                    seen_old += 1
                    seen_new += 1
                else:
                    raise PatchError(f"unexpected diff line: {body_line!r}")
                body.append(HunkLine(op, text))
                total_lines += 1
                if total_lines > MAX_DIFF_LINES:
                    raise PatchError("diff body exceeds the line limit")
                index += 1

            if seen_old != old_count or seen_new != new_count:
                raise PatchError(
                    "hunk line counts do not match its header "
                    f"(declared -{old_count}/+{new_count}, "
                    f"found -{seen_old}/+{seen_new})"
                )
            if seen_old == 0 and seen_new == 0:
                raise PatchError("hunk has no content")
            hunks.append(Hunk(old_start, old_count, new_start, new_count, body))

        if not hunks:
            raise PatchError("file section has no hunks")
        if len(hunks) > MAX_HUNKS_PER_FILE:
            raise PatchError("too many hunks in one file")
        patches.append(FilePatch(path=old_path, hunks=hunks))

    if not patches:
        raise PatchError("diff contains no file changes")
    if len(patches) > MAX_FILES_CHANGED:
        raise PatchError(f"diff changes more than {MAX_FILES_CHANGED} files")
    if len({patch.path for patch in patches}) != len(patches):
        raise PatchError("diff changes the same file more than once")
    return patches


def _apply_hunks(source: str, hunks: Sequence[Hunk]) -> tuple[str, int, int]:
    src = source.splitlines()
    out: list[str] = []
    cursor = 0
    added = removed = 0

    for hunk in sorted(hunks, key=lambda h: h.old_start):
        start = hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1
        if start < cursor:
            raise PatchError("overlapping or out-of-order hunks")
        if start > len(src):
            raise PatchError("hunk starts beyond the end of the file")

        out.extend(src[cursor:start])
        idx = start
        for line in hunk.lines:
            if line.op in (" ", "-"):
                if idx >= len(src) or src[idx] != line.text:
                    raise PatchError(
                        f"stale snapshot: context does not match at line {idx + 1}"
                    )
                if line.op == " ":
                    out.append(src[idx])
                else:
                    removed += 1
                idx += 1
            else:
                out.append(line.text)
                added += 1
        cursor = idx

    out.extend(src[cursor:])
    if len(out) > MAX_PATCHED_LINES:
        raise PatchError("patched file exceeds the line limit")

    result = "\n".join(out)
    if source.endswith("\n"):
        result += "\n"
    return result, added, removed


def validate_diff(diff_text: str, sources: Mapping[str, str]) -> PatchValidation:
    """Validate a diff against the exact uploaded snapshots.

    ``sources`` maps uploaded filename -> original text. Returns a result object
    rather than raising, so the UI can show a clear error.
    """
    try:
        patches = _parse(diff_text)
    except PatchError as exc:
        return PatchValidation(ok=False, error=str(exc))

    allowed = {normalize_path(name): text for name, text in sources.items()}
    for patch in patches:
        if patch.path not in allowed:
            return PatchValidation(
                ok=False,
                error=(
                    f"diff touches {patch.path!r}, which is not one of the "
                    f"uploaded files ({', '.join(sorted(allowed))})"
                ),
            )
        try:
            patch.before = allowed[patch.path]
            patch.after, patch.added, patch.removed = _apply_hunks(
                patch.before, patch.hunks
            )
        except PatchError as exc:
            return PatchValidation(
                ok=False, error=f"{patch.path}: {exc}"
            )
        if patch.before == patch.after:
            return PatchValidation(
                ok=False, error=f"{patch.path}: diff does not change the file"
            )

    canonical = _render_canonical(patches)
    return PatchValidation(ok=True, files=patches, canonical_diff=canonical)


def _render_canonical(patches: Sequence[FilePatch]) -> str:
    """Render the validated diff with normalized paths (what the UI shows)."""
    out: list[str] = []
    for patch in patches:
        out.append(f"--- a/{patch.path}")
        out.append(f"+++ b/{patch.path}")
        for hunk in patch.hunks:
            out.append(
                f"@@ -{hunk.old_start},{hunk.old_count} "
                f"+{hunk.new_start},{hunk.new_count} @@"
            )
            for line in hunk.lines:
                out.append(line.op + line.text)
    return "\n".join(out) + "\n"


def write_workspace(
    sources: Mapping[str, str],
    patches: Sequence[FilePatch],
    root: str | Path,
) -> Path:
    """Copy all sources into ``root`` and write patched files there only.

    Raises PatchError if any path escapes the workspace or a source is missing.
    """
    base = Path(root).resolve()
    base.mkdir(parents=True, exist_ok=True)

    for name, text in sources.items():
        relative = normalize_path(name)
        target = (base / relative).resolve()
        if base not in target.parents and target != base:
            raise PatchError(f"path escapes workspace: {name!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    for patch in patches:
        relative = normalize_path(patch.path)
        target = (base / relative).resolve()
        if base not in target.parents and target != base:
            raise PatchError(f"path escapes workspace: {patch.path!r}")
        if not target.is_file():
            raise PatchError(f"target file missing from workspace: {patch.path!r}")
        target.write_text(patch.after, encoding="utf-8")

    return base