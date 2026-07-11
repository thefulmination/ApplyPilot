"""Deterministic Workday account/session bootstrap for isolated tenant profiles."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class WorkdayOnboardingResult:
    status: str
    reason: str


def _body_text(page) -> str:
    try:
        return (page.locator("body").inner_text() or "").lower()
    except Exception:
        return ""


def _exists(page, selector: str) -> bool:
    try:
        locator = page.locator(selector)
        return locator.count() > 0 and locator.first.is_visible()
    except Exception:
        return False


def _captcha_present(page) -> bool:
    return _exists(
        page,
        'iframe[src*="recaptcha" i], iframe[src*="hcaptcha" i], '
        '.g-recaptcha, .h-captcha',
    ) or any(token in _body_text(page) for token in ("verify you are human", "complete the captcha"))


def _fill_automation(page, automation_id: str, value: str) -> None:
    control = page.locator(f'[data-automation-id="{automation_id}"]')
    if control.count() == 0:
        raise RuntimeError(f"missing_{automation_id}")
    target = control.first
    if not target.evaluate("el => ['INPUT','TEXTAREA'].includes(el.tagName)"):
        target = target.locator("input,textarea").first
    target.fill("")
    target.press_sequentially(value, delay=10)
    if target.input_value() != value:
        raise RuntimeError(f"readback_mismatch_{automation_id}")
    target.press("Tab")


def _application_step_visible(page) -> bool:
    return _exists(
        page,
        '[data-automation-id="applyFlowMyInfoPage"], '
        '[data-automation-id="applyFlowMyExpPage"], '
        '[data-automation-id="applyFlowQuestionsPage"], '
        '[data-automation-id="applyFlowVoluntaryDisclosuresPage"], '
        '[data-automation-id="applyFlowReviewPage"], '
        '[data-automation-id*="resume" i], input[type="file"], '
        '[data-automation-id="contactInformationPage"], '
        '[data-automation-id="personalInformationPage"]',
    )


def _guarded_submit(page, automation_id: str) -> None:
    """Activate Workday's event-bound click filter after controlled-input read-back."""
    page.locator('[data-automation-id="click_filter"]').last.click(timeout=5000)


def _sign_in(page, *, email: str, password: str) -> WorkdayOnboardingResult:
    if not _exists(page, '[data-automation-id="signInContent"]'):
        try:
            page.get_by_role("link", name="Sign In", exact=True).last.click()
        except Exception:
            try:
                page.get_by_role("button", name="Sign In", exact=True).last.click()
            except Exception:
                return WorkdayOnboardingResult("parked", "signin_control_missing")
    try:
        page.locator('[data-automation-id="email"]').first.wait_for(state="visible", timeout=10000)
        _fill_automation(page, "email", email)
        _fill_automation(page, "password", password)
        _guarded_submit(page, "signInSubmitButton")
        try:
            page.locator(
                '[data-automation-id="errorMessage"], '
                '[data-automation-id*="resume" i], input[type="file"], '
                '[data-automation-id="contactInformationPage"]'
            ).first.wait_for(state="attached", timeout=15000)
        except Exception:
            page.wait_for_timeout(1000)
    except Exception as exc:
        return WorkdayOnboardingResult("parked", f"signin_driver_error:{type(exc).__name__}")
    if _captcha_present(page):
        return WorkdayOnboardingResult("captcha", "workday_signin_captcha")
    if _application_step_visible(page):
        return WorkdayOnboardingResult("ready", "signed_in")
    text = _body_text(page)
    if (("invalid" in text and ("password" in text or "credentials" in text))
            or "wrong email address or password" in text
            or "account might be locked" in text):
        return WorkdayOnboardingResult("parked", "credentials_rejected")
    return WorkdayOnboardingResult("verification_pending", "signin_not_confirmed")


def _reset_password(page, *, email: str, password: str, host: str,
                    return_url: str, watch_fn=None) -> WorkdayOnboardingResult:
    from applypilot import inbox_auth

    try:
        page.locator('[data-automation-id="forgotPasswordLink"]').first.click()
        page.locator('[data-automation-id="resetPasswordButton"]').first.wait_for(
            state="attached", timeout=10000
        )
        _fill_automation(page, "email", email)
        requested_at = datetime.now(timezone.utc)
        _guarded_submit(page, "resetPasswordButton")
    except Exception as exc:
        return WorkdayOnboardingResult("parked", f"reset_request_error:{type(exc).__name__}")
    if watch_fn is None:
        watch_fn = inbox_auth.watch_gmail_for_auth_code
    match = watch_fn(
        timeout_seconds=120, poll_seconds=5, minutes=10, max_messages=1000,
        not_before=requested_at, provider_domain=host,
    )
    if not match:
        return WorkdayOnboardingResult("verification_pending", "password_reset_email_missing")
    candidate = match.candidate
    if (candidate.kind != "magic_link" or candidate.confidence != "high"
            or not inbox_auth.match_belongs_to_provider(match, host)):
        return WorkdayOnboardingResult("parked", "password_reset_link_rejected")
    try:
        page.goto(candidate.value, wait_until="domcontentloaded", timeout=30000)
        page.locator('[data-automation-id="password"]').first.wait_for(
            state="visible", timeout=10000
        )
        _fill_automation(page, "password", password)
        _fill_automation(page, "verifyPassword", password)
        _guarded_submit(page, "resetPasswordButton")
        page.wait_for_timeout(2000)
        page.goto(return_url, wait_until="domcontentloaded", timeout=30000)
        page.locator(
            '[data-automation-id="signInContent"], '
            '[data-automation-id*="resume" i], input[type="file"], '
            '[data-automation-id="contactInformationPage"]'
        ).first.wait_for(state="attached", timeout=15000)
    except Exception as exc:
        return WorkdayOnboardingResult("parked", f"password_reset_error:{type(exc).__name__}")
    if _application_step_visible(page):
        return WorkdayOnboardingResult("ready", "password_reset_session_ready")
    if _exists(page, '[data-automation-id="signInContent"]'):
        return _sign_in(page, email=email, password=password)
    return WorkdayOnboardingResult("verification_pending", "password_reset_not_confirmed")


def bootstrap_workday_account(page, *, email: str, password: str,
                              host: str | None = None, watch_fn=None) -> WorkdayOnboardingResult:
    """Create or sign into one Workday tenant without exposing credential values."""
    job_url = str(getattr(page, "url", ""))
    if not email or not password:
        return WorkdayOnboardingResult("parked", "credentials_missing")
    if _application_step_visible(page):
        return WorkdayOnboardingResult("ready", "session_already_ready")
    if not _exists(page, '[data-automation-id="signInContent"]'):
        return WorkdayOnboardingResult("parked", "login_state_missing")
    if _captcha_present(page):
        return WorkdayOnboardingResult("captcha", "workday_create_account_captcha")
    try:
        _fill_automation(page, "email", email)
        _fill_automation(page, "password", password)
        _fill_automation(page, "verifyPassword", password)
        checkbox = page.locator(
            '[data-automation-id="createAccountCheckbox"][type="checkbox"], '
            '[data-automation-id="createAccountCheckbox"] input[type="checkbox"]'
        )
        if checkbox.count() and not checkbox.first.is_checked():
            checkbox.first.click()
        _guarded_submit(page, "createAccountSubmitButton")
        try:
            page.locator('[data-automation-id="signInContent"]').first.wait_for(
                state="detached", timeout=15000
            )
        except Exception:
            page.wait_for_timeout(1000)
    except Exception as exc:
        return WorkdayOnboardingResult("parked", f"create_account_driver_error:{type(exc).__name__}")
    if _captcha_present(page):
        return WorkdayOnboardingResult("captcha", "workday_create_account_captcha")
    if _application_step_visible(page):
        return WorkdayOnboardingResult("ready", "account_created")
    if "/login" in str(getattr(page, "url", "")).lower():
        signin = _sign_in(page, email=email, password=password)
        if signin.reason == "credentials_rejected" and host:
            return _reset_password(
                page, email=email, password=password, host=host,
                return_url=job_url, watch_fn=watch_fn,
            )
        return signin
    text = _body_text(page)
    if any(token in text for token in ("already exists", "already registered", "email address is already")):
        return _sign_in(page, email=email, password=password)
    if "verification" in text and "email" in text:
        return WorkdayOnboardingResult("verification_pending", "email_verification_required")
    return WorkdayOnboardingResult("verification_pending", "account_creation_not_confirmed")
