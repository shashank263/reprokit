"""Tests for reprokit/analysis.py.

All of these run WITHOUT live API credentials and never touch the network.
Parsing is tested directly; the provider call path is tested with a stubbed
``openai.OpenAI`` client, which also lets us assert exactly how the client
was constructed (Groq base URL and key).
"""

import ast
import json

import openai
import pytest

from reprokit import analysis


_VALID_PAYLOAD = {
    "problem_summary": "Whitespace-only usernames are accepted.",
    "likely_cause": "Likely caused by a length check that does not strip whitespace.",
    "reproduction_steps": ["Open the form", "Submit a username of only spaces"],
    "suggested_test_cases": ["normal: accepts 'alice'", "edge: rejects '   '"],
    "missing_information": ["Should tabs/newlines count as whitespace?"],
}
_VALID_JSON = json.dumps(_VALID_PAYLOAD)

_GROQ_KEY = "gsk-test-not-real"


def _clear_provider_env(monkeypatch):
    for name in (
        "GROQ_API_KEY",
        "OPENAI_API_KEY",
        "REPROKIT_AI_PROVIDER",
        "REPROKIT_AI_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# parse_analysis_json (provider-independent)
# ---------------------------------------------------------------------------
def test_parse_valid_json():
    parsed = analysis.parse_analysis_json(_VALID_JSON)
    assert parsed.problem_summary == _VALID_PAYLOAD["problem_summary"]
    assert parsed.likely_cause.startswith("Likely caused by")
    assert parsed.reproduction_steps == _VALID_PAYLOAD["reproduction_steps"]
    assert parsed.suggested_test_cases == _VALID_PAYLOAD["suggested_test_cases"]
    assert parsed.missing_information == _VALID_PAYLOAD["missing_information"]
    assert parsed.parse_error is None


def test_parse_json_wrapped_in_code_fence():
    fenced = "```json\n" + _VALID_JSON + "\n```"
    parsed = analysis.parse_analysis_json(fenced)
    assert parsed.problem_summary == _VALID_PAYLOAD["problem_summary"]


def test_parse_json_with_surrounding_prose():
    noisy = "Sure! Here is the analysis:\n" + _VALID_JSON + "\nLet me know."
    parsed = analysis.parse_analysis_json(noisy)
    assert parsed.problem_summary == _VALID_PAYLOAD["problem_summary"]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "not json at all",
        "[]",  # non-object root
        json.dumps({"problem_summary": "x"}),  # missing keys
    ],
)
def test_parse_rejects_malformed(bad):
    with pytest.raises(analysis.MalformedAnalysisError):
        analysis.parse_analysis_json(bad)


def test_parse_rejects_wrong_field_types():
    payload = dict(_VALID_PAYLOAD)
    payload["reproduction_steps"] = "not a list"
    with pytest.raises(analysis.MalformedAnalysisError):
        analysis.parse_analysis_json(json.dumps(payload))

    payload2 = dict(_VALID_PAYLOAD)
    payload2["problem_summary"] = 123
    with pytest.raises(analysis.MalformedAnalysisError):
        analysis.parse_analysis_json(json.dumps(payload2))


# ---------------------------------------------------------------------------
# Stubbed OpenAI-compatible client
# ---------------------------------------------------------------------------
def _make_response(content):
    class _Msg:
        pass

    class _Choice:
        pass

    class _Resp:
        pass

    msg = _Msg()
    msg.content = content
    choice = _Choice()
    choice.message = msg
    resp = _Resp()
    resp.choices = [choice]
    return resp


def _stub_openai(monkeypatch, *, content=None, exc=None):
    """Replace openai.OpenAI and record constructor/create kwargs."""
    captured = {}

    class _Completions:
        def create(self, **kwargs):
            captured["create"] = kwargs
            if exc is not None:
                raise exc
            return _make_response(content)

    class _Client:
        def __init__(self, **kwargs):
            captured["init"] = kwargs
            self.chat = type("_Chat", (), {"completions": _Completions()})()

    monkeypatch.setattr(openai, "OpenAI", _Client)
    return captured


# ---------------------------------------------------------------------------
# Provider selection / configuration (offline)
# ---------------------------------------------------------------------------
def test_groq_is_selected_provider_by_default(monkeypatch):
    _clear_provider_env(monkeypatch)
    status = analysis.provider_status()
    assert status.provider == "groq"
    assert status.available is False
    # Pin the chosen model so a stale/removed Groq model ID is caught in review.
    assert analysis.DEFAULT_GROQ_MODEL == "openai/gpt-oss-120b"
    assert status.model == analysis.DEFAULT_GROQ_MODEL


def test_groq_provider_uses_groq_base_url():
    assert analysis.GROQ_BASE_URL == "https://api.groq.com/openai/v1"
    assert analysis.GROQ_KEY_ENV == "GROQ_API_KEY"
    assert analysis.SELECTED_PROVIDER == "groq"


def test_missing_groq_key_explains_config(monkeypatch):
    _clear_provider_env(monkeypatch)
    result = analysis.analyze_bug_report("Usernames of only spaces are accepted.")
    assert result.ok is False
    assert result.analysis is None
    assert result.status.provider == "groq"
    assert result.status.available is False
    assert "GROQ_API_KEY" in result.error
    assert "REPROKIT_AI_MODEL" in result.error


def test_no_silent_fallback_to_openai(monkeypatch):
    """An OpenAI key must NOT be used when Groq is the selected provider."""
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai-not-real")

    status = analysis.provider_status()
    assert status.provider == "groq"
    assert status.available is False

    result = analysis.analyze_bug_report("Some bug report.")
    assert result.ok is False
    assert result.status.provider == "groq"
    assert "GROQ_API_KEY" in result.error


def test_unknown_provider_is_unavailable_and_does_not_fallback(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("REPROKIT_AI_PROVIDER", "not-a-provider")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai-not-real")

    status = analysis.provider_status()
    assert status.available is False
    assert status.provider == "not-a-provider"
    assert "No automatic fallback" in status.reason

    result = analysis.analyze_bug_report("Some bug report.")
    assert result.ok is False
    assert "No automatic fallback" in result.error


def test_empty_report_short_circuits(monkeypatch):
    _clear_provider_env(monkeypatch)
    result = analysis.analyze_bug_report("   ")
    assert result.ok is False
    assert result.analysis is None
    assert "empty" in result.error.lower()


# ---------------------------------------------------------------------------
# Groq call path with a stubbed client
# ---------------------------------------------------------------------------
def test_groq_success_uses_groq_endpoint_and_key(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", _GROQ_KEY)
    captured = _stub_openai(monkeypatch, content=_VALID_JSON)

    result = analysis.analyze_bug_report("Whitespace usernames accepted.")

    assert result.ok is True
    assert result.error is None
    assert result.analysis is not None
    assert result.analysis.problem_summary == _VALID_PAYLOAD["problem_summary"]
    assert result.analysis.provider == "groq"
    assert result.analysis.model == analysis.DEFAULT_GROQ_MODEL

    # Explicit Groq endpoint + the Groq key (never OPENAI_API_KEY).
    assert captured["init"]["base_url"] == "https://api.groq.com/openai/v1"
    assert captured["init"]["api_key"] == _GROQ_KEY
    assert captured["create"]["model"] == analysis.DEFAULT_GROQ_MODEL

    system_prompt = captured["create"]["messages"][0]["content"]
    assert "Do NOT invent test identifiers" in system_prompt


def test_reprokit_ai_model_overrides_default(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", _GROQ_KEY)
    monkeypatch.setenv("REPROKIT_AI_MODEL", "openai/gpt-oss-20b")
    captured = _stub_openai(monkeypatch, content=_VALID_JSON)

    result = analysis.analyze_bug_report("Some bug report.")

    assert result.ok is True
    assert result.analysis.model == "openai/gpt-oss-20b"
    assert captured["create"]["model"] == "openai/gpt-oss-20b"


def test_groq_malformed_response_keeps_raw(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", _GROQ_KEY)
    _stub_openai(monkeypatch, content="not json")

    result = analysis.analyze_bug_report("Some bug report.")

    assert result.ok is False
    assert result.analysis is not None
    assert result.analysis.raw == "not json"
    assert result.analysis.parse_error is not None
    assert result.analysis.provider == "groq"
    assert "parse" in result.error.lower()


def test_groq_api_error_is_sanitized(monkeypatch):
    # The exception text deliberately contains the key to prove it is not leaked.
    secret = "gsk-test-super-secret-value"
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", secret)
    _stub_openai(monkeypatch, exc=RuntimeError(f"boom {secret}"))

    result = analysis.analyze_bug_report("Some bug report.")

    assert result.ok is False
    assert result.analysis is None
    assert result.error is not None
    assert secret not in result.error
    assert secret not in (result.status.reason or "")
    assert "GROQ_API_KEY" in result.error
    assert "Groq API call failed" in result.error


# ---------------------------------------------------------------------------
# OpenAI remains available only when explicitly selected
# ---------------------------------------------------------------------------
def test_openai_requires_explicit_selection(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("REPROKIT_AI_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai-not-real")
    captured = _stub_openai(monkeypatch, content=_VALID_JSON)

    result = analysis.analyze_bug_report("Some bug report.")

    assert result.ok is True
    assert result.status.provider == "openai"
    assert result.analysis.provider == "openai"
    assert captured["init"]["api_key"] == "sk-test-openai-not-real"
    # OpenAI is not forced onto the Groq base URL.
    assert "base_url" not in captured["init"]


# ---------------------------------------------------------------------------
# Safety: AI output must never trigger the test runner
# ---------------------------------------------------------------------------
def test_analysis_module_does_not_import_runner():
    # Verify via AST that the module never imports the runner (docstrings may
    # mention the name, so a substring check would be a false positive).
    with open(analysis.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
    assert not any(
        name == "reprokit.runner" or name.startswith("reprokit.runner.")
        for name in imported
    ), imported
    assert not hasattr(analysis, "run_tests")


def test_analysis_never_calls_runner(monkeypatch):
    import reprokit.runner as runner

    def _boom(*a, **k):
        raise AssertionError("the test runner must never be called from analysis")

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", _GROQ_KEY)
    _stub_openai(monkeypatch, content=_VALID_JSON)
    monkeypatch.setattr(runner, "run_tests", _boom)

    result = analysis.analyze_bug_report("Whitespace usernames accepted.")
    assert result.ok is True  # analysis succeeded without touching the runner

# ---------------------------------------------------------------------------
# Log-excerpt analysis (TraceGuard-style workflow)
# ---------------------------------------------------------------------------
_LOG_PAYLOAD = {
    "incident_summary": "Repeated HTTP 503s alongside a ValueError.",
    "findings": [
        {
            "group_key": "http_error:HTTP 503",
            "likely_cause": "Likely caused by upstream service saturation.",
            "recommended_steps": ["Check upstream health", "Review recent deploys"],
            "severity": "high",
        },
        {
            "group_key": "exception:ValueError: invalid input <str>",
            "likely_cause": "Likely caused by missing input validation.",
            "recommended_steps": ["Add validation for whitespace-only input"],
            "severity": "medium",
        },
    ],
    "missing_information": ["When did the 503s start?"],
}
_LOG_JSON = json.dumps(_LOG_PAYLOAD)


def test_parse_log_analysis_valid():
    data = analysis.parse_log_analysis_json(_LOG_JSON)
    assert data.incident_summary == _LOG_PAYLOAD["incident_summary"]
    assert len(data.findings) == 2
    assert data.findings[0].group_key == "http_error:HTTP 503"
    assert data.findings[0].severity == "high"
    assert data.findings[0].recommended_steps == [
        "Check upstream health",
        "Review recent deploys",
    ]
    assert data.missing_information == ["When did the 503s start?"]


def test_parse_log_analysis_accepts_fenced_json():
    data = analysis.parse_log_analysis_json("```json\n" + _LOG_JSON + "\n```")
    assert len(data.findings) == 2


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not json",
        "[]",
        json.dumps({"findings": "nope"}),
        json.dumps({"findings": [{"likely_cause": "x"}]}),  # missing group_key
        json.dumps(
            {"findings": [{"group_key": "k", "recommended_steps": "x"}]}
        ),
        json.dumps(
            {"findings": [{"group_key": "k", "severity": "extremely-bad"}]}
        ),
        json.dumps({"missing_information": "ask"}),
    ],
)
def test_parse_log_analysis_rejects_malformed(bad):
    with pytest.raises(analysis.MalformedAnalysisError):
        analysis.parse_log_analysis_json(bad)


def test_analyze_log_excerpt_unavailable_without_key(monkeypatch):
    _clear_provider_env(monkeypatch)
    result = analysis.analyze_log_excerpt(
        "Group error:boom | severity=medium | count=1\n  1: ERROR boom"
    )
    assert result.ok is False
    assert result.data is None
    assert result.status.provider == "groq"
    assert "GROQ_API_KEY" in result.error


def test_analyze_log_excerpt_empty_is_rejected(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", _GROQ_KEY)
    result = analysis.analyze_log_excerpt("   ")
    assert result.ok is False
    assert "No log excerpt" in result.error


def test_analyze_log_excerpt_success_and_redacted_before_send(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", _GROQ_KEY)
    captured = _stub_openai(monkeypatch, content=_LOG_JSON)

    excerpt = (
        "Group error:login | severity=medium | count=1\n"
        "  12: ERROR login failed token=gsk_supersecretvalue123"
    )
    result = analysis.analyze_log_excerpt(excerpt)

    assert result.ok is True
    assert result.data is not None
    assert result.data.provider == "groq"
    assert result.data.model == analysis.DEFAULT_GROQ_MODEL
    assert [f.group_key for f in result.data.findings] == [
        "http_error:HTTP 503",
        "exception:ValueError: invalid input <str>",
    ]

    assert captured["init"]["base_url"] == analysis.GROQ_BASE_URL
    assert captured["init"]["api_key"] == _GROQ_KEY
    user_message = captured["create"]["messages"][1]["content"]
    assert "gsk_supersecretvalue123" not in user_message
    assert "REDACTED" in user_message
    assert "Group error:login" in user_message


def test_analyze_log_excerpt_malformed_keeps_raw(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", _GROQ_KEY)
    _stub_openai(monkeypatch, content="not json")

    result = analysis.analyze_log_excerpt("Group error:x | severity=low | count=1")

    assert result.ok is False
    assert result.data is not None
    assert result.data.raw == "not json"
    assert result.data.parse_error is not None
    assert "parse" in result.error.lower()


def test_analyze_log_excerpt_api_error_is_sanitized(monkeypatch):
    secret = "gsk-log-secret-value"
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", secret)
    _stub_openai(monkeypatch, exc=RuntimeError(f"failure {secret}"))

    result = analysis.analyze_log_excerpt("Group error:x | severity=low | count=1")

    assert result.ok is False
    assert result.error is not None
    assert secret not in result.error
    assert "Groq API call failed" in result.error


# ---------------------------------------------------------------------------
# Magic Mode issue detection (Stage 2)
# ---------------------------------------------------------------------------
_MAGIC_PAYLOAD = {
    "issues": [
        {
            "title": "Unvalidated whitespace input",
            "severity": "high",
            "confidence": 0.83,
            "evidence": [{"file": "app.py", "line": 2}],
            "hypothesis": "Likely caused by a length check that does not strip.",
            "questions": ["Is whitespace-only input valid?"],
        }
    ],
    "notes": ["Only one suspicious line found."],
}
_MAGIC_JSON = json.dumps(_MAGIC_PAYLOAD)


def test_parse_magic_analysis_valid():
    from reprokit import magic

    data = analysis.parse_magic_analysis_json(_MAGIC_JSON)
    assert isinstance(data, magic.MagicAIAnalysis)
    assert len(data.issues) == 1
    issue = data.issues[0]
    assert issue.title == "Unvalidated whitespace input"
    assert issue.severity == "high"
    assert issue.confidence == 0.83
    assert issue.evidence_refs == [("app.py", 2)]
    assert issue.hypothesis.startswith("Likely caused by")
    assert data.notes == ["Only one suspicious line found."]
    assert data.parse_error is None


def test_parse_magic_analysis_accepts_fenced_json():
    data = analysis.parse_magic_analysis_json("```json\n" + _MAGIC_JSON + "\n```")
    assert len(data.issues) == 1


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not json",
        "[]",
        json.dumps({"issues": "nope"}),
        json.dumps({"issues": [{"severity": "high"}]}),  # no title
        json.dumps({"issues": [{"title": "x", "severity": "apocalyptic"}]}),
        json.dumps({"issues": [{"title": "x", "confidence": 1.5}]}),
        json.dumps({"issues": [{"title": "x", "evidence": [{"file": "a.py"}]}]}),
        json.dumps({"issues": [{"title": "x", "evidence": [{"file": "a.py", "line": "2"}]}]}),
        json.dumps({"issues": [{"title": "x", "evidence": [{"file": "a.py", "line": True}]}]}),
        json.dumps({"issues": [{"title": "x", "questions": "why?"}]}),
        json.dumps({"issues": [], "notes": "nope"}),
    ],
)
def test_parse_magic_analysis_rejects_malformed(bad):
    with pytest.raises(analysis.MalformedAnalysisError):
        analysis.parse_magic_analysis_json(bad)


def test_parse_magic_analysis_rejects_non_object_evidence_entry():
    payload = {"issues": [{"title": "x", "evidence": ["app.py:1"]}]}
    with pytest.raises(analysis.MalformedAnalysisError):
        analysis.parse_magic_analysis_json(json.dumps(payload))


def test_magic_analysis_unavailable_without_key(monkeypatch):
    _clear_provider_env(monkeypatch)
    result = analysis.analyze_magic_issues("1: code line")
    assert result.ok is False
    assert "GROQ_API_KEY" in result.error


def test_magic_analysis_sends_only_shared_answers(monkeypatch):
    """Private and skipped answers must never appear in the request payload."""
    from reprokit import magic

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", _GROQ_KEY)
    captured = _stub_openai(monkeypatch, content=_MAGIC_JSON)

    session = magic.MagicSession()
    for qid in ("q1", "q2", "q3"):
        session.add_question(magic.Question(id=qid, text=f"question {qid}"))
    shared = session.answer("q1", "production environment", "shared")
    private = session.answer("q2", "internal host db-prod-01", "private")
    skipped = session.answer("q3", "unused note", "skipped")

    # Even if all answers are passed, only shareable ones may survive.
    result = analysis.analyze_magic_issues(
        "1: def f(): pass",
        shared_answers=[shared, private, skipped],
    )

    assert result.ok is True
    payload = captured["create"]["messages"][1]["content"]
    assert "production environment" in payload
    assert "internal host db-prod-01" not in payload
    assert "unused note" not in payload
    assert "q2" not in payload
    assert "q3" not in payload


def test_magic_analysis_redacts_excerpt_before_send(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", _GROQ_KEY)
    captured = _stub_openai(monkeypatch, content=_MAGIC_JSON)

    analysis.analyze_magic_issues("1: token=gsk_abcdefghijklmnop1234")

    payload = captured["create"]["messages"][1]["content"]
    assert "gsk_abcdefghijklmnop1234" not in payload
    assert "REDACTED" in payload


def test_magic_analysis_malformed_keeps_raw(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", _GROQ_KEY)
    _stub_openai(monkeypatch, content="not json")

    result = analysis.analyze_magic_issues("1: code")

    assert result.ok is False
    assert result.data is not None
    assert result.data.raw == "not json"
    assert result.data.parse_error is not None
    assert "parse" in result.error.lower()


def test_magic_analysis_api_error_is_sanitized(monkeypatch):
    secret = "gsk-magic-secret"
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", secret)
    _stub_openai(monkeypatch, exc=RuntimeError(f"boom {secret}"))

    result = analysis.analyze_magic_issues("1: code")

    assert result.ok is False
    assert secret not in result.error
    assert "Groq API call failed" in result.error

# ---------------------------------------------------------------------------
# Magic Mode fix proposals (Stage 3)
# ---------------------------------------------------------------------------
_FIX_DIFF = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def is_valid(v):\n"
    "-    return len(v) > 0\n"
    "+    return len(v.strip()) > 0\n"
)
_FIX_PAYLOAD = {
    "explanation": "Strip whitespace before the emptiness check.",
    "diff": _FIX_DIFF,
    "files_changed": [{"path": "app.py", "lines": [2]}],
    "expected_behavior": "Whitespace-only input is rejected.",
    "tests_needed": ["normal: accepts 'alice'", "edge: rejects '   '"],
    "limitations": ["Assumes the caller passes a string."],
}
_FIX_JSON = json.dumps(_FIX_PAYLOAD)


def test_parse_fix_proposal_valid():
    data = analysis.parse_fix_proposal_json(_FIX_JSON)
    assert data.explanation == _FIX_PAYLOAD["explanation"]
    assert data.diff == _FIX_DIFF.strip()
    assert data.files_changed == [("app.py", [2])]
    assert data.expected_behavior == _FIX_PAYLOAD["expected_behavior"]
    assert data.tests_needed == _FIX_PAYLOAD["tests_needed"]
    assert data.limitations == _FIX_PAYLOAD["limitations"]
    assert data.parse_error is None


def test_parse_fix_proposal_accepts_fenced_json():
    data = analysis.parse_fix_proposal_json("```json\n" + _FIX_JSON + "\n```")
    assert data.diff == _FIX_DIFF.strip()


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not json",
        "[]",
        json.dumps({"diff": _FIX_DIFF}),  # no explanation
        json.dumps({"explanation": "x"}),  # no diff
        json.dumps({"explanation": "x", "diff": _FIX_DIFF, "files_changed": "app.py"}),
        json.dumps({"explanation": "x", "diff": _FIX_DIFF, "files_changed": [{"path": "app.py", "lines": "2"}]}),
        json.dumps({"explanation": "x", "diff": _FIX_DIFF, "files_changed": [{"path": "app.py", "lines": [True]}]}),
        json.dumps({"explanation": "x", "diff": _FIX_DIFF, "files_changed": [{"path": "app.py", "lines": [-1]}]}),
        json.dumps({"explanation": "x", "diff": _FIX_DIFF}),  # no expected_behavior
        json.dumps({"explanation": "x", "diff": _FIX_DIFF, "expected_behavior": "y", "tests_needed": "t"}),
    ],
)
def test_parse_fix_proposal_rejects_malformed(bad):
    with pytest.raises(analysis.MalformedAnalysisError):
        analysis.parse_fix_proposal_json(bad)


def test_propose_fix_unavailable_without_key(monkeypatch):
    _clear_provider_env(monkeypatch)
    result = analysis.propose_fix("ISSUE: x")
    assert result.ok is False
    assert "GROQ_API_KEY" in result.error


def test_propose_fix_sends_only_shared_answers(monkeypatch):
    from reprokit import magic

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", _GROQ_KEY)
    captured = _stub_openai(monkeypatch, content=_FIX_JSON)

    session = magic.MagicSession()
    for qid in ("q1", "q2", "q3"):
        session.add_question(magic.Question(id=qid, text=f"question {qid}"))
    shared = session.answer("q1", "must keep API compatibility", "shared")
    private = session.answer("q2", "internal host db-prod-01", "private")
    skipped = session.answer("q3", "ignore this note", "skipped")

    result = analysis.propose_fix(
        "ISSUE: x\n1: def f(): pass",
        shared_answers=[shared, private, skipped],
    )

    assert result.ok is True
    payload = captured["create"]["messages"][1]["content"]
    assert "must keep API compatibility" in payload
    assert "internal host db-prod-01" not in payload
    assert "ignore this note" not in payload
    assert "q2" not in payload and "q3" not in payload


def test_propose_fix_redacts_context_before_send(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", _GROQ_KEY)
    captured = _stub_openai(monkeypatch, content=_FIX_JSON)

    analysis.propose_fix("1: token=gsk_abcdefghijklmnop1234")

    payload = captured["create"]["messages"][1]["content"]
    assert "gsk_abcdefghijklmnop1234" not in payload
    assert "REDACTED" in payload


def test_propose_fix_malformed_keeps_raw(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", _GROQ_KEY)
    _stub_openai(monkeypatch, content="not json")

    result = analysis.propose_fix("ISSUE: x")

    assert result.ok is False
    assert result.data is not None
    assert result.data.raw == "not json"
    assert result.data.parse_error is not None


def test_propose_fix_api_error_is_sanitized(monkeypatch):
    secret = "gsk-fix-secret"
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", secret)
    _stub_openai(monkeypatch, exc=RuntimeError(f"boom {secret}"))

    result = analysis.propose_fix("ISSUE: x")

    assert result.ok is False
    assert secret not in result.error
    assert "Groq API call failed" in result.error


def test_propose_regression_test_requires_scenario_and_sends_only_shared_answers(monkeypatch):
    from reprokit import magic, studio

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", _GROQ_KEY)
    response = {
        "feasible": True,
        "language": "python",
        "framework": "pytest",
        "test_filename": "test_bug.py",
        "code": "def test_whitespace_is_rejected():\n    assert not is_valid('   ')\n",
        "scenario": "A whitespace-only username is submitted.",
        "preconditions": ["The fixed implementation is available."],
        "expected_behavior": "Whitespace-only input is rejected.",
        "why_it_catches": "The buggy implementation accepts it.",
        "limitations": [],
    }
    captured = _stub_openai(monkeypatch, content=json.dumps(response))

    session = magic.MagicSession()
    session.add_question(magic.Question(id="q1", text="Share?"))
    session.add_question(magic.Question(id="q2", text="Private?"))
    shared = session.answer("q1", "expected input is whitespace", "shared")
    private = session.answer("q2", "internal system name", "private")

    result = analysis.propose_regression_test(
        "source: token=gsk_abcdefghijklmnop1234",
        shared_answers=[shared, private],
    )

    assert result.ok is True
    assert isinstance(result.data, studio.RegressionDraft)
    assert result.data.scenario == response["scenario"]
    assert result.data.expected_behavior == response["expected_behavior"]
    prompt = captured["create"]["messages"][1]["content"]
    assert "expected input is whitespace" in prompt
    assert "internal system name" not in prompt
    assert "gsk_abcdefghijklmnop1234" not in prompt
    assert "REDACTED" in prompt


@pytest.mark.parametrize(
    ("content", "exception"),
    [
        ("not json", None),
        (None, RuntimeError("provider failure")),
    ],
)
def test_propose_regression_test_fails_safely(monkeypatch, content, exception):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", _GROQ_KEY)
    _stub_openai(monkeypatch, content=content, exc=exception)

    result = analysis.propose_regression_test("bounded source excerpt")

    assert result.ok is False
    assert result.error
    if exception is None:
        assert result.data is not None
        assert result.data.code == ""
        assert result.data.parse_error
    else:
        assert result.data is None
        assert "RuntimeError" in result.error


# ---------------------------------------------------------------------------
# Regression: malformed AI diff envelopes / formats
# ---------------------------------------------------------------------------
def test_extract_diff_text_strips_outer_markdown_fence():
    fenced = "```diff\n" + _FIX_DIFF + "```"
    assert analysis.extract_diff_text(fenced).strip() == _FIX_DIFF.strip()

    fenced_unterminated = "```diff\n" + _FIX_DIFF
    assert analysis.extract_diff_text(fenced_unterminated).strip() == _FIX_DIFF.strip()


def test_parse_fix_proposal_normalizes_fenced_diff_field():
    payload = dict(_FIX_PAYLOAD)
    payload["diff"] = "```diff\n" + _FIX_DIFF + "```"
    data = analysis.parse_fix_proposal_json(json.dumps(payload))
    assert data.diff == _FIX_DIFF.strip().strip()
    assert not data.diff.startswith("```")


def test_parse_fix_proposal_keeps_malformed_diff_content_for_validation():
    payload = dict(_FIX_PAYLOAD)
    payload["diff"] = "```diff\n@@\n```"
    data = analysis.parse_fix_proposal_json(json.dumps(payload))
    # Envelope removed, but content is NOT repaired: the patch validator rejects it.
    assert data.diff == "@@"


def test_parse_fix_proposal_missing_file_headers_is_preserved():
    payload = dict(_FIX_PAYLOAD)
    payload["diff"] = "@@ -1,2 +1,3 @@\n def f():\n+    pass\n     return 1\n"
    data = analysis.parse_fix_proposal_json(json.dumps(payload))
    assert data.diff.startswith("@@ -1,2")


def test_fix_prompt_requires_complete_hunk_headers(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", _GROQ_KEY)
    captured = _stub_openai(monkeypatch, content=_FIX_JSON)

    analysis.propose_fix("ISSUE: x")

    system_prompt = captured["create"]["messages"][0]["content"]
    assert "complete header" in system_prompt
    assert 'A bare "@@"' in system_prompt
    assert "do NOT use Markdown fences" in system_prompt


# ---------------------------------------------------------------------------
# Regression-test recommendation (Evidence-to-Fix Review Studio)
# ---------------------------------------------------------------------------
_REG_PAYLOAD = {
    "feasible": True,
    "language": "python",
    "framework": "pytest",
    "test_filename": "test_zero_division.py",
    "code": "def test_zero_division():\n    assert divide(1, 0) is None\n",
    "scenario": "b == 0",
    "preconditions": ["b == 0"],
    "expected_behavior": "Returns None",
    "why_it_catches": "Raises ZeroDivisionError before the fix.",
    "limitations": ["Assumes divide accepts numbers."],
}


def test_parse_regression_valid():
    from reprokit import studio

    data = analysis.parse_regression_test_json(json.dumps(_REG_PAYLOAD))
    assert isinstance(data, studio.RegressionDraft)
    assert data.feasible is True
    assert data.test_filename == "test_zero_division.py"
    assert "assert divide(1, 0) is None" in data.code
    assert data.preconditions == ["b == 0"]


def test_parse_regression_infeasible_requires_limitation():
    payload = {"feasible": False, "limitations": ["No module API provided."]}
    data = analysis.parse_regression_test_json(json.dumps(payload))
    assert data.feasible is False
    assert data.limitations == ["No module API provided."]

    with pytest.raises(analysis.MalformedAnalysisError):
        analysis.parse_regression_test_json(
            json.dumps({"feasible": False, "limitations": []})
        )


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not json",
        "[]",
        json.dumps({"code": "x"}),  # no feasible
        json.dumps({"feasible": "yes"}),
        json.dumps({"feasible": True, "code": ""}),
        json.dumps(
            {"feasible": True, "code": "def test_a(): pass", "test_filename": "../x.py"}
        ),
        json.dumps({"feasible": True, "code": "x", "preconditions": "b == 0"}),
    ],
)
def test_parse_regression_rejects_malformed(bad):
    with pytest.raises(analysis.MalformedAnalysisError):
        analysis.parse_regression_test_json(bad)


def test_parse_regression_accepts_fenced_json():
    data = analysis.parse_regression_test_json(
        "```json\n" + json.dumps(_REG_PAYLOAD) + "\n```"
    )
    assert data.feasible is True


def test_regression_unavailable_without_key(monkeypatch):
    _clear_provider_env(monkeypatch)
    result = analysis.propose_regression_test("ISSUE: x")
    assert result.ok is False
    assert "GROQ_API_KEY" in result.error


def test_regression_sends_only_shared_answers(monkeypatch):
    from reprokit import magic

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", _GROQ_KEY)
    captured = _stub_openai(monkeypatch, content=json.dumps(_REG_PAYLOAD))

    session = magic.MagicSession()
    for qid in ("q1", "q2", "q3"):
        session.add_question(magic.Question(id=qid, text=f"question {qid}"))
    shared = session.answer("q1", "must stay Python 3", "shared")
    private = session.answer("q2", "internal host db-prod-01", "private")
    skipped = session.answer("q3", "ignore this", "skipped")

    result = analysis.propose_regression_test(
        "1: def divide(a, b): return a / b",
        shared_answers=[shared, private, skipped],
    )

    assert result.ok is True
    payload = captured["create"]["messages"][1]["content"]
    assert "must stay Python 3" in payload
    assert "internal host db-prod-01" not in payload
    assert "ignore this" not in payload


def test_regression_malformed_keeps_raw(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", _GROQ_KEY)
    _stub_openai(monkeypatch, content="not json")

    result = analysis.propose_regression_test("ISSUE: x")

    assert result.ok is False
    assert result.data is not None
    assert result.data.raw == "not json"
    assert result.data.parse_error


def test_regression_api_error_is_sanitized(monkeypatch):
    secret = "gsk-regression-secret"
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", secret)
    _stub_openai(monkeypatch, exc=RuntimeError(f"boom {secret}"))

    result = analysis.propose_regression_test("ISSUE: x")

    assert result.ok is False
    assert secret not in result.error
    assert "Groq API call failed" in result.error
