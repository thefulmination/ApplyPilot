"""ApplyPilot distributed fleet v3.

Extends the shelved cloud-apply-fleet (``applypilot.apply.pgqueue`` /
``fleet_sync`` / ``container_worker``) into a residential, multi-machine fleet:
distributed compute + governed discovery + no-login-ATS applies across many
residential machines, coordinated through Postgres, with a global outcome-aware
rate governor, a captcha detector/router, an owner approval gate, fleet health +
recovery, and a thin token-auth broker so friend machines never hold a DB
credential or the Gmail token.

Design doc:
``docs/superpowers/specs/2026-06-26-distributed-residential-fleet-design.md``

This package REUSES the proven primitives in ``applypilot.apply.pgqueue``
(the atomic ``FOR UPDATE SKIP LOCKED`` lease, reclaim crash-safety, the
lease-owner-guarded result write) rather than reinventing them.
"""

__all__ = [
    "schema",
    "dedup",
    "config",
    "governor",
    "queue",
    "scheduler",
    "heartbeat",
    "sync",
]
