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
    extra = ", count_24h = count_24h + 1, last_applied_at = now()" if bump_cap else ""
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
    with conn.cursor() as cur:
        cur.execute(
            "SELECT scope_key, success_24h, captcha_24h, block_24h, challenge_rate, breaker_state FROM rate_governor"
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
            new = "paused" if rate >= captcha_threshold * 1.5 else "throttled"
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
    """Nightly: reset the rolling-24h counters + re-anchor the window."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE rate_governor SET window_start = now(), count_24h = 0, "
            "success_24h = 0, captcha_24h = 0, block_24h = 0, updated_at = now()"
        )
    if commit:
        conn.commit()
