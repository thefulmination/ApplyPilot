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
                "INSERT INTO worker_heartbeat (worker_id, role, state, last_error, recent_log) "
                "VALUES ('m2-2', 'apply', 'idle', 'browser unavailable', 'chrome launch failed')"
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
