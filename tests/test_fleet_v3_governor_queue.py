"""Governor + governed claims, against real Postgres.

The lease tests are the ones that matter most: a double-grab, a re-applied posting,
or a lease that ignores a tripped breaker is a real-world account/IP risk.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from psycopg.errors import DeadlockDetected

from applypilot.apply import pgqueue
from applypilot.fleet import config as fcfg
from applypilot.fleet import governor
from applypilot.fleet import queue


def _activate_policy(conn, lane):
    policy = f"test-{lane}-policy"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fleet_decision_policies (policy_version,lane,status) "
            "VALUES (%s,%s,'active') ON CONFLICT (policy_version) DO UPDATE SET status='active'",
            (policy, lane),
        )
        cur.execute(
            f"UPDATE fleet_config SET {lane}_policy_version=%s WHERE id=1",
            (policy,),
        )
    conn.commit()
    return policy


def _canonical(url, score, policy):
    now = datetime.now(timezone.utc)
    return {
        "decision_id": f"decision-{url}", "policy_version": policy,
        "decision_action": "apply", "qualification_verdict": "qualified",
        "qualification_score": 9.0, "qualification_floor": 7.0,
        "preference_score": 8.0, "outcome_score": 8.0, "final_score": float(score),
        "decision_confidence": 0.9, "decision_created_at": now,
        "decision_expires_at": now + timedelta(days=1), "input_hash": f"hash-{url}",
    }


def _seed_apply(conn, url, *, host, score=8.0, company="Co", title="Role", approved="b1", dedup_key=None):
    policy = _activate_policy(conn, "ats")
    queue.push_apply_jobs(conn, [{
        "url": url, "company": company, "title": title,
        "application_url": f"https://{host}/jobs/x", "score": score,
        "target_host": host, "dedup_key": dedup_key,
        **_canonical(url, score, policy),
    }], approved_batch=approved)


def _seed_linkedin(conn, url, *, score=9.0, company="Co", title="Role", approved="b1"):
    policy = _activate_policy(conn, "linkedin")
    queue.push_linkedin_jobs(conn, [{
        "url": url, "company": company, "title": title,
        "application_url": f"https://linkedin.com/jobs/{url}", "score": score,
        "linkedin_resolve_status": "easy_apply",
        "linkedin_resolved_at": datetime.now(timezone.utc),
        "linkedin_resolve_error": None,
        "linkedin_unresolved_kind": None, "linkedin_next_action": None,
        **_canonical(url, score, policy),
    }], approved_batch=approved)


def test_queue_pushes_require_complete_canonical_provenance(fleet_db):
    row = {
        "url": "missing", "company": "Co", "title": "Role",
        "application_url": "https://example.com/apply", "score": 9.0,
        "target_host": "example.com", "linkedin_unresolved_kind": None,
        "linkedin_next_action": None,
    }
    with pgqueue.connect(fleet_db) as conn:
        with pytest.raises(ValueError, match="canonical decision provenance"):
            queue.push_apply_jobs(conn, [row])
        with pytest.raises(ValueError, match="canonical decision provenance"):
            queue.push_linkedin_jobs(conn, [row])


@pytest.mark.parametrize(
    "mutation",
    ["legacy", "wrong_policy", "review", "unqualified", "expired", "below_floor", "below_threshold"],
)
def test_apply_lease_fails_closed_on_invalid_canonical_authority(fleet_db, mutation):
    with pgqueue.connect(fleet_db) as conn:
        if mutation == "legacy":
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO apply_queue (url,application_url,score,status,lane,approved_batch,apply_domain) "
                    "VALUES ('legacy','https://example.com',9,'queued','ats','b1','example.com')"
                )
            conn.commit()
        else:
            _seed_apply(conn, "guarded", host="example.com", score=9)
            with conn.cursor() as cur:
                if mutation == "wrong_policy":
                    cur.execute(
                        "INSERT INTO fleet_decision_policies (policy_version,lane,status) "
                        "VALUES ('other-ats','ats','canary')"
                    )
                    cur.execute("UPDATE fleet_config SET ats_policy_version='other-ats' WHERE id=1")
                elif mutation == "review":
                    cur.execute("UPDATE apply_queue SET decision_action='review' WHERE url='guarded'")
                elif mutation == "unqualified":
                    cur.execute("UPDATE apply_queue SET qualification_verdict='unqualified' WHERE url='guarded'")
                elif mutation == "expired":
                    cur.execute(
                        "UPDATE apply_queue SET decision_created_at=now()-interval '2 days', "
                        "decision_expires_at=now()-interval '1 day' WHERE url='guarded'"
                    )
                elif mutation == "below_floor":
                    cur.execute("UPDATE apply_queue SET qualification_score=6 WHERE url='guarded'")
                elif mutation == "below_threshold":
                    cur.execute("UPDATE fleet_config SET approval_threshold=9.5 WHERE id=1")
            conn.commit()
        assert queue.lease_apply(conn, "w", home_ip="1.1.1.1") is None


@pytest.mark.parametrize("mutation", ["legacy", "wrong_policy", "review", "expired", "below_floor"])
def test_linkedin_lease_fails_closed_on_invalid_canonical_authority(fleet_db, mutation):
    with pgqueue.connect(fleet_db) as conn:
        if mutation == "legacy":
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO linkedin_queue (url,application_url,score,status,lane,approved_batch) "
                    "VALUES ('legacy-li','https://linkedin.com/jobs/1',9,'queued','linkedin','b1')"
                )
            conn.commit()
        else:
            _seed_linkedin(conn, "guarded-li")
            with conn.cursor() as cur:
                if mutation == "wrong_policy":
                    cur.execute(
                        "INSERT INTO fleet_decision_policies (policy_version,lane,status) "
                        "VALUES ('other-linkedin','linkedin','canary')"
                    )
                    cur.execute(
                        "UPDATE fleet_config SET linkedin_policy_version='other-linkedin' WHERE id=1"
                    )
                elif mutation == "review":
                    cur.execute(
                        "UPDATE linkedin_queue SET decision_action='review' WHERE url='guarded-li'"
                    )
                elif mutation == "expired":
                    cur.execute(
                        "UPDATE linkedin_queue SET decision_created_at=now()-interval '2 days', "
                        "decision_expires_at=now()-interval '1 day' WHERE url='guarded-li'"
                    )
                elif mutation == "below_floor":
                    cur.execute(
                        "UPDATE linkedin_queue SET qualification_score=6 WHERE url='guarded-li'"
                    )
            conn.commit()
        assert queue.lease_linkedin(
            conn, "w", public_ip="1.1.1.1", owner_ip="1.1.1.1", min_gap_seconds=0
        ) is None


# ---- approval gate (R11) -------------------------------------------------------

def test_lease_requires_approval(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET approval_threshold=7 WHERE id=1")
        conn.commit()
        _seed_apply(conn, "u1", host="greenhouse.io", approved=None)  # not approved
        assert queue.lease_apply(conn, "w1", home_ip="1.1.1.1") is None
        assert queue.approve_jobs(conn, ["u1"], "batchA") == 1
        leased = queue.lease_apply(conn, "w1", home_ip="1.1.1.1")
        assert leased and leased["url"] == "u1"


def test_lease_apply_enforces_approval_threshold_even_when_approved(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET approval_threshold=7 WHERE id=1")
        conn.commit()
        _seed_apply(conn, "too-low", host="greenhouse.io", score=6.9, approved="batch-low")

        assert queue.lease_apply(conn, "w1", home_ip="1.1.1.1") is None


def test_lease_apply_respects_operator_threshold_below_seven(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET approval_threshold=6.5 WHERE id=1")
        conn.commit()
        _seed_apply(conn, "at-floor", host="greenhouse.io", score=6.5, approved="batch-ok")

        leased = queue.lease_apply(conn, "w1", home_ip="1.1.1.1")

    assert leased and leased["url"] == "at-floor"


def test_lease_apply_requires_positive_canary_capacity(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply(conn, "canary-required", host="greenhouse.io", score=9, approved="batch-ok")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE fleet_config SET paused=FALSE, ats_paused=FALSE, "
                "ats_apply_mode='canary', canary_enabled=FALSE, canary_remaining=NULL WHERE id=1"
            )
        conn.commit()

        assert queue.lease_apply(conn, "w1", home_ip="1.1.1.1") is None

        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET ats_apply_mode='canary', canary_enabled=TRUE, canary_remaining=0 WHERE id=1")
        conn.commit()

        assert queue.lease_apply(conn, "w1", home_ip="1.1.1.1") is None

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE apply_queue SET status='queued', lease_owner=NULL, lease_expires_at=NULL WHERE url='canary-required'"
            )
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET ats_apply_mode='canary', canary_enabled=TRUE, canary_remaining=1 WHERE id=1")
        conn.commit()
        assert queue.lease_apply(conn, "w2", home_ip="1.1.1.1") is not None


def test_repush_apply_job_clears_stale_approval_when_score_drops_below_threshold(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET approval_threshold=7 WHERE id=1")
        conn.commit()
        _seed_apply(conn, "score-drift", host="greenhouse.io", score=8.0, approved="batch-high")

        queue.push_apply_jobs(conn, [{
            "url": "score-drift",
            "company": "Co",
            "title": "Role",
            "application_url": "https://greenhouse.io/jobs/x",
            "score": 5.0,
            "target_host": "greenhouse.io",
            **_canonical("score-drift-v2", 5.0, "test-ats-policy"),
        }], approved_batch=None)

        with conn.cursor() as cur:
            cur.execute("SELECT score, approved_batch FROM apply_queue WHERE url='score-drift'")
            row = cur.fetchone()

    assert float(row["score"]) == 5.0
    assert row["approved_batch"] is None


def test_lease_linkedin_enforces_approval_threshold_even_when_approved(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE fleet_config SET approval_threshold=7, "
                "linkedin_apply_mode='canary', linkedin_canary_enabled=FALSE, linkedin_canary_remaining=NULL WHERE id=1"
            )
        conn.commit()
        _seed_linkedin(conn, "li-low", score=6.9, approved="batch-low")

        assert queue.lease_linkedin(conn, "w1", public_ip="1.1.1.1", owner_ip="1.1.1.1") is None


def test_lease_linkedin_requires_positive_canary_capacity(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        governor.ensure_scope(conn, governor.LINKEDIN_ACCOUNT, daily_cap=20, min_gap_seconds=0)
        _seed_linkedin(conn, "li-canary-required", score=9, approved="batch-ok")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE fleet_config SET linkedin_apply_mode='canary', "
                "linkedin_canary_enabled=FALSE, linkedin_canary_remaining=NULL WHERE id=1"
            )
        conn.commit()

        assert queue.lease_linkedin(conn, "w1", public_ip="1.1.1.1", owner_ip="1.1.1.1") is None

        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET linkedin_apply_mode='canary', linkedin_canary_enabled=TRUE, linkedin_canary_remaining=0 WHERE id=1")
        conn.commit()
        assert queue.lease_linkedin(conn, "w1", public_ip="1.1.1.1", owner_ip="1.1.1.1") is None

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE linkedin_queue SET status='queued', lease_owner=NULL, lease_expires_at=NULL WHERE url='li-canary-required'"
            )
            cur.execute("UPDATE fleet_config SET linkedin_apply_mode='canary', linkedin_canary_enabled=TRUE, linkedin_canary_remaining=1 WHERE id=1")
        conn.commit()
        assert queue.lease_linkedin(conn, "w2", public_ip="1.1.1.1", owner_ip="1.1.1.1") is not None


def test_ats_canary_exhaustion_does_not_globally_pause_linkedin(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply(conn, "ats-one", host="greenhouse.io", score=9, approved="ats-batch")
        governor.ensure_scope(conn, governor.LINKEDIN_ACCOUNT, daily_cap=20, min_gap_seconds=0)
        _seed_linkedin(conn, "li-after-ats", score=9, approved="li-batch")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE fleet_config SET paused=FALSE, ats_paused=FALSE, "
                "ats_apply_mode='canary', canary_enabled=TRUE, canary_remaining=1, "
                "linkedin_apply_mode='canary', linkedin_canary_enabled=TRUE, linkedin_canary_remaining=1 "
                "WHERE id=1"
            )
        conn.commit()

        assert queue.lease_apply(conn, "ats-worker", home_ip="1.1.1.1") is not None
        with conn.cursor() as cur:
            cur.execute("SELECT paused, ats_apply_mode, canary_remaining FROM fleet_config WHERE id=1")
            cfg = cur.fetchone()

        assert cfg["paused"] is False
        assert cfg["ats_apply_mode"] == "stopped"
        assert cfg["canary_remaining"] == 0
        assert queue.lease_linkedin(
            conn, "li-worker", public_ip="1.1.1.1", owner_ip="1.1.1.1", min_gap_seconds=0
        ) is not None


def test_linkedin_canary_exhaustion_does_not_block_ats(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply(conn, "ats-after-li", host="greenhouse.io", score=9, approved="ats-batch")
        governor.ensure_scope(conn, governor.LINKEDIN_ACCOUNT, daily_cap=20, min_gap_seconds=0)
        _seed_linkedin(conn, "li-one", score=9, approved="li-batch")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE fleet_config SET paused=FALSE, ats_paused=FALSE, "
                "ats_apply_mode='canary', canary_enabled=TRUE, canary_remaining=1, "
                "linkedin_apply_mode='canary', linkedin_canary_enabled=TRUE, linkedin_canary_remaining=1 "
                "WHERE id=1"
            )
        conn.commit()

        assert queue.lease_linkedin(
            conn, "li-worker", public_ip="1.1.1.1", owner_ip="1.1.1.1", min_gap_seconds=0
        ) is not None
        with conn.cursor() as cur:
            cur.execute(
                "SELECT paused, linkedin_apply_mode, linkedin_canary_remaining FROM fleet_config WHERE id=1"
            )
            cfg = cur.fetchone()

        assert cfg["paused"] is False
        assert cfg["linkedin_apply_mode"] == "stopped"
        assert cfg["linkedin_canary_remaining"] == 0
        assert queue.lease_apply(conn, "ats-worker", home_ip="1.1.1.1") is not None


def test_lease_apply_skips_company_blocklist_match(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply(conn, "openai-direct", host="greenhouse.io", score=10,
                    company="OpenAI", title="Strategy")
        _seed_apply(conn, "openai-url", host="ashbyhq.com", score=9,
                    company="HiringCafe", title="Ops")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE apply_queue SET application_url='https://jobs.ashbyhq.com/openai/1' "
                "WHERE url='openai-url'"
            )
        conn.commit()
        _seed_apply(conn, "acme-ok", host="lever.co", score=8,
                    company="Acme", title="Chief of Staff")

        leased = queue.lease_apply(conn, "w1", home_ip="1.1.1.1")

        assert leased is not None
        assert leased["url"] == "acme-ok"


def test_lease_linkedin_skips_company_blocklist_match(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        governor.ensure_scope(conn, governor.LINKEDIN_ACCOUNT, daily_cap=20, min_gap_seconds=1)
        _seed_linkedin(conn, "li-openai", score=10, company="OpenAI")
        _seed_linkedin(conn, "li-acme", score=9, company="Acme")

        leased = queue.lease_linkedin(conn, "w1", public_ip="1.1.1.1", owner_ip="1.1.1.1")

        assert leased is not None
        assert leased["url"] == "li-acme"


# ---- cross-board dedup (R9) ----------------------------------------------------

def test_lease_dedup_guard_blocks_reapply(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        # same dedup_key on two boards
        _seed_apply(conn, "u-gh", host="greenhouse.io", company="Acme", title="Chief of Staff")
        _seed_apply(conn, "u-lev", host="lever.co", company="Acme, Inc", title="CoS")
        a = queue.lease_apply(conn, "w1", home_ip="1.1.1.1")
        assert a is not None
        queue.write_apply_result(conn, "w1", a["url"], status="applied",
                                 target_host=a["target_host"], home_ip="1.1.1.1")
        # the OTHER board's posting (same company+role) must not be leasable now
        b = queue.lease_apply(conn, "w2", home_ip="2.2.2.2")
        assert b is None, "cross-board dedup should block the duplicate posting"


# ---- per-host min-gap ----------------------------------------------------------

def test_per_host_gap_blocks_then_allows(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply(conn, "h1", host="greenhouse.io", score=9, title="Role A")
        _seed_apply(conn, "h2", host="greenhouse.io", score=8, title="Role B")
        a = queue.lease_apply(conn, "w1", home_ip="1.1.1.1")
        queue.write_apply_result(conn, "w1", a["url"], status="applied",
                                 target_host="greenhouse.io", home_ip="1.1.1.1")
        # same host just hit -> gap blocks the second
        assert queue.lease_apply(conn, "w1", home_ip="1.1.1.1") is None
        # wind the host's last_applied_at back past the gap
        with conn.cursor() as cur:
            cur.execute("UPDATE rate_governor SET last_applied_at = now() - interval '300 seconds' "
                        "WHERE scope_key='host:greenhouse.io'")
        conn.commit()
        assert queue.lease_apply(conn, "w1", home_ip="1.1.1.1") is not None


# ---- governor: cap is coupled to a confirmed apply -----------------------------

def test_record_outcome_bump_cap_requires_success(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        sk = governor.host_scope("g.io")
        governor.ensure_scope(conn, sk)
        # bumping the cap/min-gap on a captcha or block is a bug -> rejected
        with pytest.raises(ValueError):
            governor.record_outcome(conn, [sk], "captcha", bump_cap=True)
        with pytest.raises(ValueError):
            governor.record_outcome(conn, [sk], "block", bump_cap=True)
        # success + bump_cap is the only valid combination
        governor.record_outcome(conn, [sk], "success", bump_cap=True)
        with conn.cursor() as cur:
            cur.execute("SELECT count_24h FROM rate_governor WHERE scope_key=%s", (sk,))
            assert cur.fetchone()["count_24h"] == 1


# ---- adaptive circuit-breaker (R6) ---------------------------------------------

def test_breaker_pauses_high_challenge_host(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        sk = governor.host_scope("walls.io")
        governor.ensure_scope(conn, sk, min_gap_seconds=1)
        # 2 success, 8 captcha -> challenge_rate 0.8 >> 0.4*1.5 -> paused
        governor.record_outcome(conn, [sk], "success")
        governor.record_outcome(conn, [sk], "success")
        for _ in range(8):
            governor.record_outcome(conn, [sk], "captcha")
        changed = dict(governor.evaluate_breakers(conn))
        assert changed.get(sk) == "paused"
        # a job on that host is not leasable while paused
        _seed_apply(conn, "p1", host="walls.io")
        assert queue.lease_apply(conn, "w1", home_ip="1.1.1.1") is None


def test_lease_ignores_expired_pause_but_keeps_demotion_sticky(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        sk = governor.host_scope("cooldown.io")
        governor.ensure_scope(conn, sk, min_gap_seconds=1)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE rate_governor SET breaker_state='paused', "
                "breaker_until=now() - interval '1 minute' WHERE scope_key=%s",
                (sk,),
            )
        conn.commit()
        _seed_apply(conn, "expired-pause", host="cooldown.io")

        leased = queue.lease_apply(conn, "w1", home_ip="1.1.1.1")
        assert leased is not None
        assert leased["url"] == "expired-pause"

        sk = governor.host_scope("demoted.io")
        governor.ensure_scope(conn, sk, min_gap_seconds=1)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE rate_governor SET breaker_state='demoted', "
                "breaker_until=now() - interval '1 minute' WHERE scope_key=%s",
                (sk,),
            )
        conn.commit()
        _seed_apply(conn, "expired-demotion", host="demoted.io")

        assert queue.lease_apply(conn, "w2", home_ip="1.1.1.1") is None


def test_breaker_pauses_at_exact_threshold_boundary(fleet_db):
    # Regression: a rate landing EXACTLY on the pause cut (1.5x captcha_threshold)
    # must pause, not throttle. 4 success + 6 captcha -> challenge_rate 6/10 = 0.6,
    # which the default captcha_threshold (0.4) makes the boundary: 0.4*1.5 evaluates
    # to 0.6000000000000001 in float64, so a naive `rate >= captcha_threshold*1.5`
    # mis-classified the exact-0.6 scope as 'throttled' and kept applying on a host
    # that should have been paused.
    with pgqueue.connect(fleet_db) as conn:
        sk = governor.host_scope("edge.io")
        governor.ensure_scope(conn, sk, min_gap_seconds=1)
        for _ in range(4):
            governor.record_outcome(conn, [sk], "success")
        for _ in range(6):
            governor.record_outcome(conn, [sk], "captcha")  # rate == 6/10 == 0.6, the boundary
        changed = dict(governor.evaluate_breakers(conn))
        assert changed.get(sk) == "paused"
        with conn.cursor() as cur:
            cur.execute("SELECT breaker_state FROM rate_governor WHERE scope_key=%s", (sk,))
            assert cur.fetchone()["breaker_state"] == "paused"


def test_breaker_demotes_on_hard_blocks(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        sk = governor.home_ip_scope("9.9.9.9")
        for _ in range(3):
            governor.record_outcome(conn, [sk], "block")
        changed = dict(governor.evaluate_breakers(conn))
        assert changed.get(sk) == "demoted"


def test_breaker_recovers_after_cooldown(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        sk = governor.host_scope("flappy.io")
        governor.ensure_scope(conn, sk)
        for _ in range(8):
            governor.record_outcome(conn, [sk], "captcha")
        governor.evaluate_breakers(conn, cool_seconds=0)  # breaker_until = now
        time.sleep(0.05)
        cleared = governor.clear_expired_breakers(conn)
        assert sk in cleared
        with pgqueue.connect(fleet_db) as c2, c2.cursor() as cur:
            cur.execute("SELECT breaker_state FROM rate_governor WHERE scope_key=%s", (sk,))
            assert cur.fetchone()["breaker_state"] == "ok"


# ---- write_apply_result bookkeeping --------------------------------------------

def test_write_result_records_outcome_and_applied_set(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply(conn, "r1", host="greenhouse.io", company="Beta", title="Data Scientist")
        a = queue.lease_apply(conn, "w1", home_ip="1.2.3.4")
        ok = queue.write_apply_result(conn, "w1", a["url"], status="applied",
                                      target_host="greenhouse.io", home_ip="1.2.3.4", est_cost_usd=0.5)
        assert ok
        with conn.cursor() as cur:
            cur.execute("SELECT success_24h, count_24h FROM rate_governor WHERE scope_key='global'")
            g = cur.fetchone()
            assert g["success_24h"] == 1 and g["count_24h"] == 1
            cur.execute("SELECT count(*) AS n FROM applied_set")
            assert cur.fetchone()["n"] == 1
        # a stale worker can't double-close
        assert queue.write_apply_result(conn, "intruder", a["url"], status="applied",
                                        target_host="greenhouse.io", home_ip="1.2.3.4") is False


def test_write_apply_result_resolves_challenge_only_after_owned_close(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply(conn, "challenged-apply", host="greenhouse.io")
        leased = queue.lease_apply(conn, "owner", home_ip="1.2.3.4")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO auth_challenge (url, worker_id, kind, route) VALUES (%s,%s,%s,%s)",
                (leased["url"], "owner", "login_gate", "owner_inbox"),
            )
        conn.commit()

        assert queue.write_apply_result(
            conn, "intruder", leased["url"], status="failed",
            target_host="greenhouse.io", home_ip="1.2.3.4",
        ) is False
        with conn.cursor() as cur:
            cur.execute("SELECT resolved_at FROM auth_challenge WHERE url=%s", (leased["url"],))
            assert cur.fetchone()["resolved_at"] is None

        assert queue.write_apply_result(
            conn, "owner", leased["url"], status="failed",
            target_host="greenhouse.io", home_ip="1.2.3.4",
        ) is True
        with conn.cursor() as cur:
            cur.execute("SELECT outcome, resolved_at FROM auth_challenge WHERE url=%s", (leased["url"],))
            challenge = cur.fetchone()
            assert challenge["outcome"] == "superseded:failed"
            assert challenge["resolved_at"] is not None


def test_write_apply_result_preserves_challenge_for_parked_linkedin_lane(fleet_db):
    url = "shared-result-challenge"
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply(conn, url, host="greenhouse.io")
        leased = queue.lease_apply(conn, "owner", home_ip="1.2.3.4")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO linkedin_queue "
                "(url, application_url, score, status, lane, lease_owner, lease_expires_at, apply_status) "
                "VALUES (%s,%s,9,'leased','linkedin','linkedin-worker',"
                "now()+interval '3650 days','challenge_pending')",
                (url, url),
            )
            cur.execute(
                "INSERT INTO auth_challenge (url, worker_id, kind, route) "
                "VALUES (%s,'owner','login_gate','owner_inbox')",
                (url,),
            )
        conn.commit()

        assert queue.write_apply_result(
            conn, "owner", leased["url"], status="failed",
            target_host="greenhouse.io", home_ip="1.2.3.4",
        ) is True
        with conn.cursor() as cur:
            cur.execute("SELECT resolved_at FROM auth_challenge WHERE url=%s", (url,))
            assert cur.fetchone()["resolved_at"] is None


# ---- write_apply_result records apply-agent cost to llm_usage (Phase 2.1) ------
# Regression: the apply lane never wrote to llm_usage, so every cost cap read $0
# while real agent money was spent. write_apply_result must insert one llm_usage
# row per close when a real est_cost_usd is supplied, mirroring write_compute_result.

def test_write_apply_result_records_llm_usage_cost(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply(conn, "cost1", host="greenhouse.io", company="Gamma", title="Analyst")
        a = queue.lease_apply(conn, "w1", home_ip="1.2.3.4")
        ok = queue.write_apply_result(conn, "w1", a["url"], status="applied",
                                      target_host="greenhouse.io", home_ip="1.2.3.4", est_cost_usd=0.42,
                                      agent="codex", agent_model="gpt-5")
        assert ok
        with conn.cursor() as cur:
            cur.execute("SELECT worker_id, task, provider, model, machine_owner, cost_usd FROM llm_usage")
            rows = cur.fetchall()
        assert len(rows) == 1, "expected exactly one llm_usage row for the apply-agent cost"
        row = rows[0]
        assert row["worker_id"] == "w1"
        assert row["task"] == "apply_agent"
        assert row["provider"] == "codex"
        assert row["model"] == "gpt-5"
        assert row["machine_owner"] is None
        assert float(row["cost_usd"]) == 0.42


def test_write_apply_result_records_llm_usage_machine_and_model(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply(conn, "cost-machine", host="ashbyhq.com", company="Gamma", title="Analyst")
        a = queue.lease_apply(conn, "w-machine", home_ip="1.2.3.4")
        ok = queue.write_apply_result(
            conn,
            "w-machine",
            a["url"],
            status="applied",
            target_host="ashbyhq.com",
            home_ip="1.2.3.4",
            est_cost_usd=0.55,
            agent="claude",
            agent_model="claude-sonnet-4",
            machine_owner="m2",
        )
        assert ok
        with conn.cursor() as cur:
            cur.execute(
                "SELECT worker_id, task, provider, model, machine_owner, cost_usd "
                "FROM llm_usage WHERE worker_id='w-machine'"
            )
            row = cur.fetchone()
        assert row["provider"] == "claude"
        assert row["model"] == "claude-sonnet-4"
        assert row["machine_owner"] == "m2"
        assert float(row["cost_usd"]) == 0.55


def test_write_apply_result_records_model_and_duration(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply(conn, "evidence1", host="ashbyhq.com", company="Epsilon", title="Staff Engineer")
        a = queue.lease_apply(conn, "w1", home_ip="1.2.3.4")
        ok = queue.write_apply_result(
            conn,
            "w1",
            a["url"],
            status="crash_unconfirmed",
            apply_status="crash_unconfirmed",
            apply_error="failed:no_result_line",
            target_host="ashbyhq.com",
            home_ip="1.2.3.4",
            est_cost_usd=0.03,
            agent="claude",
            agent_model="claude-sonnet-4",
            apply_duration_ms=4567,
        )
        assert ok
        with conn.cursor() as cur:
            cur.execute("SELECT agent_model, apply_duration_ms FROM apply_queue WHERE url='evidence1'")
            row = cur.fetchone()
        assert row["agent_model"] == "claude-sonnet-4"
        assert row["apply_duration_ms"] == 4567


def test_write_apply_result_records_durable_result_event(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply(conn, "event1", host="ashbyhq.com", company="Epsilon", title="Staff Engineer")
        a = queue.lease_apply(conn, "w1", home_ip="1.2.3.4")
        ok = queue.write_apply_result(
            conn,
            "w1",
            a["url"],
            status="crash_unconfirmed",
            apply_status="crash_unconfirmed",
            apply_error="failed:no_result_line",
            target_host="ashbyhq.com",
            home_ip="1.2.3.4",
            est_cost_usd=0.03,
            agent="claude",
            agent_model="claude-sonnet-4",
            apply_duration_ms=4567,
        )
        assert ok
        with conn.cursor() as cur:
            cur.execute(
                "SELECT url, worker_id, status, apply_error, target_host, home_ip, "
                "agent, agent_model, apply_duration_ms, result_line "
                "FROM apply_result_events WHERE url='event1' AND source='worker' "
                "ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
        assert row["worker_id"] == "w1"
        assert row["status"] == "crash_unconfirmed"
        assert row["apply_error"] == "failed:no_result_line"
        assert row["target_host"] == "ashbyhq.com"
        assert row["home_ip"] == "1.2.3.4"
        assert row["agent"] == "claude"
        assert row["agent_model"] == "claude-sonnet-4"
        assert row["apply_duration_ms"] == 4567
        assert row["result_line"] == "RESULT:failed:no_result_line"


def test_write_apply_result_records_submit_risk_evidence(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply(conn, "event-risk", host="ashbyhq.com", company="Epsilon", title="Staff Engineer")
        a = queue.lease_apply(conn, "w1", home_ip="1.2.3.4")
        ok = queue.write_apply_result(
            conn,
            "w1",
            a["url"],
            status="crash_unconfirmed",
            apply_status="crash_unconfirmed",
            apply_error="failed:no_result_line",
            target_host="ashbyhq.com",
            home_ip="1.2.3.4",
            est_cost_usd=0.03,
            agent="claude",
            agent_model="claude-sonnet-4",
            apply_duration_ms=4567,
            application_tool_calls=0,
            job_log_path="C:/logs/job.txt",
            transcript_digest="sha256:abc123",
            final_result_source="transcript",
        )
        assert ok
        with conn.cursor() as cur:
            cur.execute(
                "SELECT application_tool_calls, job_log_path, transcript_digest, final_result_source "
                "FROM apply_result_events WHERE url='event-risk' AND source='worker'"
            )
            row = cur.fetchone()
        assert row["application_tool_calls"] == 0
        assert row["job_log_path"] == "C:/logs/job.txt"
        assert row["transcript_digest"] == "sha256:abc123"
        assert row["final_result_source"] == "transcript"


def test_write_apply_result_no_llm_usage_row_when_cost_zero_or_none(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply(conn, "cost2", host="greenhouse.io", company="Delta", title="Engineer")
        a = queue.lease_apply(conn, "w1", home_ip="1.2.3.4")
        # default est_cost_usd=0 -> no llm_usage row
        ok = queue.write_apply_result(conn, "w1", a["url"], status="applied",
                                      target_host="greenhouse.io", home_ip="1.2.3.4")
        assert ok
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM llm_usage")
            assert cur.fetchone()["n"] == 0

        # distinct host: same-host min-gap would otherwise block the second lease
        _seed_apply(conn, "cost3", host="lever.co", company="Epsilon", title="Manager")
        b = queue.lease_apply(conn, "w1", home_ip="1.2.3.4")
        # explicit None -> still no row (not a cost to record)
        ok = queue.write_apply_result(conn, "w1", b["url"], status="applied",
                                      target_host="greenhouse.io", home_ip="1.2.3.4", est_cost_usd=None)
        assert ok
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM llm_usage")
            assert cur.fetchone()["n"] == 0


# ---- compute cost cap (R14) ----------------------------------------------------

def test_compute_lease_and_cost_cap(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        queue.push_compute_jobs(conn, [{"url": "c1", "task": "score", "payload": {"x": 1}}])
        c = queue.lease_compute(conn, "w1")
        assert c and c["task"] == "score"
        queue.write_compute_result(conn, "w1", "c1", result={"fit": 7}, cost_usd=0.10, model="deepseek")
        # set a tiny daily cap below spend -> next compute lease refused
        fcfg.set_cost_caps(conn, daily_usd=0.05)
        queue.push_compute_jobs(conn, [{"url": "c2", "task": "audit"}])
        assert queue.lease_compute(conn, "w1") is None


def test_compute_queue_tracks_distinct_tasks_for_same_url(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        pushed = queue.push_compute_jobs(conn, [
            {"url": "same-url", "task": "score", "payload": {"x": "score"}},
            {"url": "same-url", "task": "audit", "payload": {"x": "audit"}},
        ])
        assert pushed == 2

        first = queue.lease_compute(conn, "w1")
        assert first and first["url"] == "same-url"
        assert first["task"] in {"score", "audit"}

        assert queue.write_compute_result(
            conn, "w1", "same-url",
            result={"task": first["task"], "research_fit_score": 8},
            cost_usd=0.01, task=first["task"],
        )

        second = queue.lease_compute(conn, "w2")
        assert second and second["url"] == "same-url"
        assert {first["task"], second["task"]} == {"score", "audit"}

        with conn.cursor() as cur:
            cur.execute(
                "SELECT task, status, lease_owner, result FROM compute_queue "
                "WHERE url='same-url' ORDER BY task"
            )
            rows = cur.fetchall()

    by_task = {r["task"]: r for r in rows}
    assert by_task[first["task"]]["status"] == "done"
    assert by_task[first["task"]]["result"]["research_fit_score"] == 8
    assert by_task[second["task"]]["status"] == "leased"


def test_repush_compute_job_preserves_existing_queue_position(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        queue.push_compute_jobs(conn, [{"url": "c-priority", "task": "score", "payload": {"v": 1}}])
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE compute_queue SET updated_at = now() - interval '3 hours' "
                "WHERE url='c-priority' AND task='score'"
            )
            cur.execute("SELECT updated_at FROM compute_queue WHERE url='c-priority' AND task='score'")
            original_updated_at = cur.fetchone()["updated_at"]
        conn.commit()

        queue.push_compute_jobs(conn, [{"url": "c-priority", "task": "score", "payload": {"v": 2}}])

        with conn.cursor() as cur:
            cur.execute("SELECT payload, updated_at FROM compute_queue WHERE url='c-priority' AND task='score'")
            row = cur.fetchone()

    assert row["payload"] == {"v": 2}
    assert row["updated_at"] == original_updated_at


# ---- recurring search scheduler (RF3) ------------------------------------------

def test_search_transaction_retries_deadlock(monkeypatch):
    class Connection:
        rollbacks = 0

        def rollback(self):
            self.rollbacks += 1

    conn = Connection()
    calls = 0

    def operation():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise DeadlockDetected("transient search deadlock")
        return "completed"

    monkeypatch.setattr(queue.time, "sleep", lambda _seconds: None)

    assert queue._run_search_transaction_with_retry(conn, operation) == "completed"
    assert calls == 3
    assert conn.rollbacks == 2


def test_search_transaction_reraises_exhausted_deadlock(monkeypatch):
    class Connection:
        rollbacks = 0

        def rollback(self):
            self.rollbacks += 1

    conn = Connection()
    monkeypatch.setattr(queue.time, "sleep", lambda _seconds: None)

    with pytest.raises(DeadlockDetected):
        queue._run_search_transaction_with_retry(
            conn,
            lambda: (_ for _ in ()).throw(DeadlockDetected("persistent search deadlock")),
        )

    assert conn.rollbacks == queue._SEARCH_DEADLOCK_RETRIES

def test_search_recurs_after_completion(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO search_tasks (task_id, query, board, location, cadence_seconds) "
                        "VALUES ('t1','chief of staff','greenhouse','remote', 3600)")
        conn.commit()
        s = queue.lease_search(conn, "w1")
        assert s and s["task_id"] == "t1"
        # nothing else due
        assert queue.lease_search(conn, "w2") is None
        queue.complete_search(conn, "w1", "t1", result_count=12, board="greenhouse")
        # rescheduled into the future -> not immediately re-leasable
        assert queue.lease_search(conn, "w1") is None
        with conn.cursor() as cur:
            cur.execute("SELECT status, result_count, next_due_at > now() AS future FROM search_tasks WHERE task_id='t1'")
            r = cur.fetchone()
            assert r["status"] == "queued" and r["result_count"] == 12 and r["future"] is True


def test_search_transaction_retries_deadlock_after_rollback(monkeypatch):
    class FakeConnection:
        rollbacks = 0

        def rollback(self):
            self.rollbacks += 1

    conn = FakeConnection()
    attempts = 0

    def operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise queue.DeadlockDetected("transient deadlock")
        return "ok"

    monkeypatch.setattr(queue.time, "sleep", lambda _seconds: None)

    assert queue._run_search_transaction_with_retry(conn, operation) == "ok"
    assert attempts == 3
    assert conn.rollbacks == 2


# ---- LinkedIn mutex (R1) -------------------------------------------------------

def test_linkedin_owner_ip_and_mutex(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        governor.ensure_scope(conn, governor.LINKEDIN_ACCOUNT, daily_cap=20, min_gap_seconds=300)
        for i in range(2):
            _seed_linkedin(conn, f"li{i}", score=9 - i)
        # wrong IP -> refused
        assert queue.lease_linkedin(conn, "w1", public_ip="3.3.3.3", owner_ip="1.1.1.1") is None
        # owner IP -> leases one
        a = queue.lease_linkedin(conn, "w1", public_ip="1.1.1.1", owner_ip="1.1.1.1")
        assert a is not None
        # record the apply -> stamps the account mutex -> second machine blocked by gap
        governor.record_outcome(conn, [governor.LINKEDIN_ACCOUNT], "success", bump_cap=True)
        b = queue.lease_linkedin(conn, "w2", public_ip="1.1.1.1", owner_ip="1.1.1.1")
        assert b is None, "account:linkedin mutex must serialize -> never two concurrent sessions"


# ---- lease atomicity under concurrency (the safety property) -------------------

def test_apply_lease_atomic_under_concurrency(fleet_db):
    n_jobs, n_workers = 24, 6
    with pgqueue.connect(fleet_db) as conn:
        for i in range(n_jobs):
            _seed_apply(conn, f"j{i}", host=f"host{i}.io", score=float(i + 7))  # distinct hosts -> gap never blocks

    grabbed: list[str] = []
    lock = threading.Lock()

    def worker(wid):
        with pgqueue.connect(fleet_db) as c:
            while True:
                row = queue.lease_apply(c, wid, home_ip="1.1.1.1")
                if row is None:
                    return
                with lock:
                    grabbed.append(row["url"])

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(grabbed) == n_jobs
    assert len(set(grabbed)) == n_jobs, "a job was double-grabbed"


# ---- LinkedIn mutex SERIALIZES at claim time (R1 catastrophe) ------------------

def test_linkedin_lease_serializes_under_concurrency(fleet_db):
    # The real invariant the old test missed: two owner-IP machines, with NO
    # record_outcome between them, must NOT both get a live LinkedIn session.
    with pgqueue.connect(fleet_db) as conn:
        governor.ensure_scope(conn, governor.LINKEDIN_ACCOUNT, daily_cap=20, min_gap_seconds=300)
        for i in range(4):
            _seed_linkedin(conn, f"li{i}", score=9 - i)

    leased: list[str] = []
    lock = threading.Lock()

    def grab(wid):
        with pgqueue.connect(fleet_db) as c:
            row = queue.lease_linkedin(c, wid, public_ip="1.1.1.1", owner_ip="1.1.1.1")
            if row:
                with lock:
                    leased.append(row["url"])

    threads = [threading.Thread(target=grab, args=(f"w{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(leased) == 1, f"account:linkedin must serialize to ONE concurrent session, got {leased}"


def test_write_linkedin_result_closes_linkedin_queue(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        governor.ensure_scope(conn, governor.LINKEDIN_ACCOUNT, daily_cap=20, min_gap_seconds=1)
        _seed_linkedin(conn, "li-x", score=9)
        a = queue.lease_linkedin(conn, "w1", public_ip="1.1.1.1", owner_ip="1.1.1.1")
        assert a and a["url"] == "li-x"
        # cap reserved at lease time -> count_24h already 1
        with conn.cursor() as cur:
            cur.execute("SELECT count_24h FROM rate_governor WHERE scope_key='account:linkedin'")
            assert cur.fetchone()["count_24h"] == 1
        assert queue.write_linkedin_result(
            conn,
            "w1",
            "li-x",
            status="applied",
            apply_status="applied",
            est_cost_usd=0.25,
            target_host="linkedin.com",
            home_ip="1.1.1.1",
            agent="codex",
            agent_model="codex-default",
            apply_duration_ms=4321,
            machine_owner="home",
            application_tool_calls=3,
            job_log_path="job.log",
            transcript_digest="digest",
            final_result_source="result_line",
            result_metadata={"apply_channel": "easy_apply"},
        ) is True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, agent_model, apply_duration_ms FROM linkedin_queue WHERE url='li-x'"
            )
            queue_row = cur.fetchone()
            assert queue_row == {
                "status": "applied",
                "agent_model": "codex-default",
                "apply_duration_ms": 4321,
            }
            cur.execute(
                "SELECT queue_name, agent, agent_model, application_tool_calls, result_metadata "
                "FROM apply_result_events WHERE url='li-x'"
            )
            event = cur.fetchone()
            assert event["queue_name"] == "linkedin_queue"
            assert event["agent"] == "codex"
            assert event["agent_model"] == "codex-default"
            assert event["application_tool_calls"] == 3
            assert event["result_metadata"] == {"apply_channel": "easy_apply"}
            cur.execute(
                "SELECT worker_id, machine_owner, model, provider, cost_usd "
                "FROM llm_usage WHERE worker_id='w1'"
            )
            usage = cur.fetchone()
            assert usage["machine_owner"] == "home"
            assert usage["model"] == "codex-default"
            assert usage["provider"] == "codex"
            assert float(usage["cost_usd"]) == 0.25
            cur.execute("SELECT count_24h, success_24h FROM rate_governor WHERE scope_key='account:linkedin'")
            g = cur.fetchone()
            assert g["count_24h"] == 1 and g["success_24h"] == 1  # NOT double-bumped
        # a stale/other worker cannot close it
        assert queue.write_linkedin_result(conn, "intruder", "li-x", status="applied") is False


def test_write_linkedin_result_resolves_challenge_after_owned_close(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        governor.ensure_scope(conn, governor.LINKEDIN_ACCOUNT, daily_cap=20, min_gap_seconds=1)
        _seed_linkedin(conn, "li-challenge")
        leased = queue.lease_linkedin(conn, "owner", public_ip="1.1.1.1", owner_ip="1.1.1.1")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO auth_challenge (url, worker_id, kind, route) VALUES (%s,%s,%s,%s)",
                (leased["url"], "owner", "visible_captcha", "owner_inbox"),
            )
        conn.commit()

        assert queue.write_linkedin_result(conn, "owner", leased["url"], status="blocked") is True
        with conn.cursor() as cur:
            cur.execute("SELECT outcome, resolved_at FROM auth_challenge WHERE url=%s", (leased["url"],))
            challenge = cur.fetchone()
            assert challenge["outcome"] == "superseded:blocked"
            assert challenge["resolved_at"] is not None


# ---- numeric cap actually BLOCKS a lease (not just bookkeeping) -----------------

def test_apply_host_cap_blocks_then_allows(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        governor.ensure_scope(conn, governor.host_scope("cap.io"), daily_cap=1, min_gap_seconds=1)
        _seed_apply(conn, "k1", host="cap.io", score=9, title="A")
        _seed_apply(conn, "k2", host="cap.io", score=8, title="B")
        a = queue.lease_apply(conn, "w1", home_ip="1.1.1.1")
        assert a is not None
        queue.write_apply_result(conn, "w1", a["url"], status="applied", target_host="cap.io", home_ip="1.1.1.1")
        # cap=1 reached. Wind last_applied back so the GAP isn't the blocker -> isolate the CAP.
        with conn.cursor() as cur:
            cur.execute("UPDATE rate_governor SET last_applied_at = now() - interval '300 seconds' WHERE scope_key='host:cap.io'")
        conn.commit()
        assert queue.lease_apply(conn, "w1", home_ip="1.1.1.1") is None  # cap blocks
        with conn.cursor() as cur:
            cur.execute("UPDATE rate_governor SET daily_cap=5 WHERE scope_key='host:cap.io'")
        conn.commit()
        assert queue.lease_apply(conn, "w1", home_ip="1.1.1.1") is not None  # raised cap -> leases


# ---- 'throttled' is a REAL state: leases (paced), unlike paused -----------------

def test_throttled_host_still_leases_and_gap_does_not_compound(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        sk = governor.host_scope("thr.io")
        governor.ensure_scope(conn, sk, min_gap_seconds=10)
        for _ in range(5):
            governor.record_outcome(conn, [sk], "success")
        for _ in range(5):
            governor.record_outcome(conn, [sk], "captcha")  # rate 0.5 in [0.4,0.6) -> throttled
        assert dict(governor.evaluate_breakers(conn)).get(sk) == "throttled"
        with conn.cursor() as cur:
            cur.execute("SELECT min_gap_seconds, base_min_gap_seconds FROM rate_governor WHERE scope_key=%s", (sk,))
            r = cur.fetchone()
            assert r["base_min_gap_seconds"] == 10 and r["min_gap_seconds"] == 30  # base captured, 3x gap
        # throttled host is STILL leasable (unlike paused). A3: record_outcome now stamps
        # last_attempt_at, so wind it back past the 30s throttled gap to open the lease window
        # (the throttle widens the GAP, it does not block leasing outright like a pause).
        _seed_apply(conn, "t1", host="thr.io", score=9)
        with conn.cursor() as cur:
            cur.execute("UPDATE rate_governor SET last_attempt_at = now() - interval '1 hour' "
                        "WHERE scope_key=%s", (sk,))
        conn.commit()
        assert queue.lease_apply(conn, "w1", home_ip="1.1.1.1") is not None
        # recover -> gap restored to the pristine base (no 3x->9x compounding)
        governor.roll_window(conn)
        governor.evaluate_breakers(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT min_gap_seconds, breaker_state FROM rate_governor WHERE scope_key=%s", (sk,))
            r = cur.fetchone()
            assert r["breaker_state"] == "ok" and r["min_gap_seconds"] == 10


# ---- a parked auth wall is FROZEN out of the reclaim pool (R3 fail-safe) --------

def test_parked_challenge_frozen_against_reclaim(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        # control: a normal leased row whose ttl elapsed IS swept by reclaim. Per the anti-
        # double-submit fix an expired lease is parked crash_unconfirmed (never re-queued, since
        # it may have crashed mid-submit); the contrast that matters here is swept-vs-frozen, and
        # the parked wall below stays untouched.
        _seed_apply(conn, "ctrl", host="c.io", score=8)
        queue.lease_apply(conn, "w1", home_ip="1.1.1.1")
        with conn.cursor() as cur:
            cur.execute("UPDATE apply_queue SET lease_expires_at = now() - interval '1 hour' WHERE url='ctrl'")
        conn.commit()
        pgqueue.reclaim_stale_leases(conn, grace_seconds=0)
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM apply_queue WHERE url='ctrl'")
            assert cur.fetchone()["status"] == "crash_unconfirmed"

        # parked wall: park freezes it so the SAME elapsed ttl does NOT reclaim it
        _seed_apply(conn, "wall1", host="walled.io", score=9)
        a = queue.lease_apply(conn, "w1", home_ip="1.1.1.1")
        assert a["url"] == "wall1"
        assert queue.park_challenge(conn, "w1", "wall1") is True
        pgqueue.reclaim_stale_leases(conn, grace_seconds=0)
        with conn.cursor() as cur:
            cur.execute("SELECT status, apply_status FROM apply_queue WHERE url='wall1'")
            r = cur.fetchone()
        assert r["status"] == "leased" and r["apply_status"] == "challenge_pending", \
            "a parked wall must NOT be reclaimed and re-driven blind"
        # owner resolves -> back to the pool for a retry from the trusted box
        assert queue.resolve_challenge(conn, "wall1", requeue=True) is True
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM apply_queue WHERE url='wall1'")
            assert cur.fetchone()["status"] == "queued"
