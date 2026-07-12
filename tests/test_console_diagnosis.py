"""Tests for operator-facing /api/diagnosis and dashboard diagnosis renderer."""
from __future__ import annotations

import json
import threading
import urllib.request
from datetime import datetime, timedelta, timezone

from http.server import ThreadingHTTPServer

import pytest

from applypilot.apply import pgqueue
from applypilot.fleet import compute_context, console_app, heartbeat, queue


def _push_ready_apply(conn, rows, *, approved_batch):
    policy = "diagnosis-ats-policy"
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fleet_decision_policies (policy_version,lane,status) "
            "VALUES (%s,'ats','active') ON CONFLICT (policy_version) DO UPDATE SET status='active'",
            (policy,),
        )
        cur.execute(
            "UPDATE fleet_config SET ats_policy_version=%s WHERE id=1",
            (policy,),
        )
    queue.push_apply_jobs(conn, [{
        **row,
        "decision_id": f"decision-{row['url']}",
        "policy_version": policy,
        "decision_action": "apply",
        "qualification_verdict": "qualified",
        "qualification_score": 9.0,
        "qualification_floor": 7.0,
        "preference_score": 8.0,
        "outcome_score": 8.0,
        "final_score": float(row["score"]),
        "decision_confidence": 0.9,
        "decision_created_at": now,
        "decision_expires_at": now + timedelta(days=1),
        "input_hash": f"hash-{row['url']}",
    } for row in rows], approved_batch=approved_batch)


def test_console_page_has_diagnosis_sections_and_js_renderer(live_server):
    with urllib.request.urlopen(f"{live_server}/") as resp:
        html = resp.read().decode("utf-8")
    assert 'id="fleetState"' in html
    assert 'id="safetyRails"' in html
    assert 'id="whyNotApplying"' in html
    assert 'id="machineHealth"' in html
    assert 'id="queueFunnel"' in html
    assert 'id="queueRecommendations"' in html
    assert 'id="stateHeadline"' in html
    assert 'id="stateReason"' in html
    assert 'id="stateAction"' in html
    assert 'id="safetyGrid"' in html
    assert 'id="whyBody"' in html
    assert 'id="machineMap"' in html
    assert 'id="funnelBody"' in html
    assert 'id="agentRouting"' in html
    assert 'id="agentBody"' in html
    assert 'id="throughputForecast"' in html
    assert 'id="forecastBody"' in html
    assert 'id="dailyGoals"' in html
    assert 'id="dailyGoalsBody"' in html
    assert 'id="dataFreshness"' in html
    assert 'id="freshnessBody"' in html
    assert 'id="operatorAudit"' in html
    assert 'id="operatorAuditBody"' in html
    assert 'id="hostSourceQuality"' in html
    assert 'id="hostQualityBody"' in html
    assert 'id="workerComparison"' in html
    assert 'id="workerComparisonBody"' in html
    assert 'id="linkedinBody"' in html
    assert 'id="discoveryBody"' in html
    assert 'id="computeHealth"' in html
    assert 'id="computeBody"' in html
    assert 'id="deadmanWatchdog"' in html
    assert 'id="deadmanBody"' in html
    assert 'id="stateRecommendation"' in html
    assert "function renderDiagnosis" in html
    assert "function loadDiagnosis" in html
    assert '"/api/diagnosis"' in html
    assert "LinkedIn Lane" in html
    assert "linkedinBody" in html
    assert "Discovery Pipeline" in html
    assert "discoveryBody" in html
    assert "Compute Health" in html
    assert "computeBody" in html
    assert "Agent Routing" in html
    assert "agentBody" in html
    assert "Throughput Forecast" in html
    assert "forecastBody" in html
    assert "Daily Goals" in html
    assert "dailyGoalsBody" in html
    assert "Data Freshness" in html
    assert "freshnessBody" in html
    assert "diagnosis stale" in html
    assert "Operator Audit" in html
    assert "operatorAuditBody" in html
    assert "Host / Source Quality" in html
    assert "hostQualityBody" in html
    assert "Worker Comparison" in html
    assert "workerComparisonBody" in html
    assert "Fleet Watchdog" in html
    assert "deadmanBody" in html
    assert "roles " in html
    assert "stale " in html
    assert "desired " in html
    assert "missing " in html


@pytest.fixture()
def live_server(monkeypatch):
    monkeypatch.setattr(console_app, "_CACHED_TOKEN", None, raising=False)
    monkeypatch.setenv("APPLYPILOT_CONSOLE_TOKEN", "tok-console-diagnosis")
    server = ThreadingHTTPServer(("127.0.0.1", 0), console_app._Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=5)


def test_diagnosis_api_has_expected_shape(monkeypatch, fleet_db, live_server) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="applying")
        conn.commit()

    with urllib.request.urlopen(f"{live_server}/api/diagnosis") as resp:
        body = json.loads(resp.read().decode("utf-8"))

    assert set(body.keys()) == {
        "apply_state", "queue", "safety", "browser", "rollups",
        "linkedin", "discovery", "compute", "forecast", "source_quality",
        "daily_goals", "freshness", "audit", "worker_comparison", "deadman",
        "challenges", "diagnostics_summary",
        "agents", "recommendations",
    }
    assert set(body["safety"].keys()) >= {
        "paused", "ats_paused", "spend_cap_usd", "spent_usd",
        "canary_enabled", "canary_remaining",
    }
    assert "apply" in body["queue"]
    assert set(body["queue"]["apply"].keys()) >= {
        "queued", "leased", "active_leased", "parked_frozen", "applied", "by_lane"
    }
    assert "machines" in body["rollups"]
    assert body["rollups"]["machines"]["tarpon"]["workers"] == 1
    assert "funnel" in body["rollups"]
    assert isinstance(body["recommendations"], list)
    assert body["recommendations"][0]["title"] in {
        "Resume fleet", "Resume ATS lane", "Arm lift", "Inspect upstream queues",
        "Seed approvals", "Start discovery workers", "Start compute workers", "Monitor throughput",
    }


def test_diagnosis_includes_agent_model_and_switching_summary(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="applying")
        heartbeat.beat(conn, "m4-0", machine_owner="m4", role="apply", state="idle")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE worker_heartbeat SET current_agent=%s, current_model=%s, "
                "agent_chain=%s, last_agent_switch_at=now()-interval '6 minutes', "
                "last_agent_switch_reason=%s WHERE worker_id=%s",
                ("claude", "claude-sonnet-4", "codex,claude", "codex usage limit", "m2-0"),
            )
            cur.execute(
                "UPDATE worker_heartbeat SET current_agent=%s, current_model=%s, "
                "agent_chain=%s WHERE worker_id=%s",
                ("codex", "gpt-5", "codex", "m4-0"),
            )
            cur.execute(
                "INSERT INTO agent_availability (agent, blocked_until, reason) "
                "VALUES (%s, now() + interval '20 minutes', %s)",
                ("codex", "usage_limit_wall"),
            )
        conn.commit()

    body = console_app.diagnosis()
    agents = body["agents"]
    assert agents["workers"] == 2
    assert agents["dynamic_workers"] == 1
    assert agents["switched_workers"] == 1
    assert agents["active_agent_blocks"] == ["codex"]
    assert agents["model_usage"] == {"claude-sonnet-4": 1, "gpt-5": 1}
    assert agents["model_family_usage"] == {"claude": 1, "codex-like": 1}
    assert agents["model_missing_workers"] == 0


def test_diagnosis_includes_throughput_forecast(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="idle")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO apply_queue (url, company, title, application_url, score, status, "
                "approved_batch, lane, applied_at, updated_at) VALUES "
                "(%s,'A','Applied 1h',%s,9,'applied','batch','ats',now()-interval '30 minutes',now()-interval '30 minutes'), "
                "(%s,'B','Applied 24h',%s,8,'applied','batch','ats',now()-interval '3 hours',now()-interval '3 hours'), "
                "(%s,'C','Queued',%s,7,'queued','batch','ats',NULL,now())",
                (
                    "forecast-applied-1h", "https://a/apply",
                    "forecast-applied-24h", "https://b/apply",
                    "forecast-queued", "https://c/apply",
                ),
            )
            cur.execute(
                "INSERT INTO auth_challenge (url, kind, machine_owner, raised_at) "
                "VALUES (%s, 'visible_captcha', 'm2', now() - interval '10 minutes')",
                ("forecast-challenge",),
            )
        conn.commit()

    body = console_app.diagnosis()
    forecast = body["forecast"]
    assert forecast["applies_last_1h"] == 1
    assert forecast["applies_last_24h"] == 2
    assert forecast["live_apply_workers"] == 1
    assert forecast["leaseable_jobs"] == 1
    assert forecast["open_challenges"] == 1
    assert forecast["estimated_applies_per_hour"] == 1.0
    assert forecast["eta_hours_to_exhaust_leaseable"] == 1.0
    assert forecast["last_successful_apply_at"] is not None


def test_diagnosis_includes_host_source_quality(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO apply_queue (url, company, title, application_url, score, "
                "status, lane, target_host, apply_domain, approved_batch, apply_status, dedup_key) VALUES "
                "(%s,'Acme','Applied',%s,9,'applied','ats','boards.greenhouse.io','boards.greenhouse.io','batch',NULL,'host-quality-applied'), "
                "(%s,'Acme','Failed',%s,8,'failed','ats','boards.greenhouse.io','boards.greenhouse.io','batch','failed:no_result','host-quality-failed'), "
                "(%s,'Acme','Challenge',%s,7,'queued','ats','boards.greenhouse.io','boards.greenhouse.io','batch','challenge_pending','host-quality-challenge'), "
                "(%s,'Beta','Queued',%s,7,'queued','ats','jobs.lever.co','jobs.lever.co','batch',NULL,'host-quality-queued')",
                (
                    "host-quality-applied", "https://boards.greenhouse.io/acme/applied",
                    "host-quality-failed", "https://boards.greenhouse.io/acme/failed",
                    "host-quality-challenge", "https://boards.greenhouse.io/acme/challenge",
                    "host-quality-queued", "https://jobs.lever.co/beta/queued",
                ),
            )
            cur.execute(
                "INSERT INTO auth_challenge (url, kind, machine_owner, raised_at) "
                "VALUES (%s, 'visible_captcha', 'm2', now() - interval '10 minutes')",
                ("host-quality-challenge",),
            )
        conn.commit()

    body = console_app.diagnosis()
    hosts = {row["host"]: row for row in body["source_quality"]["hosts"]}
    greenhouse = hosts["boards.greenhouse.io"]
    assert greenhouse["applied"] == 1
    assert greenhouse["failed"] == 1
    assert greenhouse["challenges"] == 1
    assert greenhouse["queued"] == 1
    assert greenhouse["leaseable"] == 1
    assert greenhouse["challenge_rate"] == 0.3333
    assert hosts["jobs.lever.co"]["leaseable"] == 1
    assert body["source_quality"]["summary"]["hosts"] == 2
    assert body["source_quality"]["summary"]["challenge_hosts"] == 1


def test_diagnosis_includes_worker_comparison(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="idle")
        heartbeat.beat(conn, "m4-0", machine_owner="m4", role="apply", state="applying")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE worker_heartbeat SET current_agent=%s, current_model=%s WHERE worker_id=%s",
                ("claude", "claude-sonnet-4", "m2-0"),
            )
            cur.execute(
                "INSERT INTO apply_queue (url, company, title, application_url, score, "
                "status, lane, approved_batch, worker_id, est_cost_usd, apply_duration_ms, updated_at) VALUES "
                "(%s,'Acme','Applied',%s,9,'applied','ats','batch','m2-0',0.02,2500,now()), "
                "(%s,'Acme','Failed',%s,8,'failed','ats','batch','m2-0',0.03,3500,now()), "
                "(%s,'Beta','Applied',%s,7,'applied','ats','batch','m4-0',0.01,1000,now())",
                (
                    "worker-compare-applied", "https://acme/applied",
                    "worker-compare-failed", "https://acme/failed",
                    "worker-compare-m4", "https://beta/applied",
                ),
            )
            cur.execute(
                "INSERT INTO auth_challenge (url, worker_id, kind, machine_owner, raised_at) "
                "VALUES (%s, 'm2-0', 'visible_captcha', 'm2', now() - interval '10 minutes')",
                ("worker-compare-challenge",),
            )
        conn.commit()

    body = console_app.diagnosis()
    workers = {row["worker_id"]: row for row in body["worker_comparison"]["workers"]}
    assert workers["m2-0"]["machine"] == "tarpon"
    assert workers["m2-0"]["current_agent"] == "claude"
    assert workers["m2-0"]["current_model"] == "claude-sonnet-4"
    assert workers["m2-0"]["applied"] == 1
    assert workers["m2-0"]["failed"] == 1
    assert workers["m2-0"]["challenges"] == 1
    assert workers["m2-0"]["avg_duration_ms"] == 3000
    assert workers["m2-0"]["cost_usd"] == 0.05
    assert workers["m4-0"]["applied"] == 1
    assert body["worker_comparison"]["summary"]["workers"] == 2
    assert body["worker_comparison"]["summary"]["failed"] == 1
    assert body["worker_comparison"]["summary"]["challenges"] == 1


def test_diagnosis_includes_daily_goals(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE fleet_config SET daily_apply_target=5, "
                "ats_apply_mode='canary', canary_enabled=TRUE, canary_remaining=2 WHERE id=1"
            )
            cur.execute(
                "INSERT INTO apply_queue (url, company, title, application_url, score, "
                "status, lane, approved_batch, applied_at, updated_at) VALUES "
                "(%s,'Acme','Today',%s,9,'applied','ats','batch',now(),now()), "
                "(%s,'Beta','Old',%s,8,'applied','ats','batch',now()-interval '2 days',now()-interval '2 days')",
                (
                    "daily-goal-today", "https://acme/apply",
                    "daily-goal-old", "https://beta/apply",
                ),
            )
        conn.commit()

    body = console_app.diagnosis()
    goals = body["daily_goals"]
    assert goals["configured"] is True
    assert goals["applied_today"] == 1
    assert goals["target_today"] == 5
    assert goals["remaining_target"] == 4
    assert goals["canary_remaining"] == 2
    assert goals["projected_shortfall"] == 4


def test_diagnosis_daily_goals_reports_unconfigured_target(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)

    body = console_app.diagnosis()

    assert body["daily_goals"]["configured"] is False
    assert body["daily_goals"]["target_today"] is None
    assert body["daily_goals"]["message"] == "No daily target configured"


def test_diagnosis_includes_data_freshness(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="idle")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO apply_queue (url, company, title, application_url, score, "
                "status, lane, approved_batch, applied_at, updated_at) VALUES "
                "(%s,'Acme','Fresh',%s,9,'applied','ats','batch',now()-interval '30 minutes',now()-interval '30 minutes')",
                ("freshness-apply", "https://fresh/apply"),
            )
            cur.execute(
                "INSERT INTO discovered_postings (task_id, posting, worker_id, discovered_at) "
                "VALUES (%s, %s, %s, now()-interval '20 minutes')",
                ("freshness-task", json.dumps({"url": "https://fresh/job"}), "disc-0"),
            )
            cur.execute(
                "INSERT INTO compute_queue (url, task, status, updated_at) "
                "VALUES (%s, 'score', 'done', now()-interval '10 minutes')",
                ("freshness-compute",),
            )
        conn.commit()

    body = console_app.diagnosis()
    freshness = body["freshness"]
    assert freshness["generated_at"] is not None
    assert freshness["endpoint"] == "diagnosis"
    assert freshness["last_worker_beat_at"] is not None
    assert freshness["last_apply_at"] is not None
    assert freshness["last_discovery_at"] is not None
    assert freshness["last_compute_at"] is not None
    assert freshness["ages"]["last_worker_beat_seconds"] is not None


def test_diagnosis_separates_live_browser_symptoms_from_challenge_backlog(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="applying")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE worker_heartbeat SET recent_log=%s WHERE worker_id=%s",
                ("CAPTCHA present in page; manual challenge required", "m2-0"),
            )
            cur.execute(
                "INSERT INTO auth_challenge (url, kind, machine_owner, raised_at) VALUES "
                "(%s, 'visible_captcha', 'm2', now() - interval '2 hours'), "
                "(%s, 'login_gate', 'm4', now() - interval '30 hours'), "
                "(%s, 'manual_auth', 'm4', now() - interval '3 hours')",
                ("https://example.com/captcha", "https://example.com/login", "https://example.com/auth"),
            )
        conn.commit()

    body = console_app.diagnosis()
    assert body["browser"]["summary"]["problem_workers"] == 1
    assert body["browser"]["summary"]["by_issue"]["CAPTCHA present"] == 1
    assert body["challenges"]["open_auth"] == 3
    assert body["challenges"]["by_kind"] == {
        "login_gate": 1,
        "manual_auth": 1,
        "visible_captcha": 1,
    }
    assert body["challenges"]["fresh_open"] == 2
    assert body["challenges"]["stale_open"] == 1
    assert body["challenges"]["parked_open"] == 0
    assert body["challenges"]["stale_nonparked"] == 1


def test_diagnosis_labels_frozen_challenge_leases_separately(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO apply_queue "
                "(url, company, title, application_url, score, status, lane, approved_batch, lease_expires_at, updated_at) "
                "VALUES "
                "(%s,'A','Frozen',%s,9,'leased','ats','batch',now() + interval '3650 days',now()), "
                "(%s,'B','Active',%s,8,'leased','ats','batch',now() + interval '20 minutes',now())",
                (
                    "frozen-lease-url", "https://example.com/frozen",
                    "active-lease-url", "https://example.com/active",
                ),
            )
        conn.commit()

    body = console_app.diagnosis()
    apply_q = body["queue"]["apply"]
    assert apply_q["leased"] == 2
    assert apply_q["active_leased"] == 1
    assert apply_q["parked_frozen"] == 1
    assert apply_q["challenge_parked"] == 0
    assert apply_q["unexpected_frozen"] == 1
    assert apply_q["by_lane"]["ats"]["leased"] == 2
    assert apply_q["by_lane"]["ats"]["active_leased"] == 1
    assert apply_q["by_lane"]["ats"]["parked_frozen"] == 1


def test_diagnosis_worker_comparison_labels_historical_workers_without_owner(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO apply_queue (url, company, title, application_url, score, "
                "status, lane, approved_batch, worker_id, updated_at) VALUES "
                "(%s,'Acme','Historic',%s,9,'failed','ats','batch','m2-legacy',now())",
                ("historical-worker", "https://example.com/historical"),
            )
        conn.commit()

    body = console_app.diagnosis()
    workers = {row["worker_id"]: row for row in body["worker_comparison"]["workers"]}
    assert workers["m2-legacy"]["machine"] == "historical/no owner recorded"


def test_diagnosis_worker_comparison_uses_llm_usage_fallback_for_historical_worker(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO apply_queue (url, company, title, application_url, score, "
                "status, lane, approved_batch, worker_id, updated_at) VALUES "
                "(%s,'Acme','Historic',%s,9,'failed','ats','batch','m2-legacy',now())",
                ("historical-worker-fallback", "https://example.com/historical-fallback"),
            )
            cur.execute(
                "INSERT INTO llm_usage (worker_id, machine_owner, provider, model, task, cost_usd, ts) "
                "VALUES (%s, %s, %s, %s, %s, %s, now() - interval '2 minutes')",
                ("m2-legacy", "m2", "codex", "gpt-5", "apply", 0.12),
            )
        conn.commit()

    body = console_app.diagnosis()
    workers = {row["worker_id"]: row for row in body["worker_comparison"]["workers"]}
    assert workers["m2-legacy"]["machine"] == "tarpon"
    assert workers["m2-legacy"]["machine_owner"] == "m2"
    assert workers["m2-legacy"]["current_agent"] == "codex"
    assert workers["m2-legacy"]["current_model"] == "gpt-5"


def test_diagnosis_dedupes_recent_discovery_events(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO search_tasks (task_id, query, board, location, next_due_at, cadence_seconds) "
                "VALUES ('disc-dedupe', 'product manager', 'indeed', 'Remote', now(), 3600)"
            )
        queue.push_discovered(
            conn,
            task_id="disc-dedupe",
            source_label="Product Manager",
            worker_id="m2-disc-0",
            postings=[
                {"job_url": "https://example.com/d1", "title": "One"},
                {"job_url": "https://example.com/d2", "title": "Two"},
                {"job_url": "https://example.com/d3", "title": "Three"},
            ],
        )
        conn.commit()

    body = console_app.diagnosis()
    recent = body["discovery"]["recent"]
    assert len(recent) == 1
    assert recent[0]["source"] == "Product Manager"
    assert recent[0]["query"] == "product manager"
    assert recent[0]["board"] == "indeed"
    assert recent[0]["count"] == 3


def test_diagnosis_recommends_triaging_stale_challenge_backlog(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET paused=TRUE WHERE id=1")
            cur.execute(
                "INSERT INTO auth_challenge (url, kind, machine_owner, raised_at) VALUES "
                "(%s, 'login_gate', 'm2', now() - interval '30 hours'), "
                "(%s, 'visible_captcha', 'm2', now() - interval '40 hours')",
                ("https://example.com/old1", "https://example.com/old2"),
            )
        conn.commit()

    body = console_app.diagnosis()
    titles = [r["title"] for r in body["recommendations"]]
    assert titles[0] == "Resume fleet"
    assert "Clean stale challenge records" in titles


def test_diagnosis_distinguishes_aging_parked_challenge_from_stale_record(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    url = "https://example.com/parked-old"
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET paused=TRUE WHERE id=1")
            cur.execute(
                "INSERT INTO apply_queue "
                "(url, company, title, application_url, score, status, lane, approved_batch, "
                "lease_owner, lease_expires_at, apply_status, apply_error) "
                "VALUES (%s,'Co','Role',%s,9,'leased','ats','batch','worker',"
                "now() + interval '3650 days','challenge_pending','login_gate')",
                (url, url),
            )
            cur.execute(
                "INSERT INTO auth_challenge (url, worker_id, kind, route, raised_at) "
                "VALUES (%s,'worker','login_gate','owner_inbox',now() - interval '30 hours')",
                (url,),
            )
        conn.commit()

    body = console_app.diagnosis()
    challenges = body["challenges"]
    assert challenges["parked_open"] == 1
    assert challenges["stale_parked"] == 1
    assert challenges["stale_nonparked"] == 0
    titles = [r["title"] for r in body["recommendations"]]
    assert "Resolve aging parked challenges" in titles
    assert "Clean stale challenge records" not in titles


def test_diagnosis_recommends_reviewing_unclassified_ats_failures(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET paused=TRUE WHERE id=1")
            cur.execute(
                "INSERT INTO fleet_diagnoses "
                "(reason, host, machine, lane, sample_count, severity, diagnosis, recommendation, status, expires_at) "
                "VALUES "
                "('other','alpha.example','m2','ats',2,'info','job closed / expired posting','review manually','recommended', now() + interval '1 hour'), "
                "('other','beta.example','m4','ats',3,'info','selector parse failed on apply form','review manually','recommended', now() + interval '1 hour')"
            )
            for i in range(2):
                cur.execute(
                    "INSERT INTO apply_queue (url, application_url, score, status, lane, target_host, apply_error) "
                    "VALUES (%s,%s,8,'failed','ats','alpha.example','opaque_alpha_failure')",
                    (f"alpha-{i}", f"https://alpha.example/{i}"),
                )
            for i in range(3):
                cur.execute(
                    "INSERT INTO apply_queue (url, application_url, score, status, lane, target_host, apply_error) "
                    "VALUES (%s,%s,8,'failed','ats','beta.example','opaque_beta_failure')",
                    (f"beta-{i}", f"https://beta.example/{i}"),
                )
        conn.commit()

    body = console_app.diagnosis()
    titles = [r["title"] for r in body["recommendations"]]
    assert titles[0] == "Resume fleet"
    assert "Review unclassified ATS failures" in titles
    assert body["diagnostics_summary"]["other_hosts"] == 2
    assert body["diagnostics_summary"]["other_samples"] == 5
    assert body["diagnostics_summary"]["top_other_hosts"] == [
        {"host": "beta.example", "samples": 3},
        {"host": "alpha.example", "samples": 2},
    ]
    assert body["diagnostics_summary"]["other_buckets"] == {
        "expired_posting": 2,
        "parse_failure": 3,
    }
    assert body["diagnostics_summary"]["top_other_buckets"] == [
        {"bucket": "parse_failure", "samples": 3},
        {"bucket": "expired_posting", "samples": 2},
    ]
    assert body["diagnostics_summary"]["top_other_bucket_hosts"] == {
        "expired_posting": [{"host": "alpha.example", "samples": 2}],
        "parse_failure": [{"host": "beta.example", "samples": 3}],
    }
    reason = next(r["reason"] for r in body["recommendations"] if r["title"] == "Review unclassified ATS failures")
    assert "parse_failure (3)" in reason
    assert "expired_posting (2)" in reason
    assert "beta.example (3)" in reason
    assert "alpha.example (2)" in reason


def test_diagnosis_suppresses_legacy_other_advisory_without_current_unknown_failure(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET paused=TRUE WHERE id=1")
        cur.execute(
            "INSERT INTO fleet_diagnoses "
            "(reason, host, machine, lane, sample_count, severity, diagnosis, recommendation, status, expires_at) "
            "VALUES ('other','legacy.example','m2','ats',500,'info','legacy unknown','review manually',"
            "'recommended',now()+interval '1 day')"
        )
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, score, status, lane, target_host, apply_error) "
            "VALUES ('retired-known','https://legacy.example/job',5,'failed','ats','legacy.example','stale_unapproved')"
        )
        conn.commit()

    body = console_app.diagnosis()
    assert body["diagnostics_summary"]["other_samples"] == 0
    assert "Review unclassified ATS failures" not in [r["title"] for r in body["recommendations"]]


def test_diagnosis_prioritizes_frozen_leased_backlog(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="idle")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO apply_queue "
                "(url, company, title, application_url, score, status, lane, approved_batch, updated_at) "
                "VALUES (%s,'Acme','Queued',%s,9,'queued','ats','batch',now())",
                ("frozen-ready-queued", "https://example.com/queued"),
            )
            cur.execute(
                "INSERT INTO apply_queue "
                "(url, company, title, application_url, score, status, lane, approved_batch, lease_expires_at, updated_at) "
                "VALUES (%s,'Acme','Frozen',%s,9,'leased','ats','batch',now() + interval '3650 days',now())",
                ("frozen-ready-leased", "https://example.com/frozen"),
            )
        conn.commit()

    body = console_app.diagnosis()
    assert body["apply_state"]["code"] == "ready_to_apply"
    assert body["queue"]["apply"]["parked_frozen"] == 1
    assert body["queue"]["apply"]["active_leased"] == 0
    assert body["recommendations"][0]["title"] == "Release parked leases"


def test_diagnosis_recommends_reviewing_high_failure_hosts(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET paused=TRUE WHERE id=1")
            cur.execute(
                "INSERT INTO apply_queue (url, company, title, application_url, score, "
                "status, lane, target_host, apply_domain, approved_batch, apply_status, dedup_key) VALUES "
                "(%s,'Acme','Applied',%s,9,'applied','ats','boards.greenhouse.io','boards.greenhouse.io','batch',NULL,'toxic-host-applied'), "
                "(%s,'Acme','Failed 1',%s,8,'failed','ats','boards.greenhouse.io','boards.greenhouse.io','batch','failed:no_result','toxic-host-failed-1'), "
                "(%s,'Acme','Failed 2',%s,8,'failed','ats','boards.greenhouse.io','boards.greenhouse.io','batch','failed:no_result','toxic-host-failed-2'), "
                "(%s,'Acme','Failed 3',%s,8,'failed','ats','boards.greenhouse.io','boards.greenhouse.io','batch','failed:no_result','toxic-host-failed-3'), "
                "(%s,'Acme','Challenge',%s,7,'queued','ats','boards.greenhouse.io','boards.greenhouse.io','batch','challenge_pending','toxic-host-challenge')",
                (
                    "toxic-host-applied", "https://boards.greenhouse.io/acme/applied",
                    "toxic-host-failed-1", "https://boards.greenhouse.io/acme/failed-1",
                    "toxic-host-failed-2", "https://boards.greenhouse.io/acme/failed-2",
                    "toxic-host-failed-3", "https://boards.greenhouse.io/acme/failed-3",
                    "toxic-host-challenge", "https://boards.greenhouse.io/acme/challenge",
                ),
            )
            cur.execute(
                "INSERT INTO auth_challenge (url, kind, machine_owner, raised_at) "
                "VALUES (%s, 'visible_captcha', 'm2', now() - interval '10 minutes')",
                ("toxic-host-challenge",),
            )
        conn.commit()

    body = console_app.diagnosis()
    titles = [r["title"] for r in body["recommendations"]]
    assert titles[0] == "Resume fleet"
    assert "Review hostile ATS hosts" in titles
    reason = next(r["reason"] for r in body["recommendations"] if r["title"] == "Review hostile ATS hosts")
    assert "boards.greenhouse.io (adapter_fix)" in reason


def test_diagnosis_recommends_hostile_hosts_with_queued_backlog_even_if_not_leaseable(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET paused=TRUE WHERE id=1")
            cur.execute(
                "INSERT INTO apply_queue (url, company, title, application_url, score, "
                "status, lane, target_host, apply_domain, approved_batch, apply_status, dedup_key) VALUES "
                "(%s,'Acme','Failed 1',%s,8,'failed','ats','example.com','example.com',NULL,'failed:no_result','queued-risk-1'), "
                "(%s,'Acme','Failed 2',%s,8,'failed','ats','example.com','example.com',NULL,'failed:no_result','queued-risk-2'), "
                "(%s,'Acme','Failed 3',%s,8,'failed','ats','example.com','example.com',NULL,'failed:no_result','queued-risk-3'), "
                "(%s,'Acme','Queued',%s,7,'queued','ats','example.com','example.com',NULL,NULL,'queued-risk-4')",
                (
                    "queued-risk-1", "https://example.com/failed-1",
                    "queued-risk-2", "https://example.com/failed-2",
                    "queued-risk-3", "https://example.com/failed-3",
                    "queued-risk-4", "https://example.com/queued",
                ),
            )
        conn.commit()

    body = console_app.diagnosis()
    reason = next(r["reason"] for r in body["recommendations"] if r["title"] == "Review hostile ATS hosts")
    assert "example.com (adapter_fix)" in reason


def test_diagnosis_decomposes_no_leaseable_jobs_into_blockers(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO apply_queue (url, company, title, application_url, score, status, lane, apply_status, updated_at) "
                "VALUES "
                "(%s,'Acme','Queued stale',%s,7,'queued','ats','challenge_pending',now() - interval '30 hours'), "
                "(%s,'Acme','Frozen stale',%s,8,'leased','ats','challenge_pending',now())",
                (
                    "https://example.com/queued-stale", "https://example.com/queued-stale/apply",
                    "https://example.com/frozen-stale", "https://example.com/frozen-stale/apply",
                ),
            )
            cur.execute(
                "UPDATE apply_queue SET lease_expires_at = now() + interval '3650 days' WHERE url=%s",
                ("https://example.com/frozen-stale",),
            )
            cur.execute(
                "INSERT INTO auth_challenge (url, kind, machine_owner, raised_at) VALUES "
                "(%s, 'login_gate', 'm2', now() - interval '30 hours'), "
                "(%s, 'visible_captcha', 'm4', now() - interval '28 hours')",
                (
                    "https://example.com/queued-stale",
                    "https://example.com/frozen-stale",
                ),
            )
        conn.commit()

    body = console_app.diagnosis()
    assert body["apply_state"]["code"] == "no_leaseable_jobs"
    blocker_codes = [b["code"] for b in body["apply_state"]["blockers"]]
    assert blocker_codes[0] == "aging_parked_challenges"
    assert "frozen_leases" not in blocker_codes
    assert "open_challenges" in blocker_codes
    assert "more than 24 hours" in body["apply_state"]["reason"]
    assert body["apply_state"]["next_action"] == "Resolve or skip those parked jobs."
    assert body["recommendations"][0]["title"] == "Resolve aging parked challenges"
    assert "more than 24 hours" in body["recommendations"][0]["reason"]
    assert "Release parked leases" not in [r["title"] for r in body["recommendations"]]


def test_diagnosis_prioritizes_frozen_leases_when_no_leaseable_jobs_are_parked(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO apply_queue (url, company, title, application_url, score, status, lane, approved_batch, lease_expires_at, updated_at) "
                "VALUES (%s,'Acme','Frozen only',%s,9,'leased','ats','batch',now() + interval '3650 days',now())",
                ("https://example.com/frozen-only", "https://example.com/frozen-only/apply"),
            )
        conn.commit()

    body = console_app.diagnosis()
    assert body["apply_state"]["code"] == "no_leaseable_jobs"
    assert body["apply_state"]["blockers"][0]["code"] == "frozen_leases"
    assert body["apply_state"]["next_action"] == "Release or reclaim parked leases."
    assert body["recommendations"][0]["title"] == "Release parked leases"


def test_diagnosis_includes_operator_audit_log(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO fleet_console_audit (action, actor, lane, target, message, ok, created_at) "
                "VALUES (%s, %s, %s, %s, %s, TRUE, now()-interval '1 minute')",
                ("pause", "console", "ats", "fleet", "Fleet PAUSED",),
            )
        conn.commit()

    body = console_app.diagnosis()
    audit = body["audit"]
    assert audit["summary"]["rows"] == 1
    assert audit["rows"][0]["action"] == "pause"
    assert audit["rows"][0]["actor"] == "console"
    assert audit["rows"][0]["ok"] is True
    assert audit["rows"][0]["created_at"] is not None


def test_console_action_writes_secret_free_audit_row(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    ok, message = console_app.run_action({
        "action": "pause",
        "token": "super-secret-console-token",
        "dsn": "postgresql://user:pass@example.com/applypilot",
    })
    assert ok is True
    assert "PAUSED" in message

    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT action, actor, target, message, ok FROM fleet_console_audit")
            row = cur.fetchone()

    assert row["action"] == "pause"
    assert row["actor"] == "console"
    assert row["target"] == "fleet"
    assert row["ok"] is True
    joined = " ".join(str(row.get(key) or "") for key in ("action", "actor", "target", "message"))
    assert "super-secret-console-token" not in joined
    assert "postgresql://" not in joined
    assert "user:pass" not in joined


def test_diagnosis_includes_deadman_alert_and_top_recommendation(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE fleet_config SET deadman_alert=%s, deadman_alert_at=now() WHERE id=1",
                ("silent_death: no apply-worker heartbeat",),
            )
        conn.commit()

    body = console_app.diagnosis()
    assert body["deadman"]["active"] is True
    assert body["deadman"]["code"] == "silent_death"
    assert body["deadman"]["title"] == "No apply worker heartbeat"
    assert "No apply worker heartbeat" in body["deadman"]["reason"]
    assert body["deadman"]["alert"] == "silent_death: no apply-worker heartbeat"
    assert body["deadman"]["alert_at"] is not None
    assert body["recommendations"][0]["title"] == "Investigate DeadMan alert"
    assert "No apply worker heartbeat" in body["recommendations"][0]["reason"]


def test_diagnosis_parses_stalled_queue_deadman_alert(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE fleet_config SET deadman_alert=%s, deadman_alert_at=now() WHERE id=1",
                ("stalled_queue: approved backlog queued but no 'applied' row in the last 3h (9m ago)",),
            )
        conn.commit()

    body = console_app.diagnosis()
    assert body["deadman"]["code"] == "stalled_queue"
    assert body["deadman"]["title"] == "Queued backlog is stalled"
    assert "Approved backlog is not converting into applied rows" in body["deadman"]["reason"]
    assert body["recommendations"][0]["title"] == "Investigate DeadMan alert"
    assert "Approved backlog is not converting into applied rows" in body["recommendations"][0]["reason"]


def test_diagnosis_recommends_backfilling_challenge_metadata(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET paused=TRUE WHERE id=1")
            cur.execute(
                "INSERT INTO linkedin_queue (url, application_url, score, apply_status, updated_at) "
                "VALUES (%s, %s, %s, %s, now() - interval '2 hours')",
                (
                    "https://www.linkedin.com/jobs/view/missing-meta",
                    "https://www.linkedin.com/jobs/view/missing-meta",
                    0.0,
                    "challenge_pending",
                ),
            )
        conn.commit()

    body = console_app.diagnosis()
    titles = [r["title"] for r in body["recommendations"]]
    assert titles[0] == "Resume fleet"
    assert body["challenges"]["missing_metadata_rows"] == 1
    assert "Backfill challenge metadata" in titles


def test_diagnosis_respects_explicit_operator_pause_without_resume_recommendation(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE fleet_config SET paused=TRUE, ats_paused=TRUE, ats_pause_source='operator' WHERE id=1"
        )
        conn.commit()

    body = console_app.diagnosis()
    assert body["apply_state"] == {
        "code": "operator_paused",
        "severity": "info",
        "reason": "Fleet is intentionally paused by the operator.",
        "blockers": [],
    }
    titles = [row["title"] for row in body["recommendations"]]
    assert titles[0] == "Operator pause active"
    assert "Resume fleet" not in titles
    assert "Resume ATS lane" not in titles
    assert "Seed approvals" not in titles


def test_diagnosis_includes_compute_health_and_recommends_worker_start(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        compute_context.publish_context(
            conn,
            resume_text="resume",
            preference_profile={},
            kg_prompt="kg",
            search_cfg={},
            version="ctx-diag",
        )
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO compute_queue (url, task, payload, status, result, est_cost_usd) "
                "VALUES (%s,'score',%s,'queued',NULL,0)",
                ("score-queued", json.dumps({"url": "score-queued"})),
            )
            cur.execute(
                "INSERT INTO compute_queue (url, task, payload, status, result, est_cost_usd, updated_at) "
                "VALUES (%s,'score',%s,'failed',%s,0.01,now())",
                (
                    "score-rate-limit",
                    json.dumps({"url": "score-rate-limit"}),
                    json.dumps({"error": "DeepSeek 429 rate limit", "ctx_version": "ctx-diag"}),
                ),
            )
        conn.commit()

    body = console_app.diagnosis()
    compute = body["compute"]
    assert compute["context"]["version"] == "ctx-diag"
    assert compute["queue"]["queued"]["count"] == 1
    assert compute["score_workers"]["active"] == 0
    assert compute["score_workers"]["recent_failed_15m"] == 1
    assert compute["score_workers"]["recent_rate_limited_15m"] == 1
    assert body["recommendations"][0]["title"] == "Start compute workers"


def test_diagnosis_recommends_backfilling_model_telemetry(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="idle")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE worker_heartbeat SET current_agent=%s, current_model=%s WHERE worker_id=%s",
                ("claude", None, "m2-0"),
            )
            cur.execute("UPDATE fleet_config SET paused=TRUE WHERE id=1")
        conn.commit()

    body = console_app.diagnosis()
    assert body["agents"]["model_missing_workers"] == 1
    titles = [r["title"] for r in body["recommendations"]]
    assert titles[0] == "Resume fleet"
    assert "Backfill apply model telemetry" in titles


def test_diagnosis_does_not_recommend_model_backfill_for_stale_worker(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "stale-model-worker", machine_owner="m2", role="apply", state="idle")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE worker_heartbeat SET current_agent='claude', current_model=NULL, "
                "last_beat=now()-interval '1 hour' WHERE worker_id='stale-model-worker'"
            )
        conn.commit()

    body = console_app.diagnosis()
    assert body["agents"]["model_missing_workers"] == 0
    assert body["agents"]["stale_model_missing_workers"] == 1
    assert "Backfill apply model telemetry" not in [r["title"] for r in body["recommendations"]]


def test_diagnosis_includes_linkedin_lane_summary_and_halt_recommendation(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET linkedin_canary_enabled=FALSE WHERE id=1")
            cur.execute(
                "INSERT INTO linkedin_queue (url, application_url, score, status, lane, approved_batch, dedup_key) "
                "VALUES ('li-diag', 'https://linkedin.com/jobs/diag', 9, 'queued', 'ats', 'li-batch', 'li-diag')"
            )
            cur.execute(
                "INSERT INTO rate_governor (scope_key, halted_until) "
                "VALUES ('account:linkedin', now() + interval '30 minutes') "
                "ON CONFLICT (scope_key) DO UPDATE SET halted_until=EXCLUDED.halted_until"
            )
        conn.commit()

    body = console_app.diagnosis()
    assert body["linkedin"] == {
        "queued": 1,
        "applied": 0,
        "apply_mode": "steady",
        "canary_enabled": False,
        "canary_remaining": None,
        "halted": True,
    }
    assert body["recommendations"][0]["title"] == "Clear LinkedIn halt"


def test_diagnosis_includes_discovery_summary_and_recommends_worker_start(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO search_tasks (task_id, query, board, location, next_due_at, cadence_seconds) "
                "VALUES ('disc-1', 'chief of staff', 'indeed', 'Remote', now() - interval '5 minutes', 3600), "
                "('disc-2', 'strategy operations', 'greenhouse', 'Remote', now() - interval '10 minutes', 3600)"
            )
        queue.push_discovered(
            conn,
            task_id="disc-1",
            source_label="chief of staff",
            worker_id="m2-disc-0",
            postings=[
                {"job_url": "https://example.com/1", "title": "Chief of Staff"},
                {"job_url": "https://example.com/2", "title": "Program Lead"},
            ],
        )
        conn.commit()

    body = console_app.diagnosis()
    assert body["discovery"]["tasks"]["total"] == 2
    assert body["discovery"]["tasks"]["enabled"] == 2
    assert body["discovery"]["tasks"]["due_now"] == 2
    assert body["discovery"]["postings"]["pending_ingest"] == 2
    assert body["discovery"]["postings"]["last24h"] == 2
    assert body["discovery"]["workers"] == []
    assert body["recommendations"][0]["title"] == "Start discovery workers"


def test_diagnosis_machine_rollups_count_alive_and_working_workers(monkeypatch, fleet_db) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="idle")
        heartbeat.beat(conn, "m4-0", machine_owner="m4", role="apply", state="applying")
        conn.commit()

    body = console_app.diagnosis()
    machines = body["rollups"]["machines"]
    assert machines["tarpon"]["workers"] == 1
    assert machines["tarpon"]["alive"] == 1
    assert machines["tarpon"]["idle"] == 1
    assert machines["tarpon"]["working"] == 0
    assert machines["gggtower"]["workers"] == 1
    assert machines["gggtower"]["alive"] == 1
    assert machines["gggtower"]["working"] == 1


def test_diagnosis_recommends_start_apply_workers_when_jobs_are_ready_without_workers(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        _push_ready_apply(
            conn,
            [{
                "url": "https://boards.greenhouse.io/acme/jobs/diag-ready",
                "company": "Acme",
                "title": "Operations Lead",
                "application_url": "https://boards.greenhouse.io/acme/jobs/diag-ready/apply",
                "score": 9.1,
                "target_host": "boards.greenhouse.io",
            }],
            approved_batch="batch-ready",
        )
        conn.commit()

    body = console_app.diagnosis()
    assert body["apply_state"]["code"] == "ready_to_apply"
    assert body["recommendations"][0]["title"] == "Start apply workers"


def test_diagnosis_recommends_browser_repair_when_ready_worker_has_browser_issue(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        _push_ready_apply(
            conn,
            [{
                "url": "https://boards.greenhouse.io/acme/jobs/diag-browser",
                "company": "Acme",
                "title": "Program Lead",
                "application_url": "https://boards.greenhouse.io/acme/jobs/diag-browser/apply",
                "score": 9.0,
                "target_host": "boards.greenhouse.io",
            }],
            approved_batch="batch-browser",
        )
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="applying")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE worker_heartbeat SET recent_log=%s WHERE worker_id=%s",
                ("playwright disconnected while submitting application", "m2-0"),
            )
        conn.commit()

    body = console_app.diagnosis()
    assert body["apply_state"]["code"] == "ready_to_apply"
    assert body["browser"]["summary"]["problem_workers"] == 1
    assert body["recommendations"][0]["title"] == "Fix browser/backend"


def test_diagnosis_does_not_recommend_backend_repair_for_captcha_only(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        _push_ready_apply(
            conn,
            [{
                "url": "https://boards.greenhouse.io/acme/jobs/diag-captcha",
                "company": "Acme",
                "title": "Program Lead",
                "application_url": "https://boards.greenhouse.io/acme/jobs/diag-captcha/apply",
                "score": 9.0,
                "target_host": "boards.greenhouse.io",
            }],
            approved_batch="batch-captcha",
        )
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="applying")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE worker_heartbeat SET recent_log=%s WHERE worker_id=%s",
                ("CAPTCHA present in page; manual challenge required", "m2-0"),
            )
        conn.commit()

    body = console_app.diagnosis()
    assert body["apply_state"]["code"] == "ready_to_apply"
    assert body["browser"]["summary"]["problem_workers"] == 1
    assert body["browser"]["summary"]["by_issue"]["CAPTCHA present"] == 1
    assert body["recommendations"][0]["title"] != "Fix browser/backend"


def test_diagnosis_includes_desired_machine_worker_gaps(monkeypatch, fleet_db) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE fleet_desired_state ("
                "machine_owner TEXT PRIMARY KEY, desired_workers INTEGER NOT NULL)"
            )
            cur.execute(
                "INSERT INTO fleet_desired_state (machine_owner, desired_workers) "
                "VALUES ('m2', 2), ('m4', 1)"
            )
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="idle")
        _push_ready_apply(
            conn,
            [{
                "url": "https://boards.greenhouse.io/acme/jobs/diag-desired",
                "company": "Acme",
                "title": "Strategy Lead",
                "application_url": "https://boards.greenhouse.io/acme/jobs/diag-desired/apply",
                "score": 9.2,
                "target_host": "boards.greenhouse.io",
            }],
            approved_batch="batch-desired",
        )
        conn.commit()

    body = console_app.diagnosis()
    machines = body["rollups"]["machines"]
    assert machines["tarpon"]["desired"] == 2
    assert machines["tarpon"]["alive"] == 1
    assert machines["tarpon"]["missing"] == 1
    assert machines["gggtower"]["desired"] == 1
    assert machines["gggtower"]["alive"] == 0
    assert machines["gggtower"]["missing"] == 1
    assert body["rollups"]["fleet"]["desired_workers"] == 3
    assert body["rollups"]["fleet"]["alive_workers"] == 1
    assert body["rollups"]["fleet"]["missing_workers"] == 2
    assert body["recommendations"][0]["title"] == "Restore missing workers"


def test_diagnosis_includes_all_fleet_roles_and_stale_counts(monkeypatch, fleet_db) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-apply-0", machine_owner="m2", role="apply", state="idle")
        heartbeat.beat(conn, "m2-compute-0", machine_owner="m2", role="compute", state="idle")
        heartbeat.beat(conn, "m4-discovery-0", machine_owner="m4", role="discovery", state="applying")
        heartbeat.beat(conn, "home-watchdog", machine_owner="home", role="watchdog", state="idle")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE worker_heartbeat "
                "SET last_beat = now() - interval '10 minutes' "
                "WHERE worker_id = 'm2-compute-0'"
            )
        conn.commit()

    body = console_app.diagnosis()
    network = body["rollups"]["network"]
    assert network["workers"] == 4
    assert network["alive"] == 3
    assert network["stale"] == 1
    assert network["by_role"] == {"apply": 1, "compute": 1, "discovery": 1, "watchdog": 1}
    assert network["machines"]["tarpon"]["workers"] == 2
    assert network["machines"]["tarpon"]["alive"] == 1
    assert network["machines"]["tarpon"]["stale"] == 1
    assert network["machines"]["tarpon"]["by_role"] == {"apply": 1, "compute": 1}
    assert network["machines"]["gggtower"]["by_role"] == {"discovery": 1}
    assert network["machines"]["home"]["by_role"] == {"watchdog": 1}


def test_diagnosis_machine_rollups_include_network_only_machines(monkeypatch, fleet_db) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "home-watchdog", machine_owner="home", role="watchdog", state="idle")
        conn.commit()

    body = console_app.diagnosis()
    machines = body["rollups"]["machines"]
    assert "home" in machines
    assert machines["home"]["workers"] == 0
    assert machines["home"]["alive"] == 0
    assert machines["home"]["fleet_workers"] == 1
    assert machines["home"]["fleet_alive"] == 1
    assert machines["home"]["fleet_stale"] == 0
