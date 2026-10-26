"""Offline tests for the strict diff validator/applier (reprokit/patch.py)."""

import hashlib
from pathlib import Path

import pytest

from reprokit import patch


SOURCE = "def is_valid(v):\n    return len(v) > 0\n"
SOURCES = {"app.py": SOURCE}

VALID_DIFF = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def is_valid(v):\n"
    "-    return len(v) > 0\n"
    "+    return len(v.strip()) > 0\n"
)


def test_valid_diff_applies_and_counts_lines():
    result = patch.validate_diff(VALID_DIFF, SOURCES)

    assert result.ok is True, result.error
    [file_patch] = result.files
    assert file_patch.path == "app.py"
    assert file_patch.after == "def is_valid(v):\n    return len(v.strip()) > 0\n"
    assert (file_patch.added, file_patch.removed) == (1, 1)
    assert "--- a/app.py" in result.canonical_diff
    assert "+    return len(v.strip()) > 0" in result.canonical_diff


def test_unprefixed_paths_are_normalized():
    diff = VALID_DIFF.replace("a/app.py", "app.py").replace("b/app.py", "app.py")
    result = patch.validate_diff(diff, SOURCES)
    assert result.ok is True, result.error
    assert result.files[0].path == "app.py"


def test_multi_file_diff():
    sources = {"app.py": "a\nb\n", "notes.txt": "x\ny\n"}
    diff = (
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n a\n-b\n+B\n"
        "--- a/notes.txt\n+++ b/notes.txt\n@@ -1,2 +1,2 @@\n x\n-y\n+Y\n"
    )
    result = patch.validate_diff(diff, sources)
    assert result.ok is True, result.error
    assert [f.path for f in result.files] == ["app.py", "notes.txt"]


@pytest.mark.parametrize(
    "bad_path",
    ["../secret.py", "sub/../../app.py", "/etc/passwd", "C:\\temp\\app.py", "sub\\app.py"],
)
def test_unsafe_paths_rejected(bad_path):
    diff = f"--- a/{bad_path}\n+++ b/{bad_path}\n@@ -1 +1 @@\n-a\n+b\n"
    result = patch.validate_diff(diff, SOURCES)
    assert result.ok is False
    assert result.error


def test_unexpected_file_rejected():
    diff = VALID_DIFF.replace("app.py", "other.py")
    result = patch.validate_diff(diff, SOURCES)
    assert result.ok is False
    assert "not one of the uploaded files" in result.error


@pytest.mark.parametrize(
    "diff",
    [
        "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,1 @@\n+new\n",
        "--- a/app.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n",
    ],
)
def test_new_or_deleted_files_rejected(diff):
    result = patch.validate_diff(diff, SOURCES)
    assert result.ok is False
    assert "not supported" in result.error


def test_rename_rejected():
    diff = "--- a/app.py\n+++ b/renamed.py\n@@ -1 +1 @@\n-a\n+b\n"
    result = patch.validate_diff(diff, SOURCES)
    assert result.ok is False
    assert "renames" in result.error


@pytest.mark.parametrize(
    "marker",
    ["GIT binary patch", "Binary files a/app.py and b/app.py differ", "literal 0"],
)
def test_binary_patches_rejected(marker):
    diff = f"--- a/app.py\n+++ b/app.py\n{marker}\n@@ -1 +1 @@\n-a\n+b\n"
    result = patch.validate_diff(diff, SOURCES)
    assert result.ok is False
    assert "binary" in result.error.lower()


def test_oversized_diff_rejected(monkeypatch):
    monkeypatch.setattr(patch, "MAX_DIFF_BYTES", 100)
    result = patch.validate_diff(VALID_DIFF + "x" * 200, SOURCES)
    assert result.ok is False
    assert "byte limit" in result.error


def test_too_many_diff_lines_rejected(monkeypatch):
    monkeypatch.setattr(patch, "MAX_DIFF_LINES", 5)
    result = patch.validate_diff(VALID_DIFF, SOURCES)
    assert result.ok is False
    assert "line limit" in result.error


def test_too_many_files_rejected(monkeypatch):
    monkeypatch.setattr(patch, "MAX_FILES_CHANGED", 1)
    sources = {"app.py": "a\nb\n", "notes.txt": "x\ny\n"}
    diff = (
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n a\n-b\n+B\n"
        "--- a/notes.txt\n+++ b/notes.txt\n@@ -1,2 +1,2 @@\n x\n-y\n+Y\n"
    )
    result = patch.validate_diff(diff, sources)
    assert result.ok is False
    assert "more than" in result.error


@pytest.mark.parametrize(
    "diff",
    [
        "",
        "not a diff",
        "--- a/app.py\n",
        "--- a/app.py\n+++ b/app.py\n",
        "--- a/app.py\n+++ b/app.py\n@@ nonsense @@\n-a\n+b\n",
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n a\n-b\n",
        "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n?bad\n",
        "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-a\n+b\n",
    ],
)
def test_malformed_diffs_rejected(diff):
    result = patch.validate_diff(diff, SOURCES)
    assert result.ok is False
    assert result.error


def test_hunk_count_mismatch_rejected():
    diff = (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,5 +1,2 @@\n"
        " def is_valid(v):\n"
        "-    return len(v) > 0\n"
        "+    return len(v.strip()) > 0\n"
    )
    result = patch.validate_diff(diff, SOURCES)
    assert result.ok is False
    assert "line counts" in result.error


def test_stale_snapshot_rejected():
    diff = VALID_DIFF.replace("-    return len(v) > 0", "-    return len(v) > 1")
    result = patch.validate_diff(diff, SOURCES)
    assert result.ok is False
    assert "stale snapshot" in result.error


def test_hunk_beyond_end_of_file_rejected():
    diff = (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -50,1 +50,1 @@\n"
        "-x\n"
        "+y\n"
    )
    result = patch.validate_diff(diff, SOURCES)
    assert result.ok is False
    assert "beyond the end" in result.error


def test_overlapping_hunks_rejected():
    diff = (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def is_valid(v):\n"
        "-    return len(v) > 0\n"
        "+    return len(v.strip()) > 0\n"
        "@@ -2,1 +2,1 @@\n"
        "-    return len(v.strip()) > 0\n"
        "+    return bool(v.strip())\n"
    )
    result = patch.validate_diff(diff, SOURCES)
    assert result.ok is False
    assert "overlapping" in result.error


def test_no_op_diff_rejected():
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-def is_valid(v):\n+def is_valid(v):\n"
    result = patch.validate_diff(diff, SOURCES)
    assert result.ok is False
    assert "does not change" in result.error


# ---------------------------------------------------------------------------
# Workspace staging: originals must stay immutable
# ---------------------------------------------------------------------------
def test_write_workspace_preserves_originals(tmp_path):
    original_hashes = {
        name: hashlib.sha256(text.encode()).hexdigest() for name, text in SOURCES.items()
    }
    result = patch.validate_diff(VALID_DIFF, SOURCES)
    assert result.ok is True

    root = patch.write_workspace(SOURCES, result.files, tmp_path / "ws")

    after_hashes = {
        name: hashlib.sha256(text.encode()).hexdigest() for name, text in SOURCES.items()
    }
    assert after_hashes == original_hashes, "originating sources were modified"
    assert (root / "app.py").read_text(encoding="utf-8") == result.files[0].after
    assert (root / "app.py").read_text(encoding="utf-8") != SOURCE


def test_write_workspace_copies_unchanged_files_too(tmp_path):
    sources = {"app.py": "a\n", "notes.txt": "keep\n"}
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-a\n+b\n"
    result = patch.validate_diff(diff, sources)
    patch.write_workspace(sources, result.files, tmp_path / "ws")
    assert (tmp_path / "ws" / "notes.txt").read_text(encoding="utf-8") == "keep\n"


def test_write_workspace_rejects_path_escape(tmp_path):
    result = patch.validate_diff(VALID_DIFF, SOURCES)
    bad = patch.FilePatch(path="../escape.py")
    with pytest.raises(patch.PatchError):
        patch.write_workspace(SOURCES, [bad], tmp_path / "ws")

def test_wrong_filename_rejection_names_uploaded_file():
    """Regression: AI diff for basic.py while buggy_calculator.py was uploaded."""
    sources = {"buggy_calculator.py": "def divide(a, b):\n    return a / b\n"}
    diff = (
        "--- a/basic.py\n"
        "+++ b/basic.py\n"
        "@@ -1,2 +1,4 @@\n"
        " def divide(a, b):\n"
        "+    if b == 0:\n"
        "+        return None\n"
        "     return a / b\n"
    )
    result = patch.validate_diff(diff, sources)

    assert result.ok is False
    assert "basic.py" in result.error
    assert "buggy_calculator.py" in result.error
    assert result.files == []


def test_correct_filename_is_accepted_unchanged():
    sources = {"buggy_calculator.py": "def divide(a, b):\n    return a / b\n"}
    diff = (
        "--- a/buggy_calculator.py\n"
        "+++ b/buggy_calculator.py\n"
        "@@ -1,2 +1,4 @@\n"
        " def divide(a, b):\n"
        "+    if b == 0:\n"
        "+        return None\n"
        "     return a / b\n"
    )
    result = patch.validate_diff(diff, sources)
    assert result.ok is True, result.error
    assert result.files[0].path == "buggy_calculator.py"
    assert "if b == 0" in result.files[0].after


# ---------------------------------------------------------------------------
# Regression: malformed unified-diff formats observed in production
# ---------------------------------------------------------------------------
def test_bare_hunk_header_rejected():
    diff = "@@\n def is_valid(v):\n-    return len(v) > 0\n+    return bool(v)\n"
    result = patch.validate_diff(diff, SOURCES)
    assert result.ok is False
    assert "expected '--- ' file header" in result.error


def test_incomplete_hunk_header_rejected():
    diff = (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        " def is_valid(v):\n"
        "-    return len(v) > 0\n"
        "+    return bool(v)\n"
    )
    result = patch.validate_diff(diff, SOURCES)
    assert result.ok is False
    assert "line counts" in result.error


def test_missing_file_headers_rejected():
    diff = "@@ -1,2 +1,2 @@\n def is_valid(v):\n-    return len(v) > 0\n+    return bool(v)\n"
    result = patch.validate_diff(diff, SOURCES)
    assert result.ok is False
    assert "malformed diff" in result.error


def test_valid_fix_for_basic_py():
    sources = {"basic.py": "def is_valid(v):\n    return len(v) > 0\n"}
    diff = (
        "--- a/basic.py\n"
        "+++ b/basic.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def is_valid(v):\n"
        "+    v = v.strip()\n"
        "     return len(v) > 0\n"
    )
    result = patch.validate_diff(diff, sources)
    assert result.ok is True, result.error
    assert result.files[0].path == "basic.py"
    assert result.files[0].after == (
        "def is_valid(v):\n    v = v.strip()\n    return len(v) > 0\n"
    )
    assert (result.files[0].added, result.files[0].removed) == (1, 0)
