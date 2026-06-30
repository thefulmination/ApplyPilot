"""Read-only data assembly + CSV for the outcomes dashboard. This module contains
both the read-only data assembly functions and the stdlib HTTP server (serve / _Handler)."""

from __future__ import annotations

import csv
import io
import ipaddress
import json
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from applypilot.config import DB_PATH
from applypilot.lane_insights import compute_lane_insights, derive_segments
from applypilot.outcome_timeline import build_timeline

_UNIVERSE_SQL = """
    SELECT j.url, j.title, j.company, j.source_board, j.location, j.salary,
           j.fit_score, j.audit_score, j.fit_gap_category, j.applied_at
      FROM jobs j
      LEFT JOIN applications a ON a.job_url = j.url
     WHERE j.apply_status = 'applied' OR a.job_url IS NOT NULL
     ORDER BY COALESCE(j.applied_at, j.discovered_at) DESC
"""

_EVENT_COLS = (
    "message_id", "occurred_at", "stage", "outcome", "reason",
    "sender", "subject", "snippet", "body_text", "confidence", "extracted_by",
)

_CSV_FIELDS = (
    "job_url", "company", "title", "source_board", "applied_at", "current_stage",
    "outcome", "responded", "positive", "first_response_days", "decision_days",
    "silent_days",
)


def get_tracked_universe(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(_UNIVERSE_SQL).fetchall()]


def _events_for(conn, job_url: str) -> list[dict]:
    rows = conn.execute(
        f"SELECT {', '.join(_EVENT_COLS)} FROM email_events "
        "WHERE job_url = ? ORDER BY occurred_at",
        (job_url,),
    ).fetchall()
    return [dict(r) for r in rows]


def build_application_rows(conn, *, now_iso: str | None = None) -> list[dict]:
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    rows = []
    for job in get_tracked_universe(conn):
        events = _events_for(conn, job["url"])
        tl = build_timeline(job.get("applied_at"), events, now_iso=now_iso)
        rows.append({
            "job_url": job["url"],
            "title": job.get("title"),
            "company": job.get("company"),
            "source_board": job.get("source_board"),
            "applied_at": job.get("applied_at"),
            "current_stage": tl["current_stage"],
            "outcome": tl["outcome"],
            "responded": tl["responded"],
            "positive": tl["positive"],
            "first_response_days": tl["first_response_days"],
            "decision_days": tl["decision_days"],
            "silent_days": tl["silent_days"],
            "segments": derive_segments(job),
            "events": tl["ordered"],
        })
    return rows


def build_insights(rows: list[dict], *, floor: int = 8) -> dict:
    apps = [{"responded": r["responded"], "positive": r["positive"],
             "segments": r["segments"]} for r in rows]
    return compute_lane_insights(apps, floor=floor)


def build_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k) for k in _CSV_FIELDS})
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Task 7: stdlib HTTP server (read-only, loopback/private hosts only)
# ---------------------------------------------------------------------------

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>ApplyPilot Outcomes</title>
<style>
 body{font:14px system-ui;margin:1.5rem;color:#111}
 table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid #ddd;padding:6px 8px;text-align:left}
 th{cursor:pointer;background:#f6f6f6}.warm{color:#0a7a0a;font-weight:600}.cold{color:#b00}.pill{font-size:12px;padding:1px 6px;border-radius:8px;background:#eee}
 tr.detail td{background:#fafafa;font-size:13px}details{margin:2px 0}
</style></head><body>
<h2>ApplyPilot Outcomes</h2>
<div id="summary"></div>
<h3>Applications</h3>
<table id="apps"><thead><tr>
 <th>Company</th><th>Title</th><th>Board</th><th>Applied</th><th>Stage</th>
 <th>Outcome</th><th>1st reply (d)</th><th>Decision (d)</th><th>Silent (d)</th>
</tr></thead><tbody></tbody></table>
<h3>Lane insights</h3><div id="insights"></div>
<p><a href="/export.csv">Download CSV</a></p>
<script>
async function load(){
 const d = await (await fetch('/api/data')).json();
 const tb = document.querySelector('#apps tbody');
 for(const r of d.rows){
  const tr=document.createElement('tr');
  tr.innerHTML=`<td>${r.company||''}</td><td>${r.title||''}</td><td>${r.source_board||''}</td>
   <td>${(r.applied_at||'').slice(0,10)}</td><td>${r.current_stage}</td><td>${r.outcome||''}</td>
   <td>${r.first_response_days??''}</td><td>${r.decision_days??''}</td><td>${r.silent_days??''}</td>`;
  tb.appendChild(tr);
  if(r.events.length){
   const dt=document.createElement('tr');dt.className='detail';
   const td=document.createElement('td');td.colSpan=9;
   td.innerHTML=r.events.map(e=>`<details><summary>${(e.occurred_at||'').slice(0,10)} · ${e.stage} · ${e.subject||''}</summary>
     <div><b>Reason:</b> ${e.reason||'—'}</div><pre style="white-space:pre-wrap">${(e.body_text||'').slice(0,2000)}</pre></details>`).join('');
   dt.appendChild(td);tb.appendChild(dt);
  }
 }
 const ins=d.insights;
 document.querySelector('#summary').innerHTML=
   `<span class="pill">${ins.n} applications</span>
    <span class="pill">baseline reply rate ${(ins.baseline_response_rate*100).toFixed(0)}%</span>`;
 const warm=ins.segments.filter(s=>s.flag==='warm'), cold=ins.segments.filter(s=>s.flag==='cold');
 const fmt=s=>`<li><span class="${s.flag}">${s.dimension}=${s.value}</span> — ${(s.response_rate*100).toFixed(0)}% reply (${s.n_responded}/${s.n_applied}, CI ${(s.ci_low*100).toFixed(0)}–${(s.ci_high*100).toFixed(0)}%)</li>`;
 document.querySelector('#insights').innerHTML=
   `<b>Warm lanes</b><ul>${warm.map(fmt).join('')||'<li>none yet</li>'}</ul>
    <b>Cold lanes</b><ul>${cold.map(fmt).join('')||'<li>none yet</li>'}</ul>`;
}
load();
</script></body></html>"""


def _read_only_conn(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _make_handler(db_path: str):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence default stderr logging
            pass

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/index"):
                return self._send(200, _PAGE.encode(), "text/html; charset=utf-8")
            conn = _read_only_conn(db_path)
            try:
                rows = build_application_rows(conn)
                if self.path.startswith("/api/data"):
                    payload = {"rows": rows, "insights": build_insights(rows)}
                    return self._send(200, json.dumps(payload, default=str).encode(),
                                      "application/json")
                if self.path.startswith("/export.csv"):
                    return self._send(200, build_csv(rows).encode(),
                                      "text/csv; charset=utf-8")
                return self._send(404, b"not found", "text/plain")
            finally:
                conn.close()
    return _Handler


def serve(host: str = "127.0.0.1", port: int = 8765, db_path=None) -> None:
    """Serve the read-only outcomes dashboard. Binds loopback/private IPs only."""
    ip = ipaddress.ip_address(host)
    if ip.is_unspecified or not (ip.is_loopback or ip.is_private):
        raise ValueError(f"refusing to bind non-private/unspecified host {host}")
    db_path = str(db_path or DB_PATH)
    server = ThreadingHTTPServer((host, port), _make_handler(db_path))
    print(f"Outcomes dashboard: http://{host}:{port}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
