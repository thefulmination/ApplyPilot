"""Operator command for evidence-gated Workday shadow and canary runs."""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import httpx

from applypilot import config
from applypilot.apply.workday_adapter import (
    PlaywrightWorkdayPageDriver, WorkdayAdapterRunner, WorkdayRunResult,
    WorkdayState, detect_state,
    build_canonical_resume,
)
from applypilot.fleet.rollout import (
    consume_canary_approval, evaluate_expansion, issue_canary_approval,
    load_records, persist_record, require_review_ready_canary,
    run_canary_prepare, run_supervised_canary,
    run_workday_shadow, fresh_workday_candidates,
)
from applypilot.apply import tenant_sessions
from applypilot.apply.chrome import cleanup_worker, launch_chrome


def validate_canary_requirements(*, observer_present: bool,
                                 approval_token: str | None,
                                 confirm_submit: bool) -> None:
    if not observer_present:
        raise ValueError("canary requires --observer-present")
    if not approval_token:
        raise ValueError("canary requires --approval-token from the authorize stage")
    if not confirm_submit:
        raise ValueError("canary requires --confirm-submit for real submissions")


def _resume_pdf_path(job: dict) -> str | None:
    """Resolve the exact PDF that this rollout is allowed to upload."""
    resume_stem = config.resolve_resume_stem(job.get("tailored_resume_path"))
    if not resume_stem:
        return None
    pdf_path = Path(resume_stem).with_suffix(".pdf")
    return str(pdf_path) if pdf_path.exists() else None


def _canonical_resume_for_job(profile: dict, job: dict) -> dict:
    base_text = ""
    if config.RESUME_PATH.exists():
        base_text = config.RESUME_PATH.read_text(encoding="utf-8", errors="ignore")
    base = build_canonical_resume(profile=profile, resume_text=base_text)

    resume_stem = config.resolve_resume_stem(job.get("tailored_resume_path"))
    text_path = Path(resume_stem).with_suffix(".txt") if resume_stem else None
    if text_path is None or not text_path.exists() or text_path == config.RESUME_PATH:
        return base

    tailored = build_canonical_resume(
        profile=profile,
        resume_text=text_path.read_text(encoding="utf-8", errors="ignore"),
    )
    # Tailoring may change wording or omit records; verified base history remains
    # authoritative for factual Workday fields.
    return {
        "work_history": base.get("work_history") or tailored.get("work_history") or [],
        "education": base.get("education") or tailored.get("education") or [],
        "links": {**tailored.get("links", {}), **base.get("links", {})},
    }


def _wait_cdp(port: int, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://localhost:{port}/json/version", timeout=1).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("cdp_start_timeout")


def _enter_application(page) -> bool:
    """Move from a Workday job detail page into its application flow once."""
    for role in ("button", "link"):
        try:
            control = page.get_by_role(role, name=re.compile(r"^apply( now)?$", re.I)).first
            control.wait_for(state="visible", timeout=10000)
            if control.count() and control.is_visible():
                control.click()
                page.wait_for_load_state("domcontentloaded")
                try:
                    start = page.get_by_role(
                        "button", name=re.compile(r"^autofill with resume$", re.I)
                    ).first
                    start.wait_for(state="visible", timeout=10000)
                    start.click()
                    page.wait_for_url(re.compile(r"/apply/", re.I), timeout=15000)
                    page.wait_for_load_state("domcontentloaded")
                    page.locator(
                        '[data-automation-id="signInContent"], '
                        '[data-automation-id*="resume" i], input[type="file"]'
                    ).first.wait_for(state="attached", timeout=20000)
                except Exception:
                    # Some tenants route directly to the first application step.
                    pass
                return True
        except Exception:
            continue
    return False


def _executor(port: int, profile: dict):
    def execute(job: dict, *, submit: bool):
        from applypilot.apply import answer_exceptions
        from applypilot.database import get_connection
        from playwright.sync_api import sync_playwright
        target = job.get("application_url") or job["url"]
        pdf = _resume_pdf_path(job)
        with sync_playwright() as pw:
            exception_conn = get_connection()
            browser = pw.chromium.connect_over_cdp(f"http://localhost:{port}")
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
            result = None
            try:
                page.goto(target, wait_until="domcontentloaded", timeout=30000)
                _enter_application(page)
                driver = PlaywrightWorkdayPageDriver(page)
                if detect_state(driver.snapshot()) == WorkdayState.LOGIN:
                    from applypilot.apply import credential_vault
                    from applypilot.apply.workday_onboarding import bootstrap_workday_account

                    host = job.get("target_host") or ""
                    auth = bootstrap_workday_account(
                        page,
                        email=(profile.get("personal") or {}).get("email", ""),
                        password=credential_vault.get_or_create(host),
                        host=host,
                    )
                    if auth.status != "ready":
                        result = WorkdayRunResult(
                            "auth_required",
                            auth.reason,
                            {
                                "auth_status": auth.status,
                                "auth_reason": auth.reason,
                            },
                        )
                        return result
                    page.locator(
                        '[data-automation-id="file-upload-input-ref"], '
                        '[data-automation-id^="applyFlow"][data-automation-id$="Page"], '
                        '[data-automation-id="contactInformationPage"], '
                        '[data-automation-id="personalInformationPage"]'
                    ).first.wait_for(state="attached", timeout=15000)
                    driver = PlaywrightWorkdayPageDriver(page)
                job_profile = dict(profile)
                job_profile["_application_context"] = {
                    "company": job.get("company") or "",
                    "source_board": job.get("source_board") or job.get("site") or "",
                    "target_host": job.get("target_host") or "",
                }
                result = WorkdayAdapterRunner(
                    driver,
                    profile=job_profile,
                    resume_path=pdf,
                    canonical_resume=_canonical_resume_for_job(profile, job),
                    answer_resolver=lambda field: answer_exceptions.resolve_approved_answer(
                        exception_conn,
                        field.label,
                        host=job.get("target_host") or "",
                        field_key=field.key,
                    ),
                    exception_sink=lambda fields: answer_exceptions.record_exceptions(
                        exception_conn,
                        fields,
                        host=job.get("target_host") or "",
                        job_url=target,
                    ),
                    exception_reconciler=lambda fields: answer_exceptions.reconcile_resolved_exceptions(
                        exception_conn,
                        host=job.get("target_host") or "",
                        resolved_questions=[field.label for field in fields],
                        resolution_source="current_field_plan",
                    ),
                ).execute(submit=submit, job_url=job["url"])
                return result
            finally:
                try:
                    page.close()
                except Exception:
                    if result is None or not (result.metadata or {}).get("submit_clicked"):
                        raise
                # get_connection() returns a thread-local cached handle shared with
                # rollout persistence; its owner closes it at process shutdown.
    return execute


def _tenant_executor(profile: dict, *, headless: bool, base_port: int = 9460):
    sequence = {"value": 0}

    def execute(job: dict, *, submit: bool):
        pdf = _resume_pdf_path(job)
        if not pdf:
            return type("Result", (), {
                "status": "parked", "reason": "missing_resume_pdf",
                "metadata": {"cost_usd": 0.0, "submit_clicked": False},
            })()
        index = sequence["value"]
        sequence["value"] += 1
        worker_id, port = 150 + index, base_port + index
        session = tenant_sessions.select_session(job["target_host"])
        if session["state"] != "ready":
            return type("Result", (), {
                "status": "parked", "reason": "tenant_session_not_ready", "metadata": {},
            })()
        process = None
        try:
            process = launch_chrome(
                worker_id, port=port, headless=headless, kill_existing=False,
                profile_dir=session["profile_dir"],
            )
            _wait_cdp(port)
            return _executor(port, profile)(job, submit=submit)
        finally:
            if process is not None:
                cleanup_worker(worker_id, process)
    return execute


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("shadow", "prepare", "authorize", "canary", "report"))
    parser.add_argument("--cdp-port", type=int, default=9222)
    parser.add_argument("--observer-present", action="store_true")
    parser.add_argument("--approval-token")
    parser.add_argument("--confirm-submit", action="store_true")
    parser.add_argument(
        "--retry-failed-prepares",
        action="store_true",
        help="Retry previously failed non-submitting prepare records after a code fix.",
    )
    args = parser.parse_args()
    conn = config.get_connection() if hasattr(config, "get_connection") else None
    if conn is None:
        from applypilot.database import get_connection
        conn = get_connection()
    conn.row_factory = __import__("sqlite3").Row
    if args.stage == "report":
        decision = evaluate_expansion(load_records(conn))
        print(json.dumps({"allowed": decision.allowed, "reasons": decision.reasons,
                          "metrics": decision.metrics}, indent=2))
        return
    if args.stage == "canary":
        try:
            validate_canary_requirements(
                observer_present=args.observer_present,
                approval_token=args.approval_token,
                confirm_submit=args.confirm_submit,
            )
        except ValueError as exc:
            parser.error(str(exc))
    jobs = fresh_workday_candidates(
        conn, limit=10 if args.stage == "shadow" else 5,
        canary=args.stage in {"prepare", "authorize", "canary"},
        retry_failed_prepares=args.retry_failed_prepares,
    )
    if args.stage == "canary":
        try:
            require_review_ready_canary(conn, jobs, minimum=5)
        except ValueError as exc:
            parser.error(str(exc))
    if args.stage == "authorize":
        try:
            ready_urls = require_review_ready_canary(conn, jobs, minimum=5)
            token = issue_canary_approval(conn, ready_urls)
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps({"approval_token": token, "job_urls": ready_urls}, indent=2))
        return
    executor = (
        _executor(args.cdp_port, config.load_profile())
        if args.stage == "shadow"
        else _tenant_executor(config.load_profile(), headless=args.stage == "prepare")
    )
    def record_fn(record):
        persist_record(conn, record)
    if args.stage == "shadow":
        records = run_workday_shadow(jobs, executor, record_fn=record_fn)
    elif args.stage == "prepare":
        records = run_canary_prepare(jobs, executor, record_fn=record_fn)
    else:
        records = run_supervised_canary(
            jobs,
            executor,
            confirm_submit=args.confirm_submit,
            observer_approve=lambda job: consume_canary_approval(
                conn,
                args.approval_token,
                job.get("application_url") or job["url"],
            ),
            record_fn=record_fn,
        )
    print(json.dumps([record.__dict__ for record in records], indent=2, default=str))


if __name__ == "__main__":
    main()
