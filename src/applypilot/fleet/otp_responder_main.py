"""Home-box OTP responder loop (entrypoint: applypilot-fleet-otp-home).

Runs alongside the watchdog/doctor on the box that holds the mail credentials.
Each cycle, inside answer_pending, the mailbox (IMAP app-password via
get_mail_source(), falling back to the legacy Gmail API) is read only when
requests are pending; it then answers matching requests, purges expired codes,
and heartbeats. The verification code is never logged. See the 2026-07-03 relay
spec."""
from __future__ import annotations

import argparse
import logging
import os
import time

from applypilot.fleet import otp_relay, schema as fleet_schema

logger = logging.getLogger(__name__)


def run_once(conn, gmail_service=None) -> dict:
    answered = otp_relay.answer_pending(conn, gmail_service)
    purged = otp_relay.purge_expired(conn)
    return {"answered": answered, "purged": purged}


def _beat(conn, *, machine_owner, state, last_error=None):
    try:
        from applypilot.fleet.worker import _heartbeat
        _heartbeat(conn, worker_id="otp_responder", machine_owner=machine_owner,
                   home_ip="0.0.0.0", role="otp_responder", state=state,
                   last_error=last_error)
    except Exception:  # pragma: no cover - heartbeat is best-effort
        logger.debug("otp_responder heartbeat failed", exc_info=True)


def main(argv=None) -> int:  # pragma: no cover - long-running loop
    p = argparse.ArgumentParser(prog="applypilot-fleet-otp-home")
    p.add_argument("--dsn", default=os.environ.get("FLEET_PG_DSN"))
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--machine-owner", default=os.environ.get("FLEET_MACHINE_OWNER", "home"))
    p.add_argument("--once", action="store_true", help="run a single cycle then exit")
    args = p.parse_args(argv)
    if not args.dsn:
        raise SystemExit("set --dsn or FLEET_PG_DSN")

    from applypilot.apply import pgqueue

    with pgqueue.connect(args.dsn) as schema_conn:
        fleet_schema.ensure_schema_v3(schema_conn)

    while True:
        try:
            with pgqueue.connect(args.dsn) as conn:
                _beat(conn, machine_owner=args.machine_owner, state="busy")
                out = run_once(conn)
                _beat(conn, machine_owner=args.machine_owner, state="idle")
            logger.info("otp responder cycle: answered=%s purged=%s",
                        out["answered"], out["purged"])
        except Exception as exc:
            logger.exception("otp responder cycle failed; backing off")
            try:
                with pgqueue.connect(args.dsn) as error_conn:
                    _beat(
                        error_conn,
                        machine_owner=args.machine_owner,
                        state="error",
                        last_error=f"otp_responder_cycle_failed:{type(exc).__name__}",
                    )
            except Exception:
                logger.debug("otp responder failure heartbeat failed", exc_info=True)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(max(0.5, args.interval))
