"""Residential worker LOOP (scaffold) -- spec §5/§6/§7.

A :class:`WorkerLoop` is the thin per-slot loop that runs on each residential
machine (and, wrapped in the Helper app, on friend machines -- §16.5). It leases
work by ROLE, drives it, and writes the result back through the proven v3 queue
helpers (``applypilot.fleet.queue``). It NEVER touches the brain or Postgres
directly beyond the queue/governor helpers; in production it would sit behind the
broker RPC surface, but the lease/result functions share the same call shapes.

WHY EVERYTHING BROWSER-FACING IS INJECTED
-----------------------------------------
The real apply path drives a live Chromium via Playwright
(``applypilot.apply.container_worker.run_job``), the real discovery path drives
``jobspy``, and the real compute path calls an LLM. None of those are unit-
testable in CI. So the loop takes them as INJECTED callables:

    apply_fn(job)   -> html         # wraps container_worker.run_job; returns the
                                    #   final page HTML (and optionally a tuple
                                    #   (html, frames_text, final_url, status)).
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

from typing import Any, Callable, Optional

from applypilot.fleet import captcha as _captcha
from applypilot.fleet import governor
from applypilot.fleet import queue

# Roles a machine slot may carry (a machine may run several slots / roles -- §5).
ROLE_APPLY = "apply"
ROLE_COMPUTE = "compute"
ROLE_DISCOVERY = "discovery"


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
               sw_version=None, commit=True) -> None:
    """UPSERT this worker's ``worker_heartbeat`` row (~every iteration, spec §11/R5).
    A missing heartbeat for > lease TTL is what lets the reclaim sweep re-queue a
    crashed worker's job (§5 stateless self-resume)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO worker_heartbeat (worker_id, machine_owner, home_ip, role, state, current_job, sw_version, last_beat) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s, now()) "
            "ON CONFLICT (worker_id) DO UPDATE SET machine_owner=EXCLUDED.machine_owner, home_ip=EXCLUDED.home_ip, "
            "role=EXCLUDED.role, state=EXCLUDED.state, current_job=EXCLUDED.current_job, "
            "sw_version=COALESCE(EXCLUDED.sw_version, worker_heartbeat.sw_version), last_beat=now()",
            (worker_id, machine_owner, home_ip, role, state, current_job, sw_version),
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
    ) -> None:
        if role not in (ROLE_APPLY, ROLE_COMPUTE, ROLE_DISCOVERY):
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
        self.sw_version = sw_version
        self.on_owner_machine = on_owner_machine
        self.public_ip = public_ip or home_ip
        self.owner_ip = owner_ip

    # -- connection -----------------------------------------------------------
    def _connect(self):
        return self.conn_factory()

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
        """
        with self._connect() as conn:
            if self.role == ROLE_COMPUTE:
                return self._tick_compute(conn)
            if self.role == ROLE_DISCOVERY:
                return self._tick_discovery(conn)
            return self._tick_apply(conn)

    # -- COMPUTE: score/audit/tailor -- IP-free, cost-governed (§8, R14) -------
    def _tick_compute(self, conn) -> dict:
        job = queue.lease_compute(conn, self.worker_id)
        if job is None:
            self._beat(conn, state="idle")
            return {"action": "idle"}
        self._beat(conn, state="computing", current_job=job["url"])
        if self.score_fn is None:
            raise RuntimeError("compute role requires an injected score_fn")
        result, cost = self.score_fn(job)
        queue.write_compute_result(
            conn, self.worker_id, job["url"], result=result, status="done",
            cost_usd=cost or 0, model=(result or {}).get("model") if isinstance(result, dict) else None,
            task=job.get("task"), machine_owner=self.machine_owner,
        )
        self._beat(conn, state="idle")
        return {"action": "compute_done", "url": job["url"], "cost_usd": cost or 0}

    # -- DISCOVERY: governed scrape (RF2/§8.5) --------------------------------
    def _tick_discovery(self, conn) -> dict:
        task = queue.lease_search(conn, self.worker_id)
        if task is None:
            self._beat(conn, state="idle")
            return {"action": "idle"}
        self._beat(conn, state="searching", current_job=task["task_id"])
        if self.search_fn is None:
            raise RuntimeError("discovery role requires an injected search_fn")
        error = None
        postings: list = []
        try:
            postings = self.search_fn(task) or []
        except Exception:  # a scrape-block / transport error parks the task (RF3)
            error = "blocked"
        queue.complete_search(
            conn, self.worker_id, task["task_id"],
            result_count=len(postings), board=task.get("board"), error=error,
        )
        self._beat(conn, state="idle")
        return {"action": "search_done", "task_id": task["task_id"],
                "result_count": len(postings), "error": error}

    # -- APPLY: governed, dedup-gated, approval-gated, captcha-aware (§5/§7) ---
    def _tick_apply(self, conn) -> dict:
        job = queue.lease_apply(conn, self.worker_id, home_ip=self.home_ip)
        if job is None:
            self._beat(conn, state="idle")
            return {"action": "idle"}
        url = job["url"]
        target_host = job.get("target_host")
        self._beat(conn, state="applying", current_job=url)
        if self.apply_fn is None:
            raise RuntimeError("apply role requires an injected apply_fn")

        # Drive the real browser (STUBBED in tests) and classify the resulting page.
        out = self.apply_fn(job)
        html, frames_text, final_url, status = _normalize_apply_output(out)
        kind = self.classify_fn(html, frames_text=frames_text, final_url=final_url, status=status)

        if not _captcha.is_wall(kind):
            # clear / invisible_pass -> the application went through; record it.
            queue.write_apply_result(
                conn, self.worker_id, url, status="applied", apply_status="applied",
                target_host=target_host, home_ip=self.home_ip, outcome="success",
            )
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
            self._beat(conn, state="idle")
            return {"action": "skipped", "url": url, "kind": kind, "route": route}

        # owner_tray / owner_inbox: a human must solve it. Raise a challenge and
        # PARK -- the lease is HELD (not released, not closed): the job is neither
        # double-claimable nor marked applied (spec §7.3 frozen challenge-hold).
        self._raise_and_park(conn, url, kind, route=route, outcome="captcha",
                             target_host=target_host)
        return {"action": "parked_challenge", "url": url, "kind": kind, "route": route}

    def _raise_and_park(self, conn, url, kind, *, route, outcome, target_host) -> int:
        """Raise an auth_challenge row, record the wall outcome on the governor, and
        FREEZE the held lease out of the reclaim pool (``queue.park_challenge``) so the
        SAME wall is never reclaimed and re-driven blind on another machine -- the
        IP-burn fail-safe (§7.3). No apply result is written; the owner resolves the
        challenge from the trusted box. A blocked/captcha wall is also a leading
        indicator the governor must see (§6)."""
        cid = _insert_challenge(
            conn, url=url, worker_id=self.worker_id, machine_owner=self.machine_owner,
            home_ip=self.home_ip, kind=kind, route=route, commit=False,
        )
        scopes = [governor.GLOBAL, governor.home_ip_scope(self.home_ip)]
        if target_host:
            scopes.append(governor.host_scope(target_host))
        governor.record_outcome(conn, scopes, outcome, commit=False)
        queue.park_challenge(conn, self.worker_id, url, commit=False)
        conn.commit()
        self._beat(conn, state="challenge_pending", current_job=url)
        return cid

    # -- heartbeat helper -----------------------------------------------------
    def _beat(self, conn, *, state, current_job=None) -> None:
        _heartbeat(
            conn, worker_id=self.worker_id, machine_owner=self.machine_owner,
            home_ip=self.home_ip, role=self.role, state=state, current_job=current_job,
            sw_version=self.sw_version,
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
                res = {"action": "error"}
            if res.get("action") == "idle":
                time.sleep(idle_sleep_seconds)
