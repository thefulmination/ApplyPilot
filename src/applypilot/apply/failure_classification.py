"""Pure classification for apply runtime failures."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FailureEvidence:
    status: str
    transcript: str = ""
    application_tool_calls: int = 0
    tool_calls_total: int = 0
    last_tool: str = ""
    chrome_launch_ok: bool | None = None
    cdp_connect_ok: bool | None = None
    mcp_started_ok: bool | None = None
    agent_exit_code: int | None = None
    timeout_seconds: int | None = None


@dataclass(frozen=True)
class FailureClassification:
    failure_class: str
    safe_requeue: bool = False
    worker_level: bool = False


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    haystack = text.lower()
    return any(needle.lower() in haystack for needle in needles)


def classify_apply_failure(evidence: FailureEvidence) -> FailureClassification:
    transcript = evidence.transcript.lower()
    status = evidence.status.lower()
    touched_application = evidence.application_tool_calls > 0

    if _has_any(
        transcript,
        (
            "mcp startup failed",
            "handshaking with mcp server failed",
            "mcp server failed",
        ),
    ):
        return FailureClassification("mcp_start_failure", worker_level=True)

    if evidence.chrome_launch_ok is False:
        return FailureClassification("browser_launch_failure", worker_level=True)

    if evidence.cdp_connect_ok is False or _has_any(
        transcript, ("cdp", "browser connection lost")
    ):
        return FailureClassification("cdp_lost", worker_level=True)

    if (
        _has_any(transcript, ("usage limit", "session limit", "switch to another model"))
        and evidence.application_tool_calls == 0
    ):
        return FailureClassification("usage_or_session_limit", safe_requeue=True)

    if (
        _has_any(transcript, ("auth required", "invalid api key", "no access token"))
        and evidence.application_tool_calls == 0
    ):
        return FailureClassification(
            "agent_auth",
            safe_requeue=True,
            worker_level=True,
        )

    if "timeout" in status:
        if touched_application:
            return FailureClassification("post_form_crash_unconfirmed")
        return FailureClassification("timeout", safe_requeue=True, worker_level=True)

    if (
        "no_result_line" in status
        and evidence.application_tool_calls == 0
        and evidence.tool_calls_total == 0
    ):
        return FailureClassification("zero_tool_no_result", safe_requeue=True)

    if "no_result_line" in status and touched_application:
        return FailureClassification("post_browser_no_result")

    if "crash_unconfirmed" in status and touched_application:
        return FailureClassification("post_form_crash_unconfirmed")

    return FailureClassification("malformed_result")
