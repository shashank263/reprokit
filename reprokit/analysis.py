"""AI-assisted bug report analysis for ReproKit.

Provider configuration:
- **Groq is the explicitly selected provider** and the default. The OpenAI
  Python SDK is used purely as an OpenAI-compatible client pointed at
  ``https://api.groq.com/openai/v1``; the key is read only from
  ``GROQ_API_KEY`` and the model from ``REPROKIT_AI_MODEL`` (default
  ``openai/gpt-oss-120b``).
- OpenAI may be used only when selected explicitly
  (``REPROKIT_AI_PROVIDER=openai``). There is **no silent fallback** to OpenAI
  when the selected provider is unavailable.

Other design goals:
- Return a structured analysis: problem summary, likely cause (explicitly a
  hypothesis), reproduction steps, suggested test cases (normal + edge), and
  missing information / clarifying questions.
- The AI output is advisory. This module never selects test IDs, builds
  shell commands, executes code, modifies files, or triggers the test runner
  (it does not even import :mod:`reprokit.runner`).
- Handle missing credentials, API errors, empty reports, and malformed AI
  responses gracefully, and never echo secrets (keys are read from the
  environment and are never printed or embedded in messages).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

# Groq is the explicitly selected provider for this configuration. The key is
# read only from GROQ_API_KEY; there is no silent fallback to OpenAI.
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_KEY_ENV = "GROQ_API_KEY"

# Current Groq production model (verified against
# https://console.groq.com/docs/models). Override with REPROKIT_AI_MODEL.
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"

# Backwards-compatible alias: DEFAULT_MODEL is the selected provider's default.
DEFAULT_MODEL = DEFAULT_GROQ_MODEL

# Selected provider when REPROKIT_AI_PROVIDER is unset.
SELECTED_PROVIDER = "groq"

# Strict JSON contract the model must return. The parser validates this shape.
_REQUIRED_FIELDS = {
    "problem_summary": str,
    "likely_cause": str,
    "reproduction_steps": list,
    "suggested_test_cases": list,
    "missing_information": list,
}

_SYSTEM_PROMPT = """\
You are a careful software-testing analyst. Analyze the bug report provided by the user.

Respond with ONLY a single JSON object. No prose, no markdown, no code fences.
Use exactly these keys with these types:
{
  "problem_summary": string,
  "likely_cause": string,
  "reproduction_steps": array of string,
  "suggested_test_cases": array of string,
  "missing_information": array of string
}

Rules:
- "problem_summary" is a 1-3 sentence restatement of the reported problem.
- "likely_cause" is your best GUESS and must be phrased as a hypothesis (e.g.
  "Likely caused by ..."), never as a confirmed fact.
- "reproduction_steps" is an ordered list of steps that reproduce the bug.
- "suggested_test_cases" lists test *cases to add* (what behavior to verify).
  Prefix each entry with "normal: " or "edge: " as appropriate.
- "missing_information" lists clarifying questions or details still needed.
- Do NOT invent test identifiers, file paths to run, shell commands, or code
  to execute. Suggest test cases to write, not commands to run.
"""

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class MalformedAnalysisError(ValueError):
    """Raised when the AI response cannot be parsed into the required shape."""


class AIProviderError(RuntimeError):
    """Raised when the AI provider cannot be used or the API call fails."""


@dataclass
class BugReportAnalysis:
    """Structured, advisory analysis of a bug report."""

    problem_summary: str = ""
    likely_cause: str = ""  # always a hypothesis
    reproduction_steps: list[str] = field(default_factory=list)
    suggested_test_cases: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    raw: str = ""  # raw model output, for inspection only
    provider: str = ""
    model: str = ""
    parse_error: Optional[str] = None


@dataclass(frozen=True)
class ProviderStatus:
    """Whether an AI provider is usable, and why."""

    available: bool
    provider: str
    model: str
    reason: str


@dataclass
class AnalysisResult:
    """Outcome of one analysis request, covering success and all failure modes."""

    ok: bool
    analysis: Optional[BugReportAnalysis]
    error: Optional[str]  # sanitized, never contains secrets
    status: ProviderStatus


def _strip_json_fences(text: str) -> str:
    """Remove code fences or trim to the outermost object before parsing."""
    stripped = text.strip()
    match = _JSON_FENCE_RE.match(stripped)
    if match:
        return match.group(1).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        return stripped[start : end + 1]
    return stripped


def parse_analysis_json(text: str) -> BugReportAnalysis:
    """Parse a model response into a BugReportAnalysis.

    Raises MalformedAnalysisError on empty input, invalid JSON, a non-object
    root, missing keys, or wrong field types.
    """
    if not text or not text.strip():
        raise MalformedAnalysisError("response is empty")

    try:
        data = json.loads(_strip_json_fences(text))
    except json.JSONDecodeError as exc:
        raise MalformedAnalysisError(f"response is not valid JSON: {exc.msg}") from exc

    if not isinstance(data, dict):
        raise MalformedAnalysisError("JSON root must be an object")

    missing = sorted(set(_REQUIRED_FIELDS) - set(data))
    if missing:
        raise MalformedAnalysisError("missing keys: " + ", ".join(missing))

    def as_str(key: str) -> str:
        value = data[key]
        if not isinstance(value, str):
            raise MalformedAnalysisError(f"{key} must be a string")
        return value.strip()

    def as_str_list(key: str) -> list[str]:
        value = data[key]
        if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
            raise MalformedAnalysisError(f"{key} must be an array of strings")
        return [i.strip() for i in value if i.strip()]

    return BugReportAnalysis(
        problem_summary=as_str("problem_summary"),
        likely_cause=as_str("likely_cause"),
        reproduction_steps=as_str_list("reproduction_steps"),
        suggested_test_cases=as_str_list("suggested_test_cases"),
        missing_information=as_str_list("missing_information"),
        raw=text,
    )


def _resolve_model(provider_default: str, model: Optional[str]) -> str:
    return model or os.environ.get("REPROKIT_AI_MODEL") or provider_default


class _ChatProvider:
    """Shared OpenAI-compatible chat logic with an explicit key and base URL.

    Passing ``api_key`` and ``base_url`` explicitly prevents the SDK from
    falling back to ``OPENAI_API_KEY`` or the default OpenAI endpoint.
    """

    name: str = ""
    display_name: str = ""
    key_env: str = ""
    default_model: str = ""
    base_url: Optional[str] = None

    def __init__(self, model: Optional[str] = None) -> None:
        self.model = _resolve_model(self.default_model, model)

    @property
    def label(self) -> str:
        """Human-readable provider name for user-facing messages."""
        return self.display_name or self.name

    def _missing_key_reason(self) -> str:
        raise NotImplementedError

    def status(self) -> ProviderStatus:
        if os.environ.get(self.key_env):
            return ProviderStatus(
                available=True,
                provider=self.name,
                model=self.model,
                reason=(
                    f"{self.label} API key detected ({self.key_env}). "
                    "Analysis is enabled."
                ),
            )
        return ProviderStatus(
            available=False,
            provider=self.name,
            model=self.model,
reason=self._missing_key_reason(),
        )

    def analyze(self, report: str) -> str:
        """Analyze a bug report with the built-in prompt."""
        return self.analyze_prompt(_SYSTEM_PROMPT, report)

    def analyze_prompt(self, system_prompt: str, user_content: str) -> str:
        """Call the provider and return raw model text. Raises AIProviderError."""
        api_key = os.environ.get(self.key_env)
        if not api_key:
            raise AIProviderError(
                f"No {self.key_env} configured; cannot call {self.label}."
            )

        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise AIProviderError(
                "The `openai` Python package is not installed. Run: pip install openai"
            ) from exc

        client_kwargs: dict[str, str] = {"api_key": api_key}
        if self.base_url:
            client_kwargs["base_url"] = self.base_url
        client = OpenAI(**client_kwargs)

        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            )
        except Exception as exc:
            # Do not echo the exception text: it may include request details.
            raise AIProviderError(
                f"{self.label} API call failed ({type(exc).__name__}). Check "
                f"{self.key_env}, network access, and model availability "
                "(REPROKIT_AI_MODEL)."
            ) from exc

        if not response.choices:
            raise AIProviderError(f"{self.label} returned an empty response.")
        return response.choices[0].message.content or ""


class GroqProvider(_ChatProvider):
    """Groq provider using the OpenAI-compatible endpoint.

    Key: ``GROQ_API_KEY`` only. Base URL: ``https://api.groq.com/openai/v1``.
    """

    name = "groq"
    display_name = "Groq"
    key_env = GROQ_KEY_ENV
    default_model = DEFAULT_GROQ_MODEL
    base_url = GROQ_BASE_URL

    def _missing_key_reason(self) -> str:
        return (
            "Groq is the selected AI provider, but no GROQ_API_KEY is "
            "configured. To enable analysis, set the GROQ_API_KEY environment "
            f"variable. The model defaults to {DEFAULT_GROQ_MODEL} and can be "
            "overridden with REPROKIT_AI_MODEL."
        )


class OpenAIProvider(_ChatProvider):
    """OpenAI provider, usable only when explicitly selected.

    Set ``REPROKIT_AI_PROVIDER=openai`` to opt in. Never selected implicitly.
    """

    name = "openai"
    display_name = "OpenAI"
    key_env = "OPENAI_API_KEY"
    default_model = DEFAULT_OPENAI_MODEL
    base_url = None

    def _missing_key_reason(self) -> str:
        return (
            "OpenAI was explicitly selected, but no OPENAI_API_KEY is "
            "configured. Set OPENAI_API_KEY, or unset REPROKIT_AI_PROVIDER to "
            "use the default Groq provider."
        )


_PROVIDERS = {"groq": GroqProvider, "openai": OpenAIProvider}


def _resolve_provider_name(provider: Optional[str] = None) -> str:
    raw = provider or os.environ.get("REPROKIT_AI_PROVIDER") or SELECTED_PROVIDER
    return raw.strip().lower()


def _unknown_provider_status(name: str, model: Optional[str]) -> ProviderStatus:
    return ProviderStatus(
        available=False,
        provider=name,
        model=model or os.environ.get("REPROKIT_AI_MODEL") or "",
        reason=(
            f"Unknown AI provider {name!r}. Supported providers: "
            f"{', '.join(sorted(_PROVIDERS))}. No automatic fallback is performed."
        ),
    )


def provider_status(
    model: Optional[str] = None, provider: Optional[str] = None
) -> ProviderStatus:
    """Cheap availability check for the selected provider (no API call)."""
    name = _resolve_provider_name(provider)
    provider_cls = _PROVIDERS.get(name)
    if provider_cls is None:
        return _unknown_provider_status(name, model)
    return provider_cls(model=model).status()


def analyze_bug_report(
    report: str,
    *,
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> AnalysisResult:
    """Analyze a bug report, returning a result that covers every failure mode.

    This never calls the test runner or executes AI output; it only produces
    advisory structured text.
    """
    name = _resolve_provider_name(provider)
    provider_cls = _PROVIDERS.get(name)
    if provider_cls is None:
        status = _unknown_provider_status(name, model)
        return AnalysisResult(ok=False, analysis=None, error=status.reason, status=status)

    backend = provider_cls(model=model)
    status = backend.status()

    stripped = (report or "").strip()
    if not stripped:
        return AnalysisResult(
            ok=False,
            analysis=None,
            error="Bug report is empty. Enter a description to analyze.",
            status=status,
        )

    if not status.available:
        return AnalysisResult(ok=False, analysis=None, error=status.reason, status=status)

    try:
        raw = backend.analyze(stripped)
    except AIProviderError as exc:
        return AnalysisResult(ok=False, analysis=None, error=str(exc), status=status)

    try:
        analysis = parse_analysis_json(raw)
    except MalformedAnalysisError as exc:
        partial = BugReportAnalysis(
            raw=raw,
            provider=backend.name,
            model=backend.model,
            parse_error=str(exc),
        )
        return AnalysisResult(
            ok=False,
            analysis=partial,
            error=f"Could not parse the AI response into a structured analysis: {exc}",
            status=status,
        )

    analysis.provider = backend.name
    analysis.model = backend.model
    return AnalysisResult(ok=True, analysis=analysis, error=None, status=status)


# ---------------------------------------------------------------------------
# Log analysis (TraceGuard-style workflow)
# ---------------------------------------------------------------------------
_LOG_SYSTEM_PROMPT = """\
You are a careful site-reliability analyst. You are given a BOUNDED excerpt of
already-redacted log lines, grouped by error signature. Each group header looks
like: "Group <key> | severity=<level> | count=<n>".

Respond with ONLY a single JSON object. No prose, no markdown, no code fences.
Use exactly these keys:
{
  "incident_summary": string,
  "findings": [
    {
      "group_key": string,
      "likely_cause": string,
      "recommended_steps": array of string,
      "severity": "critical" | "high" | "medium" | "low"
    }
  ],
  "missing_information": array of string
}

Rules:
- Only reference log evidence that appears in the provided excerpt. NEVER
  invent log lines, stack traces, timestamps, hostnames, or error codes.
- "group_key" MUST be copied exactly from a provided "Group ..." header.
- "likely_cause" is a HYPOTHESIS. Phrase it as such (e.g. "Likely caused by ...").
  It is not a confirmed fact.
- "recommended_steps" are troubleshooting/verification suggestions, not commands.
  Do not include shell commands, file edits, or code to execute.
- "incident_summary" is at most 3 sentences and only summarizes the observed
  groups.
- If the excerpt is insufficient, say so in "missing_information" instead of
  guessing.
"""

_ALLOWED_SEVERITIES = {"critical", "high", "medium", "low"}


@dataclass
class LogAIFinding:
    """AI (hypothesis-only) enrichment for one observed log group."""

    group_key: str
    likely_cause: str = ""
    recommended_steps: list[str] = field(default_factory=list)
    severity: str = ""  # AI suggestion only; never overrides observed facts


@dataclass
class LogAIAnalysis:
    incident_summary: str = ""
    findings: list[LogAIFinding] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    raw: str = ""
    provider: str = ""
    model: str = ""
    parse_error: Optional[str] = None


@dataclass
class LogAnalysisResult:
    """Outcome of one AI log-excerpt analysis."""

    ok: bool
    data: Optional[LogAIAnalysis]
    error: Optional[str]
    status: ProviderStatus


def parse_log_analysis_json(text: str) -> LogAIAnalysis:
    """Parse an AI log-analysis response into LogAIAnalysis.

    Raises MalformedAnalysisError on empty input, invalid JSON, a non-object
    root, or wrong types.
    """
    if not text or not text.strip():
        raise MalformedAnalysisError("response is empty")
    try:
        data = json.loads(_strip_json_fences(text))
    except json.JSONDecodeError as exc:
        raise MalformedAnalysisError(f"response is not valid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise MalformedAnalysisError("JSON root must be an object")

    summary = data.get("incident_summary", "")
    if not isinstance(summary, str):
        raise MalformedAnalysisError("incident_summary must be a string")

    raw_findings = data.get("findings", [])
    if not isinstance(raw_findings, list):
        raise MalformedAnalysisError("findings must be an array")

    findings: list[LogAIFinding] = []
    for item in raw_findings:
        if not isinstance(item, dict):
            raise MalformedAnalysisError("each finding must be an object")
        group_key = item.get("group_key")
        if not isinstance(group_key, str) or not group_key.strip():
            raise MalformedAnalysisError("finding.group_key must be a non-empty string")
        cause = item.get("likely_cause", "")
        if not isinstance(cause, str):
            raise MalformedAnalysisError("finding.likely_cause must be a string")
        steps = item.get("recommended_steps", [])
        if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
            raise MalformedAnalysisError(
                "finding.recommended_steps must be an array of strings"
            )
        severity = item.get("severity", "")
        if severity is None:
            severity = ""
        if not isinstance(severity, str):
            raise MalformedAnalysisError("finding.severity must be a string")
        severity = severity.strip().lower()
        if severity and severity not in _ALLOWED_SEVERITIES:
            raise MalformedAnalysisError(
                f"finding.severity must be one of {sorted(_ALLOWED_SEVERITIES)}"
            )
        findings.append(
            LogAIFinding(
                group_key=group_key.strip(),
                likely_cause=cause.strip(),
                recommended_steps=[s.strip() for s in steps if s.strip()],
                severity=severity,
            )
        )

    raw_missing = data.get("missing_information", [])
    if not isinstance(raw_missing, list) or not all(
        isinstance(s, str) for s in raw_missing
    ):
        raise MalformedAnalysisError("missing_information must be an array of strings")

    return LogAIAnalysis(
        incident_summary=summary.strip(),
        findings=findings,
        missing_information=[s.strip() for s in raw_missing if s.strip()],
        raw=text,
    )


def analyze_log_excerpt(
    excerpt: str,
    *,
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> LogAnalysisResult:
    """Analyze a bounded, redacted log excerpt with the selected provider.

    The excerpt is redacted again here (defense in depth) before it leaves the
    process. Nothing from the model response is executed or used to drive the
    test runner.
    """
    from reprokit.logs import redact  # local import avoids import-time coupling

    name = _resolve_provider_name(provider)
    provider_cls = _PROVIDERS.get(name)
    if provider_cls is None:
        status = _unknown_provider_status(name, model)
        return LogAnalysisResult(ok=False, data=None, error=status.reason, status=status)

    backend = provider_cls(model=model)
    status = backend.status()

    safe_excerpt, _ = redact(excerpt or "")
    if not safe_excerpt.strip():
        return LogAnalysisResult(
            ok=False,
            data=None,
            error="No log excerpt to analyze.",
            status=status,
        )
    if not status.available:
        return LogAnalysisResult(ok=False, data=None, error=status.reason, status=status)

    prompt = (
        "Analyze the following bounded log excerpt. Use only these lines.\n\n"
        + safe_excerpt
    )
    try:
        raw = backend.analyze_prompt(_LOG_SYSTEM_PROMPT, prompt)
    except AIProviderError as exc:
        return LogAnalysisResult(ok=False, data=None, error=str(exc), status=status)

    try:
        data = parse_log_analysis_json(raw)
    except MalformedAnalysisError as exc:
        partial = LogAIAnalysis(
            raw=raw,
            provider=backend.name,
            model=backend.model,
            parse_error=str(exc),
        )
        return LogAnalysisResult(
            ok=False,
            data=partial,
            error=f"Could not parse the AI response: {exc}",
            status=status,
        )

    data.provider = backend.name
    data.model = backend.model
    return LogAnalysisResult(ok=True, data=data, error=None, status=status)


# ---------------------------------------------------------------------------
# Magic Mode: evidence-backed issue detection (Stage 2)
# ---------------------------------------------------------------------------
_MAGIC_SYSTEM_PROMPT = """\
You are a careful code reviewer helping a developer triage their own files.

You receive line-numbered excerpts of their uploaded files and, optionally, a
list of developer answers they explicitly chose to share.

Respond with ONLY a single JSON object. No prose, no markdown, no code fences.
Use exactly these keys:
{
  "issues": [
    {
      "title": string,
      "severity": "critical" | "high" | "medium" | "low",
      "confidence": number between 0 and 1,
      "evidence": [ { "file": string, "line": integer } ],
      "hypothesis": string,
      "questions": array of string
    }
  ],
  "notes": array of string
}

Rules:
- "file" MUST be copied exactly from a provided "=== FILE <name> ..." or
  "=== LOG FILE <name> ..." header.
- "line" MUST be an existing line number shown for that file. Never invent a
  line number.
- "hypothesis" is a HYPOTHESIS about the likely cause. Phrase it as such
  (e.g. "Likely caused by ..."). It is not a confirmed fact.
- "questions" are optional follow-up questions for the developer, at most 3.
  Analysis and safe fix generation must not depend on answers. Do not ask for secrets.
- Analyze each file as a separate unit. Do not infer that multiple uploaded
  files belong to one application or correlate logs with source without evidence.
- Do NOT propose patches, diffs, shell commands, or code to execute.
- If evidence is insufficient for an issue, return fewer issues rather than
  guessing.
- Use only the provided excerpts and shared answers. Never assume content of
  files that were not provided.
- Refer to files only by the exact names shown in the excerpt headers. Never
  mention or invent any other filename, and never ask about a file that was not
  provided.
"""


@dataclass
class MagicAnalysisResult:
    ok: bool
    data: Optional["object"]
    error: Optional[str]
    status: ProviderStatus


def parse_magic_analysis_json(text: str):
    """Parse an AI issue-detection response into magic.MagicAIAnalysis."""
    from reprokit import magic

    if not text or not text.strip():
        raise MalformedAnalysisError("response is empty")
    try:
        payload = json.loads(_strip_json_fences(text))
    except json.JSONDecodeError as exc:
        raise MalformedAnalysisError(f"response is not valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise MalformedAnalysisError("JSON root must be an object")

    raw_issues = payload.get("issues", [])
    if not isinstance(raw_issues, list):
        raise MalformedAnalysisError("issues must be an array")

    issues: list["object"] = []
    for item in raw_issues:
        if not isinstance(item, dict):
            raise MalformedAnalysisError("each issue must be an object")

        title = item.get("title", "")
        if not isinstance(title, str) or not title.strip():
            raise MalformedAnalysisError("issue.title must be a non-empty string")

        severity = item.get("severity", "medium")
        if not isinstance(severity, str):
            raise MalformedAnalysisError("issue.severity must be a string")
        severity = severity.strip().lower()
        if severity not in magic.SEVERITIES:
            raise MalformedAnalysisError(
                f"issue.severity must be one of {list(magic.SEVERITIES)}"
            )

        confidence = item.get("confidence", 0.5)
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
        ):
            raise MalformedAnalysisError("issue.confidence must be a number from 0 to 1")

        raw_evidence = item.get("evidence", [])
        if not isinstance(raw_evidence, list):
            raise MalformedAnalysisError("issue.evidence must be an array")
        refs: list[tuple[str, int]] = []
        for ref in raw_evidence:
            if not isinstance(ref, dict):
                raise MalformedAnalysisError("evidence entries must be objects")
            file_name = ref.get("file")
            line = ref.get("line")
            if not isinstance(file_name, str) or not file_name.strip():
                raise MalformedAnalysisError("evidence.file must be a non-empty string")
            if isinstance(line, bool) or not isinstance(line, int):
                raise MalformedAnalysisError("evidence.line must be an integer")
            refs.append((file_name.strip(), line))

        hypothesis = item.get("hypothesis", "")
        if not isinstance(hypothesis, str):
            raise MalformedAnalysisError("issue.hypothesis must be a string")

        raw_questions = item.get("questions", [])
        if not isinstance(raw_questions, list) or not all(
            isinstance(q, str) for q in raw_questions
        ):
            raise MalformedAnalysisError("issue.questions must be an array of strings")

        issues.append(
            magic.MagicAIIssue(
                title=title.strip(),
                severity=severity,
                evidence_refs=refs,
                hypothesis=hypothesis.strip(),
                questions=[q.strip() for q in raw_questions if q.strip()],
                confidence=float(confidence),
            )
        )

    raw_notes = payload.get("notes", [])
    if not isinstance(raw_notes, list) or not all(isinstance(n, str) for n in raw_notes):
        raise MalformedAnalysisError("notes must be an array of strings")

    return magic.MagicAIAnalysis(
        issues=issues,
        notes=[n.strip() for n in raw_notes if n.strip()],
        raw=text,
    )


def analyze_magic_issues(
    excerpt: str,
    *,
    shared_answers: Sequence[object] = (),
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> MagicAnalysisResult:
    """Detect issues in a bounded excerpt using only explicitly shared answers.

    Private and skipped answers are never passed by the caller (the UI uses
    ``MagicSession.shareable_answers()``), and this function additionally
    filters on ``Answer.is_shareable`` as defense in depth.
    """
    from reprokit.logs import redact

    name = _resolve_provider_name(provider)
    provider_cls = _PROVIDERS.get(name)
    if provider_cls is None:
        status = _unknown_provider_status(name, model)
        return MagicAnalysisResult(ok=False, data=None, error=status.reason, status=status)

    backend = provider_cls(model=model)
    status = backend.status()

    safe_excerpt, _ = redact(excerpt or "")
    if not safe_excerpt.strip():
        return MagicAnalysisResult(
            ok=False, data=None, error="No analyzable file content was provided.", status=status
        )
    if not status.available:
        return MagicAnalysisResult(ok=False, data=None, error=status.reason, status=status)

    parts = [
        "Analyze the following uploaded excerpts. Use only these lines.",
        safe_excerpt,
    ]
    shared = [
        a
        for a in shared_answers
        if getattr(a, "is_shareable", False) and str(getattr(a, "text", "")).strip()
    ]
    if shared:
        lines = ["=== SHARED DEVELOPER ANSWERS ==="]
        for answer in shared:
            safe_text, _ = redact(str(answer.text))
            lines.append(f"[{getattr(answer, 'question_id', '?')}] {safe_text}")
        parts.append("\n".join(lines))

    try:
        raw = backend.analyze_prompt(_MAGIC_SYSTEM_PROMPT, "\n\n".join(parts))
    except AIProviderError as exc:
        return MagicAnalysisResult(ok=False, data=None, error=str(exc), status=status)

    try:
        data = parse_magic_analysis_json(raw)
    except MalformedAnalysisError as exc:
        from reprokit import magic

        partial = magic.MagicAIAnalysis(
            raw=raw, provider=backend.name, model=backend.model, parse_error=str(exc)
        )
        return MagicAnalysisResult(
            ok=False,
            data=partial,
            error=f"Could not parse the AI response: {exc}",
            status=status,
        )

    data.provider = backend.name
    data.model = backend.model
    return MagicAnalysisResult(ok=True, data=data, error=None, status=status)

# ---------------------------------------------------------------------------
# Magic Mode: reviewable fix proposals (Stage 3)
# ---------------------------------------------------------------------------
_FIX_SYSTEM_PROMPT = """\
You are a careful senior engineer proposing the smallest fix for ONE issue.

Respond with ONLY a single JSON object. No prose, no markdown, no code fences.
Use exactly these keys:
{
  "explanation": string,
  "diff": string,
  "files_changed": [ { "path": string, "lines": [integer] } ],
  "expected_behavior": string,
  "tests_needed": array of string,
  "limitations": array of string
}

Rules:
- "diff" MUST be a RAW unified diff string that applies cleanly to the provided
  files. Its first line MUST be "--- a/" immediately followed by the exact
  uploaded filename; its second line MUST be "+++ b/" followed by the same
  filename.
- Every hunk MUST start with a complete header of the form
  "@@ -<old_start>,<old_count> +<new_start>,<new_count> @@", using real numbers,
  for example "@@ -2,3 +2,4 @@". A bare "@@" or "@@ -2 +2 @@" is invalid.
  old_count = context lines + removed lines; new_count = context lines + added
  lines. Both counts must match the hunk body exactly.
- Inside "diff", do NOT use Markdown fences, prose, "diff --git" headers,
  timestamps, or file mode lines. The value must start with "--- a/".
- Every hunk must include exact context lines from the provided excerpt.
- Only modify files that were provided, using their exact names from the
  ALLOWED FILES line. Do not create, delete, or rename files. If the fix would
  require changing any other file, do not return a diff; explain the limitation.
- Never use absolute paths or "..".
- Do not produce binary patches or mode changes.
- "tests_needed" are descriptions of tests to add or run, never shell commands.
- "expected_behavior" describes observable behavior after the fix.
- "limitations" must state uncertainty honestly.
- Do not claim the fix has been verified. You cannot run tests.
- Do not include commands, code to execute, or instructions to overwrite files.
"""


def extract_diff_text(raw: str) -> str:
    """Strip only the transport envelope (outer Markdown fence / blank lines).

    This does not repair or reflow diff content: missing ---/+++ headers, bare
    "@@" hunks, and wrong counts are still rejected by :mod:`reprokit.patch`.
    """
    if not isinstance(raw, str):
        raise MalformedAnalysisError("diff must be a string")
    text = raw.strip()
    if not text:
        return ""
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def parse_fix_proposal_json(text: str):
    """Parse an AI fix proposal into magic.FixProposalDraft."""
    from reprokit import magic

    if not text or not text.strip():
        raise MalformedAnalysisError("response is empty")
    try:
        payload = json.loads(_strip_json_fences(text))
    except json.JSONDecodeError as exc:
        raise MalformedAnalysisError(f"response is not valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise MalformedAnalysisError("JSON root must be an object")

    explanation = payload.get("explanation", "")
    if not isinstance(explanation, str) or not explanation.strip():
        raise MalformedAnalysisError("explanation must be a non-empty string")

    diff = payload.get("diff", "")
    if not isinstance(diff, str) or not diff.strip():
        raise MalformedAnalysisError("diff must be a non-empty string")
    diff = extract_diff_text(diff)
    if not diff:
        raise MalformedAnalysisError("diff must be a non-empty string")

    raw_files = payload.get("files_changed", [])
    if not isinstance(raw_files, list):
        raise MalformedAnalysisError("files_changed must be an array")
    files_changed: list[tuple[str, list[int]]] = []
    for entry in raw_files:
        if not isinstance(entry, dict):
            raise MalformedAnalysisError("files_changed entries must be objects")
        path = entry.get("path")
        if not isinstance(path, str) or not path.strip():
            raise MalformedAnalysisError("files_changed.path must be a non-empty string")
        lines = entry.get("lines", [])
        if not isinstance(lines, list) or not all(
            isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in lines
        ):
            raise MalformedAnalysisError(
                "files_changed.lines must be an array of positive integers"
            )
        files_changed.append((path.strip(), list(lines)))

    expected = payload.get("expected_behavior", "")
    if not isinstance(expected, str) or not expected.strip():
        raise MalformedAnalysisError("expected_behavior must be a non-empty string")

    def string_list(key: str) -> list[str]:
        value = payload.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise MalformedAnalysisError(f"{key} must be an array of strings")
        return [v.strip() for v in value if v.strip()]

    return magic.FixProposalDraft(
        explanation=explanation.strip(),
        diff=diff,
        files_changed=files_changed,
        expected_behavior=expected.strip(),
        tests_needed=string_list("tests_needed"),
        limitations=string_list("limitations"),
        raw=text,
    )


def propose_fix(
    context: str,
    *,
    shared_answers: Sequence[object] = (),
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> MagicAnalysisResult:
    """Ask the provider for a structured fix proposal.

    Only explicitly shared answers may reach the model. The diff is parsed and
    validated by :mod:`reprokit.patch`; this function does not apply anything.
    """
    from reprokit.logs import redact

    name = _resolve_provider_name(provider)
    provider_cls = _PROVIDERS.get(name)
    if provider_cls is None:
        status = _unknown_provider_status(name, model)
        return MagicAnalysisResult(ok=False, data=None, error=status.reason, status=status)

    backend = provider_cls(model=model)
    status = backend.status()

    safe_context, _ = redact(context or "")
    if not safe_context.strip():
        return MagicAnalysisResult(
            ok=False, data=None, error="No source context was provided.", status=status
        )
    if not status.available:
        return MagicAnalysisResult(ok=False, data=None, error=status.reason, status=status)

    parts = ["Propose a fix for this issue using only the provided context.", safe_context]
    shared = [
        a
        for a in shared_answers
        if getattr(a, "is_shareable", False) and str(getattr(a, "text", "")).strip()
    ]
    if shared:
        lines = ["=== SHARED DEVELOPER ANSWERS ==="]
        for answer in shared:
            safe_text, _ = redact(str(answer.text))
            lines.append(f"[{getattr(answer, 'question_id', '?')}] {safe_text}")
        parts.append("\n".join(lines))

    try:
        raw = backend.analyze_prompt(_FIX_SYSTEM_PROMPT, "\n\n".join(parts))
    except AIProviderError as exc:
        return MagicAnalysisResult(ok=False, data=None, error=str(exc), status=status)

    try:
        data = parse_fix_proposal_json(raw)
    except MalformedAnalysisError as exc:
        from reprokit import magic

        partial = magic.FixProposalDraft(
            explanation="",
            diff="",
            raw=raw,
            provider=backend.name,
            model=backend.model,
            parse_error=str(exc),
        )
        return MagicAnalysisResult(
            ok=False,
            data=partial,
            error=f"Could not parse the AI response: {exc}",
            status=status,
        )

    data.provider = backend.name
    data.model = backend.model
    return MagicAnalysisResult(ok=True, data=data, error=None, status=status)


# ---------------------------------------------------------------------------
# Magic Mode: regression-test recommendation (Evidence-to-Fix Review Studio)
# ---------------------------------------------------------------------------
_REGRESSION_SYSTEM_PROMPT = """\
You are a careful test engineer recommending ONE regression test for a bug that
has already been diagnosed. You will be given the issue, its observed evidence
lines, relevant source excerpts, the proposed fix, and any developer answers
they explicitly chose to share.

Respond with ONLY a single JSON object. No prose, no markdown, no code fences.
Use exactly these keys:
{
  "feasible": boolean,
  "language": string,
  "framework": string,
  "test_filename": string,
  "code": string,
  "scenario": string,
  "preconditions": array of string,
  "expected_behavior": string,
  "why_it_catches": string,
  "limitations": array of string
}

Rules:
- "feasible" is true only when the provided source is enough to write a concrete
  test. If it is not, set "feasible" to false, leave "code" empty, and explain
  exactly what is missing in "limitations". Never invent project details, module
  names, fixtures, or APIs that were not provided.
- Only generate Python test code (pytest style) with a function named test_*.
  For any other language set "feasible" to false and explain the limitation.
- "test_filename" must be a plain filename such as "test_bug_regression.py" -
  never a path, never one of the uploaded filenames.
- The test must be self-contained: no network, no file system writes, no shell
  commands, no time or randomness dependence.
- "expected_behavior" states the behavior after the fix; "why_it_catches"
  explains why the test would fail on the buggy version and pass after the fix.
- You cannot run the test. Never claim it passed. Do not output commands to run.
- Use only the provided context; never reference files that were not uploaded.
"""


def parse_regression_test_json(text: str):
    """Parse an AI regression-test recommendation into a studio.RegressionDraft."""
    from reprokit import studio

    if not text or not text.strip():
        raise MalformedAnalysisError("response is empty")
    try:
        payload = json.loads(_strip_json_fences(text))
    except json.JSONDecodeError as exc:
        raise MalformedAnalysisError(f"response is not valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise MalformedAnalysisError("JSON root must be an object")

    feasible = payload.get("feasible")
    if not isinstance(feasible, bool):
        raise MalformedAnalysisError("feasible must be a boolean")

    def as_text(key: str, required: bool = False) -> str:
        value = payload.get(key, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise MalformedAnalysisError(f"{key} must be a string")
        if required and not value.strip():
            raise MalformedAnalysisError(f"{key} must be a non-empty string")
        return value.strip()

    def as_list(key: str) -> list[str]:
        value = payload.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise MalformedAnalysisError(f"{key} must be an array of strings")
        return [v.strip() for v in value if v.strip()]

    limitations = as_list("limitations")
    if not feasible and not limitations:
        raise MalformedAnalysisError(
            "limitations must explain why no test could be generated"
        )

    filename = as_text("test_filename")
    if feasible and any(sep in filename for sep in ("/", "\\", "..")):
        raise MalformedAnalysisError("test_filename must be a plain filename")

    code = payload.get("code", "")
    if not isinstance(code, str):
        raise MalformedAnalysisError("code must be a string")
    if feasible and not code.strip():
        raise MalformedAnalysisError("code must be non-empty when feasible is true")
    if not feasible and code.strip():
        raise MalformedAnalysisError("code must be empty when feasible is false")

    return studio.RegressionDraft(
        feasible=feasible,
        language=as_text("language"),
        framework=as_text("framework"),
        test_filename=filename,
        code=code,
        scenario=as_text("scenario", required=feasible),
        preconditions=as_list("preconditions"),
        expected_behavior=as_text("expected_behavior", required=feasible),
        why_it_catches=as_text("why_it_catches", required=feasible),
        limitations=limitations,
        raw=text,
    )


def propose_regression_test(
    context: str,
    *,
    shared_answers: Sequence[object] = (),
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> MagicAnalysisResult:
    """Ask the provider for a regression-test recommendation.

    Only explicitly shared answers may reach the model. The result is only ever
    a proposal: nothing is executed.
    """
    from reprokit.logs import redact

    name = _resolve_provider_name(provider)
    provider_cls = _PROVIDERS.get(name)
    if provider_cls is None:
        status = _unknown_provider_status(name, model)
        return MagicAnalysisResult(ok=False, data=None, error=status.reason, status=status)

    backend = provider_cls(model=model)
    status = backend.status()

    safe_context, _ = redact(context or "")
    if not safe_context.strip():
        return MagicAnalysisResult(
            ok=False, data=None, error="No source context was provided.", status=status
        )
    if not status.available:
        return MagicAnalysisResult(ok=False, data=None, error=status.reason, status=status)

    parts = ["Recommend one regression test using only this context.", safe_context]
    shared = [
        a
        for a in shared_answers
        if getattr(a, "is_shareable", False) and str(getattr(a, "text", "")).strip()
    ]
    if shared:
        lines = ["=== SHARED DEVELOPER ANSWERS ==="]
        for answer in shared:
            safe_text, _ = redact(str(answer.text))
            lines.append(f"[{getattr(answer, 'question_id', '?')}] {safe_text}")
        parts.append("\n".join(lines))

    try:
        raw = backend.analyze_prompt(_REGRESSION_SYSTEM_PROMPT, "\n\n".join(parts))
    except AIProviderError as exc:
        return MagicAnalysisResult(ok=False, data=None, error=str(exc), status=status)

    try:
        data = parse_regression_test_json(raw)
    except MalformedAnalysisError as exc:
        from reprokit import studio

        partial = studio.RegressionDraft(
            feasible=False,
            raw=raw,
            provider=backend.name,
            model=backend.model,
            parse_error=str(exc),
        )
        return MagicAnalysisResult(
            ok=False,
            data=partial,
            error=f"Could not parse the AI response: {exc}",
            status=status,
        )

    data.provider = backend.name
    data.model = backend.model
    return MagicAnalysisResult(ok=True, data=data, error=None, status=status)
