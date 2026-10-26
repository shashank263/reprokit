"""Offline tests for reprokit/logs.py — parsing, grouping, redaction, limits."""

import pytest

from reprokit import logs


_SAMPLE = """\
2024-10-26T10:00:00Z INFO app starting
2024-10-26T10:00:01Z ERROR database connection failed
2024-10-26T10:00:02Z GET /users 500
Traceback (most recent call last):
  File "app.py", line 10, in <module>
ValueError: invalid input 'abc'
2024-10-26T10:00:03Z WARN request timeout after 30s
2024-10-26T10:00:04Z GET /missing 404
2024-10-26T10:00:05Z HTTP/1.1 503 Service Unavailable
2024-10-26T10:00:06Z HTTP/1.1 503 Service Unavailable
2024-10-26T10:00:07Z HTTP/1.1 503 Service Unavailable
"""


def _parse(text: str, **kwargs):
    return logs.parse_log(text.encode("utf-8"), "sample.log", **kwargs)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
def test_redacts_common_secrets():
    text = (
        "api_key=gsk_abcdefghijklmnop1234\n"
        "Authorization: Bearer abcdefghijklmnop\n"
        "password=hunter2\n"
        "user email jane.doe@example.com\n"
        "path C:\\Users\\jane\\app\\secret.txt\n"
        "token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk\n"
    )
    safe, count = logs.redact(text)
    assert count >= 5
    for secret in ("gsk_abcdefghijklmnop1234", "hunter2", "jane.doe@example.com"):
        assert secret not in safe
    assert "REDACTED" in safe


def test_redaction_keeps_context_lines_readable():
    safe, _ = logs.redact('ValueError: bad input for user "bob"')
    assert safe.startswith("ValueError: bad input")
    assert "bob" in safe  # quotes/names are not secrets


def test_parsed_lines_are_redacted():
    log = _parse("2024-10-26T10:00:01Z ERROR login failed for password=hunter2\n")
    rendered = "\n".join(line.text for group in log.groups for line in group.lines)
    assert "hunter2" not in rendered
    assert log.redaction_count >= 1


# ---------------------------------------------------------------------------
# Classification and grouping
# ---------------------------------------------------------------------------
def test_groups_errors_exceptions_http_and_timeouts():
    log = _parse(_SAMPLE)
    categories = {group.category for group in log.groups}
    assert {"exception", "http_error", "timeout", "error"} <= categories

    http_503 = next(g for g in log.groups if g.signature == "HTTP 503")
    assert http_503.count == 3
    assert http_503.category == "http_error"
    assert http_503.severity == "high"

    http_404 = next(g for g in log.groups if g.signature == "HTTP 404")
    assert http_404.severity == "medium"

    timeout = next(g for g in log.groups if g.category == "timeout")
    assert timeout.severity == "high"


def test_traceback_is_critical_and_keeps_continuation():
    log = _parse(_SAMPLE)
    traceback_group = next(
        g for g in log.groups if g.signature.startswith("Traceback")
    )
    assert traceback_group.severity == "critical"
    assert any("File \"app.py\"" in line.text for line in traceback_group.lines)


def test_similar_exceptions_are_grouped_after_normalization():
    text = (
        "2024-10-26T10:00:01Z ValueError: bad value 'a'\n"
        "2024-10-26T10:00:02Z ValueError: bad value 'b'\n"
    )
    log = _parse(text)
    exception_groups = [g for g in log.groups if g.category == "exception"]
    assert len(exception_groups) == 1
    assert exception_groups[0].count == 2


def test_no_matches_is_empty_but_safe():
    log = _parse("2024-10-26T10:00:00Z INFO all good\n")
    assert log.groups == []
    assert "No known error patterns" in log.summary()


def test_incident_summary_counts_groups():
    summary = _parse(_SAMPLE).summary()
    assert "error group(s)" in summary
    assert "1 critical" in summary
    assert "Most significant:" in summary
    assert "Traceback" in summary  # critical outranks the repeated 503s


# ---------------------------------------------------------------------------
# Safety limits / malformed input
# ---------------------------------------------------------------------------
def test_empty_upload_is_rejected():
    with pytest.raises(logs.EmptyLogError):
        logs.parse_log(b"", "empty.log")


def test_oversized_upload_is_rejected():
    with pytest.raises(logs.LogTooLargeError):
        logs.parse_log(b"x" * 100, "big.log", max_bytes=50)


def test_line_limit_truncates_with_warning():
    text = "\n".join(f"2024-10-26T10:00:00Z ERROR failure {i}" for i in range(30))
    log = logs.parse_log(text.encode(), "big.log", max_lines=10)
    assert log.truncated is True
    assert log.total_lines == 10
    assert any("first 10" in w for w in log.warnings)


def test_binary_nul_bytes_are_sanitized_not_crashing():
    log = logs.parse_log(b"2024-10-26T10:00:00Z ERROR bad\x00thing\n", "x.log")
    assert any("binary" in w.lower() for w in log.warnings)


def test_non_utf8_bytes_are_decoded_with_warning():
    raw = b"2024-10-26T10:00:00Z ERROR caf\xe9 failure\n"
    log = logs.parse_log(raw, "latin.log")
    assert any("UTF-8" in w for w in log.warnings)
    assert log.groups


def test_unexpected_extension_warns():
    log = logs.parse_log(b"2024-10-26T10:00:00Z ERROR boom\n", "notes.csv")
    assert any("extension" in w for w in log.warnings)


# ---------------------------------------------------------------------------
# Bounded, redacted AI excerpt
# ---------------------------------------------------------------------------
def test_ai_excerpt_is_bounded_and_redacted():
    text = "\n".join(
        f"2024-10-26T10:00:00Z ERROR failure with token=supersecrettoken{i} "
        f"for request {i}"
        for i in range(200)
    )
    log = logs.parse_log(text.encode(), "many.log")
    excerpt = logs.build_ai_excerpt(log.groups, max_lines=12, max_chars=500)

    assert excerpt
    assert len(excerpt.splitlines()) <= 12
    assert len(excerpt) <= 500
    assert "Group " in excerpt
    assert "supersecrettoken" not in excerpt
    assert "REDACTED" in excerpt


def test_ai_excerpt_empty_without_groups():
    assert logs.build_ai_excerpt([]) == ""