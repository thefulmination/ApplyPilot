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


def _seed_result_event(
    conn,
    *,
    url,
    worker_id="m2-2",
    status="crash_unconfirmed",
    apply_status="crash_unconfirmed",
    apply_error="failed:no_result_line",
    application_tool_calls=None,
):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_result_events "
            "(queue_name, url, worker_id, status, apply_status, apply_error, result_line, "
            "application_tool_calls) "
            "VALUES ('apply_queue', %s, %s, %s, %s, %s, %s, %s)",
            (
                url,
                worker_id,
                status,
                apply_status,
                apply_error,
                f"RESULT:{apply_error}",
                application_tool_calls,
            ),
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

    assert decision.action == "manual_review_required"
    assert decision.status == "accepted"
    assert "missing durable execution evidence" in decision.reason


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


def test_crash_only_scope_is_recorded_in_audit_evidence(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        autotriage.ensure_schema(conn)
        _seed_ats_job(
            conn,
            url="u-crash-scope",
            status="crash_unconfirmed",
            apply_error="crash_unconfirmed",
            dedup_key="dk-crash-scope",
        )
        out = autotriage.run_pass(
            conn,
            brain_path="C:/nonexistent/brain.db",
            limit=10,
            crash_only=True,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT evidence FROM autotriage_actions WHERE url='u-crash-scope'")
            audit = cur.fetchone()

    assert out["contexts"] == 1
    assert audit["evidence"]["triage_scope"] == "crash_only"


def test_zero_tool_no_result_crash_requeues_and_writes_audit(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        autotriage.ensure_schema(conn)
        _seed_ats_job(
            conn,
            url="u-zero-tool",
            status="crash_unconfirmed",
            apply_error="failed:no_result_line",
            dedup_key="dk-zero-tool",
            attempts=99,
        )
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO applied_set (dedup_key, company, applied_url) "
                "VALUES ('dk-zero-tool', 'Acme', 'u-zero-tool')"
            )
        conn.commit()
        _seed_result_event(conn, url="u-zero-tool", application_tool_calls=0)

        out = autotriage.run_pass(conn, brain_path="C:/nonexistent/brain.db", limit=10)

        with conn.cursor() as cur:
            cur.execute("SELECT status, apply_error FROM apply_queue WHERE url='u-zero-tool'")
            q = cur.fetchone()
            cur.execute("SELECT 1 FROM applied_set WHERE dedup_key='dk-zero-tool'")
            applied_set = cur.fetchone()
            cur.execute(
                "SELECT chosen_action, action_status, decision_source, prior_status "
                "FROM autotriage_actions WHERE url='u-zero-tool'"
            )
            audit = cur.fetchone()

    assert out["actions"]["requeue_pre_touch_crash"] == 1
    assert out["applied"] == 1
    assert q["status"] == "queued"
    assert q["apply_error"] == "requeued_by_autotriage:pre_touch_crash"
    assert applied_set is None
    assert audit["chosen_action"] == "requeue_pre_touch_crash"
    assert audit["action_status"] == "applied"
    assert audit["decision_source"] == "rules"
    assert audit["prior_status"] == "crash_unconfirmed"


def test_repeated_zero_tool_crash_is_manual_review_not_requeued_again(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        autotriage.ensure_schema(conn)
        _seed_ats_job(
            conn,
            url="u-repeat-zero-tool",
            status="crash_unconfirmed",
            apply_error="crash_unconfirmed",
            dedup_key="dk-repeat-zero-tool",
            attempts=99,
        )
        _seed_result_event(
            conn,
            url="u-repeat-zero-tool",
            apply_error="crash_unconfirmed",
            application_tool_calls=0,
        )
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO autotriage_actions "
                "(url, chosen_action, action_status, reason, created_at) VALUES "
                "('u-repeat-zero-tool','requeue_pre_touch_crash','applied','prior retry',"
                "now() - interval '2 days')"
            )
            conn.commit()

        out = autotriage.run_pass(conn, brain_path="C:/nonexistent/brain.db", limit=10)

        with conn.cursor() as cur:
            cur.execute("SELECT status FROM apply_queue WHERE url='u-repeat-zero-tool'")
            q = cur.fetchone()
            cur.execute(
                "SELECT chosen_action, action_status, reason FROM autotriage_actions "
                "WHERE url='u-repeat-zero-tool' ORDER BY created_at DESC LIMIT 1"
            )
            audit = cur.fetchone()

    assert out["actions"]["manual_review_required"] == 1
    assert out["applied"] == 0
    assert q["status"] == "crash_unconfirmed"
    assert audit["chosen_action"] == "manual_review_required"
    assert "already received an automatic requeue" in audit["reason"]


def test_mixed_manual_review_and_action_pass_commits_all_audits(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        autotriage.ensure_schema(conn)
        _seed_ats_job(
            conn,
            url="u-mixed-manual",
            status="crash_unconfirmed",
            apply_error="failed:no_result_line",
            dedup_key="dk-mixed-manual",
            attempts=99,
        )
        _seed_ats_job(
            conn,
            url="u-mixed-restart",
            status="failed",
            apply_error="failed:browser_unavailable",
            dedup_key="dk-mixed-restart",
            attempts=1,
        )
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO worker_heartbeat (worker_id, role, state, current_job, last_error, recent_log) "
                "VALUES ('m2-2','apply','idle','u-mixed-restart','browser unavailable','chrome launch failed')"
            )
        conn.commit()

        out = autotriage.run_pass(conn, brain_path="C:/nonexistent/brain.db", limit=10)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT url, action_status FROM autotriage_actions "
                "WHERE url IN ('u-mixed-manual','u-mixed-restart') ORDER BY url"
            )
            audits = cur.fetchall()

    assert out["actions"]["manual_review_required"] == 1
    assert out["actions"]["restart_worker"] == 1
    assert {row["url"]: row["action_status"] for row in audits} == {
        "u-mixed-manual": "manual_review_required",
        "u-mixed-restart": "applied",
    }


def test_tool_touch_no_result_crash_stays_parked(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        autotriage.ensure_schema(conn)
        _seed_ats_job(
            conn,
            url="u-tool-touch",
            status="crash_unconfirmed",
            apply_error="failed:no_result_line",
            dedup_key="dk-tool-touch",
            attempts=99,
        )
        _seed_result_event(conn, url="u-tool-touch", application_tool_calls=2)

        out = autotriage.run_pass(conn, brain_path="C:/nonexistent/brain.db", limit=10)

        with conn.cursor() as cur:
            cur.execute("SELECT status, apply_error FROM apply_queue WHERE url='u-tool-touch'")
            q = cur.fetchone()
            cur.execute(
                "SELECT chosen_action, action_status, decision_source FROM autotriage_actions "
                "WHERE url='u-tool-touch'"
            )
            audit = cur.fetchone()

    assert out["actions"]["manual_review_required"] == 1
    assert out["applied"] == 0
    assert q["status"] == "crash_unconfirmed"
    assert q["apply_error"] == "failed:no_result_line"
    assert audit["chosen_action"] == "manual_review_required"
    assert audit["action_status"] == "manual_review_required"
    assert audit["decision_source"] == "rules"


def test_old_zero_tool_crash_is_loaded_outside_recent_window(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        autotriage.ensure_schema(conn)
        _seed_ats_job(
            conn,
            url="u-old-zero-tool",
            status="crash_unconfirmed",
            apply_error="failed:no_result_line",
            dedup_key="dk-old-zero-tool",
            attempts=99,
        )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE apply_queue SET updated_at = now() - interval '3 days' "
                "WHERE url='u-old-zero-tool'"
            )
        conn.commit()
        _seed_result_event(conn, url="u-old-zero-tool", application_tool_calls=0)

        contexts = autotriage.load_contexts(conn, limit=10, window_minutes=60)

    assert [ctx.url for ctx in contexts] == ["u-old-zero-tool"]


def test_load_contexts_crash_only_excludes_failed_and_blocked_rows(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        autotriage.ensure_schema(conn)
        _seed_ats_job(conn, url="u-crash-only", status="crash_unconfirmed", apply_error="crash_unconfirmed")
        _seed_ats_job(conn, url="u-failed-only", status="failed", apply_error="failed:browser_unavailable")
        _seed_ats_job(conn, url="u-blocked-only", status="blocked", apply_error="challenge_pending")

        contexts = autotriage.load_contexts(conn, limit=10, window_minutes=60, crash_only=True)

    assert [ctx.url for ctx in contexts] == ["u-crash-only"]


def test_old_crash_without_execution_evidence_is_deferred_for_manual_review(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        autotriage.ensure_schema(conn)
        _seed_ats_job(
            conn,
            url="u-old-missing-evidence",
            status="crash_unconfirmed",
            apply_error="failed:worker_error:process_exit",
            dedup_key="dk-old-missing-evidence",
            attempts=99,
        )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE apply_queue SET updated_at = now() - interval '3 days' "
                "WHERE url='u-old-missing-evidence'"
            )
        conn.commit()

        out = autotriage.run_pass(
            conn,
            brain_path="C:/nonexistent/brain.db",
            limit=10,
            window_minutes=60,
            enable_llm=False,
        )

        with conn.cursor() as cur:
            cur.execute("SELECT status, apply_error FROM apply_queue WHERE url='u-old-missing-evidence'")
            queue_row = cur.fetchone()
            cur.execute(
                "SELECT chosen_action, action_status, decision_source, reason "
                "FROM autotriage_actions WHERE url='u-old-missing-evidence'"
            )
            audit = cur.fetchone()

    assert out["actions"]["manual_review_required"] == 1
    assert out["applied"] == 0
    assert queue_row["status"] == "crash_unconfirmed"
    assert queue_row["apply_error"] == "failed:worker_error:process_exit"
    assert audit["chosen_action"] == "manual_review_required"
    assert audit["action_status"] == "manual_review_required"
    assert audit["decision_source"] == "rules"
    assert "missing durable execution evidence" in audit["reason"]


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


def test_email_reconcile_review_required_is_deterministic_manual_auth(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        autotriage.ensure_schema(conn)
        _seed_ats_job(
            conn,
            url="u-email-review-rules",
            worker_id="m2-5",
            status="crash_unconfirmed",
            apply_error="email_reconcile_review_required",
            dedup_key="dk-email-review-rules",
            host="workforcenow.adp.com",
        )

        out = autotriage.run_pass(
            conn,
            brain_path="C:/nonexistent/brain.db",
            limit=10,
            enable_llm=False,
        )

        with conn.cursor() as cur:
            cur.execute(
                "SELECT worker_id, kind, route FROM auth_challenge "
                "WHERE url='u-email-review-rules' AND resolved_at IS NULL"
            )
            challenge = cur.fetchone()
            cur.execute(
                "SELECT chosen_action, action_status, decision_source FROM autotriage_actions "
                "WHERE url='u-email-review-rules'"
            )
            audit = cur.fetchone()

    assert out["actions"]["defer_manual_auth"] == 1
    assert out["applied"] == 1
    assert challenge["worker_id"] == "m2-5"
    assert challenge["kind"] == "manual_auth"
    assert challenge["route"] == "owner_inbox"
    assert audit["chosen_action"] == "defer_manual_auth"
    assert audit["action_status"] == "applied"
    assert audit["decision_source"] == "rules"


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


def test_load_contexts_skips_recent_already_applied_action(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        autotriage.ensure_schema(conn)
        _seed_ats_job(
            conn,
            url="u-open-challenge",
            worker_id="m2-2",
            status="crash_unconfirmed",
            apply_error="email_reconcile_review_required",
            dedup_key="dk-open-challenge",
        )
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO autotriage_actions "
                "(url, worker_id, chosen_action, decision_source, confidence, reason, "
                " action_status, prior_status, prior_attempts, prior_apply_error) "
                "VALUES ('u-open-challenge', 'm2-2', 'defer_manual_auth', 'llm', 0.9, "
                "'challenge already open', 'already_applied', 'crash_unconfirmed', 1, "
                "'email_reconcile_review_required')"
            )
        conn.commit()

        contexts = autotriage.load_contexts(conn, limit=10)

    assert [ctx.url for ctx in contexts] == []


def test_load_contexts_skips_recent_terminal_audit_action(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        autotriage.ensure_schema(conn)
        _seed_ats_job(
            conn,
            url="u-no-action",
            worker_id="m2-2",
            status="failed",
            apply_error="failed:budget_exhausted_mid_application",
            dedup_key="dk-no-action",
        )
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO autotriage_actions "
                "(url, worker_id, chosen_action, decision_source, confidence, reason, "
                " action_status, prior_status, prior_attempts, prior_apply_error) "
                "VALUES ('u-no-action', 'm2-2', 'no_action', 'llm', 0.9, "
                "'ambiguous budget exhaustion', 'no_action', 'failed', 1, "
                "'failed:budget_exhausted_mid_application')"
            )
        conn.commit()

        contexts = autotriage.load_contexts(conn, limit=10)

    assert [ctx.url for ctx in contexts] == []


def test_load_contexts_revisits_rejected_action_after_short_cooldown(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        autotriage.ensure_schema(conn)
        _seed_ats_job(
            conn,
            url="u-rejected-cooldown",
            worker_id="m2-2",
            status="failed",
            apply_error="failed:browser_tool_unavailable",
            dedup_key="dk-rejected-cooldown",
        )
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO autotriage_actions "
                "(url, worker_id, chosen_action, decision_source, confidence, reason, "
                " action_status, prior_status, prior_attempts, prior_apply_error, created_at) "
                "VALUES ('u-rejected-cooldown', 'm2-2', 'no_action', 'llm', 0.9, "
                "'stale rejection', 'rejected', 'failed', 1, "
                "'failed:browser_tool_unavailable', now() - interval '2 hours')"
            )
        conn.commit()

        contexts = autotriage.load_contexts(conn, limit=10)

    assert [ctx.url for ctx in contexts] == ["u-rejected-cooldown"]


def test_load_contexts_skips_known_terminal_noop_errors(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        autotriage.ensure_schema(conn)
        _seed_ats_job(
            conn,
            url="u-dedup",
            worker_id="m2-2",
            status="failed",
            apply_error="dedup:already_applied",
            dedup_key="dk-dedup",
        )
        _seed_ats_job(
            conn,
            url="u-expired",
            worker_id="m2-3",
            status="failed",
            apply_error="expired",
            dedup_key="dk-expired",
        )

        contexts = autotriage.load_contexts(conn, limit=10)

    assert [ctx.url for ctx in contexts] == []


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
