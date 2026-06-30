"""Shared pytest fixtures for the distributed fleet v3 tests.

Provides a DISPOSABLE local Postgres (from the ``applypilot-pgtest`` conda env) and
a clean-schema ``fleet_db`` fixture. Mirrors the disposable cluster in
tests/test_fleet_pgqueue.py but is kept separate (distinct fixture names) so the
existing pgqueue tests are untouched.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import schema as fleet_schema

# Tables truncated between tests (in addition to apply_queue / fleet_config).
_V3_TABLES = [
    "compute_queue", "search_tasks", "linkedin_queue", "rate_governor", "llm_usage",
    "applied_set", "answer_bank", "auth_challenge", "otp_request", "inbox_events",
    "inbox_outcomes",
    "workers", "worker_heartbeat", "poison_jobs", "remote_commands", "command_acks",
    "fleet_assets", "discovered_postings", "fleet_knobs", "fleet_diagnoses",
]


def _find_pg_bin() -> Path | None:
    cands: list[Path] = []
    if os.environ.get("APPLYPILOT_PGTEST_BIN"):
        cands.append(Path(os.environ["APPLYPILOT_PGTEST_BIN"]))
    conda = shutil.which("conda")
    bases: list[Path] = []
    if conda:
        bases.append(Path(conda).resolve().parent.parent)
    bases += [Path.home() / "anaconda3", Path.home() / "miniconda3"]
    for base in bases:
        cands.append(base / "envs" / "applypilot-pgtest" / "Library" / "bin")  # win
        cands.append(base / "envs" / "applypilot-pgtest" / "bin")              # nix
    for c in cands:
        exe = "initdb.exe" if os.name == "nt" else "initdb"
        if (c / exe).exists():
            return c
    return None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def fleet_pg():
    binp = _find_pg_bin()
    if binp is None:
        pytest.skip("applypilot-pgtest Postgres env not found "
                    "(conda create -n applypilot-pgtest -c conda-forge postgresql)")
    ext = ".exe" if os.name == "nt" else ""
    initdb, pg_ctl = binp / f"initdb{ext}", binp / f"pg_ctl{ext}"
    datadir = Path(tempfile.mkdtemp(prefix="ap_fleetpg_"))
    logfile = datadir / "server.log"
    port = _free_port()
    try:
        subprocess.run(
            [str(initdb), "-D", str(datadir), "-U", "postgres", "-A", "trust", "-E", "UTF8"],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            [str(pg_ctl), "-D", str(datadir), "-l", str(logfile),
             "-o", f"-p {port} -c listen_addresses=127.0.0.1 -c fsync=off",
             "-w", "-t", "30", "start"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as e:
        log = logfile.read_text(encoding="utf-8", errors="replace") if logfile.exists() else ""
        shutil.rmtree(datadir, ignore_errors=True)
        pytest.skip(f"could not start test Postgres (exit {e.returncode}):\n{log}")

    dsn = f"postgresql://postgres@127.0.0.1:{port}/postgres"
    try:
        yield dsn
    finally:
        subprocess.run([str(pg_ctl), "-D", str(datadir), "-m", "immediate", "-w", "stop"],
                       capture_output=True, text=True)
        shutil.rmtree(datadir, ignore_errors=True)


@pytest.fixture
def fleet_db(fleet_pg):
    """Clean v3 schema for each test; yields the DSN."""
    with pgqueue.connect(fleet_pg) as conn:
        fleet_schema.ensure_schema_v3(conn)
        with conn.cursor() as cur:
            cur.execute("TRUNCATE apply_queue;")
            for t in _V3_TABLES:
                cur.execute(f"TRUNCATE {t};")
            cur.execute("UPDATE fleet_config SET spend_cap_usd=0, paused=FALSE, "
                        "cost_cap_daily_usd=0, cost_cap_total_usd=0, "
                        "last_window_roll_at=NULL, agent_timeout_override=NULL, "
                        "canary_enabled=FALSE, canary_remaining=NULL, "
                        "linkedin_canary_enabled=FALSE, linkedin_canary_remaining=NULL, "
                        # Fleet Doctor hardening columns (H1/H2/H5/H8/H18) -- reset per test.
                        "ats_paused=FALSE, ats_pause_source=NULL, doctor_budget_day=NULL, "
                        "doctor_host_skips_today=0, doctor_pace_actions_today=0, "
                        "doctor_last_pass_at=NULL, doctor_pause_armed_at=NULL, "
                        "doctor_systemic_streak=0 WHERE id=1;")
        conn.commit()
    return fleet_pg
