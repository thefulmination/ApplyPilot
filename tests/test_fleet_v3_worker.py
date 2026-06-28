"""Captcha classifier + worker-loop tests (spec §5/§6/§7).

The classifier tests are pure (no Postgres). The WorkerLoop tests run the COMPUTE
path end-to-end against real Postgres (fake score_fn -> compute_queue 'done' +
llm_usage row) and exercise the APPLY wall path with a FAKE apply_fn returning
captcha HTML -- asserting a challenge row is raised and the job is NOT marked
applied (the lease stays held / the job parks). The browser/LLM/scrape calls are
all injected fakes; no real Chromium / API spend.
"""
from __future__ import annotations

import pytest

from applypilot.apply import pgqueue
from applypilot.fleet import captcha
from applypilot.fleet import queue
from applypilot.fleet.worker import WorkerLoop


# ===========================================================================
# 1. captcha.classify -- one sample per kind; NEVER 'clear' on a wall.
# ===========================================================================

_CLEAR_HTML = """
<html><body><h1>Application submitted</h1>
<p>Thank you, we received your application.</p></body></html>
"""

_INVISIBLE_PASS_HTML = """
<html><head>
<script src="https://www.google.com/recaptcha/api.js?render=6Lc_aBcDeFg"></script>
</head><body><form id="apply"><button>Submit</button></form>
<p>Thanks for applying!</p></body></html>
"""

_VISIBLE_RECAPTCHA_V2 = """
<html><body><form><div class="g-recaptcha" data-sitekey="6Lxxxx"></div>
<button>Verify</button></form></body></html>
"""

_VISIBLE_HCAPTCHA = """
<html><body><div class="h-captcha" data-sitekey="abc"></div>
<script src="https://hcaptcha.com/1/api.js"></script></body></html>
"""

_VISIBLE_TURNSTILE = """
<html><body><div class="cf-turnstile" data-sitekey="0x4"></div>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script></body></html>
"""

_EMAIL_OTP_HTML = """
<html><body><h2>Verify it's you</h2>
<p>We sent a verification code to your email inbox. Enter the code below.</p>
<input name="code"/></body></html>
"""

_SMS_OTP_HTML = """
<html><body><h2>Verify your phone</h2>
<p>We texted a one-time passcode to your mobile phone. Enter the verification code.</p>
<input name="otp"/></body></html>
"""

_LOGIN_GATE_HTML = """
<html><body><h2>Sign in to apply</h2>
<form><input name="user"/><input name="pass" type="password"/></form></body></html>
"""

_INVISIBLE_BLOCK_HTML = """
<html><head>
<script src="https://www.google.com/recaptcha/api.js?render=6Lc"></script></head>
<body><h1>We detected unusual traffic from your network</h1>
<p>We cannot verify that you are not a robot.</p></body></html>
"""

_CF_HTML = """
<html><head><title>Just a moment...</title></head>
<body><div class="cf-browser-verification">Checking your browser before accessing.</div>
<p>Ray ID: 7abc</p></body></html>
"""

# Other interactive anti-bot providers (status 200, NO reCAPTCHA marker) -- these
# used to fall through to 'clear' (a phantom apply). Each must be a visible_captcha.
_ARKOSE_HTML = """
<html><body><div id="arkose-challenge"></div>
<script src="https://client-api.arkoselabs.com/v2/abc/api.js"></script>
<input name="fc-token" value="x"/></body></html>
"""
_GEETEST_HTML = """
<html><body><div class="geetest_holder"></div>
<script src="https://static.geetest.com/static/js/gt.0.4.9.js"></script></body></html>
"""
_PERIMETERX_HTML = """
<html><body><div id="px-captcha"></div>
<p>Press &amp; Hold to confirm you are a human (and not a bot).</p>
<script src="https://captcha.px-cdn.net/PXxxx/main.min.js"></script></body></html>
"""
_DATADOME_HTML = """
<html><body><script>var dd={'host':'geo.captcha-delivery.com','t':'fe'}</script>
<iframe src="https://geo.captcha-delivery.com/captcha/?initialCid=x"></iframe></body></html>
"""


def test_classify_one_per_kind():
    cases = {
        "clear": classify_args(_CLEAR_HTML),
        "invisible_pass": classify_args(_INVISIBLE_PASS_HTML),
        "visible_captcha": classify_args(_VISIBLE_RECAPTCHA_V2),
        "email_otp": classify_args(_EMAIL_OTP_HTML),
        "sms_otp": classify_args(_SMS_OTP_HTML),
        "login_gate": classify_args(_LOGIN_GATE_HTML),
        "invisible_block": classify_args(_INVISIBLE_BLOCK_HTML),
        "cf": classify_args(_CF_HTML),
    }
    for expected, kwargs in cases.items():
        got = captcha.classify(**kwargs)
        assert got == expected, f"expected {expected!r}, got {got!r}"
    # every label produced is one of the eight declared kinds
    assert set(cases) <= set(captcha.KINDS)


def classify_args(html, **extra):
    base = {"html": html}
    base.update(extra)
    return base


def test_hcaptcha_and_turnstile_are_visible_captcha():
    assert captcha.classify(_VISIBLE_HCAPTCHA) == "visible_captcha"
    assert captcha.classify(_VISIBLE_TURNSTILE) == "visible_captcha"


def test_login_gate_via_final_url_redirect():
    # No wall text in the body, but the final URL redirected to a login page.
    html = "<html><body><p>loading</p></body></html>"
    assert captcha.classify(html, final_url="https://acme.com/login?next=/apply") == "login_gate"


def test_http_status_failure_is_a_block_never_clear():
    # A bland body but a 403 -> hard block, never 'clear'.
    assert captcha.classify("<html><body>ok</body></html>", status=403) == "cf"
    # invisible v3 present + 429 -> invisible_block (low score), never 'clear'.
    html = '<script src="recaptcha/api.js?render=x"></script>'
    assert captcha.classify(html, status=429) == "invisible_block"


def test_classify_never_returns_clear_on_any_wall():
    walls = [
        _VISIBLE_RECAPTCHA_V2, _VISIBLE_HCAPTCHA, _VISIBLE_TURNSTILE,
        _EMAIL_OTP_HTML, _SMS_OTP_HTML, _LOGIN_GATE_HTML,
        _INVISIBLE_BLOCK_HTML, _CF_HTML,
    ]
    for html in walls:
        assert captcha.classify(html) != "clear"
        assert captcha.is_wall(captcha.classify(html)) is True
    # and the two pass-kinds ARE 'clear'/'invisible_pass' (not walls)
    assert captcha.is_wall("clear") is False
    assert captcha.is_wall("invisible_pass") is False


def test_other_antibot_providers_are_visible_captcha_not_clear():
    # Arkose/FunCaptcha, GeeTest, PerimeterX, DataDome: a page that is ONLY one of
    # these walls (200, no reCAPTCHA marker) must NOT classify as 'clear' (which the
    # worker would record as a phantom apply). Fail-safe -> visible_captcha.
    for html in (_ARKOSE_HTML, _GEETEST_HTML, _PERIMETERX_HTML, _DATADOME_HTML):
        got = captcha.classify(html)
        assert got == "visible_captcha", f"expected visible_captcha, got {got!r}"
        assert got != "clear"


# ===========================================================================
# 2. route_for routing (spec §7.2).
# ===========================================================================

def test_route_for_dispositions():
    # email_otp -> auto (Gmail relay), no human
    assert captcha.route_for("email_otp", on_owner_machine=False) == "auto_otp"
    assert captcha.route_for("email_otp", on_owner_machine=True) == "auto_otp"
    # invisible_block / cf -> skip (nothing a human can solve)
    assert captcha.route_for("invisible_block", on_owner_machine=True) == "skip"
    assert captcha.route_for("cf", on_owner_machine=False) == "skip"
    # human-needed walls: owner box solves in tray; friend box bounces to inbox
    for kind in ("visible_captcha", "sms_otp", "login_gate"):
        assert captcha.route_for(kind, on_owner_machine=True) == "owner_tray"
        assert captcha.route_for(kind, on_owner_machine=False) == "owner_inbox"
    # pass-kinds need no routing
    assert captcha.route_for("clear", on_owner_machine=False) == "proceed"


# ===========================================================================
# 3. WorkerLoop COMPUTE path end-to-end (real Postgres, fake score_fn).
# ===========================================================================

def _factory(dsn):
    return lambda: pgqueue.connect(dsn)


def test_worker_compute_end_to_end(fleet_db):
    # seed one compute job
    with pgqueue.connect(fleet_db) as conn:
        queue.push_compute_jobs(conn, [{"url": "c1", "task": "score", "payload": {"jd": "Chief of Staff"}}])

    calls = []

    def fake_score_fn(job):
        calls.append(job["url"])
        # the fake LLM returns an advisory result + a small cost
        return ({"fit": 7, "model": "deepseek-chat"}, 0.012)

    loop = WorkerLoop(
        _factory(fleet_db), "w-compute-1", home_ip="1.2.3.4", role="compute",
        score_fn=fake_score_fn, machine_owner="jon", sw_version="0.3.0",
    )
    res = loop.run_once()
    assert res["action"] == "compute_done" and res["url"] == "c1"
    assert calls == ["c1"]

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, result FROM compute_queue WHERE url='c1'")
        row = cur.fetchone()
        assert row["status"] == "done"
        assert row["result"]["fit"] == 7
        # llm_usage row recorded (cost ledger, R14)
        cur.execute("SELECT worker_id, task, model, cost_usd FROM llm_usage")
        usage = cur.fetchall()
        assert len(usage) == 1
        assert usage[0]["worker_id"] == "w-compute-1"
        assert usage[0]["task"] == "score"
        assert usage[0]["model"] == "deepseek-chat"
        assert float(usage[0]["cost_usd"]) == pytest.approx(0.012)
        # heartbeat written, back to idle after the job
        cur.execute("SELECT state, role, sw_version FROM worker_heartbeat WHERE worker_id='w-compute-1'")
        hb = cur.fetchone()
        assert hb["state"] == "idle" and hb["role"] == "compute" and hb["sw_version"] == "0.3.0"


def test_worker_compute_idle_when_empty(fleet_db):
    loop = WorkerLoop(_factory(fleet_db), "w-idle", home_ip="1.2.3.4", role="compute",
                      score_fn=lambda j: ({}, 0))
    assert loop.run_once()["action"] == "idle"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT state FROM worker_heartbeat WHERE worker_id='w-idle'")
        assert cur.fetchone()["state"] == "idle"


# ===========================================================================
# 4. WorkerLoop APPLY wall path (fake apply_fn -> captcha HTML).
#    A wall must raise an auth_challenge row and PARK -- never mark applied.
# ===========================================================================

def _seed_one_apply(conn, url="a1", host="greenhouse.io"):
    queue.push_apply_jobs(conn, [{
        "url": url, "company": "Acme", "title": "Chief of Staff",
        "application_url": f"https://{host}/jobs/1", "score": 9.0, "target_host": host,
    }], approved_batch="batchA")


def test_worker_apply_visible_captcha_parks_and_raises_challenge(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_one_apply(conn, "a1")

    def fake_apply_fn(job):
        # the live ATS threw up a reCAPTCHA v2 checkbox wall
        return _VISIBLE_RECAPTCHA_V2

    loop = WorkerLoop(
        _factory(fleet_db), "w-apply-friend", home_ip="5.5.5.5", role="apply",
        apply_fn=fake_apply_fn, machine_owner="friend", on_owner_machine=False,
    )
    res = loop.run_once()
    assert res["action"] == "parked_challenge"
    assert res["kind"] == "visible_captcha"
    assert res["route"] == "owner_inbox"   # friend box -> bounce to owner inbox

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        # a challenge row was raised, open, routed to the owner inbox
        cur.execute("SELECT url, kind, route, resolved_at FROM auth_challenge WHERE url='a1'")
        ch = cur.fetchone()
        assert ch is not None
        assert ch["kind"] == "visible_captcha" and ch["route"] == "owner_inbox"
        assert ch["resolved_at"] is None
        # the job is PARKED + FROZEN: lease still HELD by the worker, marked
        # challenge_pending (NOT applied), and pushed out of the reclaim window so the
        # SAME wall is never reclaimed + re-driven blind (IP-burn fail-safe, §7.3).
        cur.execute("SELECT status, lease_owner, apply_status, "
                    "lease_expires_at > now() + interval '300 days' AS frozen "
                    "FROM apply_queue WHERE url='a1'")
        q = cur.fetchone()
        assert q["status"] == "leased", "wall must keep the lease held (parked), not close it"
        assert q["lease_owner"] == "w-apply-friend"
        assert q["apply_status"] == "challenge_pending" and q["apply_status"] != "applied"
        assert q["frozen"] is True, "parked wall must be frozen out of the reclaim window"
        # the captcha outcome was recorded on the governor (leading indicator, §6)
        cur.execute("SELECT captcha_24h FROM rate_governor WHERE scope_key='global'")
        assert cur.fetchone()["captcha_24h"] == 1
        # heartbeat reflects the parked challenge
        cur.execute("SELECT state FROM worker_heartbeat WHERE worker_id='w-apply-friend'")
        assert cur.fetchone()["state"] == "challenge_pending"

    # the reclaim sweep must NOT resurrect a parked wall (would re-drive the captcha)
    with pgqueue.connect(fleet_db) as conn:
        pgqueue.reclaim_stale_leases(conn, grace_seconds=0)
        with conn.cursor() as cur:
            cur.execute("SELECT status, apply_status FROM apply_queue WHERE url='a1'")
            q = cur.fetchone()
        assert q["status"] == "leased" and q["apply_status"] == "challenge_pending", \
            "reclaim must leave a parked wall frozen, not re-queue it"


def test_worker_apply_clear_marks_applied(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_one_apply(conn, "a2")

    loop = WorkerLoop(
        _factory(fleet_db), "w-apply-ok", home_ip="6.6.6.6", role="apply",
        apply_fn=lambda job: _CLEAR_HTML, machine_owner="jon", on_owner_machine=True,
    )
    res = loop.run_once()
    assert res["action"] == "applied" and res["url"] == "a2"

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, apply_status, worker_id FROM apply_queue WHERE url='a2'")
        q = cur.fetchone()
        assert q["status"] == "applied" and q["apply_status"] == "applied"
        assert q["worker_id"] == "w-apply-ok"  # core stamps the closing worker
        cur.execute("SELECT count(*) AS n FROM auth_challenge WHERE url='a2'")
        assert cur.fetchone()["n"] == 0  # no wall -> no challenge
        cur.execute("SELECT success_24h FROM rate_governor WHERE scope_key='global'")
        assert cur.fetchone()["success_24h"] == 1


def test_worker_apply_cf_block_skips_no_park(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_one_apply(conn, "a3")

    loop = WorkerLoop(
        _factory(fleet_db), "w-apply-cf", home_ip="7.7.7.7", role="apply",
        apply_fn=lambda job: _CF_HTML, on_owner_machine=False,
    )
    res = loop.run_once()
    assert res["action"] == "skipped" and res["kind"] == "cf"

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        # cf is a hard block: closed as blocked (not parked, not applied)
        cur.execute("SELECT status, apply_status, apply_error FROM apply_queue WHERE url='a3'")
        q = cur.fetchone()
        assert q["status"] == "blocked" and q["apply_status"] == "failed"
        assert q["apply_error"] == "captcha:cf"
        cur.execute("SELECT count(*) AS n FROM auth_challenge WHERE url='a3'")
        assert cur.fetchone()["n"] == 0  # nothing a human can solve -> no challenge
        cur.execute("SELECT block_24h FROM rate_governor WHERE scope_key='global'")
        assert cur.fetchone()["block_24h"] == 1


def test_worker_login_gate_parks_without_feeding_breaker(fleet_db):
    # A login wall parks (needs a human) but is NOT a bot-detection signal, so it
    # must NOT record a captcha governor outcome (else a sign-in-only host would
    # inflate challenge_rate and false-trip its breaker).
    with pgqueue.connect(fleet_db) as conn:
        _seed_one_apply(conn, "a-login", host="login.io")

    loop = WorkerLoop(
        _factory(fleet_db), "w-login", home_ip="8.8.8.8", role="apply",
        apply_fn=lambda job: _LOGIN_GATE_HTML, on_owner_machine=False,
    )
    res = loop.run_once()
    assert res["action"] == "parked_challenge" and res["kind"] == "login_gate"

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        # the wall was raised + the job parked (held, challenge_pending, not applied)
        cur.execute("SELECT count(*) AS n FROM auth_challenge WHERE url='a-login' AND resolved_at IS NULL")
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT status, apply_status FROM apply_queue WHERE url='a-login'")
        q = cur.fetchone()
        assert q["status"] == "leased" and q["apply_status"] == "challenge_pending"
        # but NO governor captcha outcome was recorded anywhere
        cur.execute("SELECT count(*) AS n FROM rate_governor WHERE captcha_24h > 0")
        assert cur.fetchone()["n"] == 0, "a login wall must not feed the captcha breaker"


def test_worker_role_validation():
    with pytest.raises(ValueError):
        WorkerLoop(_factory("x"), "w", home_ip="1.1.1.1", role="bogus")


# ===========================================================================
# 5. WorkerLoop APPLY status-passthrough path (apply_fn returns dict).
#    Prove crash != phantom-applied; captcha -> parked.
# ===========================================================================

def test_tick_apply_status_passthrough(fleet_db):
    # The new contract: apply_fn returns {"run_status": ...}. Prove crash != phantom-applied.
    from applypilot.fleet.worker import WorkerLoop
    from applypilot.apply import pgqueue
    from applypilot.fleet import queue

    def _seed(conn, url, domain="acme.com"):
        # use a distinct apply_domain per sub-case so the host-governor min-gap
        # from sub-case 1 does not block sub-cases 2 and 3
        with conn.cursor() as cur:
            cur.execute("INSERT INTO apply_queue (url, application_url, score, status, lane, approved_batch, dedup_key, apply_domain) "
                        "VALUES (%s,'http://acme.com/x','9','queued','ats','b1',%s,%s)", (url, "dk-"+url, domain))
        conn.commit()

    # applied -> applied + applied_set
    with pgqueue.connect(fleet_db) as conn:
        _seed(conn, "ja", "acme-a.com")
    loop = WorkerLoop(lambda: pgqueue.connect(fleet_db), "w1", home_ip="1.1.1.1", role="apply",
                      apply_fn=lambda job: {"run_status": "applied", "est_cost_usd": 0.01})
    assert loop.run_once()["action"] == "applied"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM apply_queue WHERE url='ja'"); assert cur.fetchone()["status"] == "applied"
        cur.execute("SELECT count(*) AS n FROM applied_set WHERE dedup_key='dk-ja'"); assert cur.fetchone()["n"] == 1

    # failed:no_result_line -> crash_unconfirmed, NOT applied
    with pgqueue.connect(fleet_db) as conn:
        _seed(conn, "jc", "acme-c.com")
    loop2 = WorkerLoop(lambda: pgqueue.connect(fleet_db), "w2", home_ip="2.2.2.2", role="apply",
                       apply_fn=lambda job: {"run_status": "failed:no_result_line", "est_cost_usd": 0.0})
    assert loop2.run_once()["action"] == "crash_unconfirmed"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM apply_queue WHERE url='jc'"); assert cur.fetchone()["status"] == "crash_unconfirmed"

    # captcha -> parked (auth_challenge raised, lease frozen)
    with pgqueue.connect(fleet_db) as conn:
        _seed(conn, "jp", "acme-p.com")
    loop3 = WorkerLoop(lambda: pgqueue.connect(fleet_db), "w3", home_ip="3.3.3.3", role="apply",
                       apply_fn=lambda job: {"run_status": "captcha", "est_cost_usd": 0.0})
    assert loop3.run_once()["action"] == "parked_challenge"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM auth_challenge WHERE url='jp' AND resolved_at IS NULL")
        assert cur.fetchone()["n"] == 1
