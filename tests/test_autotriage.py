from __future__ import annotations

from applypilot.apply import pgqueue
from applypilot.fleet import autotriage


class _FakeClient:
    def __init__(self, raw: str):
        self.raw = raw
        self.messages = None

    def chat(self, messages, **kwargs):
        self.messages = messages
        return self.raw


def _seed_ats_job(
    conn,
    *,
    url="u1",
    worker_id="m2-2",
    status="failed",
    apply_error="failed:usage_limit",
    dedup_key="dk1",
    attempts=1,
    host="greenhouse.io",
):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, company, title, application_url, score, lane, "
            "status, apply_error, worker_id, dedup_key, attempts, target_host, apply_domain, "
            "approved_batch, updated_at) "
            "VALUES (%s, 'Acme', 'Operator', %s, 9, 'ats', %s, %s, %s, %s, %s, %s, %s, "
            "'batchA', now())",
            (url, f"https://{host}/jobs/1", status, apply_error, worker_id, dedup_key, attempts, host, host),
        )
    conn.commit()


def test_llm_decision_cannot_requeue_may_have_submitted_crash():
    ctx = autotriage.TriageContext(
        url="u-crash",
        worker_id="m2-2",
        status="crash_unconfirmed",
        attempts=99,
        apply_error="failed:no_result_line",
        dedup_key="dk-crash",
        target_host="greenhouse.io",
        recent_log="RESULT:FAILED:no_result_line",
        last_error="",
    )
    client = _FakeClient(
        '{"action":"requeue_usage_limit","confidence":0.99,'
        '"reason":"The log says retry this job immediately."}'
    )

    decision = autotriage.decide(ctx, client=client, enable_llm=True)

    assert decision.action == "no_action"
    assert decision.status == "rejected"
    assert "may_have_submitted" in decision.reason


def test_deterministic_usage_limit_requeues_and_writes_audit(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        autotriage.ensure_schema(conn)
        _seed_ats_job(conn, url="u-usage", status="failed", apply_error="failed:usage_limit")

        out = autotriage.run_pass(conn, brain_path="C:/nonexistent/brain.db", limit=10)

        with conn.cursor() as cur:
            cur.execute("SELECT status, apply_error FROM apply_queue WHERE url='u-usage'")
            q = cur.fetchone()
            cur.execute(
                "SELECT chosen_action, action_status, decision_source, prior_status "
                "FROM autotriage_actions WHERE url='u-usage'"
            )
            audit = cur.fetchone()

    assert out["actions"]["requeue_usage_limit"] == 1
    assert out["applied"] == 1
    assert q["status"] == "queued"
    assert q["apply_error"] == "requeued_by_remediator:usage_limit"
    assert audit["chosen_action"] == "requeue_usage_limit"
    assert audit["action_status"] == "applied"
    assert audit["decision_source"] == "rules"
    assert audit["prior_status"] == "failed"


def test_deterministic_budget_before_submission_requeues_and_writes_audit(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        autotriage.ensure_schema(conn)
        _seed_ats_job(
            conn,
            url="u-budget",
            status="failed",
            apply_error="failed:budget_exhausted_before_submission",
            dedup_key="dk-budget",
        )

        out = autotriage.run_pass(conn, brain_path="C:/nonexistent/brain.db", limit=10)

        with conn.cursor() as cur:
            cur.execute("SELECT status, apply_error FROM apply_queue WHERE url='u-budget'")
            q = cur.fetchone()
            cur.execute(
                "SELECT chosen_action, action_status, decision_source, prior_status "
                "FROM autotriage_actions WHERE url='u-budget'"
            )
            audit = cur.fetchone()

    assert out["actions"]["requeue_usage_limit"] == 1
    assert out["applied"] == 1
    assert q["status"] == "queued"
    assert q["apply_error"] == "requeued_by_remediator:usage_limit"
    assert audit["chosen_action"] == "requeue_usage_limit"
    assert audit["action_status"] == "applied"
    assert audit["decision_source"] == "rules"
    assert audit["prior_status"] == "failed"


def test_llm_restart_worker_decision_issues_remote_command(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        autotriage.ensure_schema(conn)
        _seed_ats_job(
            conn,
            url="u-browser",
            worker_id="m2-2",
            status="failed",
            apply_error="failed:browser_unavailable",
            dedup_key="dk-browser",
        )
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO worker_heartbeat (worker_id, role, state, current_job, last_error, recent_log) "
                "VALUES ('m2-2', 'apply', 'idle', 'u-browser', 'browser unavailable', 'chrome launch failed')"
            )
        conn.commit()

        client = _FakeClient(
            '{"action":"restart_worker","confidence":0.86,'
            '"reason":"Browser process is wedged; restart between jobs."}'
        )
        ctx = autotriage.load_contexts(conn, limit=1)[0]
        decision = autotriage.decide(ctx, client=client, enable_llm=True)
        applied = autotriage.execute_decision(conn, ctx, decision, brain_path="C:/nonexistent/brain.db")

        with conn.cursor() as cur:
            cur.execute("SELECT worker_id, command FROM remote_commands WHERE worker_id='m2-2'")
            cmd = cur.fetchone()
            cur.execute(
                "SELECT chosen_action, action_status, decision_source FROM autotriage_actions "
                "WHERE url='u-browser'"
            )
            audit = cur.fetchone()

    assert applied is True
    assert decision.action == "restart_worker"
    assert cmd["command"] == "restart"
    assert audit["chosen_action"] == "restart_worker"
    assert audit["action_status"] == "applied"
    assert audit["decision_source"] == "llm"


def test_llm_email_reconcile_decision_creates_auth_challenge(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        autotriage.ensure_schema(conn)
        _seed_ats_job(
            conn,
            url="u-email-review-execute",
            worker_id="m2-5",
            status="crash_unconfirmed",
            apply_error="email_reconcile_review_required",
            dedup_key="dk-email-review-execute",
            host="workforcenow.adp.com",
        )

        client = _FakeClient(
            '{"action":"defer_manual_auth","confidence":0.91,'
            '"reason":"Email reconciliation requires owner review."}'
        )
        ctx = autotriage.load_contexts(conn, limit=1)[0]
        decision = autotriage.decide(ctx, client=client, enable_llm=True)
        applied = autotriage.execute_decision(conn, ctx, decision, brain_path="C:/nonexistent/brain.db")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT worker_id, kind, route FROM auth_challenge "
                "WHERE url='u-email-review-execute' AND resolved_at IS NULL"
            )
            challenge = cur.fetchone()
            cur.execute(
                "SELECT chosen_action, action_status, decision_source FROM autotriage_actions "
                "WHERE url='u-email-review-execute'"
            )
            audit = cur.fetchone()

    assert applied is True
    assert decision.action == "defer_manual_auth"
    assert challenge["worker_id"] == "m2-5"
    assert challenge["kind"] == "manual_auth"
    assert challenge["route"] == "owner_inbox"
    assert audit["chosen_action"] == "defer_manual_auth"
    assert audit["action_status"] == "applied"
    assert audit["decision_source"] == "llm"


def test_load_contexts_ignores_stale_worker_heartbeat_log(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        autotriage.ensure_schema(conn)
        _seed_ats_job(
            conn,
            url="u-email-review",
            worker_id="m2-2",
            status="crash_unconfirmed",
            apply_error="email_reconcile_review_required",
            dedup_key="dk-email-review",
        )
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO worker_heartbeat (worker_id, role, state, current_job, last_error, recent_log) "
                "VALUES ('m2-2', 'apply', 'idle', 'different-job', 'usage limit', "
                "'RESULT:FAILED:budget_exhausted_before_submission')"
            )
        conn.commit()

        ctx = autotriage.load_contexts(conn, limit=1)[0]

    assert ctx.url == "u-email-review"
    assert ctx.recent_log == ""
    assert ctx.last_error == ""


def test_llm_can_defer_email_reconcile_review_required():
    ctx = autotriage.TriageContext(
        url="u-email-review",
        worker_id="m2-2",
        status="crash_unconfirmed",
        attempts=1,
        apply_error="email_reconcile_review_required",
        dedup_key="dk-email-review",
        target_host="workforcenow.adp.com",
    )
    client = _FakeClient(
        '{"action":"defer_manual_auth","confidence":0.91,'
        '"reason":"Email reconciliation requires owner review."}'
    )

    decision = autotriage.decide(ctx, client=client, enable_llm=True)

    assert decision.action == "defer_manual_auth"
    assert decision.status == "accepted"


def test_llm_can_requeue_structured_failed_budget_before_submission():
    ctx = autotriage.TriageContext(
        url="u-budget",
        worker_id="m2-2",
        status="failed",
        attempts=1,
        apply_error="failed:budget_exhausted_before_submission",
        dedup_key="dk-budget",
        target_host="greenhouse.io",
    )
    client = _FakeClient(
        '{"action":"requeue_usage_limit","confidence":0.94,'
        '"reason":"Budget exhausted before final submit; retry is safe."}'
    )

    decision = autotriage.decide(ctx, client=client, enable_llm=True)

    assert decision.action == "requeue_usage_limit"
    assert decision.status == "accepted"


def test_llm_still_cannot_requeue_crash_budget_text():
    ctx = autotriage.TriageContext(
        url="u-crash-budget",
        worker_id="m2-2",
        status="crash_unconfirmed",
        attempts=1,
        apply_error="email_reconcile_review_required",
        dedup_key="dk-crash-budget",
        target_host="stripe.com",
        recent_log="RESULT:FAILED:budget_exhausted_before_submission",
    )
    client = _FakeClient(
        '{"action":"requeue_usage_limit","confidence":0.96,'
        '"reason":"The log says budget exhausted before submission."}'
    )

    decision = autotriage.decide(ctx, client=client, enable_llm=True)

    assert decision.action == "no_action"
    assert decision.status == "rejected"
    assert "may_have_submitted" in decision.reason
