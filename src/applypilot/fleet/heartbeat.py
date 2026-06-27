"""Fleet health: heartbeats, stuck-detection, poison quarantine, remote
commands + the owner dashboard snapshot (R5, R7, R12 / spec §11).

Sits on top of the v3 schema (``worker_heartbeat``, ``poison_jobs``,
``remote_commands``, ``rate_governor``, ``auth_challenge``, ``llm_usage`` and the
four queues). It is the *health substrate*: workers ``beat`` every ~20s; the
broker runs ``detect_stuck`` / ``reclaim`` loops; jobs that crash whoever claims
them are ``quarantine_job``'d after N strikes; the owner issues
``issue_command`` (restart/pause/self_update) that workers ``poll_commands`` +
``ack_command``; and ``dashboard_snapshot`` rolls it all into one read-only view.

Mirrors the governor/queue conventions: dict_row cursors, ``%s`` placeholders,
``now()`` server-side time, a ``commit=True`` knob so callers can extend the txn.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Heartbeat upsert (R5, R7).
# ---------------------------------------------------------------------------
_BEAT = """
INSERT INTO worker_heartbeat
    (worker_id, machine_owner, home_ip, role, state, current_job, job_started_at,
     success_today, captcha_today, block_today, spend_today_usd,
     cpu_pct, ram_pct, browser_count, sw_version, last_beat)
VALUES
    (%(worker_id)s, %(machine_owner)s, %(home_ip)s, %(role)s, %(state)s, %(current_job)s,
     %(job_started_at)s, %(success_today)s, %(captcha_today)s, %(block_today)s,
     %(spend_today_usd)s, %(cpu_pct)s, %(ram_pct)s, %(browser_count)s, %(sw_version)s, now())
ON CONFLICT (worker_id) DO UPDATE SET
    machine_owner   = COALESCE(EXCLUDED.machine_owner, worker_heartbeat.machine_owner),
    home_ip         = COALESCE(EXCLUDED.home_ip, worker_heartbeat.home_ip),
    role            = EXCLUDED.role,
    state           = EXCLUDED.state,
    current_job     = EXCLUDED.current_job,
    -- Preserve job_started_at across a keepalive on the SAME job: a beat that
    -- doesn't re-pass job_started_at must not NULL it (that would silently disable
    -- job_over_max stuck-detection). A change of current_job takes the new value.
    job_started_at  = CASE
        WHEN EXCLUDED.current_job IS NOT DISTINCT FROM worker_heartbeat.current_job
        THEN COALESCE(EXCLUDED.job_started_at, worker_heartbeat.job_started_at)
        ELSE EXCLUDED.job_started_at END,
    success_today   = EXCLUDED.success_today,
    captcha_today   = EXCLUDED.captcha_today,
    block_today     = EXCLUDED.block_today,
    spend_today_usd = EXCLUDED.spend_today_usd,
    cpu_pct         = EXCLUDED.cpu_pct,
    ram_pct         = EXCLUDED.ram_pct,
    browser_count   = EXCLUDED.browser_count,
    sw_version      = COALESCE(EXCLUDED.sw_version, worker_heartbeat.sw_version),
    last_beat       = now();
"""


def beat(
    conn,
    worker_id,
    *,
    machine_owner=None,
    home_ip=None,
    role="apply",
    state="idle",
    current_job=None,
    job_started_at=None,
    success_today=0,
    captcha_today=0,
    block_today=0,
    spend_today_usd=0,
    cpu_pct=None,
    ram_pct=None,
    browser_count=None,
    sw_version=None,
    commit=True,
) -> None:
    """UPSERT this worker's heartbeat row, stamping ``last_beat = now()``.

    Pass the live ``state`` on every beat. ``job_started_at`` is preserved across a
    keepalive on the SAME ``current_job`` (so a beat that omits it doesn't disable
    job_over_max detection); a change of ``current_job`` takes the new value."""
    with conn.cursor() as cur:
        cur.execute(_BEAT, {
            "worker_id": worker_id,
            "machine_owner": machine_owner,
            "home_ip": home_ip,
            "role": role,
            "state": state,
            "current_job": current_job,
            "job_started_at": job_started_at,
            "success_today": success_today,
            "captcha_today": captcha_today,
            "block_today": block_today,
            "spend_today_usd": spend_today_usd,
            "cpu_pct": cpu_pct,
            "ram_pct": ram_pct,
            "browser_count": browser_count,
            "sw_version": sw_version,
        })
    if commit:
        conn.commit()


# ---------------------------------------------------------------------------
# Stuck / dead worker detection (R7).
# ---------------------------------------------------------------------------
_DETECT_STUCK = """
SELECT worker_id, reason FROM (
    SELECT worker_id, 'no_heartbeat' AS reason
    FROM worker_heartbeat
    WHERE last_beat < now() - make_interval(secs => %(hb_timeout)s)
    UNION ALL
    SELECT worker_id, 'job_over_max' AS reason
    FROM worker_heartbeat
    WHERE state = 'applying'
      AND job_started_at IS NOT NULL
      AND job_started_at < now() - make_interval(secs => %(job_max)s)
) s
ORDER BY worker_id, reason;
"""


def detect_stuck(conn, *, heartbeat_timeout=90, job_max_seconds=600) -> list[dict]:
    """Workers that look wedged: ``no_heartbeat`` if ``last_beat`` is older than
    ``heartbeat_timeout`` seconds; ``job_over_max`` if a worker has been in the
    ``applying`` state on the same job for more than ``job_max_seconds``. A worker
    can appear with both reasons. Returns ``[{worker_id, reason}, ...]``."""
    with conn.cursor() as cur:
        cur.execute(_DETECT_STUCK, {
            "hb_timeout": heartbeat_timeout,
            "job_max": job_max_seconds,
        })
        out = [dict(r) for r in cur.fetchall()]
    conn.rollback()  # read-only: don't leave an idle-in-transaction (cf. pgqueue reads)
    return out


# ---------------------------------------------------------------------------
# Poison-job quarantine (R7).
# ---------------------------------------------------------------------------
def quarantine_job(conn, url, *, worker, reason, threshold=3, commit=True, manual=False) -> bool:
    """Bump ``poison_jobs.crash_count`` for ``url`` (creating the row on first
    strike). Once the count reaches ``threshold`` and the job is not already
    quarantined, stamp ``quarantined_at`` + ``reason``. Returns True ONLY on the
    transition that newly quarantines the job (idempotent thereafter).

    ``manual=True`` is a DELIBERATE one-shot (owner / monitor / Codex bridge): pull the
    job immediately WITHOUT accumulating ``crash_count`` (so manual quarantines never
    pollute real crash signal), tagging the reason with a ``manual:`` prefix. Returns
    True only on the newly-quarantined transition, False if already quarantined."""
    if manual:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO poison_jobs (url, crash_count, last_worker, reason, quarantined_at)
                   VALUES (%s, 0, %s, %s, now())
                   ON CONFLICT (url) DO UPDATE SET
                       last_worker    = EXCLUDED.last_worker,
                       reason         = EXCLUDED.reason,
                       quarantined_at = now()
                   WHERE poison_jobs.quarantined_at IS NULL""",
                (url, worker, f"manual:{reason}"),
            )
            newly = cur.rowcount > 0
        if commit:
            conn.commit()
        return newly
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO poison_jobs (url, crash_count, last_worker, reason)
               VALUES (%s, 1, %s, %s)
               ON CONFLICT (url) DO UPDATE SET
                   crash_count = poison_jobs.crash_count + 1,
                   last_worker = EXCLUDED.last_worker,
                   reason      = EXCLUDED.reason
               RETURNING crash_count, quarantined_at""",
            (url, worker, reason),
        )
        row = cur.fetchone()
        newly = False
        if row["crash_count"] >= threshold and row["quarantined_at"] is None:
            cur.execute(
                "UPDATE poison_jobs SET quarantined_at = now(), reason = %s "
                "WHERE url = %s AND quarantined_at IS NULL",
                (reason, url),
            )
            newly = cur.rowcount > 0
    if commit:
        conn.commit()
    return newly


def is_quarantined(conn, url) -> bool:
    """True if ``url`` has been pulled from the pool (``quarantined_at`` set)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM poison_jobs WHERE url = %s AND quarantined_at IS NOT NULL",
            (url,),
        )
        found = cur.fetchone() is not None
    conn.rollback()  # read-only
    return found


# ---------------------------------------------------------------------------
# Remote commands: owner -> machine control (R7 restart, R12 self_update).
# ---------------------------------------------------------------------------
def issue_command(conn, worker_id, command, *, target_version=None, commit=True) -> int:
    """Queue a control command for a worker (``worker_id='*'`` = fleet-wide).
    Returns the new ``remote_commands.id``."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO remote_commands (worker_id, command, target_version) "
            "VALUES (%s, %s, %s) RETURNING id",
            (worker_id, command, target_version),
        )
        cmd_id = cur.fetchone()["id"]
    if commit:
        conn.commit()
    return cmd_id


def poll_commands(conn, worker_id) -> list[dict]:
    """Open commands addressed to this worker OR broadcast to ``'*'``, oldest first.

    A broadcast is excluded only once THIS worker has acked it (via ``command_acks``),
    so a fleet-wide command reaches EVERY worker rather than being consumed by whoever
    acks first."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.id, c.worker_id, c.command, c.target_version, c.issued_at "
            "FROM remote_commands c "
            "WHERE c.acked_at IS NULL AND c.worker_id IN (%(w)s, '*') "
            "  AND NOT EXISTS (SELECT 1 FROM command_acks a "
            "                  WHERE a.command_id = c.id AND a.worker_id = %(w)s) "
            "ORDER BY c.issued_at, c.id",
            {"w": worker_id},
        )
        out = [dict(r) for r in cur.fetchall()]
    conn.rollback()  # read-only
    return out


def ack_command(conn, command_id, worker_id, *, commit=True) -> bool:
    """Ack a command FOR THIS WORKER. Records a per-worker ack (so a broadcast '*'
    is acked independently by each worker, not closed for the whole fleet) and, for a
    DIRECT command, also stamps ``remote_commands.acked_at`` (hard close). Returns True
    if this worker newly acked an open command; idempotent (a second ack -> False)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO command_acks (command_id, worker_id) VALUES (%s, %s) "
            "ON CONFLICT (command_id, worker_id) DO NOTHING",
            (command_id, worker_id),
        )
        newly = cur.rowcount > 0
        # Hard-close a direct (non-broadcast) command so it leaves the open index.
        cur.execute(
            "UPDATE remote_commands SET acked_at = now() "
            "WHERE id = %s AND worker_id = %s AND worker_id <> '*' AND acked_at IS NULL",
            (command_id, worker_id),
        )
        closed = cur.rowcount > 0
    if commit:
        conn.commit()
    return newly or closed


# ---------------------------------------------------------------------------
# Owner dashboard (R7 / spec §11) -- one read-only rollup.
# ---------------------------------------------------------------------------
def dashboard_snapshot(conn) -> dict:
    """Read-only fleet health rollup. Keys:
      ``machines``        -- per-worker heartbeat rows
      ``governor``        -- per-scope breaker_state / challenge_rate / 24h count
      ``queue_depth``     -- {apply, compute, search, linkedin} -> {status: count}
      ``captcha_backlog`` -- open (unresolved) auth_challenge count
      ``quarantine``      -- quarantined poison_jobs count
      ``spend_today``     -- SUM(llm_usage.cost_usd) over the last 24h (float)
    """
    snap: dict = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT worker_id, machine_owner, home_ip, role, state, current_job, "
            "job_started_at, success_today, captcha_today, block_today, spend_today_usd, "
            "cpu_pct, ram_pct, browser_count, sw_version, last_beat "
            "FROM worker_heartbeat ORDER BY worker_id"
        )
        snap["machines"] = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT scope_key, breaker_state, challenge_rate, count_24h "
            "FROM rate_governor ORDER BY scope_key"
        )
        snap["governor"] = [dict(r) for r in cur.fetchall()]

        queue_depth: dict[str, dict[str, int]] = {}
        for lane, table, status_col in (
            ("apply", "apply_queue", "status"),
            ("compute", "compute_queue", "status"),
            ("search", "search_tasks", "status"),
            ("linkedin", "linkedin_queue", "status"),
        ):
            cur.execute(
                f"SELECT {status_col} AS status, COUNT(*) AS n FROM {table} GROUP BY {status_col}"
            )
            queue_depth[lane] = {r["status"]: r["n"] for r in cur.fetchall()}
        snap["queue_depth"] = queue_depth

        cur.execute("SELECT COUNT(*) AS n FROM auth_challenge WHERE resolved_at IS NULL")
        snap["captcha_backlog"] = cur.fetchone()["n"]

        cur.execute("SELECT COUNT(*) AS n FROM poison_jobs WHERE quarantined_at IS NOT NULL")
        snap["quarantine"] = cur.fetchone()["n"]

        cur.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS spend FROM llm_usage "
            "WHERE ts >= now() - make_interval(hours => 24)"
        )
        snap["spend_today"] = float(cur.fetchone()["spend"])

    conn.rollback()  # read-only rollup
    return snap
