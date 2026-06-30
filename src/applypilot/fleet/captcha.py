"""Captcha / human-in-the-loop DETECTOR + solve-ROUTER (R3, spec §7.1/§7.2).

The worker classifies the post-submit / mid-form page state into one of eight
kinds, then routes that kind to a disposition. This module is **pure** (string
in, label out) and therefore fully unit-testable with no browser -- the worker
(``worker.py``) feeds it the rendered HTML + iframe text + the final URL + the
HTTP status, and acts on the label.

THE GOVERNING RULE (spec §7.1, §6 fail-safe): the classifier **never guesses**.
If a captcha provider is present but the page is ambiguous, it returns the *wall*
kind, NEVER ``clear`` -- a blind submit on a captcha page burns the residential
IP. ``clear`` / ``invisible_pass`` are only returned when nothing wall-like is
detected. ``route_for`` then maps the kind to ``auto_otp`` (Gmail relay, no
human), ``skip`` (nothing a human can solve -- invisible v3 low-score / CF /
hard block), ``owner_tray`` (solve locally on the owner box), or ``owner_inbox``
(bounce a friend-box wall to the owner's captcha inbox -- friends are never
nagged, spec §7.2).

The eight kinds (spec §7.1):
    clear           -- form proceeded normally, no wall
    invisible_pass  -- reCAPTCHA v3 passed silently (present, no challenge, not blocked)
    visible_captcha -- interactive challenge (reCAPTCHA v2, hCaptcha, Turnstile,
                       Arkose/FunCaptcha, GeeTest, PerimeterX, DataDome)
    email_otp       -- email verification-code wall (auto-solved via the Gmail relay)
    sms_otp         -- phone/SMS verification-code wall (needs the human)
    login_gate      -- "sign in / create an account to apply" wall
    invisible_block -- reCAPTCHA v3 LOW score / "unusual traffic" -- nothing to solve
    cf              -- Cloudflare interstitial / "attention required" / HTTP>=400 block
"""
from __future__ import annotations

# The eight terminal labels (kept as a tuple so callers can validate against it).
KINDS = (
    "clear",
    "invisible_pass",
    "visible_captcha",
    "email_otp",
    "sms_otp",
    "login_gate",
    "invisible_block",
    "cf",
)

# --- detection signature groups (all matched against the LOWERED html+frames) ---

# Cloudflare interstitial / hard wall.
_CF_SIGNS = (
    "just a moment",
    "cf-browser-verification",
    "attention required",
    "cf_chl_opt",          # cloudflare challenge-platform bootstrap
    "checking your browser before accessing",
    "ray id",              # CF error/interstitial footer
)

# Turnstile (Cloudflare's interactive widget) -- an interactive challenge.
_TURNSTILE_SIGNS = ("challenges.cloudflare.com", "cf-turnstile")

# hCaptcha -- interactive challenge.
_HCAPTCHA_SIGNS = ("hcaptcha.com", "h-captcha", "hcaptcha")

# reCAPTCHA v2 visible checkbox / image challenge.
_RECAPTCHA_V2_SIGNS = ("g-recaptcha", "recaptcha/api2", "recaptcha/api/fallback")

# Other interactive anti-bot providers common on ATS / job boards. Each presents a
# human-solvable challenge (Arkose/FunCaptcha puzzle, GeeTest slider, PerimeterX
# press-and-hold, DataDome captcha). WITHOUT these, a page that is ONLY one of them
# (status 200, no reCAPTCHA marker) falls through every branch to 'clear' -- and the
# worker records a PHANTOM apply for a job it never submitted. Classified as
# 'visible_captcha' (the fail-safe direction: route to a human, never blind-submit).
_INTERACTIVE_ANTIBOT_SIGNS = (
    "arkoselabs", "funcaptcha", "arkose-labs", "fc-token",   # Arkose / FunCaptcha
    "geetest", "gt_captcha", "geetest_",                       # GeeTest
    "perimeterx", "px-captcha", "_pxhd", "press & hold",       # PerimeterX
    "datadome", "captcha-delivery.com",                        # DataDome
)

# reCAPTCHA v3 invisible bootstrap (present without a visible widget).
_RECAPTCHA_V3_SIGNS = ("recaptcha/api.js?render=", "grecaptcha.execute", "?render=")

# Blocked-text shown alongside an invisible-v3 low score (no challenge to solve).
_INVISIBLE_BLOCK_SIGNS = (
    "unusual traffic",
    "cannot verify",
    "we can't verify",
    "automated requests",
    "suspicious activity",
    "your request has been blocked",
)

# OTP / verification-code wall.
_OTP_SIGNS = (
    "verification code",
    "one-time",
    "one time passcode",
    "enter the code",
    "enter the verification",
    "security code",
    "we sent you a code",
    "confirmation code",
)
_EMAIL_HINTS = ("email", "e-mail", "inbox", "sent to your email", "@")
_SMS_HINTS = ("text message", "sms", "phone", "mobile", "we texted")

# Login / account wall.
_LOGIN_SIGNS = (
    "sign in to apply",
    "log in to apply",
    "login to apply",
    "create an account to apply",
    "sign in to continue",
    "log in to continue",
    "please sign in",
    "please log in",
    "you must be logged in",
    "sign in or create",
)


def _has(hay: str, needles) -> bool:
    return any(n in hay for n in needles)


def classify(html, frames_text: str = "", final_url: str = "", status=None) -> str:
    """Classify the page state into one of :data:`KINDS`.

    Args:
        html:        the rendered page HTML (the main document).
        frames_text: concatenated text/HTML of any iframes (captcha widgets live
                     in iframes -- ``g-recaptcha``/``h-captcha``/Turnstile do).
        final_url:   the URL after any redirects (a ``/login`` redirect is a wall).
        status:      the HTTP status code, if known. ``>=400`` is treated as a block.

    Fail-safe (spec §7.1/§6): when a captcha provider is present but the exact
    state is ambiguous, returns the WALL kind, never ``clear``.
    """
    hay = f"{html or ''}\n{frames_text or ''}".lower()
    url = (final_url or "").lower()

    # ---- 0. Hard HTTP failure -> block. 403/429/503 are the classic anti-bot codes.
    # A Cloudflare/anti-bot signature on top of it stays 'cf'; a bare >=400 with an
    # invisible-block phrase is 'invisible_block'; otherwise a >=400 is a 'cf'-class
    # hard block (never 'clear').
    http_blocked = False
    try:
        if status is not None and int(status) >= 400:
            http_blocked = True
    except (TypeError, ValueError):
        http_blocked = False

    # ---- 1. Cloudflare interstitial / hard wall (highest-priority block signal).
    if _has(hay, _CF_SIGNS):
        return "cf"

    # ---- 2. Interactive captcha widgets -> a human-solvable visible challenge.
    #         Turnstile / hCaptcha / reCAPTCHA-v2 + Arkose/GeeTest/PerimeterX/DataDome.
    if (_has(hay, _TURNSTILE_SIGNS) or _has(hay, _HCAPTCHA_SIGNS)
            or _has(hay, _RECAPTCHA_V2_SIGNS) or _has(hay, _INTERACTIVE_ANTIBOT_SIGNS)):
        return "visible_captcha"

    # ---- 3. OTP / verification-code wall (email auto-solves, sms needs the human).
    if _has(hay, _OTP_SIGNS):
        # Prefer email (auto-solvable) only when an email hint is present; an
        # explicit SMS/phone hint forces sms_otp. Ambiguous -> sms_otp (the
        # safe, human-needed side -- never silently 'clear').
        if _has(hay, _SMS_HINTS) and not _has(hay, _EMAIL_HINTS):
            return "sms_otp"
        if _has(hay, _EMAIL_HINTS):
            return "email_otp"
        return "sms_otp"

    # ---- 4. Login / account wall. Either explicit wall text, or a redirect to a
    #         login URL (final_url contains '/login').
    if _has(hay, _LOGIN_SIGNS) or "/login" in url or "/signin" in url or "/sign-in" in url:
        return "login_gate"

    # ---- 5. reCAPTCHA v3 (invisible). Present without a visible challenge:
    #         if the page also carries a hard-block signal (>=400 or blocked text)
    #         it's an invisible_block (nothing to solve); otherwise it passed.
    if _has(hay, _RECAPTCHA_V3_SIGNS):
        if http_blocked or _has(hay, _INVISIBLE_BLOCK_SIGNS):
            return "invisible_block"
        return "invisible_pass"

    # ---- 6. Blocked text on its own (no provider script captured) -> invisible_block.
    if _has(hay, _INVISIBLE_BLOCK_SIGNS):
        return "invisible_block"

    # ---- 7. A bare HTTP failure with no other signal -> treat as a hard block ('cf').
    if http_blocked:
        return "cf"

    # ---- 8. Nothing wall-like detected -> clear.
    return "clear"


# Kinds that are NOT a wall: the worker proceeds / submits normally.
_PASS_KINDS = frozenset({"clear", "invisible_pass"})


def is_wall(kind: str) -> bool:
    """True if ``kind`` is a wall the worker must NOT blind-submit through."""
    return kind not in _PASS_KINDS


def route_for(kind: str, *, on_owner_machine: bool) -> str:
    """Map a classifier ``kind`` to a disposition (spec §7.2).

    Returns one of:
        ``auto_otp``     -- email_otp: solved via the Gmail relay, no human.
        ``skip``         -- invisible_block / cf: nothing a human can solve here.
        ``owner_tray``   -- a wall on the OWNER box: raise the local "needs you"
                            tray; the owner solves in the already-loaded browser.
        ``owner_inbox``  -- the same wall on a FRIEND box: bounce to the owner's
                            captcha inbox (friends are never nagged, §7.2); the
                            owner's machine re-attempts on the owner IP.
        ``proceed``      -- clear / invisible_pass: no routing needed.
    """
    if kind in _PASS_KINDS:
        return "proceed"
    if kind == "email_otp":
        return "auto_otp"
    if kind in ("invisible_block", "cf"):
        return "skip"
    # visible_captcha / sms_otp / login_gate -> a human must solve it.
    return "owner_tray" if on_owner_machine else "owner_inbox"
