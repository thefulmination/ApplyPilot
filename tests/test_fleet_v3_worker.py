"""Captcha classifier + worker-loop tests (spec §5/§6/§7).

The classifier tests are pure (no Postgres). The WorkerLoop tests run the COMPUTE
path end-to-end against real Postgres (fake score_fn -> compute_queue 'done' +
llm_usage row) and exercise the APPLY wall path with a FAKE apply_fn returning
captcha HTML -- asserting a challenge row is raised and the job is NOT marked
applied (the lease stays held / the job parks). The browser/LLM/scrape calls are
all injected fakes; no real Chromium / API spend.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from applypilot.apply import pgqueue
from applypilot.fleet import captcha
from applypilot.fleet import config as fleet_config
from applypilot.fleet import queue
from applypilot.fleet.worker import WorkerLoop


def _canonical(conn, lane: str, url: str, score: float = 9.0) -> dict:
    policy = f"test-{lane}-policy"
    config_column = "ats_policy_version" if lane == "ats" else "linkedin_policy_version"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fleet_decision_policies (policy_version,lane,status) "
            "VALUES (%s,%s,'active') ON CONFLICT (policy_version) DO UPDATE SET status='active'",
            (policy, lane),
        )
        cur.execute(f"UPDATE fleet_config SET {config_column}=%s WHERE id=1", (policy,))
    conn.commit()
    now = datetime.now(timezone.utc)
    return {
        "decision_id": f"decision-{url}", "policy_version": policy,
        "decision_action": "apply", "qualification_verdict": "qualified",
        "qualification_score": 9.0, "qualification_floor": 7.0,
        "preference_score": 8.0, "outcome_score": 8.0, "final_score": score,
        "decision_confidence": 0.9, "decision_created_at": now,
        "decision_expires_at": now + timedelta(days=1), "input_hash": f"hash-{url}",
    }


def _authorize_existing(conn, table: str, lane: str, url: str) -> None:
    provenance = _canonical(conn, lane, url)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {table} SET decision_id=%s, policy_version=%s, decision_action=%s, "
            "qualification_verdict=%s, qualification_score=%s, qualification_floor=%s, "
            "preference_score=%s, outcome_score=%s, final_score=%s, decision_confidence=%s, "
            "decision_created_at=%s, decision_expires_at=%s, input_hash=%s WHERE url=%s",
            tuple(provenance.values()) + (url,),
        )
    conn.commit()


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
        **_canonical(conn, "ats", url),
    }], approved_batch="batchA")


@pytest.mark.parametrize(("preflight_status", "preflight_reason"), [
    ("live", "gh_api_200"),
    ("uncertain", "blocked_403"),
    ("unknown", "unknown_probe_state"),
])
def test_apply_preflight_non_dead_statuses_fall_through(
    fleet_db, preflight_status, preflight_reason,
):
    queue_url = f"preflight-{preflight_status}"
    calls = []
    with pgqueue.connect(fleet_db) as conn:
        _seed_one_apply(conn, queue_url, host="jobs.example")

    loop = WorkerLoop(
        _factory(fleet_db),
        f"w-{queue_url}",
        home_ip="1.2.3.4",
        role="apply",
        apply_fn=lambda job: calls.append(job["url"]) or {
            "run_status": "applied",
            "est_cost_usd": 0.25,
        },
        preflight_fn=lambda url: (preflight_status, preflight_reason),
    )

    result = loop.run_once()

    assert result["action"] == "applied"
    assert calls == [queue_url]
    with pgqueue.connect(fleet_db) as conn:
        row = conn.execute(
            "SELECT status::text, apply_status FROM apply_queue WHERE url=%s",
            (queue_url,),
        ).fetchone()
    assert row == {"status": "applied", "apply_status": "applied"}


def test_apply_preflight_exception_falls_through(fleet_db):
    queue_url = "preflight-timeout"
    calls = []
    with pgqueue.connect(fleet_db) as conn:
        _seed_one_apply(conn, queue_url, host="jobs.example")

    def timed_out(_url):
        raise TimeoutError("probe timed out")

    loop = WorkerLoop(
        _factory(fleet_db),
        "w-preflight-timeout",
        home_ip="1.2.3.4",
        role="apply",
        apply_fn=lambda job: calls.append(job["url"]) or {
            "run_status": "applied",
            "est_cost_usd": 0.25,
        },
        preflight_fn=timed_out,
    )

    result = loop.run_once()

    assert result["action"] == "applied"
    assert calls == [queue_url]
    with pgqueue.connect(fleet_db) as conn:
        row = conn.execute(
            "SELECT status::text, apply_status FROM apply_queue WHERE url=%s",
            (queue_url,),
        ).fetchone()
    assert row == {"status": "applied", "apply_status": "applied"}


def test_apply_preflight_dead_closes_without_calling_apply_fn(fleet_db):
    calls = []
    preflight_urls = []
    with pgqueue.connect(fleet_db) as conn:
        _seed_one_apply(conn, "preflight-dead", host="jobs.example")
    loop = WorkerLoop(
        _factory(fleet_db),
        "w-preflight-dead",
        home_ip="1.2.3.4",
        role="apply",
        apply_fn=lambda job: calls.append(job) or {"run_status": "applied"},
        preflight_fn=lambda url: preflight_urls.append(url) or ("dead", "http_404"),
    )

    result = loop.run_once()

    assert result == {
        "action": "preflight_dead",
        "url": "preflight-dead",
        "reason": "http_404",
    }
    assert calls == []
    assert preflight_urls == ["https://jobs.example/jobs/1"]
    with pgqueue.connect(fleet_db) as conn:
        row = conn.execute(
            "SELECT status::text, apply_status, apply_error, est_cost_usd "
            "FROM apply_queue WHERE url=%s",
            ("preflight-dead",),
        ).fetchone()
        event = conn.execute(
            "SELECT route, failure_class, result_metadata "
            "FROM apply_result_events WHERE url=%s ORDER BY id DESC LIMIT 1",
            ("preflight-dead",),
        ).fetchone()
    assert row == {
        "status": "failed",
        "apply_status": "expired",
        "apply_error": "preflight_http_404",
        "est_cost_usd": 0,
    }
    assert event["route"] == "preflight"
    assert event["failure_class"] == "preflight_dead"
    assert event["result_metadata"] == {
        "preflight_status": "dead",
        "preflight_reason": "http_404",
    }


def test_apply_preflight_dead_reports_lost_lease_when_write_rejected(fleet_db, monkeypatch):
    calls = []
    with pgqueue.connect(fleet_db) as conn:
        _seed_one_apply(conn, "preflight-lease-lost", host="jobs.example")
    monkeypatch.setattr(queue, "write_apply_result", lambda *args, **kwargs: False)
    loop = WorkerLoop(
        _factory(fleet_db),
        "w-preflight-lease-lost",
        home_ip="1.2.3.4",
        role="apply",
        apply_fn=lambda job: calls.append(job) or {"run_status": "applied"},
        preflight_fn=lambda url: ("dead", "http_404"),
    )

    result = loop.run_once()

    assert result == {
        "action": "lease_lost",
        "url": "preflight-lease-lost",
        "reason": "http_404",
    }
    assert calls == []
    assert any(
        "preflight_dead write rejected (lease lost) preflight-lease-lost" in event
        for event in loop._events
    )
    assert not any("wrote apply preflight_dead" in event for event in loop._events)


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


def test_apply_lifecycle_hard_fault_escapes_without_normal_result(
    fleet_db, tmp_path, monkeypatch
):
    from applypilot.apply import lifecycle_fault

    monkeypatch.setattr(lifecycle_fault.config, "DB_PATH", tmp_path / "applypilot.db")
    with pgqueue.connect(fleet_db) as conn:
        _seed_one_apply(conn, "cleanup-hard-fault", "cleanup.example")

    def hard_fault(_job):
        raise lifecycle_fault.LifecycleHardFault("browser cleanup could not be proven")

    loop = WorkerLoop(
        _factory(fleet_db),
        "w-cleanup-hard-fault",
        home_ip="5.5.5.5",
        role="apply",
        apply_fn=hard_fault,
    )

    with pytest.raises(lifecycle_fault.LifecycleHardFault, match="browser cleanup"):
        loop.run_once()

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, apply_status FROM apply_queue WHERE url='cleanup-hard-fault'"
        )
        row = cur.fetchone()
        assert row["status"] == "leased"
        assert row["apply_status"] is None
        cur.execute(
            "SELECT count(*) AS n FROM apply_result_events "
            "WHERE url='cleanup-hard-fault' AND status <> 'leased'"
        )
        assert cur.fetchone()["n"] == 0


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
        cur.execute(
            "SELECT application_tool_calls, final_result_source, result_metadata "
            "FROM apply_result_events WHERE url='a2' ORDER BY id DESC LIMIT 1"
        )
        event = cur.fetchone()
        assert event["application_tool_calls"] == 0
        assert event["final_result_source"] == "legacy_html_classifier"
        assert event["result_metadata"]["evidence_quality"] == "legacy_html_classifier"


def test_worker_refuses_to_lease_when_source_version_is_stale(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_one_apply(conn, "a-stale-version")
        fleet_config.set_pinned_version(conn, "2.0.0")

    calls = []
    loop = WorkerLoop(
        _factory(fleet_db), "w-stale", home_ip="6.6.6.6", role="apply",
        apply_fn=lambda job: calls.append(job) or _CLEAR_HTML,
        machine_owner="jon", on_owner_machine=True, sw_version="1.0.0",
    )

    res = loop.run_once()
    assert res["action"] == "version_mismatch"
    assert res["expected_version"] == "2.0.0"
    assert res["sw_version"] == "1.0.0"
    assert calls == []

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, lease_owner FROM apply_queue WHERE url='a-stale-version'")
        q = cur.fetchone()
        assert q["status"] == "queued"
        assert q["lease_owner"] is None
        cur.execute("SELECT state, sw_version FROM worker_heartbeat WHERE worker_id='w-stale'")
        hb = cur.fetchone()
        assert hb["state"] == "version_mismatch"
        assert hb["sw_version"] == "1.0.0"


def test_canary_source_version_only_unblocks_the_declared_worker(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_one_apply(conn, "a-canary-version", host="canary.example")
        fleet_config.set_pinned_version(
            conn,
            "1.0.0",
            canary_version="2.0.0-rc1",
            canary_worker_id="w-canary",
        )

    non_canary_calls = []
    non_canary = WorkerLoop(
        _factory(fleet_db), "w-other", home_ip="6.6.6.6", role="apply",
        apply_fn=lambda job: non_canary_calls.append(job) or _CLEAR_HTML,
        machine_owner="jon", on_owner_machine=True, sw_version="2.0.0-rc1",
    )
    assert non_canary.run_once()["action"] == "version_mismatch"
    assert non_canary_calls == []

    canary = WorkerLoop(
        _factory(fleet_db), "w-canary", home_ip="6.6.6.6", role="apply",
        apply_fn=lambda job: _CLEAR_HTML,
        machine_owner="jon", on_owner_machine=True, sw_version="2.0.0-rc1",
    )
    res = canary.run_once()
    assert res["action"] == "applied"
    assert res["url"] == "a-canary-version"

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, worker_id FROM apply_queue WHERE url='a-canary-version'")
        q = cur.fetchone()
        assert q["status"] == "applied"
        assert q["worker_id"] == "w-canary"


def test_worker_refuses_to_lease_when_source_version_is_stale(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_one_apply(conn, "a-stale-version")
        fleet_config.set_pinned_version(conn, "2.0.0")

    calls = []
    loop = WorkerLoop(
        _factory(fleet_db), "w-stale", home_ip="6.6.6.6", role="apply",
        apply_fn=lambda job: calls.append(job) or _CLEAR_HTML,
        machine_owner="jon", on_owner_machine=True, sw_version="1.0.0",
    )

    res = loop.run_once()
    assert res["action"] == "version_mismatch"
    assert res["expected_version"] == "2.0.0"
    assert res["sw_version"] == "1.0.0"
    assert calls == []

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, lease_owner FROM apply_queue WHERE url='a-stale-version'")
        q = cur.fetchone()
        assert q["status"] == "queued"
        assert q["lease_owner"] is None
        cur.execute("SELECT state, sw_version FROM worker_heartbeat WHERE worker_id='w-stale'")
        hb = cur.fetchone()
        assert hb["state"] == "version_mismatch"
        assert hb["sw_version"] == "1.0.0"


def test_canary_source_version_only_unblocks_the_declared_worker(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_one_apply(conn, "a-canary-version", host="canary.example")
        fleet_config.set_pinned_version(
            conn,
            "1.0.0",
            canary_version="2.0.0-rc1",
            canary_worker_id="w-canary",
        )

    non_canary_calls = []
    non_canary = WorkerLoop(
        _factory(fleet_db), "w-other", home_ip="6.6.6.6", role="apply",
        apply_fn=lambda job: non_canary_calls.append(job) or _CLEAR_HTML,
        machine_owner="jon", on_owner_machine=True, sw_version="2.0.0-rc1",
    )
    assert non_canary.run_once()["action"] == "version_mismatch"
    assert non_canary_calls == []

    canary = WorkerLoop(
        _factory(fleet_db), "w-canary", home_ip="6.6.6.6", role="apply",
        apply_fn=lambda job: _CLEAR_HTML,
        machine_owner="jon", on_owner_machine=True, sw_version="2.0.0-rc1",
    )
    res = canary.run_once()
    assert res["action"] == "applied"
    assert res["url"] == "a-canary-version"

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, worker_id FROM apply_queue WHERE url='a-canary-version'")
        q = cur.fetchone()
        assert q["status"] == "applied"
        assert q["worker_id"] == "w-canary"


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


def test_greenhouse_adapter_no_confirmation_is_crash_unconfirmed(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        provenance = _canonical(conn, "ats", "gh-no-confirm")
        queue.push_apply_jobs(
            conn,
            [{
                "url": "gh-no-confirm",
                "company": "Acme",
                "title": "Staff Engineer",
                "application_url": "https://boards.greenhouse.io/acme/jobs/123",
                "score": 9.0,
                "target_host": "greenhouse.io",
                "dedup_key": "dk-gh-no-confirm",
                **provenance,
            }],
            approved_batch="batchA",
        )

    loop = WorkerLoop(
        _factory(fleet_db),
        "w-gh-adapter",
        home_ip="4.4.4.4",
        role="apply",
        apply_fn=lambda job: {
            "run_status": "failed:no_confirmation",
            "est_cost_usd": 0.0,
            "route": "adapter_submit:greenhouse",
            "failure_class": "adapter_no_confirmation",
            "last_tool": "greenhouse_adapter",
        },
    )

    assert loop.run_once()["action"] == "crash_unconfirmed"

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, apply_status, apply_error FROM apply_queue WHERE url='gh-no-confirm'")
        q = cur.fetchone()
        assert q["status"] == "crash_unconfirmed"
        assert q["apply_status"] == "crash_unconfirmed"
        assert q["apply_error"] == "failed:no_confirmation"
        cur.execute("SELECT count(*) AS n FROM applied_set WHERE dedup_key='dk-gh-no-confirm'")
        assert cur.fetchone()["n"] == 1
        cur.execute(
            "SELECT route, failure_class, last_tool FROM apply_result_events "
            "WHERE url='gh-no-confirm' ORDER BY id DESC LIMIT 1"
        )
        event = cur.fetchone()
        assert event["route"] == "adapter_submit:greenhouse"
        assert event["failure_class"] == "adapter_no_confirmation"
        assert event["last_tool"] == "greenhouse_adapter"


# ===========================================================================
# 4b. Crash + log visibility: _scrub redacts secrets; _heartbeat persists
#     last_error / recent_log (scrubbed) to worker_heartbeat.
# ===========================================================================

def test_scrub_redacts_dsn_and_tokens(monkeypatch):
    from applypilot.fleet.worker import _scrub

    # None -> "" (never leaks, never raises)
    assert _scrub(None) == ""

    # Env secret values are redacted by exact value (the surest match).
    monkeypatch.setenv("FLEET_PG_DSN", "host=localhost port=5432 dbname=applypilot_fleet user=postgres password=hunter2")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deadbeefdeadbeefdeadbeefdeadbeef00")
    monkeypatch.setenv("SOME_TOKEN", "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789")

    tb = (
        "Traceback (most recent call last):\n"
        '  File "worker.py", line 1, in run\n'
        "    connect('host=localhost port=5432 dbname=applypilot_fleet user=postgres password=hunter2')\n"
        "psycopg.OperationalError: could not connect using DATABASE_URL="
        "postgresql://postgres:hunter2@localhost:5432/applypilot_fleet\n"
        "Authorization: Bearer sk-deadbeefdeadbeefdeadbeefdeadbeef00\n"
        "leaked token ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789\n"
        # JSON `"key":"value"` form: the closing quote of the KEY sits between the
        # keyword and the colon. A naive `keyword[:=]` pattern never matches here,
        # so these short, never-in-env values would leak unless the regex handles
        # the quoted-key form explicitly. (Regression guard for the S1 bypass.)
        'agent error body: {"error":"auth","api_key":"AKIAEXAMPLE12345","password":"S3cretDbPw"}\n'
        '  spaced json: { "token": "abc123def456" }\n'
    )
    out = _scrub(tb)
    # No secret material survives.
    for leak in ("hunter2", "password=hunter2",
                 "sk-deadbeefdeadbeefdeadbeefdeadbeef00",
                 "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
                 "postgresql://postgres:hunter2@localhost",
                 "dbname=applypilot_fleet",
                 # JSON-form values (short + not present in os.environ): these are the
                 # exact strings the broken pattern let through.
                 "AKIAEXAMPLE12345", "S3cretDbPw", "abc123def456"):
        assert leak not in out, f"secret leaked through _scrub: {leak!r}"
    assert "[REDACTED]" in out
    # The benign frame text survives so the traceback is still useful.
    assert "Traceback" in out and "OperationalError" in out


def test_heartbeat_persists_scrubbed_last_error_and_recent_log(fleet_db, monkeypatch):
    from applypilot.fleet.worker import _heartbeat, _scrub

    monkeypatch.setenv("FLEET_PG_DSN", "host=db port=5432 dbname=x user=u password=topsecretpw")
    planted = (
        "ERROR boom\n"
        "connect: host=db port=5432 dbname=x user=u password=topsecretpw\n"
        "token sk-aaaabbbbccccddddeeeeffffgggghhhh1234\n"
    )
    le = _scrub(planted)[:4000]
    rl = _scrub("line1\nline2\nhost=db password=topsecretpw\n")[-8000:]

    with pgqueue.connect(fleet_db) as conn:
        _heartbeat(conn, worker_id="w-crash", machine_owner="jon", home_ip="1.2.3.4",
                   role="apply", state="idle", last_error=le, recent_log=rl)
        # UPSERT path: a second beat OVERWRITES both fields.
        _heartbeat(conn, worker_id="w-crash", machine_owner="jon", home_ip="1.2.3.4",
                   role="apply", state="applying", last_error="second", recent_log="freshtail")

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT state, last_error, recent_log FROM worker_heartbeat WHERE worker_id='w-crash'")
        row = cur.fetchone()
    assert row["state"] == "applying"
    assert row["last_error"] == "second" and row["recent_log"] == "freshtail"  # overwrote, not coalesced

    # And what we stored from the planted secrets carries NO secret material.
    for leak in ("topsecretpw", "sk-aaaabbbbccccddddeeeeffffgggghhhh1234", "password=topsecretpw"):
        assert leak not in le and leak not in rl


def test_run_forever_records_scrubbed_crash(fleet_db, monkeypatch):
    """run_forever's tick-exception handler must CAPTURE the traceback (the prior bug
    swallowed it) -- scrubbed, capped, and visible via the next heartbeat."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-zzzzyyyyxxxxwwwwvvvvuuuuttttssss9999")

    def boom_factory():
        raise RuntimeError("kaboom secret sk-zzzzyyyyxxxxwwwwvvvvuuuuttttssss9999")

    loop = WorkerLoop(boom_factory, "w-boom", home_ip="9.9.9.9", role="compute",
                      score_fn=lambda j: ({}, 0))
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 1  # let exactly one tick run, then stop

    loop.run_forever(idle_sleep_seconds=0, stop=stop)
    assert loop._last_error is not None
    assert "RuntimeError" in loop._last_error
    assert "sk-zzzzyyyyxxxxwwwwvvvvuuuuttttssss9999" not in loop._last_error
    assert len(loop._last_error) <= 4000


def test_run_forever_does_not_swallow_lifecycle_hard_fault(fleet_db):
    from applypilot.apply.lifecycle_fault import LifecycleHardFault

    loop = WorkerLoop(
        lambda: pgqueue.connect(fleet_db),
        "w-hard-fault",
        home_ip="9.9.9.9",
        role="compute",
        score_fn=lambda job: ({}, 0),
    )
    loop.run_once = lambda: (_ for _ in ()).throw(LifecycleHardFault("stop worker"))

    with pytest.raises(LifecycleHardFault, match="stop worker"):
        loop.run_forever(idle_sleep_seconds=0)


# ===========================================================================
# 5. WorkerLoop APPLY status-passthrough path (apply_fn returns dict).
#    Prove crash != phantom-applied; captcha -> parked.
# ===========================================================================

def test_tick_apply_status_passthrough(fleet_db):
    # The new contract: apply_fn returns {"run_status": ...}. Prove crash != phantom-applied.
    from applypilot.fleet.worker import WorkerLoop
    from applypilot.apply import pgqueue

    def _seed(conn, url, domain="acme.com"):
        # use a distinct apply_domain per sub-case so the host-governor min-gap
        # from sub-case 1 does not block sub-cases 2 and 3
        with conn.cursor() as cur:
            cur.execute("INSERT INTO apply_queue (url, application_url, score, status, lane, approved_batch, dedup_key, apply_domain) "
                        "VALUES (%s,'http://acme.com/x','9','queued','ats','b1',%s,%s)", (url, "dk-"+url, domain))
        conn.commit()
        _authorize_existing(conn, "apply_queue", "ats", url)

    # applied -> applied + applied_set
    with pgqueue.connect(fleet_db) as conn:
        _seed(conn, "ja", "acme-a.com")
    loop = WorkerLoop(lambda: pgqueue.connect(fleet_db), "w1", home_ip="1.1.1.1", role="apply",
                      apply_fn=lambda job: {"run_status": "applied", "est_cost_usd": 0.01})
    assert loop.run_once()["action"] == "applied"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM apply_queue WHERE url='ja'")
        assert cur.fetchone()["status"] == "applied"
        cur.execute("SELECT count(*) AS n FROM applied_set WHERE dedup_key='dk-ja'")
        assert cur.fetchone()["n"] == 1

    # A zero-tool no-result proves the application was untouched, so it is safely requeued.
    with pgqueue.connect(fleet_db) as conn:
        _seed(conn, "jc", "acme-c.com")
    loop2 = WorkerLoop(lambda: pgqueue.connect(fleet_db), "w2", home_ip="2.2.2.2", role="apply",
                       apply_fn=lambda job: {
                           "run_status": "failed:no_result_line",
                           "est_cost_usd": 0.0,
                           "agent": "claude",
                           "agent_model": "claude-sonnet-4",
                           "duration_ms": 3210,
                           "route": "agent",
                           "failure_class": "zero_tool_no_result",
                           "tool_calls_total": 0,
                           "application_tool_calls": 0,
                           "last_tool": "",
                           "result_metadata": {
                               "job_log": "worker.log",
                               "adapter_name": "greenhouse",
                               "adapter_plan_ready": True,
                           },
                       })
    assert loop2.run_once()["action"] == "zero_tool_requeue"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, attempts, apply_error FROM apply_queue WHERE url='jc'")
        row = cur.fetchone()
        assert row["status"] == "queued"
        assert row["attempts"] == 0
        assert row["apply_error"] == "failed:zero_tool_no_result"
        cur.execute(
            "SELECT count(*) AS n FROM apply_result_events "
            "WHERE url='jc' AND status <> 'leased'"
        )
        assert cur.fetchone()["n"] == 0
        cur.execute("UPDATE apply_queue SET status='failed' WHERE url='jc'")
        conn.commit()

    # captcha -> parked (auth_challenge raised, lease frozen)
    with pgqueue.connect(fleet_db) as conn:
        _seed(conn, "jp", "acme-p.com")
    loop3 = WorkerLoop(lambda: pgqueue.connect(fleet_db), "w3", home_ip="3.3.3.3", role="apply",
                       apply_fn=lambda job: {
                           "run_status": "captcha",
                           "est_cost_usd": 0.12,
                           "agent": "codex",
                           "agent_model": "gpt-test",
                           "route": "agent",
                           "tool_calls_total": 4,
                           "application_tool_calls": 2,
                           "last_tool": "browser_click",
                           "result_metadata": {"preflight_status": "live"},
                       })
    assert loop3.run_once()["action"] == "parked_challenge"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM auth_challenge WHERE url='jp' AND resolved_at IS NULL")
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT status, apply_status, est_cost_usd FROM apply_queue WHERE url='jp'")
        parked = cur.fetchone()
        assert parked["status"] == "leased"
        assert parked["apply_status"] == "challenge_pending"
        assert float(parked["est_cost_usd"]) == 0.12
        cur.execute(
            "SELECT status, route, est_cost_usd, tool_calls_total, application_tool_calls "
            "FROM apply_result_events WHERE url='jp' ORDER BY id DESC LIMIT 1"
        )
        event = cur.fetchone()
        assert event["status"] == "challenge_pending"
        assert event["route"] == "agent"
        assert event["est_cost_usd"] == pytest.approx(0.12)
        assert event["tool_calls_total"] == 4
        assert event["application_tool_calls"] == 2

    # adapter plan gaps are a zero-spend owner/profile task, never a paid-agent failure.
    with pgqueue.connect(fleet_db) as conn:
        _seed(conn, "jprofile", "greenhouse-profile.com")
    loop4 = WorkerLoop(
        lambda: pgqueue.connect(fleet_db),
        "w4",
        home_ip="4.4.4.4",
        role="apply",
        apply_fn=lambda job: {
            "run_status": "profile_required",
            "est_cost_usd": 0.0,
            "route": "adapter_plan:greenhouse",
            "failure_class": "adapter_unmapped_required",
            "result_metadata": {"unmapped_required": ["Relocation"]},
        },
    )
    assert loop4.run_once()["action"] == "parked_challenge"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, apply_status, est_cost_usd FROM apply_queue WHERE url='jprofile'")
        parked = cur.fetchone()
        assert parked["status"] == "leased"
        assert parked["apply_status"] == "challenge_pending"
        assert float(parked["est_cost_usd"]) == 0.0
        cur.execute("SELECT kind FROM auth_challenge WHERE url='jprofile' AND resolved_at IS NULL")
        assert cur.fetchone()["kind"] == "profile_required"


def test_apply_worker_injects_attempt_store_only_when_factory_configured(fleet_db):
    captured = {}

    with pgqueue.connect(fleet_db) as conn:
        provenance = _canonical(conn, "ats", "attempt-store-job")
        queue.push_apply_jobs(
            conn,
            [{
                "url": "attempt-store-job",
                "company": "Acme",
                "title": "Operator",
                "application_url": "https://boards.greenhouse.io/acme/jobs/1",
                "score": 9.0,
                "target_host": "boards.greenhouse.io",
                "dedup_key": "attempt-store-dedup",
                **provenance,
            }],
            approved_batch="batchA",
        )

    sentinel = object()

    def factory(conn, job):
        captured["factory_job"] = job["url"]
        return sentinel

    def apply_fn(job, *, attempt_store=None):
        captured["store"] = attempt_store
        return {"run_status": "failed:form_error", "est_cost_usd": 0.0}

    loop = WorkerLoop(
        _factory(fleet_db),
        "w-attempt-store",
        home_ip="5.5.5.5",
        role="apply",
        apply_fn=apply_fn,
        attempt_store_factory=factory,
    )

    assert loop.run_once()["action"] == "failed"
    assert captured == {"factory_job": "attempt-store-job", "store": sentinel}


# ===========================================================================
# 6. WorkerLoop LINKEDIN path (_tick_linkedin).
#    applied->applied+applied_set; failed:no_result_line->crash_unconfirmed;
#    captcha->parked + halted_until set (one tx).
# ===========================================================================

def test_tick_linkedin_routes(fleet_db):
    from applypilot.fleet.worker import WorkerLoop
    from applypilot.apply import pgqueue

    def _seed(conn, url):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO linkedin_queue "
                "(url, application_url, score, status, lane, approved_batch, dedup_key, "
                "linkedin_resolve_status, linkedin_resolved_at) "
                "VALUES (%s,'https://linkedin.com/jobs/x','9','queued','linkedin','b1',%s,'easy_apply',now())",
                (url, "dk-" + url),
            )
        conn.commit()
        _authorize_existing(conn, "linkedin_queue", "linkedin", url)

    # Pre-seed the governor with min_gap_seconds=0 so back-to-back sub-cases can all lease.
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO rate_governor (scope_key, daily_cap, min_gap_seconds, base_min_gap_seconds) "
                "VALUES ('account:linkedin', 100, 0, 0) ON CONFLICT (scope_key) DO UPDATE "
                "SET min_gap_seconds=0, base_min_gap_seconds=0, last_applied_at=NULL, daily_cap=100",
            )
        conn.commit()

    # applied -> applied + applied_set
    with pgqueue.connect(fleet_db) as conn:
        _seed(conn, "ka")
    loop = WorkerLoop(lambda: pgqueue.connect(fleet_db), "w1", home_ip="1.1.1.1", role="linkedin",
                      public_ip="1.1.1.1", owner_ip="1.1.1.1",
                      apply_fn=lambda job: {"run_status": "applied", "est_cost_usd": 0.0})
    assert loop.run_once()["action"] == "applied"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM linkedin_queue WHERE url='ka'")
        assert cur.fetchone()["status"] == "applied"
        cur.execute("SELECT count(*) AS n FROM applied_set WHERE dedup_key='dk-ka'")
        assert cur.fetchone()["n"] == 1

    # failed:no_result_line -> crash_unconfirmed (never phantom-applied)
    with pgqueue.connect(fleet_db) as conn:
        _seed(conn, "kc")
    loop2 = WorkerLoop(lambda: pgqueue.connect(fleet_db), "w2", home_ip="1.1.1.1", role="linkedin",
                       public_ip="1.1.1.1", owner_ip="1.1.1.1",
                       apply_fn=lambda job: {"run_status": "failed:no_result_line", "est_cost_usd": 0.0})
    assert loop2.run_once()["action"] == "crash_unconfirmed"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM linkedin_queue WHERE url='kc'")
        assert cur.fetchone()["status"] == "crash_unconfirmed"

    # captcha -> parked + halted_until set (one tx)
    with pgqueue.connect(fleet_db) as conn:
        _seed(conn, "kp")
    loop3 = WorkerLoop(lambda: pgqueue.connect(fleet_db), "w3", home_ip="1.1.1.1", role="linkedin",
                       public_ip="1.1.1.1", owner_ip="1.1.1.1",
                       apply_fn=lambda job: {"run_status": "captcha", "est_cost_usd": 0.0})
    assert loop3.run_once()["action"] == "parked_challenge"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT halted_until FROM rate_governor WHERE scope_key='account:linkedin'")
        assert cur.fetchone()["halted_until"] is not None
        cur.execute("SELECT count(*) AS n FROM auth_challenge WHERE url='kp' AND resolved_at IS NULL")
        assert cur.fetchone()["n"] == 1


def test_linkedin_challenge_insert_failure_rolls_back_park_transaction(fleet_db, monkeypatch):
    from applypilot.fleet import worker as worker_module

    url = "linkedin-atomic-park"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fleet_decision_policies (policy_version,lane,status) "
            "VALUES ('linkedin-atomic-policy','linkedin','active')"
        )
        cur.execute(
            "INSERT INTO linkedin_queue "
            "(url, application_url, score, status, lane, approved_batch, dedup_key, "
            "linkedin_resolve_status, linkedin_resolved_at, decision_id, policy_version, "
            "decision_action, qualification_verdict, qualification_score, qualification_floor, "
            "preference_score, outcome_score, final_score, decision_confidence, "
            "decision_created_at, decision_expires_at, input_hash) "
            "VALUES (%s,'https://linkedin.com/jobs/x',9,'queued','linkedin','b1',%s,'easy_apply',now(),"
            "'decision-linkedin-atomic','linkedin-atomic-policy','apply','qualified',9,7,8,8,9,.9,"
            "now(),now() + interval '1 day','hash-linkedin-atomic')",
            (url, "dk-" + url),
        )
        cur.execute(
            "UPDATE fleet_config SET linkedin_policy_version='linkedin-atomic-policy', "
            "linkedin_apply_mode='steady', paused=FALSE WHERE id=1"
        )
        cur.execute(
            "INSERT INTO rate_governor (scope_key, daily_cap, min_gap_seconds, base_min_gap_seconds) "
            "VALUES ('account:linkedin',100,0,0) ON CONFLICT (scope_key) DO UPDATE "
            "SET min_gap_seconds=0, base_min_gap_seconds=0, last_applied_at=NULL, daily_cap=100"
        )
        conn.commit()

    def fail_challenge_insert(*args, **kwargs):
        raise RuntimeError("simulated challenge insert failure")

    monkeypatch.setattr(worker_module, "_insert_challenge", fail_challenge_insert)
    loop = WorkerLoop(
        lambda: pgqueue.connect(fleet_db), "linkedin-atomic-worker",
        home_ip="1.1.1.1", role="linkedin", public_ip="1.1.1.1", owner_ip="1.1.1.1",
        apply_fn=lambda job: {"run_status": "captcha", "est_cost_usd": 0.0},
    )

    with pytest.raises(RuntimeError, match="simulated challenge insert failure"):
        loop.run_once()

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, apply_status, lease_expires_at < now() + interval '1 day' AS ordinary_lease "
            "FROM linkedin_queue WHERE url=%s",
            (url,),
        )
        row = cur.fetchone()
        assert row["status"] == "leased"
        assert row["apply_status"] is None
        assert row["ordinary_lease"] is True
        cur.execute("SELECT count(*) AS n FROM auth_challenge WHERE url=%s", (url,))
        assert cur.fetchone()["n"] == 0
