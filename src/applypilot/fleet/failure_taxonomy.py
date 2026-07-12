"""Canonical operator-facing groups for raw fleet failure reasons."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping


def canonical_failure_group(reason: str | None) -> str:
    token = str(reason or "").strip().lower()
    if not token:
        return "unclassified"
    if "challenge_skipped" in token:
        return "operator_skipped"
    if "stale_unapproved" in token or "canonical_provenance_missing" in token:
        return "retired_unapproved"
    if token in {"failed:reason", "reason", "failed:unspecified", "unspecified"}:
        return "malformed_failure_reason"
    if "not_a_job_application" in token:
        return "not_an_application"
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
        "site_error",
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
        "bar_license", "required_current_residence",
    )):
        return "eligibility"
    if any(part in token for part in (
        "auth", "login", "verification", "captcha", "cloudflare", "challenge_pending",
        "challenge_skipped",
    )):
        return "access_or_verification"
    if any(part in token for part in (
        "application_limit", "rate_limited", "too_many_attempts",
    )):
        return "rate_or_application_limit"
    if any(part in token for part in (
        "no_confirmation",
        "crash_unconfirmed",
        "submission_uncertain",
        "outcome unresolved",
        "no confirmed applied event or inbox outcome",
    )):
        return "submission_uncertain"
    if "unsafe_prior_attempt" in token:
        return "submission_uncertain"
    if any(part in token for part in (
        "reference", "loom", "video", "photo_required", "resume_data", "resume_text",
        "resume_content", "screenshots_unavailable",
    )):
        return "missing_required_material"
    if any(part in token for part in (
        "contract", "part_time", "internship", "not_full_time", "not_salaried",
        "not_a_salaried",
        "not_a_paid_job",
    )):
        return "job_type_excluded"
    if "no_decline_option" in token or "no_truthful_option" in token:
        return "form_or_profile_constraint"
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
