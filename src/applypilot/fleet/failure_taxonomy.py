"""Canonical operator-facing groups for raw fleet failure reasons."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping


def canonical_failure_group(reason: str | None) -> str:
    token = str(reason or "").strip().lower()
    if not token:
        return "unclassified"
    if "requeued_by_" in token:
        return "remediation_history"
    if "usage_limit" in token:
        return "provider_usage_limit"
    if "email_reconcile_review_required" in token or "manual_hold" in token:
        return "manual_review"
    if "dedup" in token or "already_applied" in token:
        return "duplicate_or_already_applied"
    if "expired" in token or "availability_unavailable" in token:
        return "unavailable"
    if "no_result" in token:
        return "agent_no_result"
    if any(part in token for part in (
        "page_error", "suspicious_page", "wrong_job_redirect", "page_redirected",
    )):
        return "page_or_content_failure"
    if any(part in token for part in (
        "browser", "playwright", "mcp", "cdp", "tooling", "no_browser_tool",
        "connection_lost", "connection_refused",
    )):
        return "browser_infrastructure"
    if any(part in token for part in (
        "timeout", "stuck", "navigation_loop", "validation_stuck",
    )):
        return "timeout_or_stuck"
    if "budget" in token or "out_of_budget" in token:
        return "budget_exhausted"
    if any(part in token for part in (
        "not_eligible", "citizenship", "work_auth", "wrong_role", "job_mismatch",
    )):
        return "eligibility"
    if any(part in token for part in (
        "auth", "login", "verification", "captcha", "cloudflare", "challenge_pending",
    )):
        return "access_or_verification"
    if any(part in token for part in (
        "application_limit", "rate_limited", "too_many_attempts",
    )):
        return "rate_or_application_limit"
    if "no_confirmation" in token or "crash_unconfirmed" in token:
        return "submission_uncertain"
    if any(part in token for part in (
        "reference", "loom", "video", "photo_required", "resume_data", "resume_text",
    )):
        return "missing_required_material"
    if any(part in token for part in (
        "contract", "part_time", "internship", "not_full_time", "not_salaried",
    )):
        return "job_type_excluded"
    if "spam" in token:
        return "spam_or_abuse_filter"
    if any(part in token for part in (
        "adapter_unsupported", "host_policy", "automation_blocked_host", "company_blocklist",
        "user_excluded",
    )):
        return "routing_or_policy"
    return "other"


def group_reason_counts(reasons: Mapping[str, int]) -> dict[str, int]:
    grouped: Counter[str] = Counter()
    for reason, count in reasons.items():
        grouped[canonical_failure_group(reason)] += int(count or 0)
    return dict(sorted(grouped.items(), key=lambda item: (-item[1], item[0])))
