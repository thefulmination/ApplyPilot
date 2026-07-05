"""Residential worker LOOP -- spec §5/§6/§7.

A :class:`WorkerLoop` is the thin per-slot loop that runs on each residential
machine (and, wrapped in the Helper app, on friend machines -- §16.5). It leases
work by ROLE, drives it, and writes the result back through the proven v3 queue
helpers (``applypilot.fleet.queue``). It NEVER touches the brain or Postgres
directly beyond the queue/governor helpers; in production it would sit behind the
broker RPC surface, but the lease/result functions share the same call shapes.

WHY EVERYTHING BROWSER-FACING IS INJECTED
-----------------------------------------
The real apply path drives a live Chromium via Playwright
(``applypilot.fleet.launcher.run_job``), the real discovery path drives
``jobspy``, and the real compute path calls an LLM. None of those are unit-
testable in CI. So the loop takes them as INJECTED callables:

    apply_fn(job)   -> {"run_status": str, "est_cost_usd": float}
                                    # wraps launcher.run_job; returns run_job's
                                    #   terminal status (the agent already
                                    #   classified by SEEING the page). Legacy
                                    #   path: may also return a bare HTML string
                                    #   or (html, frames_text, final_url, status)
                                    #   tuple (old contract; existing tests use this).
    search_fn(task) -> postings     # wraps jobspy; returns a list of posting dicts.
    score_fn(job)   -> (result, cost_usd)   # wraps the LLM scorer/auditor.
    classify_fn     = captcha.classify       # the pure detector (§7.1).

In tests these are FAKES (see tests/test_fleet_v3_worker.py): a fake ``score_fn``
drives the compute path end-to-end against real Postgres, and a fake ``apply_fn``
returning captcha HTML proves the wall path raises an ``auth_challenge`` row and
PARKS the job (never marks it applied).

``conn_factory()`` returns a fresh pgqueue connection (dict_row cursors); the loop
owns the connection lifecycle so it can reconnect after a transient DB blip.
"""
from __future__ import annotations

import collections
import datetime as _dt
import os
import re
from typing import Any, Callable, Optional

from applypilot.fleet import captcha as _captcha
from applypilot.fleet import governor
from applypilot.fleet import queue

# Roles a machine slot may carry (a machine may run several slots / roles -- §5).
ROLE_APPLY = "apply"
ROLE_COMPUTE = "compute"
ROLE_DISCOVERY = "discovery"
ROLE_LINKEDIN = "linkedin"

_REDACTED = "[REDACTED]"

# Pattern-based secret redaction. The fleet submits REAL applications, so a single
# leaked DSN / password / token reaching worker_heartbeat or the LAN console is a
# CRITICAL defect. These patterns catch the structural secrets (connection strings,
# bearer tokens, known key prefixes); _scrub() additionally redacts the LITERAL
# values of secret-bearing env vars present in os.environ (see below).
_SECRET_PATTERNS = [
    # libpq conninfo key=value secrets (password / pwd anywhere in a DSN/conninfo).
    re.compile(r"(?i)\b(password|pwd)\s*=\s*\S+"),
    # A full libpq keyword DSN (host=... ... dbname=...) -- redact the whole run so
    # host/user/dbname triplets never leak even without a password token present.
    re.compile(r"(?i)\bhost\s*=\s*\S+(?:\s+\w+\s*=\s*\S+)*"),
    # postgres:// (and other) URL DSNs, incl. embedded user:pass@host credentials.
    re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s'\"]+"),
    # JSON / header style "password": "...", token=..., api_key: ...
    # NOTE: the optional ['\"]? BEFORE the separator lets the keyword's own closing
    # quote sit between it and the colon (JSON `"api_key":"..."`); the value class
    # [^\s,'\"}{]+ stops at JSON/quote delimiters so we redact exactly the value.
    re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key)\b['\"]?\s*[:=]\s*['\"]?[^\s,'\"}{]+"),
    # Authorization: Bearer <token>
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"),
    # Known token prefixes: OpenAI-style sk-..., GitHub ghp_/gho_/ghs_..., etc.
    re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[opsu]_[A-Za-z0-9]{20,}"),
    # Long base64-ish opaque blobs (likely keys/tokens). 40+ chars to avoid eating words.
    re.compile(r"\b[A-Za-z0-9+/_\-]{40,}={0,2}\b"),
]


def _scrub(text: Optional[str]) -> str:
    """Redact secrets from any text BEFORE it is shipped to Postgres or rendered in
    the LAN console. Returns "" for None. This is the load-bearing safety control:

      * the LITERAL values of secret-bearing env vars (FLEET_PG_DSN /
        APPLYPILOT_FLEET_DSN / DATABASE_URL / DEEPSEEK_API_KEY / any *_API_KEY /
        *_TOKEN) are removed first -- an exact-value match is the surest redaction;
      * then structural patterns (conninfo, URL DSNs, bearer/api tokens, known key
        prefixes, long base64 blobs) catch anything constructed at runtime.

    Never raises (a logging/scrub error must never break the heartbeat)."""
    if text is None:
        return ""
    try:
        s = str(text)
        # 1) Exact env-value redaction (longest first so a value that is a substring
        #    of another doesn't leave a fragment behind).
        try:
            secret_values = []
            for k, v in os.environ.items():
                if not v or len(v) < 4:
                    continue
                ku = k.upper()
                if (ku in ("FLEET_PG_DSN", "APPLYPILOT_FLEET_DSN", "DATABASE_URL",
                           "DEEPSEEK_API_KEY")
                        or ku.endswith("_API_KEY") or ku.endswith("_TOKEN")
                        or ku.endswith("_DSN") or ku.endswith("_SECRET")
                        or ku.endswith("_PASSWORD")):
                    secret_values.append(v)
            for v in sorted(set(secret_values), key=len, reverse=True):
                s = s.replace(v, _REDACTED)
        except Exception:
            pass
        # 2) Structural pattern redaction.
        for pat in _SECRET_PATTERNS:
            s = pat.sub(_REDACTED, s)
        return s
    except Exception:
        # Never let scrubbing failure leak raw text OR break the caller.
        return ""


def _insert_challenge(conn, *, url, worker_id, machine_owner, home_ip, kind, route,
                      screenshot_url=None, commit=True) -> int:
    """Raise an ``auth_challenge`` row (captcha inbox, spec §7.3). Surrogate ``id``
    PK so the SAME url can wall on a friend box AND be re-attempted on the owner
    box without colliding (C4). Returns the new challenge id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO auth_challenge (url, worker_id, machine_owner, home_ip, kind, route, screenshot_url) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (url, worker_id, machine_owner, home_ip, kind, route, screenshot_url),
        )
        cid = cur.fetchone()["id"]
    if commit:
        conn.commit()
    return cid


def _heartbeat(conn, *, worker_id, machine_owner, home_ip, role, state, current_job=None,
               sw_version=None, last_error=None, recent_log=None, current_agent=None,
               current_model=None, agent_chain=None, last_agent_switch_at=None,
               last_agent_switch_reason=None, commit=True) -> None:
    """UPSERT this worker's ``worker_heartbeat`` row (~every iteration, spec §11/R5).
    A missing heartbeat for > lease TTL is what lets the reclaim sweep re-queue a
    crashed worker's job (§5 stateless self-resume).

    ``last_error`` / ``recent_log`` (crash-visibility, scrubbed by the caller) OVERWRITE
    on every beat (set ...=EXCLUDED.x), unlike ``sw_version`` which COALESCEs to keep a
    previously reported version when a beat omits it."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO worker_heartbeat (worker_id, machine_owner, home_ip, role, state, current_job, "
            "sw_version, last_error, recent_log, current_agent, current_model, agent_chain, "
            "last_agent_switch_at, last_agent_switch_reason, last_beat) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now()) "
            "ON CONFLICT (worker_id) DO UPDATE SET machine_owner=EXCLUDED.machine_owner, home_ip=EXCLUDED.home_ip, "
            "role=EXCLUDED.role, state=EXCLUDED.state, current_job=EXCLUDED.current_job, "
            "sw_version=COALESCE(EXCLUDED.sw_version, worker_heartbeat.sw_version), "
            "last_error=EXCLUDED.last_error, recent_log=EXCLUDED.recent_log, "
            "current_agent=EXCLUDED.current_agent, current_model=EXCLUDED.current_model, "
            "agent_chain=EXCLUDED.agent_chain, last_agent_switch_at=COALESCE(EXCLUDED.last_agent_switch_at, worker_heartbeat.last_agent_switch_at), "
            "last_agent_switch_reason=COALESCE(EXCLUDED.last_agent_switch_reason, worker_heartbeat.last_agent_switch_reason), "
            "last_beat=now()",
            (worker_id, machine_owner, home_ip, role, state, current_job, sw_version,
             last_error, recent_log, current_agent, current_model, agent_chain,
             last_agent_switch_at, last_agent_switch_reason),
        )
    if commit:
        conn.commit()


def _normalize_apply_output(out) -> tuple[str, str, str, Optional[int]]:
    """Accept either a bare HTML string or a (html, frames_text, final_url, status)
    tuple from ``apply_fn`` and normalize to the 4-tuple the classifier wants."""
    if isinstance(out, (tuple, list)):
        html = out[0] if len(out) > 0 else ""
        frames = out[1] if len(out) > 1 else ""
        final_url = out[2] if len(out) > 2 else ""
        status = out[3] if len(out) > 3 else None
        return html or "", frames or "", final_url or "", status
    return out or "", "", "", None


class WorkerLoop:
    """One worker slot. Call :meth:`run_once` in a loop (or :meth:`run_forever`).

    Args:
        conn_factory:  zero-arg callable returning a fresh pgqueue connection.
        worker_id:     unique per slot (machine + slot index).
        home_ip:       this machine's residential egress IP (governor key).
        role:          one of ``apply`` / ``compute`` / ``discovery``.
        apply_fn:      INJECTED. ``apply_fn(job) -> html`` (or a 4-tuple). Wraps
                       ``container_worker.run_job``. STUBBED in tests.
        score_fn:      INJECTED. ``score_fn(job) -> (result_dict, cost_usd)``.
                       Wraps the LLM scorer/auditor. STUBBED in tests.
        search_fn:     INJECTED. ``search_fn(task) -> list[posting]``. Wraps
                       ``jobspy``. STUBBED in tests.
        classify_fn:   the captcha detector; defaults to ``captcha.classify``.
        machine_owner: who owns the physical machine (for the captcha inbox / R12).
        sw_version:    reported in the heartbeat (R12 / §16.5).
    """

    def __init__(
        self,
        conn_factory: Callable[[], Any],
        worker_id: str,
        *,
        home_ip: str,
        role: str,
        apply_fn: Optional[Callable[[dict], Any]] = None,
        score_fn: Optional[Callable[[dict], tuple]] = None,
        search_fn: Optional[Callable[[dict], list]] = None,
        classify_fn: Callable[..., str] = _captcha.classify,
        machine_owner: Optional[str] = None,
        sw_version: Optional[str] = None,
        on_owner_machine: bool = False,
        public_ip: Optional[str] = None,
        owner_ip: Optional[str] = None,
        compute_fns: Optional[dict] = None,
        log_tail_fn: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        if role not in (ROLE_APPLY, ROLE_COMPUTE, ROLE_DISCOVERY, ROLE_LINKEDIN):
            raise ValueError(f"unknown role: {role!r}")
        self.conn_factory = conn_factory
        self.worker_id = worker_id
        self.home_ip = home_ip
        self.role = role
        self.apply_fn = apply_fn
        self.score_fn = score_fn
        self.search_fn = search_fn
        self.classify_fn = classify_fn
        self.machine_owner = machine_owner
        if sw_version is None:
            from applypilot.fleet import software_version

            sw_version = software_version.current_sw_version()
        self.sw_version = sw_version
        self.on_owner_machine = on_owner_machine
        self.public_ip = public_ip or home_ip
        self.owner_ip = owner_ip
        # Crash + log visibility (shipped on every beat). _last_error holds the most
        # recent SCRUBBED traceback (None until a tick crashes). _events is an in-memory
        # ring telling the recent tick story (leased X / wrote Y). log_tail_fn, when set
        # (apply lane), returns the rich agent log tail; otherwise the ring is shipped.
        self._last_error: Optional[str] = None
        self._events: collections.deque = collections.deque(maxlen=40)
        self._log_tail_fn = log_tail_fn
        self._started_at = _dt.datetime.now(_dt.timezone.utc)
        self.current_agent: Optional[str] = None
        self.current_model: Optional[str] = None
        self.agent_chain: Optional[str] = None
        self.last_agent_switch_at: Optional[_dt.datetime] = None
        self.last_agent_switch_reason: Optional[str] = None
        # Remote-command state: a 'pause' flips this until 'resume'; consumed in
        # run_once BETWEEN jobs (never mid-apply). See _handle_commands.
        self._paused = False
        if compute_fns is not None:
            self.compute_fns = compute_fns
            self._legacy_score_fn = None
        elif score_fn is not None:
            # Back-compat: old score_fn received the full job dict; _tick_compute
            # now calls fn(payload), so we wrap it to restore the original behaviour.
            _sf = score_fn
            self.compute_fns = {"score": _sf}
            self._legacy_score_fn = _sf
        else:
            self.compute_fns = {}
            self._legacy_score_fn = None

    # -- connection -----------------------------------------------------------
    def _connect(self):
        return self.conn_factory()

    # -- event ring (crash/log visibility) ------------------------------------
    def _record_event(self, msg) -> None:
        """Append a timestamped, SCRUBBED one-line event to the in-memory ring. Cheap
        (bounded deque, one scrub) and MUST NOT raise -- a logging failure can never be
        allowed to change tick control flow or break a heartbeat."""
        try:
            import datetime as _dt
            ts = _dt.datetime.now(_dt.timezone.utc).strftime("%H:%M:%S")
            line = _scrub(str(msg)).replace("\n", " ")[:300]
            self._events.append(f"{ts} {line}")
        except Exception:
            pass

    # -- remote commands (owner -> worker, consumed BETWEEN jobs) --------------
    def _handle_commands(self, conn) -> Optional[str]:
        """Consume open remote_commands for this worker at the top of run_once --
        i.e. strictly BETWEEN jobs, never mid-apply. Every command is acked (a
        broadcast '*' is acked per-worker so it reaches the whole fleet).

        pause/resume flip ``self._paused``; restart/drain return the command name so
        the caller exits the process (the supervisor -- fleet-agent reconcile or the
        scheduled task -- respawns it); self_update is acked as a no-op (superseded
        by the fleet-agent pull-updater, spec 2026-07-03). Fail-safe: a poll error
        returns None and the tick proceeds -- a DB blip must never stop a worker."""
        from applypilot.fleet import heartbeat as _hb
        try:
            cmds = _hb.poll_commands(conn, self.worker_id)
        except Exception:
            return None
        stop: Optional[str] = None
        for c in cmds:
            cmd = c.get("command")
            try:
                _hb.ack_command(conn, c["id"], self.worker_id)
            except Exception:
                continue  # never acked -> re-delivered next tick; do not act twice
            if cmd == "pause":
                self._paused = True
                self._record_event("command: pause (idling until resume)")
            elif cmd == "resume":
                self._paused = False
                self._record_event("command: resume")
            elif cmd in ("restart", "drain"):
                issued_at = c.get("issued_at")
                if c.get("worker_id") == self.worker_id and issued_at and issued_at < self._started_at:
                    self._record_event(f"command: stale {cmd} ignored")
                    continue
                self._record_event(f"command: {cmd} -> exiting between jobs")
                stop = cmd
            elif cmd == "self_update":
                self._record_event(
                    "command: self_update acked as no-op (fleet-agent pull-updater owns code rollout)")
            else:
                self._record_event(f"command: unknown {cmd!r} acked + ignored")
        return stop

    # -- the single tick ------------------------------------------------------
    def run_once(self) -> dict:
        """Lease one unit of work for this role, process it, heartbeat, and return
        a small status dict describing what happened. Returns ``{'action':'idle'}``
        when there is nothing to lease.

        Outcomes (``action``):
            ``compute_done``      -- a compute job was scored and written.
            ``search_done``       -- a search task ran and rescheduled.
            ``applied``           -- an apply job submitted + result written.
            ``parked_challenge``  -- an apply hit a wall: a challenge was raised
                                     and the job was PARKED (NOT marked applied).
            ``skipped``           -- an apply wall that no human can solve (cf /
                                     invisible_block): closed as blocked.
            ``idle``              -- nothing eligible to lease.
            ``paused``            -- a remote 'pause' is in effect: heartbeat only.
            ``stop``              -- a remote 'restart'/'drain' arrived: the caller
                                     must exit this worker process (between jobs).
        """
        with self._connect() as conn:
            stop = self._handle_commands(conn)
            if stop is not None:
                return {"action": "stop", "command": stop}
            if self._paused:
                self._beat(conn, state="paused")
                return {"action": "paused"}
            if self.role == ROLE_COMPUTE:
                return self._tick_compute(conn)
            if self.role == ROLE_DISCOVERY:
                return self._tick_discovery(conn)
            if self.role == ROLE_LINKEDIN:
                return self._tick_linkedin(conn)
            return self._tick_apply(conn)

    # -- COMPUTE: score/audit/tailor -- IP-free, cost-governed (§8, R14) -------
    def _tick_compute(self, conn) -> dict:
        job = queue.lease_compute(conn, self.worker_id)
        if job is None:
            self._beat(conn, state="idle")
            return {"action": "idle"}
        task = job.get("task") or "score"
        self._record_event(f"leased compute {task} {job.get('url')}")
        fn = self.compute_fns.get(task)
        if fn is None:
            raise RuntimeError(f"compute role has no handler for task {task!r}")
        self._beat(conn, state="computing", current_job=job["url"])
        # Legacy score_fn receives the full job dict; new compute_fns receive payload.
        if fn is self._legacy_score_fn:
            result, cost = fn(job)
        else:
            result, cost = fn(job.get("payload") or {"url": job["url"]})
        queue.write_compute_result(
            conn, self.worker_id, job["url"], result=result, status=result.get("status", "done"),
            cost_usd=cost or 0, model=result.get("model"), provider=result.get("provider"),
            task=task, machine_owner=self.machine_owner,
        )
        self._record_event(f"wrote compute {result.get('status', 'done')} {job.get('url')}")
        self._beat(conn, state="idle")
        return {"action": "compute_done", "url": job["url"], "task": task, "cost_usd": cost or 0}

    # -- DISCOVERY: governed scrape (RF2/§8.5) --------------------------------
    def _tick_discovery(self, conn) -> dict:
        task = queue.lease_search(conn, self.worker_id)
        if task is None:
            self._beat(conn, state="idle")
            return {"action": "idle"}
        self._record_event(f"leased search {task.get('task_id')} {task.get('query') or task.get('board')}")
        self._beat(conn, state="searching", current_job=task["task_id"])
        if self.search_fn is None:
            raise RuntimeError("discovery role requires an injected search_fn")
        error = None
        postings: list = []
        try:
            postings = self.search_fn(task) or []
        except Exception:  # a scrape-block / transport error parks the task (RF3)
            error = "blocked"
        staged = 0
        if not error and postings:
            staged = queue.push_discovered(
                conn, task_id=task["task_id"],
                source_label=task.get("query") or task.get("board"),
                worker_id=self.worker_id, postings=postings, commit=False,
            )
        queue.complete_search(
            conn, self.worker_id, task["task_id"],
            result_count=len(postings), board=task.get("board"), error=error,
        )
        self._record_event(
            f"wrote search {('error:' + error) if error else 'ok'} "
            f"{task.get('task_id')} found={len(postings)} staged={staged}")
        self._beat(conn, state="idle")
        return {"action": "search_done", "task_id": task["task_id"],
                "result_count": len(postings), "staged": staged, "error": error}

    # -- APPLY: governed, dedup-gated, approval-gated, captcha-aware (§5/§7) ---
    def _tick_apply(self, conn) -> dict:
        job = queue.lease_apply(conn, self.worker_id, home_ip=self.home_ip)
        if job is None:
            self._beat(conn, state="idle")
            return {"action": "idle"}
        url = job["url"]
        target_host = job.get("target_host")
        self._record_event(f"leased apply {url} host={target_host}")
        self._beat(conn, state="applying", current_job=url)
        if self.apply_fn is None:
            raise RuntimeError("apply role requires an injected apply_fn")

        # Drive the real browser (STUBBED in tests) and classify the resulting page.
        out = self.apply_fn(job)
        if isinstance(out, dict) and "run_status" in out:
            return self._apply_status_passthrough(conn, url, target_host, out)
        # --- legacy html-classify path (existing test fakes return html/4-tuple) ---
        html, frames_text, final_url, status = _normalize_apply_output(out)
        kind = self.classify_fn(html, frames_text=frames_text, final_url=final_url, status=status)

        if not _captcha.is_wall(kind):
            # clear / invisible_pass -> the application went through; record it.
            queue.write_apply_result(
                conn, self.worker_id, url, status="applied", apply_status="applied",
                target_host=target_host, home_ip=self.home_ip, outcome="success",
            )
            self._record_event(f"wrote apply applied {url} ({kind})")
            self._beat(conn, state="idle")
            return {"action": "applied", "url": url, "kind": kind}

        route = _captcha.route_for(kind, on_owner_machine=self.on_owner_machine)

        if route == "auto_otp":
            # email_otp would be auto-solved via the Gmail relay then RE-classified.
            # That relay is broker-side and STUBBED out of this scaffold; for the
            # loop we raise an owner-routed challenge so the job is never lost. A
            # production build resolves the code and re-drives the form here.
            self._raise_and_park(conn, url, kind, route="owner_inbox", outcome="captcha",
                                 target_host=target_host)
            return {"action": "parked_challenge", "url": url, "kind": kind, "route": "auto_otp"}

        if route == "skip":
            # cf / invisible_block: nothing a human can solve. Close as blocked so
            # the governor's block/breaker math sees it; do NOT park for a human.
            queue.write_apply_result(
                conn, self.worker_id, url, status="blocked", apply_status="failed",
                apply_error=f"captcha:{kind}", target_host=target_host, home_ip=self.home_ip,
                outcome="block",
            )
            self._record_event(f"wrote apply blocked {url} (captcha:{kind})")
            self._beat(conn, state="idle")
            return {"action": "skipped", "url": url, "kind": kind, "route": route}

        # owner_tray / owner_inbox: a human must solve it. Raise a challenge and
        # PARK -- the lease is HELD (not released, not closed): the job is neither
        # double-claimable nor marked applied (spec §7.3 frozen challenge-hold).
        # A pure LOGIN wall is not a bot-detection signal (the site just requires an
        # account), so it records NO governor outcome -- otherwise a login-only host
        # would inflate challenge_rate and false-trip its breaker. Real captcha walls
        # (visible_captcha / sms_otp) DO feed the breaker.
        wall_outcome = None if kind == "login_gate" else "captcha"
        self._raise_and_park(conn, url, kind, route=route, outcome=wall_outcome,
                             target_host=target_host)
        return {"action": "parked_challenge", "url": url, "kind": kind, "route": route}

    _WALL_STATUSES = ("captcha", "login_issue", "auth_required")
    _CRASH_STATUSES = ("failed:no_result_line", "failed:timeout")

    def _linkedin_halt_seconds(self) -> int:
        import os
        return int(os.environ.get("APPLYPILOT_LINKEDIN_HALT_COOLDOWN") or 21600)

    def _tick_linkedin(self, conn) -> dict:
        # Pre-checks (belts; the lease SQL is the real enforcement). Session pre-flight +
        # halt pre-check; the interlock is held by the entrypoint for the worker's life.
        job = queue.lease_linkedin(conn, self.worker_id, public_ip=self.public_ip, owner_ip=self.owner_ip)
        if job is None:
            self._beat(conn, state="idle")
            return {"action": "idle"}
        url = job["url"]
        self._record_event(f"leased linkedin {url}")
        self._beat(conn, state="applying", current_job=url)
        if self.apply_fn is None:
            raise RuntimeError("linkedin role requires an injected apply_fn")
        out = self.apply_fn(job)
        run_status = (out or {}).get("run_status") or ""
        cost = (out or {}).get("est_cost_usd", 0)
        from applypilot.apply.launcher import is_usage_limit_result
        if is_usage_limit_result(run_status):
            queue.requeue_linkedin(conn, self.worker_id, url, apply_error=run_status[:200])
            self._record_event(f"requeued linkedin (usage_limit) {url}")
            self._beat(conn, state="idle")
            return {"action": "usage_limit", "url": url}
        if run_status == "applied":
            queue.write_linkedin_result(conn, self.worker_id, url, status="applied", apply_status="applied",
                                        est_cost_usd=cost, outcome="success",
                                        apply_channel=(out or {}).get("apply_channel"),
                                        apply_external_host=(out or {}).get("apply_external_host"))
            self._record_event(f"wrote linkedin applied {url}")
            self._beat(conn, state="idle")
            return {"action": "applied", "url": url}
        if (run_status in ("login_issue", "auth_required")
                and (out or {}).get("apply_channel") == "external"):
            external_host = (out or {}).get("apply_external_host")
            apply_error = "external_auth_required"
            if external_host:
                apply_error = f"{apply_error}:{str(external_host)[:160]}"
            queue.write_linkedin_result(conn, self.worker_id, url, status="failed",
                                        apply_status="auth_required", apply_error=apply_error[:200],
                                        est_cost_usd=cost,
                                        apply_channel=(out or {}).get("apply_channel"),
                                        apply_external_host=external_host)
            self._record_event(f"wrote linkedin external auth_required {url}")
            self._beat(conn, state="idle")
            return {"action": "external_auth_required", "url": url}
        if run_status in self._WALL_STATUSES:
            queue.park_linkedin_challenge(conn, self.worker_id, url, halt_seconds=self._linkedin_halt_seconds())
            _insert_challenge(conn, url=url, worker_id=self.worker_id, machine_owner=self.machine_owner,
                              home_ip=self.home_ip, kind="visible_captcha" if run_status == "captcha" else "login_gate",
                              route="owner_inbox")
            self._record_event(f"parked linkedin challenge {url} ({run_status})")
            self._beat(conn, state="challenge_pending", current_job=url)
            return {"action": "parked_challenge", "url": url}
        if run_status in self._CRASH_STATUSES or run_status.startswith("failed:worker_error"):
            queue.write_linkedin_result(conn, self.worker_id, url, status="crash_unconfirmed",
                                        apply_status="crash_unconfirmed", apply_error=run_status[:200], est_cost_usd=cost)
            self._record_event(f"wrote linkedin crash_unconfirmed {url} ({run_status})")
            self._beat(conn, state="idle")
            return {"action": "crash_unconfirmed", "url": url}
        queue.write_linkedin_result(conn, self.worker_id, url, status="failed", apply_status="failed",
                                    apply_error=(run_status or "unknown")[:200], est_cost_usd=cost)
        self._record_event(f"wrote linkedin failed {url} ({run_status or 'unknown'})")
        self._beat(conn, state="idle")
        return {"action": "failed", "url": url}

    def _apply_status_passthrough(self, conn, url, target_host, res: dict) -> dict:
        """Route off run_job's terminal status (the agent already classified by SEEING
        the page). applied -> applied; a wall -> park; a ran-but-no-clean-result crash
        -> crash_unconfirmed (possibly submitted, never re-leased); else -> failed."""
        run_status = res.get("run_status") or ""
        cost = res.get("est_cost_usd", 0)
        agent = res.get("agent")   # which apply agent ran -> attributes the spend (agent_budget)
        agent_model = res.get("agent_model") or res.get("model")
        duration_ms = res.get("apply_duration_ms") or res.get("duration_ms")
        # Agent usage/session-limit wall (turn-1, page never touched -> launcher returns
        # failed:usage_limit): RE-QUEUE, never park. This lease provably did nothing, so
        # re-queuing cannot double-submit; crash_unconfirmed would strand it permanently.
        # The driver reads action=='usage_limit' to back off + switch agents (agent_switch).
        from applypilot.apply.launcher import is_usage_limit_result
        if is_usage_limit_result(run_status):
            queue.requeue_apply(conn, self.worker_id, url, apply_error=run_status[:200])
            self._record_event(f"requeued apply (usage_limit) {url}")
            self._beat(conn, state="idle")
            return {"action": "usage_limit", "url": url}
        if run_status == "applied":
            queue.write_apply_result(conn, self.worker_id, url, status="applied", apply_status="applied",
                                     target_host=target_host, home_ip=self.home_ip, est_cost_usd=cost,
                                     outcome="success", agent=agent, agent_model=agent_model,
                                     apply_duration_ms=duration_ms)
            self._record_event(f"wrote apply applied {url}")
            self._beat(conn, state="idle")
            return {"action": "applied", "url": url}
        if run_status in self._WALL_STATUSES:
            kind = "login_gate" if run_status in ("login_issue", "auth_required") else "visible_captcha"
            route = _captcha.route_for(kind, on_owner_machine=self.on_owner_machine)
            wall_outcome = None if kind == "login_gate" else "captcha"
            self._raise_and_park(conn, url, kind, route=route, outcome=wall_outcome, target_host=target_host)
            return {"action": "parked_challenge", "url": url}
        if run_status in self._CRASH_STATUSES or run_status.startswith("failed:worker_error"):
            queue.write_apply_result(conn, self.worker_id, url, status="crash_unconfirmed",
                                     apply_status="crash_unconfirmed", apply_error=run_status[:200],
                                     target_host=target_host, home_ip=self.home_ip, est_cost_usd=cost,
                                     agent=agent, agent_model=agent_model,
                                     apply_duration_ms=duration_ms)
            self._record_event(f"wrote apply crash_unconfirmed {url} ({run_status})")
            self._beat(conn, state="idle")
            return {"action": "crash_unconfirmed", "url": url}
        queue.write_apply_result(conn, self.worker_id, url, status="failed", apply_status="failed",
                                 apply_error=(run_status or "unknown")[:200],
                                 target_host=target_host, home_ip=self.home_ip, est_cost_usd=cost,
                                 agent=agent, agent_model=agent_model,
                                 apply_duration_ms=duration_ms)
        self._record_event(f"wrote apply failed {url} ({run_status or 'unknown'})")
        self._beat(conn, state="idle")
        return {"action": "failed", "url": url}

    def _raise_and_park(self, conn, url, kind, *, route, outcome, target_host) -> int:
        """Raise an auth_challenge row, optionally record the wall outcome on the
        governor, and FREEZE the held lease out of the reclaim pool
        (``queue.park_challenge``) so the SAME wall is never reclaimed and re-driven
        blind on another machine -- the IP-burn fail-safe (§7.3). No apply result is
        written; the owner resolves the challenge from the trusted box. ``outcome`` may
        be None (e.g. a login wall) -> no governor outcome is recorded, so a non-bot
        wall doesn't pollute the breaker's challenge_rate (§6)."""
        cid = _insert_challenge(
            conn, url=url, worker_id=self.worker_id, machine_owner=self.machine_owner,
            home_ip=self.home_ip, kind=kind, route=route, commit=False,
        )
        if outcome is not None:
            scopes = [governor.GLOBAL, governor.home_ip_scope(self.home_ip)]
            if target_host:
                scopes.append(governor.host_scope(target_host))
            governor.record_outcome(conn, scopes, outcome, commit=False)
        queue.park_challenge(conn, self.worker_id, url, commit=False)
        conn.commit()
        self._record_event(f"parked challenge {url} ({kind}->{route})")
        self._beat(conn, state="challenge_pending", current_job=url)
        return cid

    # -- heartbeat helper -----------------------------------------------------
    def _build_recent_log(self) -> Optional[str]:
        """Freshest SCRUBBED log tail to ship on this beat, capped to the LAST 8000
        chars (bound PG growth -- S3). Prefers the rich agent log via log_tail_fn (apply
        lane); a missing/failed tail falls back to the in-memory event ring. NEVER raises:
        any logging error returns None so the heartbeat still fires (S2)."""
        try:
            text: Optional[str] = None
            if self._log_tail_fn is not None:
                try:
                    text = self._log_tail_fn()
                except Exception:
                    text = None
            if not text:
                try:
                    text = "\n".join(self._events)
                except Exception:
                    text = None
            if not text:
                return None
            scrubbed = _scrub(text)
            return scrubbed[-8000:] if len(scrubbed) > 8000 else scrubbed
        except Exception:
            return None

    def _beat(self, conn, *, state, current_job=None) -> None:
        # recent_log / last_error are crash-visibility extras; building them must NEVER
        # break the heartbeat (S2). last_error is already scrubbed + capped at capture.
        try:
            recent_log = self._build_recent_log()
        except Exception:
            recent_log = None
        last_error = self._last_error
        _heartbeat(
            conn, worker_id=self.worker_id, machine_owner=self.machine_owner,
            home_ip=self.home_ip, role=self.role, state=state, current_job=current_job,
            sw_version=self.sw_version, last_error=last_error, recent_log=recent_log,
            current_agent=self.current_agent, current_model=self.current_model,
            agent_chain=self.agent_chain, last_agent_switch_at=self.last_agent_switch_at,
            last_agent_switch_reason=self.last_agent_switch_reason,
        )

    # -- long-running driver (not exercised by unit tests) --------------------
    def run_forever(self, *, idle_sleep_seconds: float = 5.0, stop=None) -> None:  # pragma: no cover
        """Lease-process-heartbeat forever, sleeping ``idle_sleep_seconds`` between
        empty leases. ``stop`` is an optional zero-arg predicate; the loop exits
        when it returns True. Not covered by unit tests (it sleeps + runs forever)."""
        import time

        while True:
            if stop is not None and stop():
                return
            try:
                res = self.run_once()
            except Exception:
                # A transient failure must not kill the slot; the reclaim sweep
                # re-queues anything we were holding (§5 stateless self-resume).
                # ALSO capture the (scrubbed) traceback so the crash is VISIBLE in the
                # console -- previously this except swallowed it (the invisibility bug).
                # Recovery behavior is otherwise UNCHANGED.
                try:
                    import traceback
                    tb = _scrub(traceback.format_exc())
                    self._last_error = tb[:4000]
                    last_line = next((ln for ln in reversed(tb.splitlines()) if ln.strip()), "")
                    self._record_event("ERROR: " + last_line)
                except Exception:
                    pass
                res = {"action": "error"}
            if res.get("action") == "stop":
                return  # remote restart/drain: exit between jobs; supervisor respawns
            if res.get("action") in ("idle", "paused"):
                time.sleep(idle_sleep_seconds)
