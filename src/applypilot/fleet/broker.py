"""Thin API BROKER -- the trust boundary (spec §5, §9, §12).

Friend machines never hold a Postgres DSN, the Gmail OAuth token, passwords, or
the SQLite brain. They hold only a per-machine **bearer token** and talk to this
broker over a tiny RPC surface. The broker is the ONE process that holds the DB
credential (and, on the trusted spot, the Gmail token); it authenticates every
call against ``workers.token_hash`` and routes to the proven queue/governor/config
primitives -- it does NOT reimplement leases, the governor, or the cost cap.

Security invariants enforced here:
  * **Token auth, hashed at rest.** ``enroll_worker`` returns a plaintext token
    ONCE; only ``sha256(token).hexdigest()`` is stored. ``authenticate`` is an
    sha256 compare that also rejects a ``revoked_at`` row. Every RPC authenticates
    first.
  * **Capability gate.** A worker may only lease lanes its ``capabilities`` JSONB
    permits (``can_ats`` / ``can_compute`` / ``can_discover`` / ``can_linkedin``).
  * **LinkedIn = single account, owner-IP only (R1).** The LinkedIn lane is leased
    with the worker's *registered* ``public_ip`` and the fleet's ``owner_ip``;
    ``queue.lease_linkedin`` refuses any IP != owner IP. A friend on a different
    egress IP can NEVER cross into the LinkedIn lane even with a valid token.
  * **Defer-on-unknown.** ``get_answer`` returns a SINGLE owner-vetted answer or
    ``None`` (the worker then defers) -- never a bulk dump of the answer bank.
  * **No secrets here.** ``request_otp`` is a STUB that records a bookkeeping row
    and returns a note; the real Gmail read happens owner-side. This module never
    reads Gmail, never holds the Gmail token, and never persists a code (E1).

The optional FastAPI ``app`` (guarded import) exposes the same surface over
Bearer-token POST endpoints. If FastAPI is not importable the ``Broker`` class
still ships and is fully usable/testable on its own.
"""
from __future__ import annotations

import hashlib
import secrets
from typing import Any

from applypilot.apply import pgqueue
from applypilot.fleet import answer_bank, config as fleet_config, queue


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------
def _hash_token(token: str) -> str:
    """sha256 hex of a bearer token (what we store + compare; never the plaintext)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# Map a requested lane -> the capability flag that gates it. A worker's
# ``capabilities`` JSONB must have the flag truthy to lease that lane.
_LANE_CAPABILITY = {
    "ats": "can_ats",
    "apply": "can_ats",
    "compute": "can_compute",
    "search": "can_discover",
    "discover": "can_discover",
    "linkedin": "can_linkedin",
}


class AuthError(Exception):
    """Raised when a worker_id/token pair fails authentication (or is revoked)."""


class CapabilityError(Exception):
    """Raised when an authenticated worker requests a lane it is not provisioned for."""


class Broker:
    """Token-authenticated facade over the v3 queue / governor / config.

    Stateless apart from ``owner_ip`` (the single residential egress allowed to
    drive the LinkedIn lane, R1). Every method takes an OPEN psycopg connection
    (dict_row), mirroring the rest of the fleet layer so transaction scope stays
    with the caller and everything is testable against a throwaway Postgres.
    """

    def __init__(self, *, owner_ip: str | None = None) -> None:
        self.owner_ip = owner_ip

    # -- enrollment / auth ---------------------------------------------------
    def enroll_worker(
        self,
        conn,
        machine_owner: str,
        *,
        capabilities: dict[str, Any],
        public_ip: str | None = None,
        worker_id: str | None = None,
    ) -> tuple[str, str]:
        """Create a ``workers`` row and return ``(worker_id, plaintext_token)``.

        The plaintext token is returned ONCE and never stored -- only its sha256
        hash lands in ``token_hash``. The token is what gets baked into the
        per-machine installer (§16.5); the broker can only ever verify it, never
        recover it.
        """
        worker_id = worker_id or f"w_{secrets.token_hex(6)}"
        token = secrets.token_urlsafe(32)
        token_hash = _hash_token(token)
        import json as _json
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO workers (worker_id, machine_owner, token_hash, public_ip, capabilities) "
                "VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (worker_id) DO UPDATE SET machine_owner=EXCLUDED.machine_owner, "
                "token_hash=EXCLUDED.token_hash, public_ip=EXCLUDED.public_ip, "
                "capabilities=EXCLUDED.capabilities, revoked_at=NULL",
                (worker_id, machine_owner, token_hash, public_ip, _json.dumps(capabilities or {})),
            )
        conn.commit()
        return worker_id, token

    def _load_worker(self, conn, worker_id: str) -> dict[str, Any] | None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT worker_id, machine_owner, token_hash, public_ip, capabilities, "
                "validated, revoked_at FROM workers WHERE worker_id=%s",
                (worker_id,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def authenticate(self, conn, worker_id: str, token: str) -> bool:
        """True iff ``worker_id`` exists, is not revoked, and ``sha256(token)``
        matches the stored hash. Constant-time hash compare; no plaintext stored."""
        row = self._load_worker(conn, worker_id)
        if row is None or row["revoked_at"] is not None or not row["token_hash"]:
            return False
        return secrets.compare_digest(row["token_hash"], _hash_token(token))

    def _require(self, conn, worker_id: str, token: str) -> dict[str, Any]:
        """Authenticate and return the worker row, or raise ``AuthError``."""
        row = self._load_worker(conn, worker_id)
        if (
            row is None
            or row["revoked_at"] is not None
            or not row["token_hash"]
            or not secrets.compare_digest(row["token_hash"], _hash_token(token))
        ):
            raise AuthError("invalid worker_id/token (or revoked)")
        return row

    def revoke_worker(self, conn, worker_id: str) -> None:
        """Stamp ``revoked_at`` -- the per-machine kill switch (§12). One row; the
        machine's token stops authenticating immediately on the next RPC."""
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workers SET revoked_at=now() WHERE worker_id=%s AND revoked_at IS NULL",
                (worker_id,),
            )
        conn.commit()

    @staticmethod
    def _capabilities(row: dict[str, Any]) -> dict[str, Any]:
        caps = row.get("capabilities")
        if isinstance(caps, str):  # defensive: some drivers hand back text
            import json as _json
            caps = _json.loads(caps)
        return caps or {}

    # -- lease (routes by lane + capability) ---------------------------------
    def lease(
        self,
        conn,
        worker_id: str,
        token: str,
        *,
        lane: str,
        home_ip: str | None = None,
        owner_ip: str | None = None,
        ttl_seconds: int | None = None,
        sw_version: str | None = None,
    ) -> dict[str, Any] | None:
        """Authenticate, gate on capability, then route to the right queue lease.

        ``lane`` in {``ats``/``apply``, ``compute``, ``search``/``discover``,
        ``linkedin``}.

        SECURITY: ``home_ip`` and ``owner_ip`` are IGNORED for the gate. The
        breaker home-IP and the LinkedIn owner-IP are taken from SERVER-SIDE state
        -- the worker's registered ``public_ip`` and the broker's configured
        ``owner_ip`` -- never from the (untrusted) caller. The params are accepted
        only for call-site compatibility; a worker can NOT supply its own IP to
        satisfy the LinkedIn mutex or to retarget its rate-breaker.
        """
        worker = self._require(conn, worker_id, token)
        cap_flag = _LANE_CAPABILITY.get(lane)
        if cap_flag is None:
            raise ValueError(f"unknown lane: {lane!r}")
        if not self._capabilities(worker).get(cap_flag):
            raise CapabilityError(f"worker {worker_id} lacks capability {cap_flag} for lane {lane}")
        if sw_version is None and not fleet_config.worker_version_matches(conn, worker_id):
            return None

        reg_ip = worker.get("public_ip")  # registered egress -- the ONLY IP we trust
        kw = {} if ttl_seconds is None else {"ttl_seconds": ttl_seconds}
        if lane in ("ats", "apply"):
            return queue.lease_apply(conn, worker_id, home_ip=reg_ip, sw_version=sw_version, **kw)
        if lane == "compute":
            return queue.lease_compute(conn, worker_id, sw_version=sw_version, **kw)
        if lane in ("search", "discover"):
            return queue.lease_search(conn, worker_id, sw_version=sw_version, **kw)
        if lane == "linkedin":
            # R1 mutex: the worker's REGISTERED public_ip vs the broker-configured
            # owner_ip -- both server-side. queue.lease_linkedin refuses a mismatch.
            return queue.lease_linkedin(
                conn, worker_id, public_ip=reg_ip, owner_ip=self.owner_ip,
                sw_version=sw_version, **kw,
            )
        raise ValueError(f"unknown lane: {lane!r}")  # pragma: no cover

    # -- result writes (route by lane) ---------------------------------------
    def write_result(
        self,
        conn,
        worker_id: str,
        token: str,
        *,
        lane: str,
        url: str,
        status: str,
        target_host: str | None = None,
        home_ip: str | None = None,
        apply_status: str | None = None,
        apply_error: str | None = None,
        est_cost_usd: float = 0,
        outcome: str | None = None,
        # compute-lane fields:
        result: Any = None,
        cost_usd: float = 0,
        model: str | None = None,
        task: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
    ) -> bool:
        """Authenticate then forward to the lease-owner-guarded result writer for
        the lane (apply / linkedin / compute). The underlying writers reject a
        non-holder. The LinkedIn lane closes the SEPARATE linkedin_queue (and the
        account:linkedin governor), never apply_queue."""
        worker = self._require(conn, worker_id, token)
        if lane in ("ats", "apply"):
            # Bind the breaker keys (host, home_ip) to what was recorded on the leased
            # row at lease time, NOT to client-supplied values -- otherwise a worker
            # could report outcomes under a different host/IP to dodge its breaker.
            eff_host, eff_home = target_host, home_ip
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(target_host, apply_domain) AS th, worker_home_ip "
                    "FROM apply_queue WHERE url=%s AND lease_owner=%s",
                    (url, worker_id),
                )
                r = cur.fetchone()
            if r:
                eff_host = r["th"] or target_host
                eff_home = r["worker_home_ip"] if r["worker_home_ip"] is not None else worker.get("public_ip")
            return queue.write_apply_result(
                conn, worker_id, url, status=status, target_host=eff_host,
                home_ip=eff_home, apply_status=apply_status, apply_error=apply_error,
                est_cost_usd=est_cost_usd, outcome=outcome,
            )
        if lane == "linkedin":
            return queue.write_linkedin_result(
                conn, worker_id, url, status=status, apply_status=apply_status,
                apply_error=apply_error, est_cost_usd=est_cost_usd, outcome=outcome,
            )
        if lane == "compute":
            return queue.write_compute_result(
                conn, worker_id, url, result=result, status=status or "done",
                cost_usd=cost_usd, model=model, task=task,
                machine_owner=worker.get("machine_owner"),
                tokens_in=tokens_in, tokens_out=tokens_out,
            )
        raise ValueError(f"unknown lane for write_result: {lane!r}")

    def complete_search(
        self,
        conn,
        worker_id: str,
        token: str,
        task_id: str,
        *,
        result_count: int = 0,
        board: str | None = None,
        error: str | None = None,
        cadence_seconds: int | None = None,
    ) -> bool:
        """Authenticate then close+reschedule a discovery task (forwards to queue)."""
        self._require(conn, worker_id, token)
        return queue.complete_search(
            conn, worker_id, task_id, result_count=result_count, board=board,
            error=error, cadence_seconds=cadence_seconds,
        )

    # -- heartbeat -----------------------------------------------------------
    def heartbeat(self, conn, worker_id: str, token: str, **fields) -> Any:
        """Authenticate then forward to ``applypilot.fleet.heartbeat.beat``.

        Imported lazily so the broker module loads even if the heartbeat sibling
        module is not yet present (it is built in parallel)."""
        self._require(conn, worker_id, token)
        from applypilot.fleet import heartbeat  # lazy: sibling module
        return heartbeat.beat(conn, worker_id, **fields)

    # -- config / version (NO secrets) ---------------------------------------
    def get_config(self, conn, worker_id: str, token: str) -> dict[str, Any]:
        """The worker's view of fleet config: the version it should run, whether
        the fleet is paused, and its own capability flags. **No secrets** -- never
        the DSN, never the Gmail token, never the answer bank, never another
        machine's row."""
        worker = self._require(conn, worker_id, token)
        return {
            "worker_id": worker_id,
            "version": fleet_config.version_for_worker(conn, worker_id),
            "paused": bool(fleet_config.get_config(conn).get("paused")),
            "validated": bool(worker.get("validated")),
            "capabilities": self._capabilities(worker),
        }

    # -- remote commands -----------------------------------------------------
    def poll_commands(self, conn, worker_id: str, token: str) -> list[dict[str, Any]]:
        """Un-acked owner -> machine commands for this worker (or fleet-wide '*').
        The friend does nothing; the watchdog polls + executes locally (§11).
        Delegates to ``heartbeat`` so the per-worker broadcast accounting is shared
        (a '*' command reaches every worker, not just the first acker)."""
        self._require(conn, worker_id, token)
        from applypilot.fleet import heartbeat  # lazy: sibling module
        return heartbeat.poll_commands(conn, worker_id)

    def ack_command(self, conn, worker_id: str, token: str, command_id: int) -> bool:
        """Ack a polled command FOR THIS WORKER (idempotent). A broadcast is acked
        per-worker; a direct command is hard-closed."""
        self._require(conn, worker_id, token)
        from applypilot.fleet import heartbeat  # lazy: sibling module
        return heartbeat.ack_command(conn, command_id, worker_id)

    # -- answer bank (single answer, defer-on-unknown, NEVER bulk) -----------
    def get_answer(self, conn, worker_id: str, token: str, question: str) -> str | None:
        """ONE owner-vetted answer for a screening question, or ``None`` (the
        worker then DEFERS rather than guessing -- R10). Never returns a bulk
        dump of the answer bank; an unknown question is recorded for the owner's
        defer queue and ``None`` is returned (fail-safe)."""
        self._require(conn, worker_id, token)
        return answer_bank.get_answer(conn, question)

    # -- Gmail OTP relay (STUB -- real read is OWNER-SIDE, no token here) -----
    def request_otp(
        self, conn, worker_id: str, token: str, sender_hint: str | None, url: str | None
    ) -> dict[str, Any]:
        """Record an OTP-request bookkeeping row and return a STUB.

        IMPORTANT (E1 / §7.4): this broker module does NOT read Gmail and holds NO
        Gmail token. The real ``gmail.readonly`` inbox lives on ONE trusted spot
        (the owner box or a separately-credentialed relay process) which reads the
        last-~60s code for the matching sender and returns it over the RPC -- the
        code is NEVER persisted. This method only writes a request audit-trail row
        and returns a documented stub so the surface is wired without any secret
        living in this module. (Single-delivery -- not handing the same code to two
        workers -- is the OWNER-SIDE relay's job when it matches+consumes the email;
        this row is bookkeeping, not an enforced mutex.)
        """
        self._require(conn, worker_id, token)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO otp_request (worker_id, url, sender_hint) VALUES (%s,%s,%s) RETURNING id",
                (worker_id, url, sender_hint),
            )
            req_id = cur.fetchone()["id"]
        conn.commit()
        return {
            "status": "stub",
            "request_id": req_id,
            "note": "real Gmail read is owner-side; broker holds no Gmail token and never persists the code",
        }

    # -- assets (broker-served blobs; friends hold no PG DSN) ----------------
    def fetch_assets(self, conn, worker_id: str, token: str, name: str) -> bytes | None:
        """Serve a worker asset (profile.json / resume.pdf) as a blob. Friends get
        their application DATA from the broker, never a Postgres DSN (§5/§16)."""
        self._require(conn, worker_id, token)
        return pgqueue.get_asset(conn, name)

    # -- captcha / human-in-the-loop inbox -----------------------------------
    def raise_challenge(
        self,
        conn,
        worker_id: str,
        token: str,
        url: str,
        kind: str,
        route: str,
        *,
        screenshot_url: str | None = None,
        home_ip: str | None = None,
    ) -> int:
        """Insert an ``auth_challenge`` row (the captcha inbox / R3). Returns the
        new row id. The screenshot is a broker-served blob ref, never inline BYTEA
        (E2). The job itself stays a frozen ``challenge_pending`` lease-hold on the
        worker side -- this only records the wall for the owner's review."""
        worker = self._require(conn, worker_id, token)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO auth_challenge (url, worker_id, machine_owner, home_ip, kind, route, screenshot_url) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (url, worker_id, worker.get("machine_owner"), home_ip, kind, route, screenshot_url),
            )
            cid = cur.fetchone()["id"]
        conn.commit()
        return cid


# ---------------------------------------------------------------------------
# OPTIONAL: FastAPI app (Bearer-token POST endpoints). Guarded import -- if
# FastAPI is not installed the Broker class above still ships + is testable.
# ---------------------------------------------------------------------------
def build_app(*, owner_ip: str | None = None, dsn: str | None = None):
    """Build a FastAPI app exposing the broker over Bearer-token POST endpoints.

    The ``Authorization: Bearer <token>`` header + a ``worker_id`` in the body
    authenticate every call. Each handler opens a short-lived broker DSN
    connection -- the friend never sees it. Raises ``RuntimeError`` if FastAPI is
    not importable (callers should guard the call)."""
    try:
        from fastapi import Body, FastAPI, Header, HTTPException
    except Exception as exc:  # pragma: no cover - exercised only where fastapi absent
        raise RuntimeError("fastapi not installed; build_app unavailable") from exc

    app = FastAPI(title="ApplyPilot fleet broker")
    broker = Broker(owner_ip=owner_ip)

    def _token(authorization: str | None) -> str:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        return authorization.split(" ", 1)[1].strip()

    def _conn():
        return pgqueue.connect(dsn)

    def _guard(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except AuthError:
            raise HTTPException(status_code=401, detail="unauthorized")
        except CapabilityError as exc:
            raise HTTPException(status_code=403, detail=str(exc))

    @app.post("/lease")
    def _lease(body: dict = Body(...), authorization: str | None = Header(None)):
        token = _token(authorization)
        with _conn() as conn:
            # NB: home_ip / owner_ip are deliberately NOT forwarded from the client.
            # The broker derives the breaker IP + LinkedIn owner IP from server-side
            # state, so a caller cannot supply its own IP to satisfy the gate.
            return {"job": _guard(
                broker.lease, conn, body["worker_id"], token,
                lane=body["lane"], ttl_seconds=body.get("ttl_seconds"),
                sw_version=body.get("sw_version"),
            )}

    @app.post("/write_result")
    def _write_result(body: dict = Body(...), authorization: str | None = Header(None)):
        token = _token(authorization)
        wid = body.pop("worker_id")
        with _conn() as conn:
            return {"ok": _guard(broker.write_result, conn, wid, token, **body)}

    @app.post("/heartbeat")
    def _heartbeat(body: dict = Body(...), authorization: str | None = Header(None)):
        token = _token(authorization)
        wid = body.pop("worker_id")
        with _conn() as conn:
            return {"ok": _guard(broker.heartbeat, conn, wid, token, **body)}

    @app.post("/get_config")
    def _get_config(body: dict = Body(...), authorization: str | None = Header(None)):
        token = _token(authorization)
        with _conn() as conn:
            return _guard(broker.get_config, conn, body["worker_id"], token)

    @app.post("/poll_commands")
    def _poll_commands(body: dict = Body(...), authorization: str | None = Header(None)):
        token = _token(authorization)
        with _conn() as conn:
            return {"commands": _guard(broker.poll_commands, conn, body["worker_id"], token)}

    @app.post("/ack_command")
    def _ack_command(body: dict = Body(...), authorization: str | None = Header(None)):
        token = _token(authorization)
        with _conn() as conn:
            return {"ok": _guard(broker.ack_command, conn, body["worker_id"], token, body["command_id"])}

    @app.post("/get_answer")
    def _get_answer(body: dict = Body(...), authorization: str | None = Header(None)):
        token = _token(authorization)
        with _conn() as conn:
            return {"answer": _guard(broker.get_answer, conn, body["worker_id"], token, body["question"])}

    @app.post("/request_otp")
    def _request_otp(body: dict = Body(...), authorization: str | None = Header(None)):
        token = _token(authorization)
        with _conn() as conn:
            return _guard(
                broker.request_otp, conn, body["worker_id"], token,
                body.get("sender_hint"), body.get("url"),
            )

    @app.post("/raise_challenge")
    def _raise_challenge(body: dict = Body(...), authorization: str | None = Header(None)):
        token = _token(authorization)
        wid = body.pop("worker_id")
        with _conn() as conn:
            return {"id": _guard(broker.raise_challenge, conn, wid, token,
                                 body["url"], body["kind"], body["route"],
                                 screenshot_url=body.get("screenshot_url"),
                                 home_ip=body.get("home_ip"))}

    return app


# Eagerly build a module-level ``app`` only if FastAPI is importable; otherwise
# leave it None so importing this module never fails on a friend box without web deps.
try:  # pragma: no cover - environment-dependent
    app = build_app()
except Exception:  # pragma: no cover
    app = None
