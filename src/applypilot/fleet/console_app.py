"""applypilot-fleet-console: a LOCAL, LAN-ONLY web control panel for the apply fleet.

This replaces hand-run SQL with a tiny, audited HTTP surface that is safe enough to
operate a system that submits REAL job applications under the owner's name.

DESIGN / SAFETY (the review rubric S1-S6 lives here):
  * stdlib ONLY -- http.server.ThreadingHTTPServer + json + argparse. NO new pip deps.
    The HTML/CSS/JS frontend is embedded as a string below.
  * Every consequential mutation goes through a STRICT ALLOW-LIST (a dict), never
    eval/getattr on the request action string, and never SQL built from request input.
  * Reuses the proven, tested fleet functions (pgqueue / apply_home_main / codex_bridge)
    rather than reinventing their queries. Connections are short-lived (one per request)
    and always closed in a finally.
  * NO LinkedIn WRITE controls anywhere -- LinkedIn is the catastrophe lane and is shown
    strictly read-only. There is no arm/resume/apply action for it.
  * LAN-ONLY bind: the server refuses to bind 0.0.0.0 or any public IP. It binds a private
    IPv4 (10/8, 172.16/12, 192.168/16) or 127.0.0.1 only.
  * No secrets are written to disk or logged; the DSN is never printed.

WORKER LIVENESS SOURCE (per the task's explicit instruction):
  The apply workers DO write worker_heartbeat. WorkerLoop._beat -> worker.py:_heartbeat
  UPSERTs worker_heartbeat with last_beat=now() on EVERY tick (idle, applying, etc.), and
  apply_worker_main builds a WorkerLoop(role='apply'). So liveness here is REAL, derived
  from worker_heartbeat.last_beat: alive = last_beat within 150s. We filter to role='apply'
  (the LinkedIn lane is excluded from the controllable view) and join current_job (a url)
  back to apply_queue for the company/title of the job a worker is on.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hmac
import http.cookies
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from psycopg import errors

from applypilot.apply import pgqueue
from applypilot.fleet import apply_home_main as H
from applypilot.fleet import codex_bridge as cb
from applypilot.fleet import console_machines
from applypilot.fleet import queue as _queue
from applypilot.fleet.worker import _scrub  # reuse the worker's secret redactor (S1: scrub on READ too)

DEFAULT_PORT = 8787
_HB_ALIVE_SECONDS = 150          # apply workers beat ~every tick; >150s stale = dead
_INFERRED_WINDOW_SECONDS = 300   # fallback only (unused: apply workers DO beat)
_LOG_LAST_ERROR_CAP = 4000       # mirror the worker write caps on read (S3)
_LOG_RECENT_CAP = 8000


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _seconds_since(ts: Any) -> int | None:
    if ts is None or not hasattr(ts, "tzinfo"):
        return None
    now = _utcnow()
    t = ts if ts.tzinfo is not None else ts.replace(tzinfo=_dt.timezone.utc)
    return int((now - t).total_seconds())


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _git_text(args: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=_repo_root(),
            text=True,
            capture_output=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _deployment_info(conn) -> dict[str, Any]:
    """Read-only console/schema/version facts for the top dashboard banner."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='worker_heartbeat' "
            "AND column_name IN ('current_agent','current_model','agent_chain',"
            "'last_agent_switch_at','last_agent_switch_reason')"
        )
        telemetry_cols = {r["column_name"] for r in cur.fetchall()}
        cur.execute("SELECT to_regclass('public.fleet_console_audit') AS audit_table")
        audit_table = cur.fetchone()["audit_table"]
        cur.execute(
            "SELECT COALESCE(sw_version, '(unknown)') AS version, COUNT(*) AS workers "
            "FROM worker_heartbeat GROUP BY 1 ORDER BY 1"
        )
        worker_versions = [
            {"version": r["version"], "workers": int(r["workers"] or 0)}
            for r in cur.fetchall()
        ]
    conn.rollback()
    return {
        "console": {
            "branch": _git_text(["rev-parse", "--abbrev-ref", "HEAD"]) or "unknown",
            "commit": _git_text(["rev-parse", "--short", "HEAD"]) or "unknown",
            "path": str(_repo_root()),
        },
        "schema": {
            "agent_telemetry": {
                "current_agent",
                "current_model",
                "agent_chain",
                "last_agent_switch_at",
                "last_agent_switch_reason",
            }.issubset(telemetry_cols),
            "audit_table": bool(audit_table),
        },
        "worker_versions": worker_versions,
    }


# ---------------------------------------------------------------------------
# READ: status snapshot (all queries via short-lived connection)
# ---------------------------------------------------------------------------
def _gate_and_queue(conn) -> tuple[dict, dict]:
    """fleet_config gate + apply_queue depth + leasable + spend, in one read txn.

    leasable mirrors queue.lease_apply's eligibility EXACTLY: status=queued AND
    approved_batch IS NOT NULL AND NOT EXISTS(applied_set with same dedup_key).
    spent_usd = SUM(est_cost_usd) over apply_queue (same SUM the cap halt uses)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT paused, canary_enabled, canary_remaining, spend_cap_usd "
            "FROM fleet_config WHERE id=1"
        )
        cfg = cur.fetchone() or {}

        cur.execute("SELECT COALESCE(SUM(est_cost_usd),0) AS s FROM apply_queue")
        spent = float(cur.fetchone()["s"])

        cur.execute("SELECT status, COUNT(*) AS n FROM apply_queue GROUP BY status")
        depth = {r["status"]: int(r["n"]) for r in cur.fetchall()}

        cur.execute(
            "SELECT COUNT(*) AS n FROM apply_queue q "
            "WHERE q.status='queued' AND q.approved_batch IS NOT NULL "
            "  AND NOT EXISTS (SELECT 1 FROM applied_set a WHERE a.dedup_key = q.dedup_key)"
        )
        leasable = int(cur.fetchone()["n"])
    conn.rollback()  # read-only

    gate = {
        "paused": bool(cfg.get("paused")),
        "canary_enabled": bool(cfg.get("canary_enabled")),
        "canary_remaining": (int(cfg["canary_remaining"])
                             if cfg.get("canary_remaining") is not None else None),
        "spend_cap_usd": float(cfg.get("spend_cap_usd") or 0),
        "spent_usd": spent,
        "leasable": leasable,
    }
    queue = {
        "apply": {
            "queued": depth.get("queued", 0),
            "leased": depth.get("leased", 0),
            "applied": depth.get("applied", 0),
            "failed": depth.get("failed", 0) + depth.get("blocked", 0),
            "crash_unconfirmed": depth.get("crash_unconfirmed", 0),
        }
    }
    return gate, queue


def _workers(conn) -> list[dict]:
    """Per-apply-worker liveness from worker_heartbeat (REAL: apply workers beat every
    tick). alive = last_beat within 150s. current_job (a url) is joined to apply_queue
    for the company/title.

    The applied count is derived from AUTHORITATIVE apply_queue rows
    (status='applied' AND worker_id = this worker), NOT worker_heartbeat.success_today:
    the apply path (WorkerLoop._beat -> worker._heartbeat) never writes success_today,
    so it would be a misleading permanent 0. apply_queue.worker_id is set on every
    terminal write by pgqueue.write_result, so this count is real."""
    out: list[dict] = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT wh.worker_id, wh.machine_owner, wh.state, wh.role, wh.current_job, "
            "       wh.sw_version, wh.last_beat, aq.company AS cur_company, aq.title AS cur_title, "
            "       (SELECT COUNT(*) FROM apply_queue done "
            "          WHERE done.worker_id = wh.worker_id "
            "            AND done.status = 'applied') AS applied_n "
            "FROM worker_heartbeat wh "
            "LEFT JOIN apply_queue aq ON aq.url = wh.current_job "
            "WHERE wh.role = 'apply' "
            "ORDER BY wh.worker_id"
        )
        rows = cur.fetchall()
    conn.rollback()  # read-only
    for r in rows:
        secs = _seconds_since(r["last_beat"])
        alive = secs is not None and secs <= _HB_ALIVE_SECONDS
        current = None
        if r["current_job"] and r["state"] == "applying":
            current = {"company": r["cur_company"], "title": r["cur_title"]}
        machine_owner = console_machines.infer_machine_owner(
            r["worker_id"], r.get("machine_owner")
        )
        out.append({
            "worker_id": r["worker_id"],
            "machine_owner": machine_owner,
            "machine_display_name": console_machines.display_name(machine_owner),
            "alive": bool(alive),
            "health": "alive" if alive else "stale",
            "last_beat": _iso(r["last_beat"]),
            "seconds_since": secs,
            "role": r["role"],
            "state": r["state"],
            "lane": r["role"],
            "sw_version": r.get("sw_version"),
            "applied": int(r["applied_n"] or 0),
            "current": current,
        })
    return out


def _recent(conn, limit: int = 15) -> list[dict]:
    """Newest-first recent terminal events from cb.recent_results, flattened to the
    panel's row shape (apply lane only carries company/title detail)."""
    res = cb._recent_results(conn, limit)  # rolls back internally; reuses the bridge query
    rows: list[dict] = []
    for r in res.get("results", []):
        detail = r.get("detail") or {}
        rows.append({
            "time": r.get("finished_at"),
            "worker": detail.get("worker_id"),  # apply rows don't carry a worker; stays None
            "status": r.get("status"),
            "company": detail.get("company"),
            "title": detail.get("title"),
            "lane": r.get("lane"),
        })
    return rows[:limit]


def _linkedin_summary(conn) -> dict:
    """READ-ONLY LinkedIn rollup. halted = account:linkedin governor halted_until in the
    future. There is intentionally NO write path for any of this."""
    with conn.cursor() as cur:
        cur.execute("SELECT status, COUNT(*) AS n FROM linkedin_queue GROUP BY status")
        depth = {r["status"]: int(r["n"]) for r in cur.fetchall()}
        cur.execute(
            "SELECT linkedin_canary_enabled FROM fleet_config WHERE id=1"
        )
        cfg = cur.fetchone() or {}
        cur.execute(
            "SELECT halted_until FROM rate_governor WHERE scope_key='account:linkedin'"
        )
        gov = cur.fetchone()
    conn.rollback()  # read-only
    halted = False
    if gov and gov.get("halted_until") is not None:
        secs = _seconds_since(gov["halted_until"])
        halted = secs is not None and secs < 0  # halted_until still in the future
    return {
        "queued": depth.get("queued", 0),
        "applied": depth.get("applied", 0),
        "canary_enabled": bool(cfg.get("linkedin_canary_enabled")),
        "halted": bool(halted),
    }


def _discovery(conn) -> dict:
    """Discovery-lane status (READ-ONLY, PG-only — never touches the SQLite brain):
    the search_tasks work-list, the discovered_postings staging area, and discovery-worker
    liveness (worker_heartbeat role='discovery', alive = beat within 150s)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS total, count(*) FILTER (WHERE enabled) AS enabled, "
            "       count(*) FILTER (WHERE enabled AND next_due_at IS NOT NULL "
            "                        AND next_due_at <= now()) AS due_now "
            "FROM search_tasks"
        )
        t = dict(cur.fetchone())
        cur.execute(
            "SELECT count(*) AS total, "
            "       count(*) FILTER (WHERE synced_to_home_at IS NULL) AS pending, "
            "       count(*) FILTER (WHERE discovered_at > now() - interval '24 hours') AS last24h "
            "FROM discovered_postings"
        )
        d = dict(cur.fetchone())
        cur.execute(
            "SELECT wh.worker_id, wh.state, wh.last_beat, "
            "       (SELECT COUNT(*) FROM discovered_postings dp "
            "          WHERE dp.worker_id = wh.worker_id "
            "            AND dp.discovered_at > now() - interval '24 hours') AS found_24h "
            "FROM worker_heartbeat wh WHERE wh.role = 'discovery' ORDER BY wh.worker_id"
        )
        wrows = cur.fetchall()
        cur.execute(
            "SELECT dp.discovered_at, dp.source_label, st.query, st.board "
            "FROM discovered_postings dp LEFT JOIN search_tasks st ON st.task_id = dp.task_id "
            "ORDER BY dp.discovered_at DESC LIMIT 10"
        )
        rrows = cur.fetchall()
    conn.rollback()  # read-only
    workers = []
    for r in wrows:
        secs = _seconds_since(r["last_beat"])
        workers.append({
            "worker_id": r["worker_id"],
            "alive": bool(secs is not None and secs <= _HB_ALIVE_SECONDS),
            "last_beat": _iso(r["last_beat"]),
            "seconds_since": secs,
            "state": r["state"],
            "found_24h": int(r["found_24h"] or 0),
        })
    recent = [{
        "time": _iso(r["discovered_at"]),
        "source": r["source_label"],
        "query": r["query"],
        "board": r["board"],
    } for r in rrows]
    return {
        "tasks": {"total": int(t["total"] or 0), "enabled": int(t["enabled"] or 0),
                  "due_now": int(t["due_now"] or 0)},
        "postings": {"total": int(d["total"] or 0), "pending_ingest": int(d["pending"] or 0),
                     "last24h": int(d["last24h"] or 0)},
        "workers": workers,
        "recent": recent,
    }


def _diagnostics(conn) -> dict:
    """READ-ONLY Fleet Doctor surface (its own endpoint, NOT in the 4s /api/status poll):
      ``clusters``        -- live failure clusters over the rolling window (analyze())
      ``auto_fixes``      -- ACTIVE fleet_knobs joined to their recent auto_applied diagnoses
                             (type, scope, reason, expires, knob_id, diagnosis_id, how_to_reverse)
      ``recommendations`` -- status='recommended' fleet_diagnoses rows (human queue)

    All free text is re-scrubbed on read (defense in depth -- S1) and sizes are capped. The
    Doctor already scrubbed before storing; we scrub AGAIN here. LinkedIn never appears: the
    Doctor only ever clusters / acts on the apply (ats) lane."""
    from applypilot.fleet import doctor as _doctor
    try:
        clustered = _doctor.analyze(conn, window_minutes=60)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        clustered = {"cluster_rows": []}
    clusters = []
    for c in (clustered.get("cluster_rows") or [])[:50]:
        clusters.append({
            "reason": c.get("reason"),
            "host": _scrub(c.get("host"))[:200],
            "machine": _scrub(c.get("machine"))[:120],
            "lane": c.get("lane"),
            "sample_count": int(c.get("sample_count") or 0),
        })
    with conn.cursor() as cur:
        # Active knobs + the most recent auto-applied diagnosis for the same scope (for the
        # Reverse button + how_to_reverse text). LEFT JOIN LATERAL keeps it one row per knob.
        # H18: the join is CONSTRAINED TO THE KNOB'S SCOPE (not just the auto_action type prefix),
        # so with two active host_skips each knob shows ITS OWN diagnosis -- the old type-prefix-only
        # match showed the most-recent diagnosis text for ALL knobs of a type (operator could Reverse
        # on misleading evidence). Per type: host_skip -> d2.host=scope; pace -> d2.host=substr(scope,6);
        # timeout_bump/pause -> d2.lane=scope; quarantine -> d2.host=knob host (url has no diag host,
        # fall back to type-only via the OR).
        cur.execute(
            "SELECT k.id AS knob_id, k.knob_type, k.scope_key, k.value_text, k.reason, "
            "       k.created_at, k.expires_at, d.id AS diagnosis_id, d.how_to_reverse, d.diagnosis "
            "FROM fleet_knobs k "
            "LEFT JOIN LATERAL ("
            "   SELECT id, how_to_reverse, diagnosis FROM fleet_diagnoses d2 "
            "   WHERE d2.auto_action LIKE k.knob_type || '%' AND d2.status='auto_applied' "
            "     AND ( "
            "       (k.knob_type='host_skip'    AND d2.host = "
            "           CASE WHEN k.scope_key LIKE 'host:%' THEN substr(k.scope_key, 6) ELSE k.scope_key END) "
            "       OR (k.knob_type='pace_or_pause' AND k.scope_key LIKE 'host:%' "
            "           AND d2.host = substr(k.scope_key, 6)) "
            "       OR (k.knob_type='pace_or_pause' AND k.scope_key='ats' AND d2.lane = k.scope_key) "
            "       OR (k.knob_type='timeout_bump' AND d2.lane = k.scope_key) "
            "       OR (k.knob_type='quarantine') "
            "     ) "
            "   ORDER BY d2.created_at DESC LIMIT 1"
            ") d ON TRUE "
            "WHERE k.active AND (k.expires_at IS NULL OR k.expires_at > now()) "
            "ORDER BY k.created_at DESC LIMIT 100"
        )
        krows = cur.fetchall()
        cur.execute(
            "SELECT id, cluster_key, reason, host, machine, lane, sample_count, severity, "
            "       diagnosis, recommendation, created_at "
            "FROM fleet_diagnoses WHERE status='recommended' "
            "ORDER BY created_at DESC LIMIT 100"
        )
        rrows = cur.fetchall()
        # H11/H15: quarantined URLs surfaced INDEPENDENT of fleet_knobs, so a still-quarantined url
        # whose audit knob already expired (the 7-day knob TTL only expires the audit row, not the
        # permanent quarantine) is still visible + has a reversible path. manual: reasons only.
        cur.execute(
            "SELECT url, reason, quarantined_at FROM poison_jobs "
            "WHERE quarantined_at IS NOT NULL AND reason LIKE 'manual:%' "
            "ORDER BY quarantined_at DESC LIMIT 100"
        )
        qrows = cur.fetchall()
    conn.rollback()  # read-only
    auto_fixes = [{
        "knob_id": r["knob_id"],
        "diagnosis_id": r["diagnosis_id"],
        "knob_type": r["knob_type"],
        "scope_key": _scrub(r.get("scope_key"))[:300],
        "value_text": _scrub(r.get("value_text"))[:200],
        "reason": _scrub(r.get("reason"))[:200],
        "created_at": _iso(r.get("created_at")),
        "expires_at": _iso(r.get("expires_at")),
        "how_to_reverse": _scrub(r.get("how_to_reverse"))[:400],
    } for r in krows]
    recommendations = [{
        "diagnosis_id": r["id"],
        "reason": r.get("reason"),
        "host": _scrub(r.get("host"))[:200],
        "machine": _scrub(r.get("machine"))[:120],
        "lane": r.get("lane"),
        "sample_count": int(r.get("sample_count") or 0),
        "severity": r.get("severity"),
        "diagnosis": _scrub(r.get("diagnosis"))[:600],
        "recommendation": _scrub(r.get("recommendation"))[:600],
        "created_at": _iso(r.get("created_at")),
    } for r in rrows]
    quarantined = [{
        "url": _scrub(r.get("url"))[:300],
        "reason": _scrub(r.get("reason"))[:200],
        "quarantined_at": _iso(r.get("quarantined_at")),
    } for r in qrows]
    return {"clusters": clusters, "auto_fixes": auto_fixes,
            "recommendations": recommendations, "quarantined": quarantined}


def _doctor_signal(conn) -> dict:
    """H18: a SMALL, above-the-fold Doctor signal folded into the fast /api/status poll (NOT the
    heavy diagnostics blob): last_pass_at, the active auto-fix count, and the newest active
    auto-fix timestamp/id so the frontend can show a Doctor card + fire a toast on a NEW auto-fix.
    Also surfaces ats_paused provenance (H8) so the banner can label a Doctor pause vs operator."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT doctor_last_pass_at, ats_paused, ats_pause_source, "
            "deadman_alert, deadman_alert_at FROM fleet_config WHERE id=1")
        cfg = cur.fetchone() or {}
        cur.execute(
            "SELECT count(*) AS n, max(created_at) AS newest, max(id) AS newest_id "
            "FROM fleet_knobs WHERE active AND (expires_at IS NULL OR expires_at > now())")
        k = cur.fetchone() or {}
    conn.rollback()  # read-only
    return {
        "last_pass_at": _iso(cfg.get("doctor_last_pass_at")),
        "active_auto_fix_count": int(k.get("n") or 0),
        "newest_auto_action_at": _iso(k.get("newest")),
        "newest_auto_fix_id": k.get("newest_id"),
        "ats_paused": bool(cfg.get("ats_paused")),
        "ats_pause_source": cfg.get("ats_pause_source"),
        "deadman_alert": cfg.get("deadman_alert"),
        "deadman_alert_at": _iso(cfg.get("deadman_alert_at")),
    }


def diagnostics() -> dict:
    """Open a short-lived connection, build the Doctor surface, always close."""
    conn = pgqueue.connect()
    try:
        return _diagnostics(conn)
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()


def _outcomes(conn, limit: int = 25) -> list[dict]:
    """READ-ONLY recent inbox_outcomes (newest first). Parameterized LIMIT only (S5);
    free text re-scrubbed + capped (S1/S3); timestamps ISO-serialized; conn.rollback()
    marks it read-only."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT message_id, company, title, stage, outcome, sender_domain, occurred_at "
            "FROM inbox_outcomes ORDER BY occurred_at DESC NULLS LAST LIMIT %s",
            (int(limit),),
        )
        rows = cur.fetchall()
    conn.rollback()  # read-only
    return [{
        "message_id": r.get("message_id"),
        "time": _iso(r.get("occurred_at")),
        "company": _scrub(r.get("company") or "")[:200],
        "title": _scrub(r.get("title") or "")[:200],
        "stage": r.get("stage"),
        "outcome": r.get("outcome"),
        "sender_domain": r.get("sender_domain"),
    } for r in rows]


def outcomes() -> dict:
    """Open a short-lived connection, read recent outcomes, always close."""
    conn = pgqueue.connect()
    try:
        return {"outcomes": _outcomes(conn)}
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()


def build_status() -> dict:
    """Assemble the full /api/status object. Opens its OWN short-lived connections
    (each helper rolls back and we always close) so no connection is ever leaked."""
    conn = pgqueue.connect()  # reads APPLYPILOT_FLEET_DSN / DATABASE_URL
    try:
        gate, queue = _gate_and_queue(conn)
        try:
            from applypilot.fleet import console_diagnosis

            fleet_diagnosis = console_diagnosis.queue_diagnosis(conn)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            fleet_diagnosis = None
        try:
            gate["should_halt"] = bool(pgqueue.should_halt(conn))
        except Exception:
            gate["should_halt"] = gate["paused"]
        workers = _workers(conn)
        recent = _recent(conn, 15)
        ch = cb._challenges(conn)
        challenges = len(ch.get("challenges", []))
        linkedin = _linkedin_summary(conn)
        try:
            doctor_sig = _doctor_signal(conn)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            doctor_sig = None
        try:
            discovery = _discovery(conn)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            discovery = None
        try:
            deployment = _deployment_info(conn)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            deployment = None
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
    return {
        "now": _utcnow().isoformat(),
        "gate": gate,
        "queue": queue,
        "workers": workers,
        "recent": recent,
        "challenges": challenges,
        "linkedin": linkedin,
        "doctor": doctor_sig,
        "discovery": discovery,
        "deployment": deployment,
        "fleet_diagnosis": fleet_diagnosis,
        # H19: the DeadMan alert (fleet_config.deadman_alert/_at, written by
        # applypilot.fleet.deadman's run_deadman) surfaced top-level -- NOT nested
        # under "doctor" -- so a silent fleet death/stall/running-hot is visible
        # even when the Doctor signal itself failed to load.
        "deadman_alert": (doctor_sig or {}).get("deadman_alert"),
        "deadman_alert_at": (doctor_sig or {}).get("deadman_alert_at"),
    }


# ---------------------------------------------------------------------------
# READ: /api/challenges -- unions open auth_challenge rows with PARKED rows
# (apply_status='challenge_pending') from BOTH lanes, grouped kind -> host, so the
# operator can see the whole captcha/OTP backlog (including rows a worker parked
# but that never raised/kept an auth_challenge row) in one place.
# ---------------------------------------------------------------------------
# _host_of is the console's name for the shared queue.host_of helper (kept as a
# module-level alias so this module's other call sites need no change). The
# single host/kind derivation lives in fleet/queue.py so this detail view and
# the CLI `challenges --grouped` count view (queue.challenge_summary) on both
# home mains can never diverge.
_host_of = _queue.host_of


def _age_hours(ts: Any, now: _dt.datetime) -> float | None:
    if ts is None or not hasattr(ts, "tzinfo"):
        return None
    t = ts if ts.tzinfo is not None else ts.replace(tzinfo=_dt.timezone.utc)
    return round((now - t).total_seconds() / 3600.0, 1)


def _challenges_data(conn) -> dict:
    """READ-ONLY: open auth_challenge rows + parked rows from apply_queue/linkedin_queue,
    unioned by url, grouped kind -> host. Parked rows with no matching open challenge
    group under kind '(no challenge row)'. Fetched via the SHARED queue._challenge_rows
    (lane=None -- both queues) so this detail view and queue.challenge_summary's count
    view can never diverge on what counts as an open/parked row."""
    now = _utcnow()
    open_challenges, parked_by_url = _queue._challenge_rows(conn, None)

    challenge_by_url: dict[str, dict] = {r["url"]: r for r in open_challenges}

    urls = set(challenge_by_url) | set(parked_by_url)
    groups: dict[tuple[str, str], list[dict]] = {}
    for url in urls:
        ch = challenge_by_url.get(url)
        lane, park = parked_by_url.get(url, (None, None))
        kind = ch["kind"] if ch else "(no challenge row)"
        host = _host_of(url)
        raised_at = ch["raised_at"] if ch else None
        updated_at = park["updated_at"] if park else None
        row = {
            "url": url,
            "lane": lane,
            "company": park["company"] if park else None,
            "title": park["title"] if park else None,
            "score": park["score"] if park else None,
            "kind": kind,
            "machine": ch["machine_owner"] if ch else None,
            "screenshot_url": ch["screenshot_url"] if ch else None,
            "age_hours": _age_hours(raised_at if ch else updated_at, now),
        }
        groups.setdefault((kind, host), []).append(row)

    return {
        "groups": [
            {"kind": kind, "host": host, "rows": rows}
            for (kind, host), rows in groups.items()
        ],
        "counts": {
            "open_challenges": len(open_challenges),
            "parked": len(parked_by_url),
        },
    }


def build_challenges() -> dict:
    """Open a short-lived connection, build the /api/challenges payload, always close."""
    conn = pgqueue.connect()  # reads APPLYPILOT_FLEET_DSN / DATABASE_URL
    try:
        return _challenges_data(conn)
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()


# ---------------------------------------------------------------------------
# READ: per-worker crash + log tail (separate endpoint; NOT in the 4s /api/status
# poll, so the big text blobs never bloat it). The worker already scrubbed this text
# before storing it; we scrub AGAIN on read (defense in depth -- S1) and cap sizes (S3).
# ---------------------------------------------------------------------------
def _worker_logs(conn, worker_id: str) -> dict | None:
    """Return {worker_id, last_beat, last_error, recent_log} for one worker, or None if
    no such worker_heartbeat row exists (handler -> 404). The query is PARAMETERIZED
    (never SQL built from request input -- S5). Text is re-scrubbed + size-capped."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT worker_id, last_beat, last_error, recent_log "
            "FROM worker_heartbeat WHERE worker_id = %s",
            (worker_id,),
        )
        row = cur.fetchone()
    conn.rollback()  # read-only
    if row is None:
        return None
    last_error = _scrub(row.get("last_error"))[:_LOG_LAST_ERROR_CAP]
    recent_log = _scrub(row.get("recent_log"))
    if len(recent_log) > _LOG_RECENT_CAP:
        recent_log = recent_log[-_LOG_RECENT_CAP:]
    return {
        "worker_id": row["worker_id"],
        "last_beat": _iso(row.get("last_beat")),
        "last_error": last_error,
        "recent_log": recent_log,
    }


def worker_logs(worker_id: str) -> dict | None:
    """Open a short-lived connection, read one worker's logs, always close. None -> 404."""
    conn = pgqueue.connect()
    try:
        return _worker_logs(conn, worker_id)
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()


# ---------------------------------------------------------------------------
# WRITE: the strict action allow-list. NO LinkedIn action exists here by design.
# ---------------------------------------------------------------------------
def _do_arm_canary(conn, body: dict) -> str:
    k = body.get("k")
    if not isinstance(k, int) or isinstance(k, bool) or k < 1 or k > 200:
        raise ValueError("arm_canary requires integer k in [1,200]")
    H.set_canary(conn, k)
    return f"Canary armed: up to {k} real applications will be submitted, then auto-pause."


def _do_lift_canary(conn, body: dict) -> str:
    # "Lift canary" must mean RUN UNBOUNDED: lift_canary alone does NOT unpause, so we
    # also clear the pause here (per the task contract).
    H.lift_canary(conn)
    pgqueue.set_paused(conn, False)
    return "Canary lifted: the fleet will now apply UNBOUNDED (no cap on count)."


def _do_pause(conn, body: dict) -> str:
    pgqueue.set_paused(conn, True)
    return "Fleet PAUSED: no new applications will be leased."


def _do_resume(conn, body: dict) -> str:
    pgqueue.set_paused(conn, False)
    return "Fleet RESUMED: leasing is enabled again."


def _do_reclaim(conn, body: dict) -> str:
    rows = pgqueue.reclaim_stale_leases(conn)  # THE crash-safe reclaim; never a raw UPDATE
    return f"Reclaimed {len(rows)} stale lease(s)."


def _do_set_cap(conn, body: dict) -> str:
    usd = body.get("usd")
    # Reject NaN/Infinity explicitly: `usd < 0` is False for BOTH NaN (all NaN
    # comparisons are False) and +inf, so the old guard let them through and
    # silently DISABLED the spend rail (should_halt's `spend_cap_usd > 0` is
    # False for NaN -> treated as "no cap"; +inf would 500 on NUMERIC(10,2)).
    if (isinstance(usd, bool) or not isinstance(usd, (int, float))
            or not math.isfinite(usd) or usd < 0):
        raise ValueError("set_cap requires a finite number usd >= 0")
    pgqueue.set_spend_cap(conn, float(usd))
    cap_txt = "no cap" if usd == 0 else f"${float(usd):.2f}"
    return f"Spend cap set to {cap_txt}."


def _searches_config_path() -> str:
    """Locate the project's searches config. Prefer APPLYPILOT_DIR (the launcher sets it),
    else <repo>/.applypilot/searches.yaml. Raises if absent."""
    candidates = []
    app_dir = os.environ.get("APPLYPILOT_DIR")
    if app_dir:
        candidates.append(os.path.join(app_dir, "searches.yaml"))
    repo_root = Path(__file__).resolve().parents[3]  # fleet -> applypilot -> src -> repo
    candidates.append(str(repo_root / ".applypilot" / "searches.yaml"))
    for p in candidates:
        if os.path.isfile(p):
            return p
    raise FileNotFoundError("searches.yaml not found (set APPLYPILOT_DIR or place it in .applypilot/)")


def _apply_searches_to_fleet(cfg: dict) -> dict:
    """Translate the apply tool's searches.yaml (queries/locations/sites) into the fleet
    discovery scheduler's {'searches':[...]} shape. A config that already has 'searches' is
    returned unchanged.

    SAFETY: LinkedIn is EXCLUDED from discovery boards. The home box holds the LinkedIn apply
    session (the catastrophe lane); anonymous LinkedIn scraping from that IP could correlate
    and endanger it. Discovery scrapes the non-LinkedIn boards (e.g. indeed) only."""
    if cfg.get("searches"):
        return cfg  # already fleet-native
    queries = []
    for q in (cfg.get("queries") or []):
        val = q.get("query") if isinstance(q, dict) else q
        if val:
            queries.append(val)
    boards = [s for s in (cfg.get("sites") or ["indeed"]) if s != "linkedin"] or ["indeed"]
    locations = []
    for loc in (cfg.get("locations") or []):
        if isinstance(loc, dict) and loc.get("location"):
            locations.append(loc["location"])
        elif isinstance(loc, str) and loc:
            locations.append(loc)
    if "Remote" not in locations:
        locations.append("Remote")
    if not locations:
        locations = ["Remote"]
    defaults = cfg.get("defaults") or {}
    params = {
        "results_wanted": int(defaults.get("results_per_site", 50)),
        "hours_old": int(defaults.get("hours_old", 72)),
    }
    searches = [{"query": q, "boards": boards, "locations": locations, "params": params}
                for q in queries]
    return {"searches": searches}


def _do_expand_searches(conn, body: dict) -> str:
    """Seed/refresh search_tasks from searches.yaml (PG-only — does NOT write the brain and
    does NOT submit any application). Calls the scheduler directly so the console never has to
    import the JobSpy-heavy discovery worker module."""
    from applypilot.fleet.scheduler import expand_search_config  # lazy: keeps the console light
    path = _searches_config_path()
    if path.endswith((".yaml", ".yml")):
        import yaml
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    else:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    config = _apply_searches_to_fleet(raw)
    n = expand_search_config(conn, config)
    conn.commit()  # run_action rolls back in finally, so write actions must commit themselves
    roles = len(config.get("searches") or [])
    return (f"Seeded/refreshed {n} search task(s) across {roles} roles "
            f"(LinkedIn excluded from discovery for safety). Discovery workers will pick them up.")


# ---------------------------------------------------------------------------
# FLEET DOCTOR controls (exactly two, both CONSERVATIVE / bookkeeping). These are the
# ONLY new actions added to the allow-list. Reversing a Doctor knob is conservative-or-
# neutral (clear host_skip = nothing else re-approved; clear timeout_override = back to
# NULL/default; deactivate a pace/pause knob = audit-only, the human still resumes
# explicitly). Dismissing a recommendation is pure bookkeeping. There is deliberately NO
# action that APPLIES a recommendation or does anything activity-increasing -- those stay
# manual via the existing controls. NO LinkedIn action exists here.
# ---------------------------------------------------------------------------
def _do_doctor_revert(conn, body: dict) -> str:
    """Reverse a single Doctor auto-fix: deactivate its fleet_knobs row, undo the knob's
    conservative-or-neutral side (clear the timeout override / restore the paced gap to its
    base), and mark its diagnosis 'reverted'. Identified by knob_id (preferred) or
    diagnosis_id. Parameterized SQL only. NEVER re-approves jobs, never resumes a pause, never
    raises a cap -- a revert can only relax a conservative knob, which is itself safe."""
    knob_id = body.get("knob_id")
    diagnosis_id = body.get("diagnosis_id")
    if not _is_pos_int(knob_id) and not _is_pos_int(diagnosis_id):
        raise ValueError("doctor_revert requires a positive integer knob_id or diagnosis_id")
    with conn.cursor() as cur:
        if _is_pos_int(knob_id):
            cur.execute("SELECT id, knob_type, scope_key, value_text FROM fleet_knobs WHERE id=%s AND active",
                        (int(knob_id),))
        else:
            # Resolve the active knob for this diagnosis via the auto_action prefix + scope.
            cur.execute(
                "SELECT k.id, k.knob_type, k.scope_key, k.value_text FROM fleet_knobs k "
                "JOIN fleet_diagnoses d ON d.auto_action LIKE k.knob_type || '%' "
                "WHERE d.id=%s AND k.active "
                "ORDER BY k.created_at DESC LIMIT 1",
                (int(diagnosis_id),),
            )
        knob = cur.fetchone()
        if knob is None:
            raise ValueError("no active Doctor knob found to revert")
        ktype, scope, vtext = knob["knob_type"], knob["scope_key"], knob.get("value_text")
        # Deactivate the knob (audit twin stays for history; active=FALSE removes its effect).
        cur.execute("UPDATE fleet_knobs SET active=FALSE WHERE id=%s", (knob["id"],))
        # Undo the knob's mechanical side -- each branch is conservative or neutral. H11: the
        # quarantine + host_skip branches now ACTUALLY clear the underlying state (the old
        # comment-only fall-through made those Reverse buttons lies).
        if ktype == "timeout_bump":
            # H7: restore the PRE-bump override captured in value_text ('new|prev'), not an
            # unconditional NULL -- preserving an operator-set override. Restore only if no OTHER
            # active timeout_bump remains (single shared column).
            prev = None
            if vtext and "|" in str(vtext):
                tail = str(vtext).split("|", 1)[1].strip()
                prev = int(tail) if tail.isdigit() else None
            cur.execute("SELECT count(*) AS n FROM fleet_knobs WHERE active AND knob_type='timeout_bump'")
            if int(cur.fetchone()["n"]) == 0:
                cur.execute("UPDATE fleet_config SET agent_timeout_override=%s, updated_at=now() WHERE id=1",
                            (prev,))
        elif ktype == "pace_or_pause":
            if scope and scope.startswith("host:"):
                # H4: a host pace lived in the Doctor-owned doctor_min_gap_floor; clearing it lets
                # the host run at its breaker-owned gap again. (We never touch min_gap_seconds --
                # that is the watchdog breaker's column.)
                cur.execute(
                    "UPDATE rate_governor SET doctor_min_gap_floor=NULL, updated_at=now() WHERE scope_key=%s",
                    (scope,))
            elif scope == "ats":
                # H8: a Doctor-authored ATS pause (ats_pause_source='doctor') -- Reverse DOES clear
                # it (the old Reverse was a no-op so it looked broken). NEVER clears an operator/cost
                # ats pause. This only ever touches ats_paused, never the shared fleet_config.paused,
                # so it cannot resume the LinkedIn lane.
                cur.execute(
                    "UPDATE fleet_config SET ats_paused=FALSE, ats_pause_source=NULL, "
                    "doctor_pause_armed_at=NULL, updated_at=now() "
                    "WHERE id=1 AND ats_pause_source='doctor'")
        elif ktype == "host_skip":
            # H6/H11: a host_skip lived in rate_governor.doctor_skip_until on the host scope.
            # Clearing it lets the host lease again immediately (approval was preserved -- nothing
            # to re-approve). This is the REAL undo the how_to_reverse text promises.
            cur.execute(
                "UPDATE rate_governor SET doctor_skip_until=NULL, updated_at=now() WHERE scope_key=%s",
                ("host:" + scope if scope and not scope.startswith("host:") else scope,))
        elif ktype == "quarantine":
            # H11: actually clear poison_jobs.quarantined_at for this url (manual: reasons only --
            # never un-quarantine a real crash-accumulated poison). The job is retained, never deleted.
            cur.execute(
                "UPDATE poison_jobs SET quarantined_at=NULL, reviewed=TRUE "
                "WHERE url=%s AND reason LIKE 'manual:%%'", (scope,))
        # Mark the audit diagnosis 'reverted'. If the caller named a diagnosis_id, mark exactly
        # that row. Otherwise (the UI's knob_id path) match the audit rows by the knob's
        # IDENTITY -- the same auto_action prefix the _diagnostics LATERAL join uses -- scoped to
        # the host/lane the diagnosis actually carries for this knob_type. The OLD COALESCE(host,
        # lane,'') = COALESCE(scope,...) match was a NO-OP for production-shaped diagnoses:
        # timeout_bump carries host=<triggering host>+lane='ats' but knob scope='ats' ('host'!='ats'),
        # and pace carries host=<host> but knob scope='host:'||<host> -- neither matched, leaving the
        # row stuck in 'auto_applied' (a phantom active auto-fix; corrupts the D3 audit trail).
        if _is_pos_int(diagnosis_id):
            cur.execute(
                "UPDATE fleet_diagnoses SET status='reverted', updated_at=now() "
                "WHERE id=%s AND status IN ('auto_applied','open')", (int(diagnosis_id),))
        else:
            # Per-knob-type scoping of the audit rows this knob produced:
            #   host_skip   -> diagnosis.host == knob.scope (the host)
            #   timeout_bump-> diagnosis.lane == knob.scope ('ats'); host is the *triggering* host
            #   pace_or_pause(pace)  -> knob.scope='host:'||<host>, diagnosis.host==<host>
            #   pace_or_pause(pause) -> knob.scope='ats', diagnosis.lane=='ats'
            if ktype == "host_skip":
                # A5: the host_skip knob scope is now canonical 'host:<h>'; the diagnosis carries the
                # BARE host, so strip the prefix to match.
                bare = scope[len("host:"):] if scope and scope.startswith("host:") else scope
                where, params = "host = %s", (bare,)
            elif ktype == "timeout_bump":
                where, params = "lane = %s", (scope,)
            elif ktype == "pace_or_pause" and scope and scope.startswith("host:"):
                where, params = "host = %s", (scope[len("host:"):],)
            else:  # pace_or_pause pause (scope='ats') or any lane-scoped knob
                where, params = "lane = %s", (scope,)
            cur.execute(
                "UPDATE fleet_diagnoses SET status='reverted', updated_at=now() "
                "WHERE status IN ('auto_applied','open') AND auto_action LIKE %s || '%%' AND " + where,
                (ktype, *params),
            )
    conn.commit()  # run_action rolls back in finally, so write actions must commit themselves
    return f"Reverted Doctor {ktype} (scope {scope!r}). The knob is deactivated; nothing was re-approved or resumed."


def _do_doctor_dismiss(conn, body: dict) -> str:
    """Dismiss a Doctor RECOMMENDATION (status -> 'dismissed'). Pure bookkeeping: it does NOT
    apply the recommendation or change any fleet state. Parameterized SQL only."""
    diagnosis_id = body.get("diagnosis_id")
    if not _is_pos_int(diagnosis_id):
        raise ValueError("doctor_dismiss requires a positive integer diagnosis_id")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE fleet_diagnoses SET status='dismissed', updated_at=now() "
            "WHERE id=%s AND status='recommended'", (int(diagnosis_id),))
        n = cur.rowcount
    conn.commit()
    if n == 0:
        raise ValueError("no open recommendation with that id")
    return "Recommendation dismissed (bookkeeping only -- no fleet state changed)."


def _is_pos_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


# ---------------------------------------------------------------------------
# Challenge triage ops -- the ONLY mutations here go through queue.resolve_challenge
# (apply/ats lane) or queue.resolve_linkedin_challenge (linkedin lane). Both already
# stamp auth_challenge.resolved_at/outcome internally (queue.py:219-240 / :613-635),
# so we never re-stamp it here, and neither function touches rate_governor -- a
# LinkedIn halt (account:linkedin) is untouched by resolving a challenge. Routing is
# strictly by the request's ``lane`` field so an ats op can never reach a
# linkedin_queue row (and vice versa) even if the SAME url happens to be parked in
# both queues.
# ---------------------------------------------------------------------------
_LANE_RESOLVERS = {
    "apply": _queue.resolve_challenge,
    "linkedin": _queue.resolve_linkedin_challenge,
}


def _do_challenge_resolve(conn, body: dict, *, requeue: bool):
    """Shared body for challenge_requeue (requeue=True) / challenge_skip (requeue=False).
    Idempotent: a url with no parked row is a success no-op (queue_rows: 0), never an
    error, per the triage contract."""
    url = body.get("url")
    lane = body.get("lane")
    if not isinstance(url, str) or not url:
        raise ValueError("url is required")
    resolver = _LANE_RESOLVERS.get(lane)
    if resolver is None:
        return False, "unknown lane"
    found = resolver(conn, url, requeue=requeue, commit=True)
    n = 1 if found else 0
    verb = "requeued" if requeue else "skipped"
    return f"Challenge {verb} for {lane} url (queue_rows: {n})."


def _do_challenge_requeue(conn, body: dict):
    return _do_challenge_resolve(conn, body, requeue=True)


def _do_challenge_skip(conn, body: dict):
    return _do_challenge_resolve(conn, body, requeue=False)


def _do_challenge_skip_host(conn, body: dict):
    """Skip (requeue=False) up to 200 parked urls on one host, in one lane. Selects the
    candidate urls read-only first, then routes each through the SAME
    resolve_challenge/resolve_linkedin_challenge primitive as challenge_skip -- no new
    UPDATE of queue rows. host+lane scoped so this can never spill into the other lane
    or another host."""
    host = body.get("host")
    lane = body.get("lane")
    if not isinstance(host, str) or not host:
        raise ValueError("host is required")
    resolver = _LANE_RESOLVERS.get(lane)
    if resolver is None:
        return False, "unknown lane"
    table = "apply_queue" if lane == "apply" else "linkedin_queue"
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT url FROM {table} WHERE apply_status='challenge_pending' "
            "AND (url LIKE %s OR url LIKE %s) LIMIT 200",
            (f"https://{host}/%", f"https://www.{host}/%"),
        )
        urls = [r["url"] for r in cur.fetchall()]
    n = 0
    for url in urls:
        if resolver(conn, url, requeue=False, commit=True):
            n += 1
    return f"Skipped {n} parked challenge(s) on {host} ({lane}) (queue_rows: {n})."


# Explicit allow-list. The handler indexes THIS dict by the action string; it never
# eval()s or getattr()s anything derived from the request. Any other action -> 400.
# NOTE: discovery's expand_searches is PG-only (no brain write); there is deliberately NO
# brain-ingest ("pull") action and no LinkedIn APPLY/scrape action on this surface. The
# challenge_* ops below are an exception carved out deliberately: they never apply,
# scrape, or resume LinkedIn -- they ONLY resolve a parked auth wall via the existing
# resolve_challenge/resolve_linkedin_challenge primitives, lane-routed so the two queues
# can never cross-contaminate, and they never touch rate_governor (a LinkedIn halt is
# untouched). The two doctor_* actions are CONSERVATIVE/bookkeeping (revert a
# conservative knob / dismiss a rec) -- they never apply a recommendation and never do
# anything activity-increasing.
_ACTIONS = {
    "arm_canary": _do_arm_canary,
    "lift_canary": _do_lift_canary,
    "pause": _do_pause,
    "resume": _do_resume,
    "reclaim": _do_reclaim,
    "set_cap": _do_set_cap,
    "expand_searches": _do_expand_searches,
    "doctor_revert": _do_doctor_revert,
    "doctor_dismiss": _do_doctor_dismiss,
    "challenge_requeue": _do_challenge_requeue,
    "challenge_skip": _do_challenge_skip,
    "challenge_skip_host": _do_challenge_skip_host,
}

_AUDIT_ACTION_CAP = 120
_AUDIT_MESSAGE_CAP = 500
_AUDIT_LANE_CAP = 120
_AUDIT_TARGET_CAP = 300
_AUDIT_READ_LIMIT = 25
_AUDIT_SECRET_WORD_RE = re.compile(
    r"(?i)\b(token|password|passwd|pwd|secret|api[_-]?key)\s+[^\s,'\"}{]+"
)


def _audit_text(value: Any, limit: int) -> str:
    safe = _scrub("" if value is None else str(value))
    safe = _AUDIT_SECRET_WORD_RE.sub(r"\1 [REDACTED]", safe)
    return safe[:limit]


def _audit_optional_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    return _audit_text(value, limit)


def _audit_action(conn, *, action: str, ok: bool, message: str,
                  lane: str | None = None, target: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fleet_console_audit (action, ok, message, lane, target) "
            "VALUES (%s,%s,%s,%s,%s)",
            (
                _audit_text(action or "unknown", _AUDIT_ACTION_CAP),
                bool(ok),
                _audit_text(message or "", _AUDIT_MESSAGE_CAP),
                _audit_optional_text(lane, _AUDIT_LANE_CAP),
                _audit_optional_text(target, _AUDIT_TARGET_CAP),
            ),
        )
    conn.commit()


def _rollback_quietly(conn) -> None:
    try:
        conn.rollback()
    except Exception:
        pass


def _try_audit_action(conn, **kwargs) -> None:
    try:
        _audit_action(conn, **kwargs)
    except Exception:
        _rollback_quietly(conn)


def audit_lifecycle_event(action: str, message: str) -> None:
    """Best-effort audit for console process lifecycle events.

    Lifecycle audit must never prevent the dashboard from starting or stopping.
    """
    conn = None
    try:
        conn = pgqueue.connect()
        _audit_action(conn, action=action, ok=True, message=message, lane="console")
    except Exception:
        if conn is not None:
            _rollback_quietly(conn)
    finally:
        if conn is not None:
            conn.close()


def audit_rows(limit: int = _AUDIT_READ_LIMIT) -> dict[str, Any]:
    limit = max(1, min(int(limit), _AUDIT_READ_LIMIT))
    conn = pgqueue.connect()
    try:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, created_at, action, ok, message, lane, target "
                    "FROM fleet_console_audit "
                    "ORDER BY created_at DESC, id DESC "
                    "LIMIT %s",
                    (limit,),
                )
                rows = cur.fetchall()
        except errors.UndefinedTable:
            _rollback_quietly(conn)
            return {
                "rows": [],
                "schema_missing": True,
                "reason": "Console audit table is not installed; apply the fleet v3 schema migration.",
            }
        return {
            "rows": [
                {
                    "time": _iso(r["created_at"]),
                    "action": _audit_text(r["action"], _AUDIT_ACTION_CAP),
                    "ok": bool(r["ok"]),
                    "result": "ok" if r["ok"] else "failed",
                    "message": _audit_text(r["message"] or "", _AUDIT_MESSAGE_CAP),
                    "lane": _audit_optional_text(r["lane"], _AUDIT_LANE_CAP),
                    "target": _audit_optional_text(r["target"], _AUDIT_TARGET_CAP),
                }
                for r in rows
            ],
            "schema_missing": False,
        }
    finally:
        _rollback_quietly(conn)
        conn.close()


def run_action(body: dict) -> tuple[bool, str]:
    """Validate + dispatch a single allowed action against a short-lived connection.
    Returns (ok, message). Connection always closed.

    An action fn normally returns a success message (str) -> (True, msg). It may
    instead return a (False, msg) tuple for an expected, non-exceptional failure
    (e.g. an unknown lane) -- distinct from raising ValueError/Exception, which
    the HTTP layer maps to 400/500."""
    action = body.get("action")
    fn = _ACTIONS.get(action) if isinstance(action, str) else None
    if fn is None:
        conn = None
        try:
            conn = pgqueue.connect()
            _try_audit_action(conn, action=str(action), ok=False, message="unknown action")
        except Exception:
            if conn is not None:
                _rollback_quietly(conn)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        return False, "unknown action"
    conn = pgqueue.connect()
    try:
        try:
            result = fn(conn, body)
        except Exception as exc:
            _rollback_quietly(conn)
            _try_audit_action(conn, action=action, ok=False, message=str(exc),
                              lane=body.get("lane"), target=body.get("url") or body.get("host"))
            raise
        ok, message = result if isinstance(result, tuple) else (True, result)
        _try_audit_action(conn, action=action, ok=bool(ok), message=str(message),
                          lane=body.get("lane"), target=body.get("url") or body.get("host"))
        return ok, message
    finally:
        _rollback_quietly(conn)
        conn.close()


# ---------------------------------------------------------------------------
# Bind safety (S2): private IPv4 / loopback only -- never 0.0.0.0, never public.
# ---------------------------------------------------------------------------
def _is_private_bind(host: str) -> bool:
    """True only for a loopback or RFC1918 private IPv4 literal. 0.0.0.0 and any public
    address are rejected. We require a literal IP (no DNS) so the bind target is explicit."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.version != 4:
        return False
    if ip == ipaddress.IPv4Address("0.0.0.0"):
        return False
    return bool(ip.is_loopback or ip.is_private)


def _detect_private_ip() -> str:
    """Best-effort discovery of THIS box's private LAN IPv4 (no traffic actually sent;
    connect() on a UDP socket just picks the egress interface). Falls back to 127.0.0.1."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        cand = s.getsockname()[0]
    except Exception:
        cand = "127.0.0.1"
    finally:
        s.close()
    if _is_private_bind(cand):
        return cand
    # Scan resolved interface addresses for any private IPv4 before giving up.
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if _is_private_bind(addr) and not ipaddress.ip_address(addr).is_loopback:
                return addr
    except Exception:
        pass
    return "127.0.0.1"


def resolve_host(requested: str | None) -> str:
    """Resolve + validate the bind host. If none requested, auto-detect a private IP.
    A requested host that is not a private/loopback IPv4 literal raises (refuse to start)."""
    if not requested:
        return _detect_private_ip()
    if not _is_private_bind(requested):
        raise SystemExit(
            f"refusing to bind {requested!r}: LAN-only. Use a private IPv4 "
            "(10.x / 172.16-31.x / 192.168.x) or 127.0.0.1 -- never 0.0.0.0 or a public IP."
        )
    return requested


# ---------------------------------------------------------------------------
# Mutation token gate (audit finding: the console had zero auth). Every POST
# /api/action must present the token via X-Console-Token header or the
# console_token cookie. GET / can arm the cookie one time via ?token=<t>.
# NEVER print or log the token except the one arm-URL line at startup.
# ---------------------------------------------------------------------------
_CACHED_TOKEN: str | None = None
_TOKEN_COOKIE_NAME = "console_token"
_TOKEN_HEADER_NAME = "X-Console-Token"


def get_console_token() -> str:
    """Returns APPLYPILOT_CONSOLE_TOKEN if set, else a process-cached random token
    generated once via secrets.token_urlsafe(24)."""
    global _CACHED_TOKEN
    env_tok = os.environ.get("APPLYPILOT_CONSOLE_TOKEN")
    if env_tok:
        return env_tok
    if _CACHED_TOKEN is None:
        _CACHED_TOKEN = secrets.token_urlsafe(24)
    return _CACHED_TOKEN


def _token_ok(handler: Any) -> bool:
    """Checks the request's X-Console-Token header, falling back to the
    console_token cookie, against get_console_token() via a constant-time compare."""
    expected = get_console_token()
    header_tok = handler.headers.get(_TOKEN_HEADER_NAME)
    if header_tok and hmac.compare_digest(header_tok, expected):
        return True
    cookie_header = handler.headers.get("Cookie")
    if cookie_header:
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(cookie_header)
        except Exception:
            return False
        morsel = jar.get(_TOKEN_COOKIE_NAME)
        if morsel and hmac.compare_digest(morsel.value, expected):
            return True
    return False


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server_version = "ApplyPilotFleetConsole/1.0"

    # Silence the default access log so no URLs/IPs/secrets land in stdout.
    def log_message(self, *args, **kwargs):  # noqa: D401
        return

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if path == "/":
            import urllib.parse as _up
            qs = _up.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            tok = (qs.get("token") or [None])[0]
            if tok and hmac.compare_digest(tok, get_console_token()):
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Cache-Control", "no-store")
                self.send_header(
                    "Set-Cookie", f"{_TOKEN_COOKIE_NAME}={tok}; HttpOnly; Path=/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send(200, _INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/status":
            try:
                self._send_json(200, build_status())
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if path == "/api/diagnosis":
            conn = None
            try:
                from applypilot.fleet import console_diagnosis

                conn = pgqueue.connect()
                self._send_json(200, console_diagnosis.full_diagnosis(conn))
            except Exception as e:
                self._send_json(500, {"error": _scrub(str(e))[:500]})
            finally:
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    conn.close()
            return
        if path == "/api/diagnostics":
            # Dedicated endpoint (NOT folded into the 4s /api/status poll): the Doctor blob
            # (clusters + auto-fixes + recommendations) can be large, so it is fetched on its
            # own cadence by the Diagnostics section.
            try:
                self._send_json(200, diagnostics())
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if path == "/api/outcomes":
            try:
                self._send_json(200, outcomes())
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        if path == "/api/challenges":
            try:
                self._send_json(200, build_challenges())
            except Exception as e:
                self._send_json(500, {"error": str(e)})   # match sibling GET routes
            return
        if path == "/api/agents":
            from applypilot.fleet import console_agents

            conn = None
            try:
                conn = pgqueue.connect()
                self._send_json(200, console_agents.agent_summary(conn))
            except Exception as e:
                self._send_json(500, {"error": _scrub(str(e))[:500]})
            finally:
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    conn.close()
            return
        if path == "/api/audit":
            try:
                self._send_json(200, audit_rows())
            except Exception as e:
                self._send_json(500, {"error": _scrub(str(e))[:500]})
            return
        if path == "/api/logs":
            # Separate endpoint (NOT folded into /api/status) so the big text blobs
            # never bloat the 4s poll. The `worker` param is validated against an
            # existing worker_heartbeat row; an unknown/missing worker -> 400/404.
            import urllib.parse as _up
            qs = _up.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            wid = (qs.get("worker") or [None])[0]
            if not wid or not isinstance(wid, str):
                self._send_json(400, {"error": "missing worker param"})
                return
            try:
                logs = worker_logs(wid)
            except Exception as e:
                self._send_json(500, {"error": str(e)})
                return
            if logs is None:
                self._send_json(404, {"error": "unknown worker"})
                return
            self._send_json(200, logs)
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not _token_ok(self):
            self._send_json(401, {"ok": False, "error": "bad token"})
            return
        path = self.path.split("?", 1)[0]
        if path != "/api/action":
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            # parse_constant rejects bare NaN/Infinity/-Infinity tokens at the
            # parser (json.loads accepts them by default); _do_set_cap's
            # isfinite check is the necessary guard, this is defense in depth.
            def _reject_nonfinite(tok):  # noqa: ANN001
                raise ValueError(f"non-finite JSON literal not allowed: {tok}")
            body = json.loads(raw or b"{}", parse_constant=_reject_nonfinite)
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except Exception:
            self._send_json(400, {"ok": False, "error": "bad request body"})
            return
        try:
            ok, msg = run_action(body)
        except ValueError as e:
            self._send_json(400, {"ok": False, "error": str(e)})
            return
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})
            return
        if not ok:
            self._send_json(400, {"ok": False, "error": msg})
            return
        try:
            status = build_status()
        except Exception as e:
            status = {"error": str(e)}
        self._send_json(200, {"ok": True, "message": msg, "status": status})


# ---------------------------------------------------------------------------
# Embedded frontend (single page; polls /api/status every 4s)
# ---------------------------------------------------------------------------
_INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ApplyPilot Fleet Console</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c2230; --border:#2a3140;
    --fg:#e6edf3; --muted:#8b96a6; --green:#2ea043; --green2:#3fb950;
    --red:#da3633; --red2:#f85149; --amber:#d29922; --blue:#388bfd; --grey:#5b6470;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
    font-family:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;font-size:14px}
  header{padding:14px 20px;border-bottom:1px solid var(--border);display:flex;
    align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
  h1{font-size:16px;margin:0;font-weight:600;letter-spacing:.3px}
  .sub{color:var(--muted);font-size:12px}
  .wrap{padding:18px 20px;max-width:1200px;margin:0 auto}
  .banner{padding:16px 20px;border-radius:10px;font-size:20px;font-weight:700;
    display:flex;align-items:center;gap:14px;margin-bottom:16px;border:1px solid var(--border)}
  .banner .dot{width:14px;height:14px;border-radius:50%}
  .banner.run{background:rgba(46,160,67,.12);color:var(--green2);border-color:rgba(46,160,67,.4)}
  .banner.run .dot{background:var(--green2);box-shadow:0 0 10px var(--green2)}
  .banner.pause{background:rgba(218,54,51,.12);color:var(--red2);border-color:rgba(218,54,51,.4)}
  .banner.pause .dot{background:var(--red2);box-shadow:0 0 10px var(--red2)}
  .banner small{margin-left:auto;font-size:12px;font-weight:400;color:var(--muted)}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:12px;margin-bottom:18px}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px}
  .card .label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.6px}
  .card .val{font-size:24px;font-weight:700;margin-top:6px}
  .card .hint{color:var(--muted);font-size:11px;margin-top:4px}
  .band.primary{border-left:4px solid var(--blue)}
  .headline{font-size:24px;font-weight:750;margin:6px 0}
  .actionline{margin-top:10px;color:var(--fg);font-weight:600}
  .rec-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px;margin-top:12px}
  .action-item{background:var(--panel2);border:1px solid var(--border);border-left:3px solid var(--blue);border-radius:8px;padding:9px 10px;min-width:0}
  .action-item.warn{border-left-color:var(--amber)}
  .action-item.halted,.action-item.severe{border-left-color:var(--red2)}
  .action-item .k{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.4px;overflow-wrap:anywhere}
  .action-item .t{font-weight:700;margin-top:3px;overflow-wrap:anywhere}
  .action-item .r{color:var(--muted);font-size:12px;margin-top:3px;overflow-wrap:anywhere}
  .metric-grid,.diagnosis-grid,.machine-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
  .mini{background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:10px}
  .mini span{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.4px}
  .mini b{display:block;font-size:20px;margin-top:4px}
  .mini small{display:block;margin-top:5px;color:var(--muted)}
  .mini a{color:var(--blue);text-decoration:none}
  .funnel{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px}
  .fstep{background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:10px;min-height:70px}
  .fstep span{display:block;color:var(--muted);font-size:11px}
  .fstep b{font-size:22px}
  .table-scroll{max-width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch}
  .table-scroll table{min-width:620px}
  section{background:var(--panel);border:1px solid var(--border);border-radius:10px;
    padding:14px 16px;margin-bottom:16px}
  section h2{font-size:13px;margin:0 0 10px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--border)}
  th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.4px}
  tr:last-child td{border-bottom:none}
  .ldot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px;vertical-align:middle}
  .ldot.on{background:var(--green2);box-shadow:0 0 6px var(--green2)}
  .ldot.off{background:var(--grey)}
  .s-applied{color:var(--green2);font-weight:600}
  .s-failed{color:var(--red2);font-weight:600}
  .s-crash{color:var(--amber);font-weight:600}
  .mut{color:var(--muted)}
  .controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
  button{font:inherit;border:1px solid var(--border);background:var(--panel2);color:var(--fg);
    padding:8px 14px;border-radius:8px;cursor:pointer}
  button:hover{border-color:var(--blue)}
  button.danger{border-color:rgba(218,54,51,.5);color:var(--red2)}
  button.go{border-color:rgba(46,160,67,.5);color:var(--green2)}
  input[type=number]{font:inherit;background:var(--bg);border:1px solid var(--border);
    color:var(--fg);padding:8px;border-radius:8px;width:90px}
  .li-strip{display:flex;flex-wrap:wrap;gap:18px;align-items:center}
  .li-strip .chip{background:var(--panel2);border:1px solid var(--border);border-radius:8px;
    padding:6px 12px}
  .li-strip .ro{margin-left:auto;color:var(--amber);font-size:12px}
  .toast{position:fixed;right:20px;bottom:20px;max-width:420px;background:var(--panel2);
    border:1px solid var(--border);border-left:4px solid var(--blue);border-radius:8px;
    padding:12px 16px;box-shadow:0 8px 24px rgba(0,0,0,.4);opacity:0;transform:translateY(8px);
    transition:opacity .2s,transform .2s;font-size:13px}
  .toast.show{opacity:1;transform:translateY(0)}
  .toast.err{border-left-color:var(--red2)}
  .toast.ok{border-left-color:var(--green2)}
  label.inl{display:inline-flex;align-items:center;gap:6px;color:var(--muted);font-size:12px}
  select{font:inherit;background:var(--bg);border:1px solid var(--border);color:var(--fg);
    padding:8px;border-radius:8px}
  .logblk{margin-top:10px}
  .logblk .lbl{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
  pre.log{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:10px 12px;
    margin:0;max-height:280px;overflow:auto;white-space:pre-wrap;word-break:break-word;
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;color:var(--fg)}
  pre.log.err{border-color:rgba(218,54,51,.45)}
  .dgrp{margin-bottom:14px}
  .dgrp .lbl{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
  .rec{background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:8px;overflow:hidden;overflow-wrap:anywhere}
  .rec .rh{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:4px}
  .rec .rh .mut{min-width:0;flex:1 1 180px;overflow-wrap:anywhere}
  .rec .rh button.sm{flex:0 0 auto}
  .rec .sev{font-size:10px;text-transform:uppercase;letter-spacing:.5px;padding:2px 7px;border-radius:6px;
    border:1px solid var(--border);color:var(--muted)}
  .rec .sev.warn{color:var(--amber);border-color:rgba(210,153,34,.4)}
  .rec .sev.severe{color:var(--red2);border-color:rgba(218,54,51,.4)}
  .rec .body{font-size:13px}
  .rec .recm{color:var(--muted);font-size:12px;margin-top:4px}
  @media(max-width:560px){.rec .rh button.sm{margin-left:0!important}}
  button.sm{padding:5px 10px;font-size:12px}
  #challenges .chgrp{margin-bottom:14px;border:1px solid var(--border);border-radius:8px;overflow:hidden}
  #challenges .chgrp-h{display:flex;flex-wrap:wrap;align-items:center;gap:10px;
    background:var(--panel2);padding:8px 12px;font-size:12px;color:var(--muted)}
  #challenges .chgrp-h b{color:var(--fg)}
  #challenges .chrow{display:flex;flex-wrap:wrap;align-items:center;gap:10px;
    padding:10px 12px;border-top:1px solid var(--border)}
  #challenges .chrow .meta{flex:1 1 220px;min-width:0}
  #challenges .chrow .meta .ct{font-weight:600;overflow-wrap:anywhere}
  #challenges .chrow .meta .sub{color:var(--muted);font-size:11px;margin-top:2px}
  #challenges .chrow .btns{display:flex;flex-wrap:wrap;gap:8px}
  #challenges button{min-height:36px}
  .tokbanner{position:fixed;top:0;left:0;right:0;z-index:1000;background:var(--red2);
    color:#1a0000;font-weight:700;text-align:center;padding:10px 14px;font-size:13px;
    display:none}
  .tokbanner.show{display:block}
  .deadmanbanner{position:fixed;top:0;left:0;right:0;z-index:1001;background:var(--red2);
    color:#1a0000;font-weight:700;text-align:center;padding:10px 14px;font-size:13px;
    display:none}
  .deadmanbanner.show{display:block}
</style>
</head>
<body>
<div class="tokbanner" id="tokBanner">token expired — open the ?token= URL printed by the console window</div>
<div class="deadmanbanner" id="deadmanBanner"></div>
<header>
  <div>
    <h1>ApplyPilot Fleet Console</h1>
    <div class="sub">LAN-only control panel &mdash; this operates a system that submits REAL applications</div>
    <div class="sub" id="deploymentMeta">console version loading</div>
  </div>
  <div class="sub" id="conn">connecting&hellip;</div>
</header>
<div class="wrap">
  <div class="banner pause" id="banner">
    <span class="dot"></span><span id="bannerText">LOADING</span>
    <small id="updated"></small>
  </div>

  <section id="fleetState" class="band primary">
    <h2>Fleet State</h2>
    <div id="stateHeadline" class="headline">Loading</div>
    <div id="stateReason" class="sub"></div>
    <div id="nextAction" class="actionline">Recommended Next Action: Loading</div>
    <h2 style="margin-top:14px">Action Queue</h2>
    <div id="recommendationList" class="rec-list"><div class="mut">loading recommendations</div></div>
  </section>

  <section id="laneActivity">
    <h2>Lane Activity</h2>
    <div id="laneActivityGrid" class="diagnosis-grid">
      <div class="mini"><span>Apply lane</span><b>&mdash;</b><small>loading</small></div>
      <div class="mini"><span>Compute lane</span><b>&mdash;</b><small>loading</small></div>
      <div class="mini"><span>Discovery lane</span><b>&mdash;</b><small>loading</small></div>
      <div class="mini"><span>LinkedIn lane</span><b>&mdash;</b><small>loading</small></div>
    </div>
  </section>

  <section id="safetyRails">
    <h2>Safety Rails</h2>
    <div class="metric-grid" id="safetyGrid">
      <div class="card"><div class="label">Canary</div>
        <div class="val" id="cCanary">&mdash;</div><div class="hint" id="cCanaryHint"></div></div>
      <div class="card"><div class="label">Spend</div>
        <div class="val" id="cSpend">&mdash;</div><div class="hint" id="cSpendHint">of cap</div></div>
      <div class="card"><div class="label">Apply queue</div>
        <div class="val" id="cQueued">&mdash;</div><div class="hint">queued</div></div>
      <div class="card"><div class="label">Leasable now</div>
        <div class="val" id="cLeasable">&mdash;</div><div class="hint" id="cLeasableHint">leaseable after pause/canary gates</div></div>
      <div class="card"><div class="label">Open challenges</div>
        <div class="val" id="cChallenges">&mdash;</div><div class="hint">need a human</div></div>
      <div class="card"><div class="label">Doctor</div>
        <div class="val" id="cDoctor">&mdash;</div><div class="hint" id="cDoctorHint">auto-fixes active</div></div>
    </div>
    <h2 style="margin-top:14px">Lane State</h2>
    <div class="diagnosis-grid lane-state" id="laneStateGrid">
      <div class="mini"><span>Shared pause</span><b>&mdash;</b><small>loading</small></div>
      <div class="mini"><span>ATS pause</span><b>&mdash;</b><small>loading</small></div>
      <div class="mini"><span>ATS leaseable</span><b>&mdash;</b><small>loading</small></div>
      <div class="mini"><span>LinkedIn owner IP</span><b>&mdash;</b><small>loading</small></div>
      <div class="mini"><span>LinkedIn canary</span><b>&mdash;</b><small>loading</small></div>
    </div>
  </section>

  <section id="whyNotApplying">
    <h2>Why Not Applying</h2>
    <div id="whyBody" class="diagnosis-grid"></div>
  </section>

  <section id="agentRouting">
    <h2>Agent Routing</h2>
    <div id="agentVerdict" class="sub"></div>
    <div id="agentSummary" class="diagnosis-grid" style="margin-top:10px;margin-bottom:12px"></div>
    <div class="table-scroll"><table><thead><tr><th>Worker</th><th>Machine</th><th>Agent</th><th>Model</th><th>Chain</th><th>Version</th><th>Switch</th></tr></thead>
      <tbody id="agentWorkers"><tr><td colspan="7" class="mut">loading</td></tr></tbody></table></div>
  </section>

  <section id="machineHealth">
    <h2>Machine Health</h2>
    <div id="machineMap" class="machine-grid"></div>
    <div class="dgrp" style="margin-top:12px">
      <div class="lbl">Stale Apply Workers</div>
      <div id="staleWorkers" class="mut">loading stale worker details</div>
    </div>
  </section>

  <section id="deploymentDrift">
    <h2>Deployment Drift</h2>
    <div id="deploymentDriftSummary" class="sub"></div>
    <div class="table-scroll"><table><thead><tr><th>Version</th><th>Workers</th><th>Status</th></tr></thead>
      <tbody id="deploymentDriftRows"><tr><td colspan="3" class="mut">loading</td></tr></tbody></table></div>
  </section>

  <section id="browserHealth">
    <h2>Browser Health</h2>
    <div id="browserBody" class="diagnosis-grid"></div>
    <div class="dgrp" style="margin-top:12px">
      <div class="lbl">Wall Queue</div>
      <div id="browserWallQueue" class="mut">loading browser wall queue</div>
    </div>
  </section>

  <section id="workerComparison">
    <h2>Worker Comparison</h2>
    <div class="table-scroll"><table><thead><tr><th>Worker</th><th>Applied</th><th>Total</th><th>Success</th><th>Crash</th><th>Cost / Apply</th></tr></thead>
      <tbody id="workerComparisonRows"><tr><td colspan="6" class="mut">loading</td></tr></tbody></table></div>
  </section>

  <section id="queueFunnel">
    <h2>Queue Funnel</h2>
    <div id="funnelBody" class="funnel"></div>
  </section>

  <section id="hostQuality">
    <h2>Host Quality</h2>
    <div class="table-scroll"><table><thead><tr><th>Host</th><th>Total</th><th>Applied</th><th>Failed</th><th>Challenges</th><th>Bad Rate</th></tr></thead>
      <tbody id="hostQualityRows"><tr><td colspan="6" class="mut">loading</td></tr></tbody></table></div>
  </section>

  <section id="auditLog">
    <h2>Audit Log</h2>
    <div class="table-scroll"><table><thead><tr><th>Time</th><th>Action</th><th>Result</th><th>Message</th></tr></thead>
      <tbody id="auditRows"><tr><td colspan="4" class="mut">audit endpoint not loaded</td></tr></tbody></table></div>
  </section>

  <section>
    <h2>Controls</h2>
    <div class="controls">
      <label class="inl">k <input type="number" id="canaryK" min="1" max="200" value="10"></label>
      <button class="go" id="btnArm">Arm canary</button>
      <button class="go" id="btnLift">Lift canary (run unbounded)</button>
      <button class="danger" id="btnPause">Pause</button>
      <button id="btnResume">Resume</button>
      <button id="btnReclaim">Reclaim stale leases</button>
      <span style="width:1px;height:24px;background:var(--border)"></span>
      <label class="inl">$ <input type="number" id="capUsd" min="0" step="0.01" placeholder="cap"></label>
      <button id="btnCap">Set $ cap</button>
    </div>
  </section>

  <section id="challenges">
    <h2>Challenges (need a human)</h2>
    <div class="sub" style="margin-bottom:12px">
      Parked applications waiting on a captcha / OTP / login wall, grouped by kind then host.
      Solve it in the browser, then Re-queue (or Skip if it's not worth it).
    </div>
    <div id="chList" class="mut">&hellip;</div>
  </section>

  <section>
    <h2>Workers (apply lane)</h2>
    <div class="table-scroll"><table><thead><tr><th>Worker</th><th>Live</th><th>Last beat</th><th>Applied</th><th>Current job</th></tr></thead>
      <tbody id="workers"><tr><td colspan="5" class="mut">&hellip;</td></tr></tbody></table></div>
  </section>

  <section>
    <h2>Recent activity</h2>
    <div class="table-scroll"><table><thead><tr><th>Time</th><th>Worker</th><th>Status</th><th>Company / Title</th></tr></thead>
      <tbody id="recent"><tr><td colspan="4" class="mut">&hellip;</td></tr></tbody></table></div>
  </section>

  <section>
    <h2>Worker logs (crash + recent activity)</h2>
    <div class="controls" style="margin-bottom:12px">
      <label class="inl">worker
        <select id="logWorker"><option value="">&mdash;</option></select>
      </label>
      <button id="btnLogs">Refresh</button>
      <span class="mut" style="font-size:12px">Last crash traceback + recent log tail, shipped via the heartbeat (secrets scrubbed). Read-only.</span>
    </div>
    <div id="logArea" class="mut" style="font-size:12px">Select a worker to view its logs.</div>
  </section>

  <section>
    <h2>Discovery lane</h2>
    <div class="cards" style="margin-bottom:12px">
      <div class="card"><div class="label">Search tasks</div>
        <div class="val" id="dTasks">&mdash;</div><div class="hint" id="dTasksHint">enabled</div></div>
      <div class="card"><div class="label">Due now</div>
        <div class="val" id="dDue">&mdash;</div><div class="hint">ready to scrape</div></div>
      <div class="card"><div class="label">Postings staged</div>
        <div class="val" id="dStaged">&mdash;</div><div class="hint" id="dStagedHint">pending ingest</div></div>
      <div class="card"><div class="label">Found (24h)</div>
        <div class="val" id="dFound24">&mdash;</div><div class="hint">new postings</div></div>
    </div>
    <div class="controls" style="margin-bottom:12px">
      <button class="go" id="btnExpand">Expand searches (seed tasks)</button>
      <span class="mut" style="font-size:12px">Seeds <code>search_tasks</code> from searches.yaml &mdash; queues scrape work, submits nothing. Discovery workers run per-machine (<code>applypilot-fleet-discovery</code>).</span>
    </div>
    <div class="table-scroll"><table><thead><tr><th>Discovery worker</th><th>Live</th><th>Last beat</th><th>Found 24h</th></tr></thead>
      <tbody id="discWorkers"><tr><td colspan="4" class="mut">&hellip;</td></tr></tbody></table></div>
    <div class="table-scroll" style="margin-top:10px"><table><thead><tr><th>Found</th><th>Source</th><th>Search</th></tr></thead>
      <tbody id="discRecent"><tr><td colspan="3" class="mut">&hellip;</td></tr></tbody></table></div>
  </section>

<section>
  <h2>Recent outcomes (read-only)</h2>
  <div class="table-scroll"><table><thead><tr><th>Time</th><th>Company / Title</th><th>Stage</th><th>Outcome</th></tr></thead>
    <tbody id="outcomes"><tr><td colspan="4" class="mut">&hellip;</td></tr></tbody></table></div>
</section>

  <section>
    <h2>Diagnostics (Doctor)</h2>
    <div class="sub" style="margin-bottom:12px">
      Bounded, reversible, <b>conservative-only</b> auto-fixes (skip a blocking host, raise a
      timeout within a ceiling, quarantine a poison url, pace down / pause). The Doctor can never
      resume, re-approve, raise the spend cap, lower the gap, or touch LinkedIn. Everything else is
      a recommendation for you.
    </div>
    <div class="dgrp"><div class="lbl">Live failure clusters (last 60m)</div>
      <div class="table-scroll"><table><thead><tr><th>Reason</th><th>Host</th><th>Machine</th><th>Samples</th></tr></thead>
        <tbody id="docClusters"><tr><td colspan="4" class="mut">&hellip;</td></tr></tbody></table></div>
    </div>
    <div class="dgrp"><div class="lbl">Active auto-fixes</div>
      <div class="table-scroll"><table><thead><tr><th>Type</th><th>Scope</th><th>Reason</th><th>Expires</th><th></th></tr></thead>
        <tbody id="docAuto"><tr><td colspan="5" class="mut">&hellip;</td></tr></tbody></table></div>
    </div>
    <div class="dgrp"><div class="lbl">Recommendations (need a human)</div>
      <div id="docRecs" class="mut">&hellip;</div>
    </div>
  </section>

  <section>
    <h2>LinkedIn (read-only &mdash; the catastrophe lane has no controls here)</h2>
    <div class="li-strip">
      <div class="chip">Queued: <b id="liQueued">&mdash;</b></div>
      <div class="chip">Applied: <b id="liApplied">&mdash;</b></div>
      <div class="chip">Canary: <b id="liCanary">&mdash;</b></div>
      <div class="chip">Halted: <b id="liHalted">&mdash;</b></div>
      <span class="ro">read-only</span>
    </div>
  </section>
</div>
<div class="toast" id="toast"></div>

<script>
"use strict";
let lastUpdate = null;

function esc(s){ return s==null ? "" : String(s).replace(/[&<>"]/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

function pct(v){
  if(v==null || isNaN(v)) return "—";
  return Math.round(Number(v) * 100) + "%";
}

function money(v){
  if(v==null || isNaN(v)) return "—";
  return "$" + Number(v).toFixed(2);
}

function machineName(obj, fallback){
  return (obj && obj.machine_display_name) || fallback || "(unknown)";
}

function openWorkerLogs(workerId){
  const sel = document.getElementById("logWorker");
  if(!sel) return false;
  if(workerId && !Array.from(sel.options).some(o => o.value === workerId)){
    const opt = document.createElement("option");
    opt.value = workerId;
    opt.textContent = workerId;
    sel.appendChild(opt);
  }
  sel.value = workerId || "";
  loadLogs();
  document.getElementById("logArea").scrollIntoView({block:"center"});
  return false;
}

function rel(iso){
  if(!iso) return "never";
  const t = new Date(iso).getTime();
  const d = Math.max(0, Math.round((Date.now()-t)/1000));
  if(d<60) return d+"s ago";
  if(d<3600) return Math.round(d/60)+"m ago";
  return Math.round(d/3600)+"h ago";
}

function toast(msg, kind){
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "toast show " + (kind||"");
  clearTimeout(el._t);
  el._t = setTimeout(()=>{ el.className = "toast " + (kind||""); }, 5000);
}

async function loadDiagnosis(){
  try{
    const r = await fetch("/api/diagnosis", {cache:"no-store"});
    if(!r.ok) return;
    renderDiagnosis(await r.json());
  }catch(e){}
}

async function loadAgents(){
  try{
    const r = await fetch("/api/agents", {cache:"no-store"});
    if(!r.ok) return;
    renderAgents(await r.json());
  }catch(e){}
}

function renderDiagnosis(d){
  const q = d.queue || {};
  const ats = q.ats || {};
  const li = q.linkedin || {};
  const roll = d.rollups || {};
  const state = q.state || {};
  document.getElementById("stateHeadline").textContent = state.code || "unknown";
  document.getElementById("stateReason").textContent = state.reason || "";
  const recs = d.recommendations || [];
  document.getElementById("nextAction").textContent = recs.length
    ? "Recommended Next Action: " + recs[0].title + " — " + recs[0].reason
    : "Recommended Next Action: No recommendation";
  renderRecommendationList(recs);
  document.getElementById("whyBody").innerHTML = [
    ["ATS queued", ats.queued],
    ["ATS approved", ats.approved],
    ["ATS leaseable", ats.leaseable],
    ["ATS dedup-blocked", ats.dedup_blocked],
    ["LinkedIn queued", li.queued],
    ["LinkedIn leaseable", li.leaseable],
    ["LinkedIn canary exhausted", li.canary_exhausted ? "yes" : "no"]
  ].map(([k,v]) => '<div class="mini"><span>'+esc(k)+'</span><b>'+esc(v)+'</b></div>').join("");
  const browser = d.browser || {};
  const counts = browser.counts || {};
  const examples = browser.examples || {};
  const keys = Object.keys(counts);
  document.getElementById("browserBody").innerHTML = keys.length
    ? keys.map(k => {
        const ex = examples[k] || {};
        const detail = ex.worker_id
          ? '<small>'+esc(machineName(ex, ex.machine_owner))+' / '+esc(ex.worker_id)+
            ' · <a href="'+esc(ex.logs_url||"#")+'" onclick="return openWorkerLogs('+
            esc(JSON.stringify(String(ex.worker_id)))+')">logs</a></small>'
          : "";
        return '<div class="mini"><span>'+esc(k)+'</span><b>'+esc(counts[k])+'</b>'+detail+'</div>';
      }).join("")
    : '<div class="mut">no classified browser failures</div>';
  renderBrowserWallQueue(browser.wall_queue || []);
  document.getElementById("funnelBody").innerHTML = [
    ["Queued", ats.queued],
    ["Approved", ats.approved],
    ["Leaseable", ats.leaseable],
    ["Leased", ats.leased],
    ["Applied", ats.applied],
    ["Failed", ats.failed],
    ["Crash unconfirmed", ats.crash_unconfirmed]
  ].map(([k,v]) => '<div class="fstep"><span>'+esc(k)+'</span><b>'+esc(v)+'</b></div>').join("");
  renderHostQuality(roll.host_quality || []);
  const machines = roll.machines || {};
  document.getElementById("machineMap").innerHTML = Object.keys(machines).length
    ? Object.keys(machines).map(k => '<div class="mini"><span>'+esc(machines[k].display_name || k)+'</span><b>'+
      esc(machines[k].workers)+'</b><small class="mut">'+esc(machines[k].alive_workers||0)+
      ' alive / '+esc(machines[k].stale_workers||0)+' stale</small></div>').join("")
    : '<div class="mut">no machine heartbeats</div>';
  renderWorkerComparison(roll.worker_comparison || []);
}

function renderAgents(d){
  const verdict = d.verdict || {};
  document.getElementById("agentVerdict").textContent = (verdict.code || "unknown") + " — " + (verdict.reason || "");
  renderAgentSummary(d);
  const rows = d.workers || [];
  const body = document.getElementById("agentWorkers");
  body.innerHTML = rows.length ? rows.map(w =>
    '<tr><td>'+esc(w.worker_id)+'</td><td>'+esc(w.machine_display_name||w.machine_owner||"")+'</td><td>'+
    esc(w.current_agent||"unknown")+'</td><td>'+esc(w.current_model||"unknown")+'</td><td>'+
    esc(w.agent_chain||"")+'</td><td>'+esc(w.sw_version||"unknown")+'</td><td>'+
    esc(w.last_agent_switch_reason||"")+'</td></tr>'
  ).join("") : '<tr><td colspan="7" class="mut">no apply worker agent telemetry</td></tr>';
}

function renderAgentSummary(d){
  const el = document.getElementById("agentSummary");
  if(!el) return;
  const workers = d.workers || [];
  const verdict = d.verdict || {};
  const availability = d.availability || {};
  const spend = d.spend_24h || [];
  const modelCounts = {};
  workers.forEach(w => {
    if(w.current_agent || w.current_model){
      const key = (w.current_agent || "unknown") + " / " + (w.current_model || "model unknown");
      modelCounts[key] = (modelCounts[key] || 0) + 1;
    }
  });
  const modelKeys = Object.keys(modelCounts);
  const modelText = modelKeys.length
    ? modelKeys.map(k => k + "×" + modelCounts[k]).join(", ")
    : "unknown";
  const switched = workers.filter(w =>
    w.last_agent_switch_reason || String(w.agent_chain || "").indexOf(">") >= 0
  ).length;
  const telemetryMissing = verdict.code === "telemetry_missing" || !modelKeys.length;
  const switchingText = telemetryMissing ? "not observable" : (switched ? "observed" : "not observed");
  const cards = [
    ["Model in use", modelText,
      modelKeys.length ? "current worker heartbeat telemetry" : "workers have not reported agent/model telemetry"],
    ["Dynamic switching", switchingText,
      telemetryMissing ? "redeploy/restart workers before judging switching" : (switched ? switched+" worker(s) show switch evidence" : "no switch rows reported")],
  ];
  Object.keys(availability).sort().forEach(agent => {
    const row = availability[agent] || {};
    cards.push([
      "Agent " + agent,
      row.blocked ? "blocked" : "ready",
      row.blocked ? ((row.reason || "blocked") + (row.blocked_until ? " until " + row.blocked_until : "")) : "not currently blocked",
    ]);
  });
  if(spend.length){
    spend.forEach(row => cards.push([
      "24h spend " + (row.provider || "unknown"),
      money(row.cost_usd || 0),
      (row.count || 0) + " call(s)" + (row.model ? " / " + row.model : " / model unknown"),
    ]));
  } else {
    cards.push(["24h spend", "$0.00", "no apply-agent usage recorded"]);
  }
  el.innerHTML = cards.map(([label, value, hint]) =>
    '<div class="mini"><span>'+esc(label)+'</span><b>'+esc(value)+'</b><small>'+esc(hint)+'</small></div>'
  ).join("");
}

function renderRecommendationList(recs){
  const el = document.getElementById("recommendationList");
  if(!el) return;
  if(!recs.length){
    el.innerHTML = '<div class="mut">no current recommendations</div>';
    return;
  }
  el.innerHTML = recs.map(r => {
    const sev = r.severity || "info";
    return '<div class="action-item '+esc(sev)+'"><div class="k">'+
      esc([sev, r.lane, r.action_type].filter(Boolean).join(" / "))+'</div><div class="t">'+
      esc(r.title || r.code || "Recommendation")+'</div><div class="r">'+
      esc(r.reason || "")+'</div></div>';
  }).join("");
}

function renderBrowserWallQueue(rows){
  const el = document.getElementById("browserWallQueue");
  if(!el) return;
  if(!rows.length){
    el.innerHTML = '<div class="mut">no browser/login/captcha walls in worker logs</div>';
    return;
  }
  el.innerHTML = '<div class="table-scroll"><table><thead><tr>'+
    '<th>Kind</th><th>Machine</th><th>Worker</th><th>Sample</th><th>Action</th><th>Logs</th>'+
    '</tr></thead><tbody>' + rows.map(r =>
      '<tr><td>'+esc(r.kind||"unknown")+'</td><td>'+esc(r.machine_display_name||r.machine_owner||"")+
      '</td><td>'+esc(r.worker_id||"")+'</td><td>'+esc(r.sample||"")+'</td><td>'+
      esc(r.action||"Open logs and inspect before scaling applies.")+'</td><td><a href="'+
      esc(r.logs_url||"#")+'" onclick="return openWorkerLogs('+esc(JSON.stringify(String(r.worker_id||"")))+')">logs</a></td></tr>'
    ).join("") + '</tbody></table></div>';
}

function renderWorkerComparison(rows){
  const body = document.getElementById("workerComparisonRows");
  if(!body) return;
  body.innerHTML = rows.length ? rows.map(w =>
    '<tr><td>'+esc(w.worker_id)+'</td><td>'+esc(w.applied||0)+'</td><td>'+esc(w.total||0)+
    '</td><td>'+esc(pct(w.success_rate))+'</td><td>'+esc(pct(w.crash_rate))+
    '</td><td>'+esc(money(w.cost_per_applied))+'</td></tr>'
  ).join("") : '<tr><td colspan="6" class="mut">no worker outcome data yet</td></tr>';
}

function renderHostQuality(rows){
  const body = document.getElementById("hostQualityRows");
  if(!body) return;
  if(!rows.length){
    body.innerHTML = '<tr><td colspan="6" class="mut">no host outcome data yet</td></tr>';
    return;
  }
  body.innerHTML = rows.map(h => {
    const total = Number(h.total || 0);
    const bad = Number(h.failed || 0) + Number(h.challenges || 0);
    const badRate = total ? (bad / total) : null;
    return '<tr><td>'+esc(h.host || "(unknown)")+'</td><td>'+esc(total)+'</td><td>'+
      esc(h.applied || 0)+'</td><td>'+esc(h.failed || 0)+'</td><td>'+
      esc(h.challenges || 0)+'</td><td>'+esc(pct(badRate))+'</td></tr>';
  }).join("");
}

function renderStaleWorkers(workers){
  const el = document.getElementById("staleWorkers");
  if(!el) return;
  const stale = (workers || []).filter(w => !w.alive);
  if(!stale.length){
    el.innerHTML = '<div class="mut">no stale apply workers</div>';
    return;
  }
  el.innerHTML = '<div class="table-scroll"><table><thead><tr>'+
    '<th>Worker</th><th>Machine</th><th>Last beat</th><th>State</th><th>Version</th>'+
    '</tr></thead><tbody>' + stale.map(w =>
      '<tr><td>'+esc(w.worker_id)+'</td><td>'+esc(w.machine_display_name||w.machine_owner||"")+
      '</td><td>'+esc(w.last_beat ? rel(w.last_beat) : "never")+
      '</td><td>'+esc(w.state||"")+'</td><td>'+esc(w.sw_version||"unknown")+'</td></tr>'
    ).join("") + '</tbody></table></div>';
}

function classifyBuild(version){
  const v = String(version || "(unknown)");
  if(v === "(unknown)") return "unknown build";
  if(v.indexOf(".dirty") >= 0 || v.indexOf("-dirty") >= 0) return "dirty build";
  return "reported build";
}

function renderDeploymentDrift(dep){
  const rowsEl = document.getElementById("deploymentDriftRows");
  const summaryEl = document.getElementById("deploymentDriftSummary");
  if(!rowsEl || !summaryEl) return;
  const versions = (dep && dep.worker_versions) || [];
  if(!versions.length){
    summaryEl.textContent = "No worker versions reported.";
    rowsEl.innerHTML = '<tr><td colspan="3" class="mut">no worker version telemetry</td></tr>';
    return;
  }
  const total = versions.reduce((n, row) => n + Number(row.workers || 0), 0);
  const dirty = versions.filter(row => classifyBuild(row.version) === "dirty build")
    .reduce((n, row) => n + Number(row.workers || 0), 0);
  const unknown = versions.filter(row => classifyBuild(row.version) === "unknown build")
    .reduce((n, row) => n + Number(row.workers || 0), 0);
  summaryEl.textContent = total + " worker heartbeat(s) across " + versions.length +
    " version group(s)" + (dirty || unknown ? " — reconcile dirty/unknown builds before comparing telemetry." : ".");
  rowsEl.innerHTML = versions.map(row => {
    const status = classifyBuild(row.version);
    return '<tr><td>'+esc(row.version || "(unknown)")+'</td><td>'+esc(row.workers || 0)+
      '</td><td>'+esc(status)+'</td></tr>';
  }).join("");
}

function renderLaneState(s){
  const grid = document.getElementById("laneStateGrid");
  if(!grid) return;
  const diag = s.fleet_diagnosis || {};
  const ats = diag.ats || {};
  const linkedin = diag.linkedin || {};
  const gate = s.gate || {};
  const statusLinkedin = s.linkedin || {};
  const doctor = s.doctor || {};
  const sharedPaused = Boolean(gate.paused || ats.paused);
  const atsPaused = Boolean(ats.ats_paused || doctor.ats_paused);
  const ownerKnown = linkedin.owner_ip_context_known;
  const ownerReady = linkedin.owner_ip_ready;
  const linkedinCanaryEnabled = linkedin.canary_enabled != null
    ? linkedin.canary_enabled : statusLinkedin.canary_enabled;
  const linkedinCanaryRemaining = linkedin.canary_remaining;
  const linkedinCanary = linkedinCanaryEnabled
    ? (linkedinCanaryRemaining == null ? "armed" : linkedinCanaryRemaining)
    : "off";
  const cells = [
    ["Shared pause", sharedPaused ? "on" : "off",
      sharedPaused ? "apply lane halted; compute/discovery may continue" : "shared kill switch clear"],
    ["ATS pause", atsPaused ? "on" : "off",
      atsPaused ? "source " + (doctor.ats_pause_source || "unknown") : "ATS pause clear"],
    ["ATS leaseable", ats.leaseable == null ? "—" : ats.leaseable,
      "approved, canary-safe, not deduped"],
    ["LinkedIn owner IP", ownerKnown ? (ownerReady ? "ready" : "blocked") : "unknown",
      ownerKnown ? "owner-IP context checked" : "owner-IP context unavailable"],
    ["LinkedIn canary", linkedinCanary,
      linkedin.canary_exhausted ? "exhausted; re-arm before lease" : "catastrophe lane cap"],
  ];
  grid.innerHTML = cells.map(([label, value, hint]) =>
    '<div class="mini"><span>'+esc(label)+'</span><b>'+esc(value)+'</b><small>'+esc(hint)+'</small></div>'
  ).join("");
}

function renderLaneActivity(s){
  const grid = document.getElementById("laneActivityGrid");
  if(!grid) return;
  const diag = s.fleet_diagnosis || {};
  const ats = diag.ats || {};
  const liDiag = diag.linkedin || {};
  const gate = s.gate || {};
  const q = (s.queue && s.queue.apply) || {};
  const recent = s.recent || [];
  const discovery = s.discovery || {};
  const discoveryWorkers = discovery.workers || [];
  const linkedin = s.linkedin || {};
  const computeRecent = recent.filter(r => r.lane === "compute").length;
  const applyStatus = (gate.paused || ats.paused || ats.ats_paused || gate.should_halt)
    ? "halted" : (Number(ats.leaseable || 0) > 0 ? "ready" : "idle");
  const computeStatus = computeRecent ? "active" : "idle";
  const discoveryAlive = discoveryWorkers.filter(w => w.alive).length;
  const discoveryStatus = discoveryAlive ? "active" : "idle";
  const linkedinStatus = linkedin.halted ? "halted" : (Number(liDiag.leaseable || 0) > 0 ? "ready" : "limited");
  const cards = [
    ["Apply lane", applyStatus,
      (ats.leaseable == null ? "leaseable unknown" : ats.leaseable + " leaseable") +
      " / " + (q.queued || 0) + " queued"],
    ["Compute lane", computeStatus,
      computeRecent + " recent compute result(s)"],
    ["Discovery lane", discoveryStatus,
      discoveryAlive + " worker(s) alive / " + (((discovery.tasks || {}).due_now) || 0) + " due search task(s)"],
    ["LinkedIn lane", linkedinStatus,
      (liDiag.leaseable == null ? "leaseable unknown" : liDiag.leaseable + " leaseable") +
      " / " + (linkedin.queued || liDiag.queued || 0) + " queued"],
  ];
  grid.innerHTML = cards.map(([label, value, hint]) =>
    '<div class="mini"><span>'+esc(label)+'</span><b>'+esc(value)+'</b><small>'+esc(hint)+'</small></div>'
  ).join("");
}

function renderAudit(d){
  const body = document.getElementById("auditRows");
  const rows = (d && d.rows) || [];
  if(!rows.length){
    body.innerHTML = '<tr><td colspan="4" class="mut">no audit entries yet</td></tr>';
    return;
  }
  body.innerHTML = rows.map(r =>
    '<tr><td class="mut">'+esc(rel(r.time))+'</td><td>'+esc(r.action||"—")+
    '</td><td>'+esc(r.result || (r.ok ? "ok" : "failed"))+'</td><td>'+
    esc(r.message||"—")+'</td></tr>'
  ).join("");
}

async function loadAudit(){
  const body = document.getElementById("auditRows");
  try{
    const r = await fetch("/api/audit", {cache:"no-store"});
    if(!r.ok){ body.innerHTML = '<tr><td colspan="4" class="mut">audit unavailable</td></tr>'; return; }
    renderAudit(await r.json());
  }catch(e){
    body.innerHTML = '<tr><td colspan="4" class="mut">audit unavailable</td></tr>';
  }
}

function render(s){
  lastUpdate = Date.now();
  document.getElementById("conn").textContent = "connected";
  const g = s.gate, q = s.queue.apply, li = s.linkedin, doc = s.doctor || {};
  const dep = s.deployment || {};
  const liveAts = ((s.fleet_diagnosis || {}).ats) || {};
  const consoleInfo = dep.console || {};
  const schema = dep.schema || {};
  const worker_versions = dep.worker_versions || [];
  const versionText = worker_versions.length
    ? worker_versions.map(v => (v.version || "(unknown)") + "×" + (v.workers || 0)).join(", ")
    : "none";
  document.getElementById("deploymentMeta").textContent =
    "console " + (consoleInfo.branch || "unknown") + "@" + (consoleInfo.commit || "unknown") +
    " · schema telemetry=" + (schema.agent_telemetry ? "ok" : "missing") +
    " audit=" + (schema.audit_table ? "ok" : "missing") +
    " · worker_versions " + versionText;
  renderDeploymentDrift(dep);

  // DeadMan: a RED fixed banner when fleet_config.deadman_alert is set (silent
  // fleet death / stall / running-hot) so the owner sees it even glancing at a phone.
  const dmBanner = document.getElementById("deadmanBanner");
  if(dmBanner){
    if(s.deadman_alert){
      dmBanner.textContent = "DEADMAN ALERT: " + s.deadman_alert + " (" + rel(s.deadman_alert_at) + ")";
      dmBanner.className = "deadmanbanner show";
    } else {
      dmBanner.textContent = "";
      dmBanner.className = "deadmanbanner";
    }
  }
  // H8: an ATS-only Doctor pause (ats_paused, source='doctor') is a distinct, milder state than a
  // full operator/cost halt -- label it so the operator knows the LinkedIn lane is untouched and
  // that it auto-reverts. The shared paused/spend-cap halt still shows as the hard PAUSE/HALTED.
  const doctorAtsPause = doc.ats_paused && doc.ats_pause_source === "doctor";
  const paused = g.paused || g.should_halt;
  const banner = document.getElementById("banner");
  banner.className = "banner " + ((paused || doctorAtsPause) ? "pause" : "run");
  document.getElementById("bannerText").textContent =
    paused ? (g.should_halt && !g.paused ? "HALTED (spend cap)" : "PAUSED")
           : (doctorAtsPause ? "ATS PAUSED by Fleet Doctor (auto)" : "RUNNING");
  document.getElementById("updated").textContent =
    "updated " + rel(s.now) + (g.should_halt ? " • halt active"
      : doctorAtsPause ? " • LinkedIn lane UNAFFECTED" : "");

  // H18: above-the-fold Doctor card + a toast when a NEW auto-fix appears between polls.
  const dc = document.getElementById("cDoctor");
  if(dc){
    dc.textContent = (doc.active_auto_fix_count!=null) ? doc.active_auto_fix_count : "—";
    document.getElementById("cDoctorHint").textContent =
      "active • last pass " + rel(doc.last_pass_at);
    if(window._lastDoctorFixId!=null && doc.newest_auto_fix_id!=null
       && doc.newest_auto_fix_id > window._lastDoctorFixId){
      toast("Fleet Doctor applied a new auto-fix", "ok");
    }
    if(doc.newest_auto_fix_id!=null) window._lastDoctorFixId = doc.newest_auto_fix_id;
  }

  document.getElementById("cCanary").textContent =
    g.canary_enabled ? (g.canary_remaining==null ? "on" : g.canary_remaining) : "off";
  document.getElementById("cCanaryHint").textContent =
    g.canary_enabled ? "remaining (auto-pause at 0)" : "disabled (unbounded if running)";
  document.getElementById("cSpend").textContent = "$" + (g.spent_usd||0).toFixed(2);
  document.getElementById("cSpendHint").textContent =
    g.spend_cap_usd>0 ? ("of $" + g.spend_cap_usd.toFixed(2) + " cap") : "no cap set";
  document.getElementById("cQueued").textContent = q.queued;
  document.getElementById("cLeasable").textContent =
    liveAts.leaseable == null ? g.leasable : liveAts.leaseable;
  document.getElementById("cLeasableHint").textContent =
    liveAts.leaseable == null ? "eligible if running" : "leaseable after pause/canary gates";
  document.getElementById("cChallenges").textContent = s.challenges;
  renderLaneState(s);
  renderLaneActivity(s);
  renderStaleWorkers(s.workers || []);

  const wt = document.getElementById("workers");
  if(!s.workers.length){ wt.innerHTML = '<tr><td colspan="5" class="mut">no apply workers heartbeating</td></tr>'; }
  else wt.innerHTML = s.workers.map(w=>{
    const cur = w.current ? esc((w.current.company||"")+" — "+(w.current.title||"")) : '<span class="mut">idle</span>';
    const beat = w.last_beat ? (rel(w.last_beat)+(w.seconds_since!=null?" ("+w.seconds_since+"s)":"")) : "never";
    return '<tr><td>'+esc(w.worker_id)+'</td>'+
      '<td><span class="ldot '+(w.alive?"on":"off")+'"></span>'+(w.alive?"alive":"down")+'</td>'+
      '<td class="mut">'+esc(beat)+'</td><td>'+(w.applied||0)+'</td><td>'+cur+'</td></tr>';
  }).join("");

  const rt = document.getElementById("recent");
  if(!s.recent.length){ rt.innerHTML = '<tr><td colspan="4" class="mut">no recent activity</td></tr>'; }
  else rt.innerHTML = s.recent.map(r=>{
    const cls = r.status==="applied"?"s-applied":(r.status==="crash_unconfirmed"?"s-crash":
      (r.status==="failed"||r.status==="blocked"?"s-failed":"mut"));
    const ct = esc([r.company,r.title].filter(Boolean).join(" — "));
    return '<tr><td class="mut">'+esc(rel(r.time))+'</td><td>'+(r.worker?esc(r.worker):'<span class="mut">—</span>')+
      '</td><td class="'+cls+'">'+esc(r.status)+'</td><td>'+(ct||'<span class="mut">—</span>')+'</td></tr>';
  }).join("");

  document.getElementById("liQueued").textContent = li.queued;
  document.getElementById("liApplied").textContent = li.applied;
  document.getElementById("liCanary").textContent = li.canary_enabled ? "armed" : "off";
  document.getElementById("liHalted").textContent = li.halted ? "YES" : "no";

  // Populate the worker-logs <select> from apply + discovery workers, preserving the
  // current selection. Only rebuild when the worker set changes (don't clobber an open
  // dropdown every 4s).
  const ids = [];
  (s.workers||[]).forEach(w=>ids.push(w.worker_id));
  if(s.discovery && s.discovery.workers){ s.discovery.workers.forEach(w=>ids.push(w.worker_id)); }
  const sel = document.getElementById("logWorker");
  const sig = ids.join("|");
  if(sel._sig !== sig){
    sel._sig = sig;
    const cur = sel.value;
    sel.innerHTML = '<option value="">— select worker —</option>' +
      ids.map(id=>'<option value="'+esc(id)+'">'+esc(id)+'</option>').join("");
    if(ids.indexOf(cur) >= 0) sel.value = cur;
  }

  const d = s.discovery;
  if(d){
    document.getElementById("dTasks").textContent = d.tasks.total;
    document.getElementById("dTasksHint").textContent = d.tasks.enabled + " enabled";
    document.getElementById("dDue").textContent = d.tasks.due_now;
    document.getElementById("dStaged").textContent = d.postings.total;
    document.getElementById("dStagedHint").textContent = d.postings.pending_ingest + " pending ingest";
    document.getElementById("dFound24").textContent = d.postings.last24h;
    const dw = document.getElementById("discWorkers");
    if(!d.workers.length){ dw.innerHTML = '<tr><td colspan="4" class="mut">no discovery workers heartbeating &mdash; start one with applypilot-fleet-discovery</td></tr>'; }
    else dw.innerHTML = d.workers.map(w=>{
      const beat = w.last_beat ? (rel(w.last_beat)+(w.seconds_since!=null?" ("+w.seconds_since+"s)":"")) : "never";
      return '<tr><td>'+esc(w.worker_id)+'</td>'+
        '<td><span class="ldot '+(w.alive?"on":"off")+'"></span>'+(w.alive?"alive":"down")+'</td>'+
        '<td class="mut">'+esc(beat)+'</td><td>'+(w.found_24h||0)+'</td></tr>';
    }).join("");
    const dr = document.getElementById("discRecent");
    if(!d.recent.length){ dr.innerHTML = '<tr><td colspan="3" class="mut">no postings discovered yet</td></tr>'; }
    else dr.innerHTML = d.recent.map(r=>{
      const search = esc([r.query,r.board].filter(Boolean).join(" · "));
      return '<tr><td class="mut">'+esc(rel(r.time))+'</td><td>'+esc(r.source||"—")+
        '</td><td>'+(search||'<span class="mut">—</span>')+'</td></tr>';
    }).join("");
  }
}

async function poll(){
  try{
    const r = await fetch("/api/status", {cache:"no-store"});
    if(!r.ok) throw new Error("status "+r.status);
    render(await r.json());
  }catch(e){
    document.getElementById("conn").textContent = "disconnected — retrying";
  }
}

function showTokenBanner(){
  const b = document.getElementById("tokBanner");
  if(b) b.className = "tokbanner show";
}

async function act(action, extra, confirmMsg){
  if(confirmMsg && !window.confirm(confirmMsg)) return;
  try{
    const r = await fetch("/api/action", {method:"POST",
      credentials: "same-origin",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify(Object.assign({action}, extra||{}))});
    if(r.status === 401){
      showTokenBanner();
      toast("token expired — open the ?token= URL printed by the console window", "err");
      return {ok:false, error:"token expired"};
    }
    const j = await r.json();
    if(!r.ok || !j.ok){ toast(j.error || ("action failed ("+r.status+")"), "err"); }
    else{ toast(j.message || "done", "ok"); if(j.status) render(j.status); }
    return j;
  }catch(e){ toast("request failed: "+e.message, "err"); return {ok:false, error:e.message}; }
}

document.getElementById("btnArm").onclick = ()=>{
  const k = parseInt(document.getElementById("canaryK").value, 10);
  if(!(k>=1 && k<=200)){ toast("k must be an integer 1..200", "err"); return; }
  act("arm_canary", {k},
    "Arm "+k+" → the fleet will submit up to "+k+" REAL job applications under your name, then auto-pause. Continue?");
};
document.getElementById("btnLift").onclick = ()=>act("lift_canary", null,
  "Lift canary → the fleet will apply UNBOUNDED (no cap on how many real applications it submits) and will be UNPAUSED. Continue?");
document.getElementById("btnResume").onclick = ()=>act("resume", null,
  "Resume → leasing is re-enabled and the fleet may submit real applications again. Continue?");
document.getElementById("btnPause").onclick = ()=>act("pause", null);
document.getElementById("btnReclaim").onclick = ()=>act("reclaim", null);
document.getElementById("btnCap").onclick = ()=>{
  const v = document.getElementById("capUsd").value;
  const usd = parseFloat(v);
  if(!(usd>=0)){ toast("cap must be a number ≥ 0", "err"); return; }
  act("set_cap", {usd},
    "Set spend cap to $"+usd.toFixed(2)+" → the fleet halts once total est cost reaches this. ($0 = NO cap.) Continue?");
};

document.getElementById("btnExpand").onclick = ()=>act("expand_searches", null,
  "Expand searches → seed/refresh the discovery queue (search_tasks) from your searches.yaml. This queues scrape work; it does NOT submit any applications and does NOT write your brain. Continue?");

async function loadLogs(){
  const wid = document.getElementById("logWorker").value;
  const area = document.getElementById("logArea");
  if(!wid){ area.className = "mut"; area.style.fontSize = "12px";
    area.textContent = "Select a worker to view its logs."; return; }
  area.className = "mut"; area.textContent = "loading…";
  try{
    const r = await fetch("/api/logs?worker=" + encodeURIComponent(wid), {cache:"no-store"});
    const j = await r.json();
    if(!r.ok){ area.textContent = (j && j.error) ? j.error : ("error " + r.status); return; }
    const err = j.last_error ? j.last_error : "(no crash recorded)";
    const log = j.recent_log ? j.recent_log : "(no recent log)";
    area.className = "";
    area.innerHTML =
      '<div class="logblk"><div class="lbl">Last crash (' + esc(wid) +
        ', beat ' + esc(rel(j.last_beat)) + ')</div>' +
        '<pre class="log err">' + esc(err) + '</pre></div>' +
      '<div class="logblk"><div class="lbl">Recent log tail</div>' +
        '<pre class="log">' + esc(log) + '</pre></div>';
  }catch(e){ area.textContent = "request failed: " + e.message; }
}
document.getElementById("btnLogs").onclick = loadLogs;
document.getElementById("logWorker").onchange = loadLogs;

// --- Fleet Doctor: its own fetch (NOT in the 4s status poll; the blob can be large) ---
function renderDiagnostics(d){
  const ct = document.getElementById("docClusters");
  if(!d.clusters || !d.clusters.length){ ct.innerHTML = '<tr><td colspan="4" class="mut">no failure clusters in the window</td></tr>'; }
  else ct.innerHTML = d.clusters.map(c=>
    '<tr><td>'+esc(c.reason)+'</td><td class="mut">'+esc(c.host)+'</td><td class="mut">'+
    esc(c.machine)+'</td><td>'+(c.sample_count||0)+'</td></tr>').join("");

  const at = document.getElementById("docAuto");
  if(!d.auto_fixes || !d.auto_fixes.length){ at.innerHTML = '<tr><td colspan="5" class="mut">no active auto-fixes</td></tr>'; }
  else at.innerHTML = d.auto_fixes.map(a=>{
    const exp = a.expires_at ? rel(a.expires_at).replace(" ago"," from now").replace("never","never") : "—";
    return '<tr><td>'+esc(a.knob_type)+'</td><td class="mut">'+esc(a.scope_key)+'</td><td class="mut">'+
      esc(a.reason||"—")+'</td><td class="mut">'+esc(a.expires_at||"—")+'</td>'+
      '<td><button class="sm" data-revert="'+esc(a.knob_id)+'">Reverse</button></td></tr>';
  }).join("");
  at.querySelectorAll("button[data-revert]").forEach(b=>{
    b.onclick = ()=>act("doctor_revert", {knob_id: parseInt(b.getAttribute("data-revert"),10)},
      "Reverse this Doctor auto-fix? It deactivates the conservative knob (e.g. clears a host_skip or restores a paced gap). Nothing is re-approved and the fleet is NOT resumed.")
      .then(loadDiagnostics);
  });

  const rc = document.getElementById("docRecs");
  if(!d.recommendations || !d.recommendations.length){ rc.className="mut"; rc.textContent = "no open recommendations"; }
  else { rc.className=""; rc.innerHTML = d.recommendations.map(r=>{
    const sev = (r.severity==="severe"?"severe":(r.severity==="warn"?"warn":""));
    return '<div class="rec"><div class="rh"><span class="sev '+sev+'">'+esc(r.severity||"info")+
      '</span><span class="mut" style="font-size:12px">'+esc(r.reason||"")+
      (r.host?(" · "+esc(r.host)):"")+'</span>'+
      '<button class="sm" style="margin-left:auto" data-dismiss="'+esc(r.diagnosis_id)+'">Dismiss</button></div>'+
      '<div class="body">'+esc(r.diagnosis||"")+'</div>'+
      '<div class="recm">'+esc(r.recommendation||"")+'</div></div>';
  }).join(""); }
  rc.querySelectorAll("button[data-dismiss]").forEach(b=>{
    b.onclick = ()=>act("doctor_dismiss", {diagnosis_id: parseInt(b.getAttribute("data-dismiss"),10)})
      .then(loadDiagnostics);
  });
}

async function loadDiagnostics(){
  try{
    const r = await fetch("/api/diagnostics", {cache:"no-store"});
    if(!r.ok) return;
    renderDiagnostics(await r.json());
  }catch(e){ /* leave the last render; the status poll drives the connection banner */ }
}

function renderOutcomes(d){
  const t = document.getElementById("outcomes");
  const rows = (d && d.outcomes) || [];
  if(!rows.length){ t.innerHTML = '<tr><td colspan="4" class="mut">no outcomes yet</td></tr>'; return; }
  t.innerHTML = rows.map(r=>{
    const ct = esc([r.company,r.title].filter(Boolean).join(" — "));
    return '<tr><td class="mut">'+esc(rel(r.time))+'</td><td>'+(ct||'<span class="mut">—</span>')+
      '</td><td>'+esc(r.stage||"—")+'</td><td>'+esc(r.outcome||"—")+'</td></tr>';
  }).join("");
}
async function loadOutcomes(){
  try{
    const r = await fetch("/api/outcomes", {cache:"no-store"});
    if(!r.ok) return;
    renderOutcomes(await r.json());
  }catch(e){ /* leave last render */ }
}
loadOutcomes();
setInterval(loadOutcomes, 15000);

// --- Challenges triage: its own fetch of /api/challenges, folded into the same 4s cycle. ---
function renderChallenges(d){
  const wrap = document.getElementById("chList");
  const groups = (d && d.groups) || [];
  if(!groups.length){ wrap.className = "mut"; wrap.textContent = "no open challenges — nothing parked"; return; }
  wrap.className = "";
  wrap.innerHTML = groups.map((g, gi)=>{
    const rows = g.rows || [];
    const lane = rows.length ? rows[0].lane : null;
    const rowsHtml = rows.map((r, ri)=>{
      const ct = esc([r.company, r.title].filter(Boolean).join(" — ")) || '<span class="mut">—</span>';
      const scoreTxt = (r.score!=null) ? (" · score " + r.score) : "";
      const ageTxt = (r.age_hours!=null) ? (r.age_hours + "h old") : "age unknown";
      return '<div class="chrow">' +
        '<div class="meta"><div class="ct">' + ct + '</div>' +
        '<div class="sub">' + esc(ageTxt + scoreTxt + (r.lane ? (" · " + r.lane) : "")) + '</div></div>' +
        '<div class="btns">' +
        '<a href="' + esc(r.url) + '" target="_blank" rel="noopener"><button class="sm" type="button">Open job</button></a>' +
        '<button class="sm go" data-req="' + gi + ':' + ri + '">I solved it → Re-queue</button>' +
        '<button class="sm danger" data-skip="' + gi + ':' + ri + '">Skip</button>' +
        '</div></div>';
    }).join("");
    return '<div class="chgrp">' +
      '<div class="chgrp-h"><b>' + esc(g.kind||"") + '</b><span>·</span><b>' + esc(g.host||"") +
      '</b><span>·</span><span>' + rows.length + '</span>' +
      '<button class="sm danger" style="margin-left:auto" data-skiphost="' + gi + '">Skip all on host</button>' +
      '</div>' + rowsHtml + '</div>';
  }).join("");

  wrap.querySelectorAll("button[data-req]").forEach(b=>{
    b.onclick = async ()=>{
      const [gi, ri] = b.getAttribute("data-req").split(":").map(Number);
      const row = groups[gi].rows[ri];
      const j = await act("challenge_requeue", {url: row.url, lane: row.lane});
      if(j && j.ok !== false) loadChallenges();
    };
  });
  wrap.querySelectorAll("button[data-skip]").forEach(b=>{
    b.onclick = async ()=>{
      const [gi, ri] = b.getAttribute("data-skip").split(":").map(Number);
      const row = groups[gi].rows[ri];
      const j = await act("challenge_skip", {url: row.url, lane: row.lane});
      if(j && j.ok !== false) loadChallenges();
    };
  });
  wrap.querySelectorAll("button[data-skiphost]").forEach(b=>{
    b.onclick = async ()=>{
      const gi = Number(b.getAttribute("data-skiphost"));
      const g = groups[gi];
      const lane = g.rows.length ? g.rows[0].lane : null;
      if(!window.confirm("Skip all " + g.rows.length + " parked challenge(s) on " + g.host + "?")) return;
      const j = await act("challenge_skip_host", {host: g.host, lane});
      if(j && j.ok !== false) loadChallenges();
    };
  });
}

async function loadChallenges(){
  try{
    const r = await fetch("/api/challenges", {cache:"no-store"});
    if(r.status === 401){ showTokenBanner(); return; }
    if(!r.ok) return;
    renderChallenges(await r.json());
  }catch(e){ /* leave last render; the status poll drives the connection banner */ }
}
loadChallenges();
setInterval(loadChallenges, 4000);

loadDiagnosis();
setInterval(loadDiagnosis, 15000);
loadAgents();
setInterval(loadAgents, 15000);
loadAudit();
setInterval(loadAudit, 15000);
poll();
setInterval(poll, 4000);
loadDiagnostics();
setInterval(loadDiagnostics, 15000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="applypilot-fleet-console",
                                description="LAN-only web control panel for the apply fleet.")
    p.add_argument("--host", default=None,
                   help="Private IPv4 to bind (10.x/172.16-31.x/192.168.x) or 127.0.0.1. "
                        "Auto-detects a private IP if omitted. Refuses 0.0.0.0 / public IPs.")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = p.parse_args(argv)

    host = resolve_host(args.host)
    if not _is_private_bind(host):  # belt-and-suspenders: never reach bind() on a public host
        raise SystemExit(f"refusing to bind non-private host {host!r}")

    server = ThreadingHTTPServer((host, args.port), _Handler)
    # Do NOT print the DSN or any secret -- only the LAN URL the owner should open.
    print(f"ApplyPilot fleet console (LAN-only) -> http://{host}:{args.port}")
    print("Open this from a machine on your LAN. Do NOT port-forward it.")
    print(f"One-tap arm URL (sets the mutation-auth cookie): "
          f"http://{host}:{args.port}/?token={get_console_token()}")
    audit_lifecycle_event(
        "console_start",
        f"started on http://{host}:{args.port} from {_repo_root()}",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        audit_lifecycle_event(
            "console_stop",
            f"stopped on http://{host}:{args.port} from {_repo_root()}",
        )
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
