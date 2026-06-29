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
import ipaddress
import json
import math
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from applypilot.apply import pgqueue
from applypilot.fleet import apply_home_main as H
from applypilot.fleet import codex_bridge as cb

DEFAULT_PORT = 8787
_HB_ALIVE_SECONDS = 150          # apply workers beat ~every tick; >150s stale = dead
_INFERRED_WINDOW_SECONDS = 300   # fallback only (unused: apply workers DO beat)


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
            "SELECT wh.worker_id, wh.state, wh.role, wh.current_job, "
            "       wh.last_beat, aq.company AS cur_company, aq.title AS cur_title, "
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
        out.append({
            "worker_id": r["worker_id"],
            "alive": bool(alive),
            "last_beat": _iso(r["last_beat"]),
            "seconds_since": secs,
            "lane": r["role"],
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


def build_status() -> dict:
    """Assemble the full /api/status object. Opens its OWN short-lived connections
    (each helper rolls back and we always close) so no connection is ever leaked."""
    conn = pgqueue.connect()  # reads APPLYPILOT_FLEET_DSN / DATABASE_URL
    try:
        gate, queue = _gate_and_queue(conn)
        try:
            gate["should_halt"] = bool(pgqueue.should_halt(conn))
        except Exception:
            gate["should_halt"] = gate["paused"]
        workers = _workers(conn)
        recent = _recent(conn, 15)
        ch = cb._challenges(conn)
        challenges = len(ch.get("challenges", []))
        linkedin = _linkedin_summary(conn)
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
    }


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


# Explicit allow-list. The handler indexes THIS dict by the action string; it never
# eval()s or getattr()s anything derived from the request. Any other action -> 400.
_ACTIONS = {
    "arm_canary": _do_arm_canary,
    "lift_canary": _do_lift_canary,
    "pause": _do_pause,
    "resume": _do_resume,
    "reclaim": _do_reclaim,
    "set_cap": _do_set_cap,
}


def run_action(body: dict) -> tuple[bool, str]:
    """Validate + dispatch a single allowed action against a short-lived connection.
    Returns (ok, message). Connection always closed."""
    action = body.get("action")
    fn = _ACTIONS.get(action) if isinstance(action, str) else None
    if fn is None:
        return False, "unknown action"
    conn = pgqueue.connect()
    try:
        msg = fn(conn, body)
        return True, msg
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
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
        if path == "/":
            self._send(200, _INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/status":
            try:
                self._send_json(200, build_status())
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
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
</style>
</head>
<body>
<header>
  <div>
    <h1>ApplyPilot Fleet Console</h1>
    <div class="sub">LAN-only control panel &mdash; this operates a system that submits REAL applications</div>
  </div>
  <div class="sub" id="conn">connecting&hellip;</div>
</header>
<div class="wrap">
  <div class="banner pause" id="banner">
    <span class="dot"></span><span id="bannerText">LOADING</span>
    <small id="updated"></small>
  </div>

  <div class="cards">
    <div class="card"><div class="label">Canary</div>
      <div class="val" id="cCanary">&mdash;</div><div class="hint" id="cCanaryHint"></div></div>
    <div class="card"><div class="label">Spend</div>
      <div class="val" id="cSpend">&mdash;</div><div class="hint" id="cSpendHint">of cap</div></div>
    <div class="card"><div class="label">Apply queue</div>
      <div class="val" id="cQueued">&mdash;</div><div class="hint">queued</div></div>
    <div class="card"><div class="label">Leasable now</div>
      <div class="val" id="cLeasable">&mdash;</div><div class="hint">approved &amp; not deduped</div></div>
    <div class="card"><div class="label">Open challenges</div>
      <div class="val" id="cChallenges">&mdash;</div><div class="hint">need a human</div></div>
  </div>

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

  <section>
    <h2>Workers (apply lane)</h2>
    <table><thead><tr><th>Worker</th><th>Live</th><th>Last beat</th><th>Applied</th><th>Current job</th></tr></thead>
      <tbody id="workers"><tr><td colspan="5" class="mut">&hellip;</td></tr></tbody></table>
  </section>

  <section>
    <h2>Recent activity</h2>
    <table><thead><tr><th>Time</th><th>Worker</th><th>Status</th><th>Company / Title</th></tr></thead>
      <tbody id="recent"><tr><td colspan="4" class="mut">&hellip;</td></tr></tbody></table>
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

function render(s){
  lastUpdate = Date.now();
  document.getElementById("conn").textContent = "connected";
  const g = s.gate, q = s.queue.apply, li = s.linkedin;
  const paused = g.paused || g.should_halt;
  const banner = document.getElementById("banner");
  banner.className = "banner " + (paused ? "pause" : "run");
  document.getElementById("bannerText").textContent =
    paused ? (g.should_halt && !g.paused ? "HALTED (spend cap)" : "PAUSED") : "RUNNING";
  document.getElementById("updated").textContent =
    "updated " + rel(s.now) + (g.should_halt ? " • halt active" : "");

  document.getElementById("cCanary").textContent =
    g.canary_enabled ? (g.canary_remaining==null ? "on" : g.canary_remaining) : "off";
  document.getElementById("cCanaryHint").textContent =
    g.canary_enabled ? "remaining (auto-pause at 0)" : "disabled (unbounded if running)";
  document.getElementById("cSpend").textContent = "$" + (g.spent_usd||0).toFixed(2);
  document.getElementById("cSpendHint").textContent =
    g.spend_cap_usd>0 ? ("of $" + g.spend_cap_usd.toFixed(2) + " cap") : "no cap set";
  document.getElementById("cQueued").textContent = q.queued;
  document.getElementById("cLeasable").textContent = g.leasable;
  document.getElementById("cChallenges").textContent = s.challenges;

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

async function act(action, extra, confirmMsg){
  if(confirmMsg && !window.confirm(confirmMsg)) return;
  try{
    const r = await fetch("/api/action", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify(Object.assign({action}, extra||{}))});
    const j = await r.json();
    if(!r.ok || !j.ok){ toast(j.error || ("action failed ("+r.status+")"), "err"); }
    else{ toast(j.message || "done", "ok"); if(j.status) render(j.status); }
  }catch(e){ toast("request failed: "+e.message, "err"); }
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

poll();
setInterval(poll, 4000);
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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
