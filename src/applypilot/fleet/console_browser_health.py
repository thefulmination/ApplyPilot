"""Deterministic worker-log classification for fleet console health panels."""
from __future__ import annotations

from urllib.parse import quote

from applypilot.fleet import console_machines


_RULES = [
    ("browser_backend_crashed", "error", ("browser_backend_crashed",)),
    ("browser_backend_crashed", "error", ("browser_crashed",)),
    ("browser_backend_crashed", "error", ("browser backend", "crashed")),
    ("browser_service_unavailable", "error", ("econnrefused",)),
    ("browser_service_unavailable", "error", ("browser_unavailable",)),
    ("browser_service_unavailable", "error", ("browser service", "not responding")),
    ("browser_server_unavailable", "error", ("browser_server_unavailable",)),
    ("browser_server_unavailable", "error", ("browser server unavailable",)),
    ("captcha", "warn", ("captcha",)),
    ("captcha", "warn", ("hcaptcha",)),
    ("captcha", "warn", ("error_invalid_task_data",)),
    ("login_gate", "warn", ("login_gate",)),
    ("login_gate", "warn", ("login_issue",)),
    ("login_gate", "warn", ("auth_required",)),
    ("login_gate", "warn", ("login page",)),
    ("email_otp", "warn", ("email_otp",)),
    ("email_otp", "warn", ("email_verification_required",)),
    ("email_otp", "warn", ("verification code",)),
    ("email_otp", "warn", ("one-time",)),
    ("email_otp", "warn", ("enter the code",)),
    ("email_otp", "warn", ("security code",)),
    ("employer_application_cap", "info", ("limit the number of applications",)),
    ("usage_limit", "warn", ("session limit",)),
    ("usage_limit", "warn", ("usage_limit",)),
    ("usage_limit", "warn", ("hit your usage limit",)),
    ("usage_limit", "warn", ("usage limit reached",)),
    ("timeout", "warn", ("timeout",)),
    ("no_result_line", "warn", ("no_result_line",)),
]


def classify_text(text: str | None) -> dict:
    lower = (text or "").lower()
    for kind, severity, needles in _RULES:
        if all(n in lower for n in needles):
            return {"kind": kind, "severity": severity}
    return {"kind": "unknown", "severity": "info"}


def summarize_worker_logs(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    examples: dict[str, dict] = {}
    for row in rows:
        text = "\n".join(str(row.get(k) or "") for k in ("last_error", "recent_log"))
        cls = classify_text(text)
        kind = cls["kind"]
        if kind == "unknown":
            continue
        counts[kind] = counts.get(kind, 0) + 1
        machine_owner = console_machines.infer_machine_owner(
            row.get("worker_id"), row.get("machine_owner")
        )
        worker_id = row.get("worker_id")
        examples.setdefault(kind, {
            "worker_id": worker_id,
            "machine_owner": machine_owner,
            "machine_display_name": console_machines.display_name(machine_owner),
            "severity": cls["severity"],
            "logs_url": f"/api/logs?worker={quote(str(worker_id or ''))}",
        })
    return {"counts": counts, "examples": examples}
