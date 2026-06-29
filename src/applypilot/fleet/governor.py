"""Outcome-aware adaptive rate governor (R6, R1, RF2).

Scopes (``rate_governor.scope_key``):
  ``global``            -- fleet-wide daily cap
  ``host:<domain>``     -- per-apply-host min-gap + cap + breaker
  ``board:<name>``      -- per-discovery-board scrape min-gap + cap + breaker (RF2)
  ``home_ip:<ip>``      -- per-residential-IP cap + breaker (the per-IP protection)
  ``account:linkedin``  -- the single-account LinkedIn mutex (R1)

The breaker reads a RISING per-scope ``challenge_rate`` (= (captcha+block)/total) as a
LEADING INDICATOR of flagging and throttles/pauses/demotes BEFORE a hard block.
"""
from __future__ import annotations

GLOBAL = "global"
LINKEDIN_ACCOUNT = "account:linkedin"

_OUTCOME_COL = {"success": "success_24h", "captcha": "captcha_24h", "block": "block_24h"}

# Tolerance for the breaker rate cuts. challenge_rate is a PG REAL fraction (e.g. 6/10),
# so an exact boundary like 0.6 can land one ULP under a naive `captcha_threshold * 1.5`
# (0.4 * 1.5 == 0.6000000000000001 in float64). Comparing with this epsilon makes the
# cut land ON the boundary -> an exact 6/10 challenge_rate pauses instead of throttling.
_RATE_EPS = 1e-9


def host_scope(host: str) -> str:
    return f"host:{host}"


def board_scope(board: str) -> str:
    return f"board:{board}"


def home_ip_scope(ip: str) -> str:
    return f"home_ip:{ip}"


def ensure_scope(conn, scope_key, *, daily_cap=1_000_000, min_gap_seconds=90, commit=True) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rate_governor (scope_key, daily_cap, min_gap_seconds, base_min_gap_seconds) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT (scope_key) DO NOTHING",
            (scope_key, daily_cap, min_gap_seconds, min_gap_seconds),
        )
    if commit:
        conn.commit()


def record_outcome(conn, scope_keys, outcome, *, bump_cap=False, commit=True) -> None:
    """Increment the outcome counter on each scope. On a confirmed apply
    (``bump_cap=True``) also bump ``count_24h`` (the cap) + stamp ``last_applied_at``
    (the min-gap / mutex). All in one transaction (caller may extend it)."""
    if outcome not in _OUTCOME_COL:
        raise ValueError(f"unknown outcome: {outcome}")
    if bump_cap and outcome != "success":
        # The cap counter + min-gap stamp represent a CONFIRMED apply; a captcha/block
        # must never advance them (would under-count the remaining daily budget AND
        # push the next allowed apply out by a full gap on a non-apply).
        raise ValueError("bump_cap is only valid with outcome='success'")
    col = _OUTCOME_COL[outcome]
    # A3: stamp last_attempt_at on EVERY outcome (success + captcha + block), not just confirmed
    # applies. The ATS apply lease gates the min-gap / Doctor floor off COALESCE(last_applied_at,
    # last_attempt_at), so a never-succeeded (hard-blocking) host -- whose last_applied_at stays
    # NULL -- is still spaced instead of leasing back-to-back at zero gap. count_24h + last_applied_at
    # remain CONFIRMED-apply only (bump_cap).
    extra = ", count_24h = count_24h + 1, last_applied_at = now(), last_attempt_at = now()" if bump_cap \
        else ", last_attempt_at = now()"
    with conn.cursor() as cur:
        for sk in scope_keys:
            cur.execute(
                "INSERT INTO rate_governor (scope_key) VALUES (%s) ON CONFLICT (scope_key) DO NOTHING", (sk,)
            )
            cur.execute(
                f"UPDATE rate_governor SET {col} = {col} + 1{extra}, updated_at = now() WHERE scope_key = %s",
                (sk,),
            )
    if commit:
        conn.commit()


def evaluate_breakers(
    conn, *, captcha_threshold=0.4, min_samples=8, throttle_gap_multiplier=3, cool_seconds=1800, commit=True
):
    """Adaptive circuit-breaker. For each scope: repeated hard blocks -> demoted
    (compute-only, sticky); high ``challenge_rate`` with enough samples -> paused
    or throttled (auto-recovers at ``breaker_until``); a recovered scope -> ok.
    Returns ``[(scope_key, new_state), ...]``."""
    changed = []
    # Compute the pause cut once, with an epsilon so an exact boundary rate (e.g. 6/10)
    # is treated as >= the cut rather than slipping under it by one float ULP (see _RATE_EPS).
    pause_at = captcha_threshold * 1.5 - _RATE_EPS
    with conn.cursor() as cur:
        # A1: ORDER BY scope_key imposes ONE global lock order on rate_governor rows. The Doctor's
        # sweep_expired pre-locks its target scopes in the SAME ORDER BY scope_key order, so the two
        # writers can never take the per-row UPDATE locks in opposite orders -> no AB-BA deadlock
        # (SQLSTATE 40P01) under a correlated outage that makes both touch the most rows.
        cur.execute(
            "SELECT scope_key, success_24h, captcha_24h, block_24h, challenge_rate, breaker_state "
            "FROM rate_governor ORDER BY scope_key"
        )
        rows = cur.fetchall()
    for r in rows:
        total = r["success_24h"] + r["captcha_24h"] + r["block_24h"]
        state = r["breaker_state"]
        rate = float(r["challenge_rate"] or 0)
        new = state
        if r["block_24h"] >= 3:
            new = "demoted"  # sticky: hard-blocked IP/host -> compute-only + alert
        elif total >= min_samples and rate >= captcha_threshold:
            new = "paused" if rate >= pause_at else "throttled"
        elif state in ("throttled", "paused") and (total < min_samples or rate < captcha_threshold * 0.5):
            new = "ok"
        if new == state:
            continue
        with conn.cursor() as cur:
            if new == "throttled":
                # Widen the gap from the PRISTINE base (not the current value) so
                # repeated throttle->recover->throttle cycles can't compound it
                # (3x, 9x, 27x...). base is captured once, on the first throttle.
                cur.execute(
                    "UPDATE rate_governor SET breaker_state='throttled', "
                    "base_min_gap_seconds = COALESCE(base_min_gap_seconds, min_gap_seconds), "
                    "min_gap_seconds = COALESCE(base_min_gap_seconds, min_gap_seconds) * %s, "
                    "breaker_until = now() + make_interval(secs => %s), updated_at = now() WHERE scope_key = %s",
                    (throttle_gap_multiplier, cool_seconds, r["scope_key"]),
                )
            elif new == "paused":
                cur.execute(
                    "UPDATE rate_governor SET breaker_state='paused', breaker_until = now() + make_interval(secs => %s), "
                    "updated_at = now() WHERE scope_key = %s",
                    (cool_seconds, r["scope_key"]),
                )
            elif new == "demoted":
                cur.execute(
                    "UPDATE rate_governor SET breaker_state='demoted', updated_at = now() WHERE scope_key = %s",
                    (r["scope_key"],),
                )
            else:  # ok -- restore the pristine min-gap
                cur.execute(
                    "UPDATE rate_governor SET breaker_state='ok', breaker_until = NULL, "
                    "min_gap_seconds = COALESCE(base_min_gap_seconds, min_gap_seconds), updated_at = now() WHERE scope_key = %s",
                    (r["scope_key"],),
                )
        changed.append((r["scope_key"], new))
    if commit:
        conn.commit()
    return changed


def trip_breaker(conn, scope_key, *, state="paused", cool_seconds=1800, commit=True) -> None:
    """A2: AUTO-EXPIRING breaker trip. Set breaker_state + breaker_until = now() + cool together
    (mirroring evaluate_breakers' throttled/paused branches) so clear_expired_breakers can recover
    it at TTL. This is the ONLY breaker-write primitive a transient/systemic event should use --
    NEVER monitor.pause_scope (breaker_until=NULL, sticky human-only). ``state`` in
    {'throttled','paused'} (a 'demoted' trip is sticky-by-design and not offered here)."""
    if state not in ("throttled", "paused"):
        raise ValueError(f"trip_breaker state must be throttled|paused, got {state!r}")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO rate_governor (scope_key) VALUES (%s) ON CONFLICT (scope_key) DO NOTHING",
                    (scope_key,))
        cur.execute(
            "UPDATE rate_governor SET breaker_state=%s, breaker_until = now() + make_interval(secs => %s), "
            "updated_at = now() WHERE scope_key = %s",
            (state, cool_seconds, scope_key),
        )
    if commit:
        conn.commit()


def clear_expired_breakers(conn, *, commit=True):
    """Restore throttled/paused scopes to ok once ``breaker_until`` has passed.
    (Demoted scopes are sticky -- they require an explicit reset / re-promotion.)"""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE rate_governor SET breaker_state='ok', breaker_until = NULL, "
            "min_gap_seconds = COALESCE(base_min_gap_seconds, min_gap_seconds), updated_at = now() "
            "WHERE breaker_state IN ('throttled','paused') AND breaker_until IS NOT NULL AND breaker_until < now() "
            "RETURNING scope_key"
        )
        out = [r["scope_key"] for r in cur.fetchall()]
    if commit:
        conn.commit()
    return out


def roll_window(conn, *, commit=True) -> None:
    """Nightly: reset the rolling-24h counters + re-anchor the window.
    halted_until is deliberately NOT reset here -- a LinkedIn account halt must
    survive a nightly window roll (see test_roll_window_preserves_halt)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE rate_governor SET window_start = now(), count_24h = 0, "
            "success_24h = 0, captcha_24h = 0, block_24h = 0, updated_at = now()"
        )
    if commit:
        conn.commit()
