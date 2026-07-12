"""FLEET DOCTOR v1 -- bounded, reversible, monotonically-conservative auto-remediation.

The Doctor reads the fleet's centralized failure data (apply_queue.apply_error /
apply_status / target_host / worker_id; the per-worker recent_log / last_error are
NOT read here — log-content root-cause analysis lives in the Fleet Diagnoser,
fleet/diagnoser.py), clusters failures over a rolling window, and applies a CLOSED set of four auto-fixes,
each of which can ONLY make the fleet MORE conservative. Everything else it finds
becomes a human RECOMMENDATION row -- never auto-applied.

== THE KEYSTONE INVARIANT (D1) ==
Every AUTO action passes through ``_assert_conservative`` before it touches the DB. The
gate is an ALLOW-LIST over a CLOSED set of four ``knob_type``s {host_skip, timeout_bump,
quarantine, pace_or_pause} and it raises ``ConservativeViolation`` on anything that is
not allow-listed OR that would INCREASE fleet activity (resume/un-pause, re-approve or
apply, raise the spend cap, lower the inter-apply gap, edit the prompt/profile/resume,
raise result counts, or touch the LinkedIn lane in ANY way). There is no code path that
mutates apply state for an auto action without first calling this gate.

NO LLM in v1: every diagnosis is RULE-TEMPLATED per reason group (we know what each
failure class means). ``_llm_explain_hook`` is a clearly-marked, deliberately-inert seam
for a future explain pass; it MUST stay non-mutating and is never required for safety.

LinkedIn (D2 / H1 -- the catastrophe fix): the Doctor never reads the linkedin lane to act
on it and never writes ``linkedin_queue`` / ``account:linkedin`` / any linkedin knob. ``LANE``
excludes it; the gate hard-rejects any action whose lane/scope mentions linkedin. CRUCIALLY the
Doctor's lane-pause routes to the ATS-ONLY ``fleet_config.ats_paused`` flag (via
``pgqueue.set_ats_paused``), NEVER the shared ``fleet_config.paused`` -- the LinkedIn worker
reads ``paused`` via ``should_halt`` but never ``ats_paused``, so a Doctor pause can NOT halt the
LinkedIn catastrophe lane. ``_assert_conservative`` validates the ACTUATOR (which column/scope is
written), not just the action label: a pause MUST declare actuator='ats_paused' and any action
declaring the forbidden ``set_paused``/``fleet_config.paused`` actuator is refused.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import sqlite3
import logging
import math as _math
import os
from pathlib import Path
import time

from applypilot.fleet import governor, heartbeat
from applypilot.fleet.failure_taxonomy import canonical_failure_group
from applypilot.fleet.worker import _scrub  # reuse the worker's secret redactor before storing log text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Closed allow-list of auto-fix knob types. ANYTHING else the Doctor finds is a
# human recommendation, never an auto action. (D1 / D5)
# ---------------------------------------------------------------------------
AUTO_KNOBS = ("host_skip", "timeout_bump", "quarantine", "pace_or_pause")

# ---------------------------------------------------------------------------
# H1 EFFECT-LEVEL GATE: the SET of DB actuators a Doctor auto action is allowed to write.
# The gate validates the actuator (which column/scope), not just the action label. The
# single forbidden actuator that matters is fleet_config.paused (read by BOTH lanes'
# should_halt -> a pause there halts LinkedIn). The Doctor's pause routes to ats_paused
# (ATS-only) instead, which the LinkedIn lane never reads. _assert_conservative refuses any
# action whose declared actuator is 'set_paused'/'fleet_config.paused' or that targets the
# 'global'/'account:linkedin' governor scope.
_ALLOWED_PAUSE_ACTUATOR = "ats_paused"            # the ONLY pause target the Doctor may use
_FORBIDDEN_ACTUATORS = frozenset({
    "set_paused", "fleet_config.paused", "paused",  # shared kill switch -> would halt LinkedIn
    "halted_until", "account:linkedin", "linkedin_queue", "kill_linkedin",
})

# A10: CLOSED allow-set of the DB actuators each knob_type may write -- inverted from the old
# deny-list-on-an-optional-field. ``actuator`` is now MANDATORY on every auto action and validated
# against this set, so a correct label with the wrong SQL (or a future _apply_* edit that writes a
# different column) is refused. Every Doctor mutation is routed through _execute_actuator, and a
# static test fails if a forbidden mutation (set_paused / fleet_config paused / breaker /
# account:linkedin / halted_until) appears outside that single dispatcher.
_ALLOWED_ACTUATORS: dict[str, frozenset] = {
    "host_skip": frozenset({"doctor_skip_until"}),
    "pace_or_pause": frozenset({"doctor_min_gap_floor", "ats_paused"}),
    "timeout_bump": frozenset({"agent_timeout_override"}),
    "quarantine": frozenset({"poison_jobs.quarantined_at"}),
}

# Reason groups -- what each failure class MEANS (drives the rule-templated diagnosis).
# A worker writes one of these into apply_queue.apply_error / apply_status; we map the
# raw token to a group so clustering + templating is stable.
_HARD_BLOCK = "hard_block"
_TIMEOUT = "timeout"
_AGENT = "agent"
_DEAD = "dead"
_AUTH = "auth"
_LOCATION = "location"
_NON_ACTIONABLE = "non_actionable"

_REASON_GROUPS: dict[str, tuple[str, ...]] = {
    _HARD_BLOCK: ("captcha", "blocked", "cloudflare_blocked", "site_blocked", "rate_limited"),
    _TIMEOUT: ("failed:timeout", "timeout"),
    _AGENT: ("no_result_line", "stuck", "suspicious_page"),
    _DEAD: ("expired",),
    _AUTH: ("email_verification_required", "auth_required", "login"),
    _LOCATION: ("not_eligible_location",),
}

# Bounds (D5). The timeout override is clamped to [current_default .. _TIMEOUT_CEILING].
#
# H7 INVARIANT: _TIMEOUT_CEILING MUST be < the watchdog's job_max_seconds, or the very agent
# the bump was meant to let finish is killed + quarantined as job_over_max at job_max -- the
# Doctor would MANUFACTURE the poison pattern. job_max is read from the SAME source the watchdog
# uses (WatchdogConfig.job_max_seconds, default 600) so the two cannot drift; the ceiling is a
# safety margin below it. ``_assert_timeout_ceiling_below_watchdog`` (called at import) fails
# loudly if this inequality is ever broken.
_DEFAULT_AGENT_TIMEOUT = 300        # mirrors apply_worker_main._setup_apply_env's default


def _watchdog_job_max() -> int:
    """The watchdog's job_max_seconds kill threshold, from the SAME source the watchdog reads
    (so the timeout ceiling can't drift above it). Best-effort: falls back to 600 on any error."""
    try:
        from applypilot.fleet.watchdog import WatchdogConfig
        return int(WatchdogConfig.job_max_seconds)
    except Exception:  # pragma: no cover - defensive
        return 600


_TIMEOUT_MARGIN_BELOW_WATCHDOG = 60   # ceiling sits at least this far under the watchdog kill
_TIMEOUT_CEILING = max(_DEFAULT_AGENT_TIMEOUT + 1, _watchdog_job_max() - _TIMEOUT_MARGIN_BELOW_WATCHDOG)
_TIMEOUT_BUMP_STEP = 90               # H7: additive (cur+90), not cur*2 -- gentle, bounded ratchet
_PACE_MULTIPLIER = 3                # pace-down widens the host min_gap from its pristine base
_PACE_GAP_CEILING = 3600            # never widen a gap past 1h (still conservative; bounded)


def _assert_timeout_ceiling_below_watchdog() -> None:
    """H7: hard-assert the timeout ceiling is strictly below the watchdog kill. Run at import
    AND directly testable. A violation here means a timeout_bump could push an agent past the
    watchdog's job_max -> killed + quarantined -> manufactured poison."""
    jm = _watchdog_job_max()
    if not (_TIMEOUT_CEILING < jm):
        raise AssertionError(
            f"_TIMEOUT_CEILING ({_TIMEOUT_CEILING}s) must be < watchdog job_max_seconds ({jm}s) "
            "or a timeout_bump manufactures the job_over_max poison pattern (H7)")


_assert_timeout_ceiling_below_watchdog()

# Thresholds (defaults; tunable per call).
_HOST_SKIP_MIN = 3                  # >=K hard-block failures on a host (with denominator, H6)
_HOST_SKIP_MIN_SAMPLES = 5          # H6: require >=N host attempts (denominator) before a skip
_HOST_SKIP_MIN_RATE = 0.3          # H6: AND hb/attempts >= this -- a healthy big host won't trip
_TIMEOUT_BUMP_MIN = 3               # >=3 timeout failures clustered -> bump the timeout
_QUARANTINE_MIN = 3                 # a single url failed >=K times -> quarantine it
_QUARANTINE_MIN_WORKERS = 2         # H10: require the failures span >=2 workers (not one flaky box)
_LANE_HARD_BLOCK_RATE = 0.5         # hard-block share of a lane's failures -> pace down
_LANE_PAUSE_RATE = 0.8              # severe: pause the apply lane (still human-only to RESUME)
_LANE_MIN_SAMPLES = 5               # don't pace on a tiny sample
_LANE_PAUSE_MIN_SAMPLES = 20        # H5: higher floor (ATTEMPTS) before a lane-wide pause
_LANE_PAUSE_MIN_HOSTS = 3           # H5: hard-blocks must span >=3 hosts before any pause
_LANE_PAUSE_MIN_WORKERS = 2         # H5: ...AND >=K workers (concentrated -> host_skip instead)
_PAUSE_DEBOUNCE_SECONDS = 240       # H5: require the breach on 2 consecutive passes (>= 1 interval)

# H2 AGGREGATE CIRCUIT-BREAKER + blast-radius caps (per-pass + per-day).
_SYSTEMIC_HOST_COUNT = 5           # > this many distinct hosts crossing threshold in one pass...
_SYSTEMIC_HOST_FRACTION = 0.2      # ...OR > this fraction of hosts-with-traffic -> SYSTEMIC
# A6: a correlated event is only SYSTEMIC (fleet-wide, not one bad home_IP/worker) when the
# tripping hosts span >= this many distinct workers. A single flagged residential IP makes every
# host IT touches captcha, but those failures concentrate on ONE worker -- the home_ip breaker +
# watchdog restart handle that, and the per-host auto-fixes on OTHER healthy workers must proceed.
_SYSTEMIC_MIN_WORKERS = 2
# A6: the absolute floor of the relative-with-floor host threshold (max(M_abs, ceil(P%*traffic))).
_SYSTEMIC_HOST_ABS = 5
# A6: emit the de-duplicated fleet-wide escalation alert on the Nth consecutive systemic pass.
_SYSTEMIC_STREAK_ALERT = 3
_MAX_HOST_SKIPS_PER_PASS = 3       # <= this many host_skips applied per pass; overflow -> recommend
_MAX_PACE_PER_PASS = 5             # <= this many pace actions per pass; overflow -> recommend
_MAX_HOST_SKIPS_PER_DAY = 12       # daily budget; overflow -> recommend-only + alert
_MAX_PACE_PER_DAY = 40             # daily budget for pace actions

# TTLs (seconds). Everything the Doctor sets is bounded in time; the sweep expires it.
_KNOB_TTL = {
    "host_skip": 24 * 3600,
    "timeout_bump": 24 * 3600,
    "quarantine": 7 * 24 * 3600,
    "pace_or_pause": 6 * 3600,
}

# The Doctor only ever touches the apply (ats) lane. LinkedIn is excluded BY CONSTRUCTION
# (D2): it is never in this set, so it is never clustered, never acted on.
LANE = "ats"


class ConservativeViolation(RuntimeError):
    """Raised by ``_assert_conservative`` when an action is not on the closed allow-list
    or would INCREASE fleet activity. A raised ConservativeViolation must abort the auto
    action -- it is the load-bearing D1 control."""


# ---------------------------------------------------------------------------
# D1 GATE -- the single chokepoint every auto action passes through.
# ---------------------------------------------------------------------------
# Tokens that signal an activity-INCREASING intent. If any appears as an action 'op'
# (or, defensively, anywhere structural in the action), the gate refuses. This is a
# deny-list layered UNDER the allow-list: an action must (a) be one of the four knob
# types AND (b) carry none of these increasing intents.
_FORBIDDEN_OPS = frozenset({
    "resume", "unpause", "un_pause", "re_approve", "reapprove", "approve",
    "apply", "submit", "raise_cap", "raise_spend_cap", "increase_cap",
    "lower_gap", "decrease_gap", "speed_up", "edit_prompt", "edit_profile",
    "edit_resume", "tailor", "increase_results", "raise_results", "linkedin",
})


def _mentions_linkedin(*vals) -> bool:
    """True if any value mentions the linkedin lane (D2 -- never act on it)."""
    for v in vals:
        if v is None:
            continue
        if "linkedin" in str(v).lower():
            return True
    return False


def _assert_conservative(action: dict) -> None:
    """THE keystone gate (D1). Raise ``ConservativeViolation`` unless ``action`` is one of
    the four allow-listed, monotonically-conservative auto-fixes. Never mutates anything.

    An action is a dict with at least ``{'knob_type': <one of AUTO_KNOBS>}``. We reject:
      * any knob_type not in the closed AUTO_KNOBS allow-list;
      * any action that mentions the LinkedIn lane anywhere (scope/lane/op) -- D2;
      * any action carrying an activity-INCREASING op (resume/re-approve/raise-cap/
        lower-gap/edit-prompt/apply/...);
      * a timeout_bump whose target exceeds the ceiling or is <= the default (a bump must
        RAISE within [default..900], never lower the effective timeout);
      * a pace_or_pause that would LOWER a gap, or a 'pause' targeting anything but the
        apply lane (we never pause/alter LinkedIn from here).
    """
    if not isinstance(action, dict):
        raise ConservativeViolation(f"action must be a dict, got {type(action).__name__}")

    knob = action.get("knob_type")
    if knob not in AUTO_KNOBS:
        raise ConservativeViolation(
            f"knob_type {knob!r} is not in the closed allow-list {AUTO_KNOBS}; "
            "everything else is a human recommendation, never an auto action"
        )

    # D2: never act on / mention LinkedIn.
    if _mentions_linkedin(action.get("lane"), action.get("scope_key"), action.get("op"),
                          action.get("host"), action.get("url")):
        raise ConservativeViolation("LinkedIn lane is off-limits to the Doctor (D2)")

    # Activity-increasing op deny-list (layered under the allow-list).
    op = action.get("op")
    if op is not None and str(op) in _FORBIDDEN_OPS:
        raise ConservativeViolation(f"op {op!r} would increase fleet activity -- refused")

    # H1 EFFECT-LEVEL GATE: validate the ACTUATOR, not just the action label. The action may
    # declare the DB target it will write via 'actuator'; if present it must NOT be the shared
    # kill switch (fleet_config.paused) or any LinkedIn-scoped governor row -- those have a blast
    # radius (the LinkedIn lane) the per-action lane label never inspects. This is THE structural
    # fix for the catastrophe: a 'conservative-for-ATS' pause that wrote fleet_config.paused was
    # catastrophic-for-LinkedIn.
    actuator = action.get("actuator")
    if actuator is not None and str(actuator) in _FORBIDDEN_ACTUATORS:
        raise ConservativeViolation(
            f"actuator {actuator!r} has a blast radius beyond the ATS lane (would touch the shared "
            "kill switch / LinkedIn) -- refused (H1). A Doctor pause must route to ats_paused.")

    # A10: actuator is now MANDATORY for every auto action and must be in the CLOSED per-knob
    # allow-set (allow-by-default-deny inverted from the old optional deny-list). A correct label
    # with the wrong column is refused here.
    if actuator is None:
        raise ConservativeViolation(
            f"every auto action must declare an 'actuator'; {knob!r} declared none (A10)")
    allowed = _ALLOWED_ACTUATORS.get(knob, frozenset())
    if str(actuator) not in allowed:
        raise ConservativeViolation(
            f"actuator {actuator!r} is not in the allow-set {sorted(allowed)} for knob_type {knob!r} (A10)")

    # Per-knob bound checks (D5).
    if knob == "timeout_bump":
        new = action.get("new_timeout")
        cur = action.get("current_default", _DEFAULT_AGENT_TIMEOUT)
        if not isinstance(new, int) or isinstance(new, bool):
            raise ConservativeViolation("timeout_bump requires an integer new_timeout")
        if new > _TIMEOUT_CEILING:
            raise ConservativeViolation(
                f"timeout_bump {new}s exceeds the ceiling {_TIMEOUT_CEILING}s")
        if new <= cur:
            # A bump must RAISE the timeout (a longer timeout is conservative); lowering it
            # would let the agent be killed sooner -> more retries -> more host hits.
            raise ConservativeViolation(
                f"timeout_bump {new}s does not raise the current default {cur}s")

    if knob == "pace_or_pause":
        sub = action.get("op_kind")  # 'pace' (widen gap) | 'pause' (apply lane)
        if sub == "pace":
            new_gap = action.get("new_gap")
            old_gap = action.get("old_gap")
            if not isinstance(new_gap, int) or isinstance(new_gap, bool):
                raise ConservativeViolation("pace requires an integer new_gap")
            if old_gap is not None and new_gap < old_gap:
                raise ConservativeViolation(
                    f"pace new_gap {new_gap}s is SMALLER than {old_gap}s -- would speed up; refused")
            if new_gap > _PACE_GAP_CEILING:
                raise ConservativeViolation(
                    f"pace new_gap {new_gap}s exceeds the ceiling {_PACE_GAP_CEILING}s")
            # H1: a pace must target a per-host governor scope ('host:'||<host>), never the
            # 'global' or 'account:linkedin' scope (the lease doesn't even read 'global', and
            # touching account:linkedin would reach the LinkedIn lane).
            sk = str(action.get("scope_key") or "")
            if not sk.startswith("host:"):
                raise ConservativeViolation(
                    f"pace scope_key {sk!r} must be a per-host scope 'host:<h>' (H1); refused")
        elif sub == "pause":
            if action.get("lane") not in (None, LANE):
                raise ConservativeViolation("pause may only target the apply (ats) lane")
            # H1: the pause actuator MUST be the ATS-only flag. A pause action that did not
            # declare actuator='ats_paused' (or declared a forbidden one) is refused -- this is
            # what guarantees a Doctor pause can never write fleet_config.paused and halt LinkedIn.
            if action.get("actuator") != _ALLOWED_PAUSE_ACTUATOR:
                raise ConservativeViolation(
                    f"pause actuator must be {_ALLOWED_PAUSE_ACTUATOR!r} (ATS-only); got "
                    f"{action.get('actuator')!r} -- a Doctor pause may NEVER write the shared "
                    "fleet_config.paused (it is read by the LinkedIn lane). (H1)")
        else:
            raise ConservativeViolation(f"pace_or_pause requires op_kind in (pace, pause), got {sub!r}")

    # host_skip + quarantine have no numeric bound to check; reaching here = allow-listed.


# ---------------------------------------------------------------------------
# ANALYZE -- cluster failures over the rolling window.
# ---------------------------------------------------------------------------
def _reason_group(token: str | None) -> str | None:
    """Map a raw apply_error / apply_status token to its reason group, or None if the
    token doesn't match any known failure class (a token containing 'rate_limited' maps
    to hard_block via substring, per the spec's ``*rate_limited``)."""
    if not token:
        return None
    t = str(token).strip().lower()
    for group, members in _REASON_GROUPS.items():
        for m in members:
            if t == m or (m == "rate_limited" and "rate_limited" in t):
                return group
    canonical = canonical_failure_group(t)
    if canonical in {
        "retired_unapproved", "duplicate_or_already_applied", "operator_skipped",
        "remediation_history", "unclassified",
    }:
        return _NON_ACTIONABLE
    if canonical == "unavailable":
        return _DEAD
    if canonical in {"rate_or_application_limit", "spam_or_abuse_filter"}:
        return _HARD_BLOCK
    if canonical == "timeout_or_stuck":
        return _TIMEOUT
    if canonical == "access_or_verification":
        return _AUTH
    if canonical in {"eligibility", "job_type_excluded", "form_or_profile_constraint"}:
        return _LOCATION
    if canonical in {
        "agent_no_result", "browser_infrastructure", "submission_uncertain",
        "page_or_content_failure", "budget_exhausted", "routing_or_policy",
        "manual_review", "provider_usage_limit", "missing_required_material",
        "not_an_application", "malformed_failure_reason",
    }:
        return _AGENT
    return None


def analyze(conn, *, window_minutes: int = 60) -> dict:
    """Read the fleet's failure data and cluster it. Pure read (rolls back). Returns:

      {
        "window_minutes": int,
        "host_failures":   {host -> {group -> count}},        # apply lane only
        "url_failures":    {url  -> {"count": int, "host": str, "reason": str}},
        "cluster_rows":    [ {reason, host, machine, lane, sample_count, group}, ... ],
        "lane_hard_block_rate": float,   # hard-block share of all apply-lane failures
        "lane_failures":   int,
      }

    Clustering key is (reason_group x target_host x worker_id/machine x lane). LinkedIn is
    excluded by construction: we only read apply_queue (lane='ats') + role='apply' workers.
    """
    host_failures: dict[str, dict[str, int]] = {}
    host_workers: dict[str, set] = {}          # H5/H10: distinct worker_ids per host
    url_failures: dict[str, dict] = {}
    url_workers: dict[str, set] = {}           # H10: distinct worker_ids per url
    cluster_map: dict[tuple, dict] = {}
    lane_total = 0
    lane_hard = 0

    with conn.cursor() as cur:
        # Recently-updated apply_queue rows carrying a terminal failure signal. We read
        # both apply_error and apply_status (a worker may stamp either); target_host is the
        # governor key; worker_id is the acting machine. Only the apply (ats) lane.
        cur.execute(
            "SELECT url, COALESCE(target_host, apply_domain) AS host, worker_id, "
            "       apply_error, apply_status, status, attempts "
            "FROM apply_queue "
            "WHERE lane = %s "
            "  AND updated_at >= now() - make_interval(mins => %s) "
            "  AND (apply_error IS NOT NULL OR status IN ('blocked','failed','crash_unconfirmed')) ",
            (LANE, window_minutes),
        )
        rows = [dict(r) for r in cur.fetchall()]
    conn.rollback()  # read-only

    for r in rows:
        token = r.get("apply_error") or r.get("apply_status")
        group = _reason_group(token)
        if group == _NON_ACTIONABLE:
            continue
        if group is None:
            # An unrecognized failure token is still surfaced as a generic cluster so a
            # human can SEE it (recommendation lane), but it never drives an auto-fix.
            group = "other"
        host = r.get("host") or "unknown"
        machine = r.get("worker_id") or "unknown"
        url = r.get("url")

        if group != "other":
            host_failures.setdefault(host, {}).setdefault(group, 0)
            host_failures[host][group] += 1
            host_workers.setdefault(host, set()).add(machine)

        # Per-url poison signal. H10: restrict the poison pattern to HARD reasons (hard_block /
        # timeout) -- an agent/auth/dead failure is a prompt/profile/stale issue, not a poison url,
        # so counting it would let a prompt bug quarantine the fleet's best jobs one url at a time.
        if group in (_HARD_BLOCK, _TIMEOUT):
            uf = url_failures.setdefault(url, {"count": 0, "host": host, "reason": group})
            uf["count"] += 1
            url_workers.setdefault(url, set()).add(machine)
            # Prefer a hard reason label over a stale one for display.
            if uf["reason"] in ("other", _DEAD) and group not in ("other", _DEAD):
                uf["reason"] = group

        ckey = (group, host, machine, LANE)
        c = cluster_map.setdefault(ckey, {
            "reason": group, "host": host, "machine": machine, "lane": LANE,
            "sample_count": 0, "group": group,
        })
        c["sample_count"] += 1

        lane_total += 1
        if group == _HARD_BLOCK:
            lane_hard += 1

    # H5/H6: per-host ATTEMPTS from the governor host scope (the denominator that turns an
    # absolute failure count into a RATE -- a 400-row host with 3 stray captchas must not trip).
    host_attempts: dict[str, int] = {}
    hosts = list(host_failures.keys())
    if hosts:
        scope_keys = [governor.host_scope(h) for h in hosts]
        with conn.cursor() as cur:
            cur.execute(
                "SELECT scope_key, (success_24h + captcha_24h + block_24h) AS attempts "
                "FROM rate_governor WHERE scope_key = ANY(%s)",
                (scope_keys,))
            gov_rows = {r["scope_key"]: int(r["attempts"] or 0) for r in cur.fetchall()}
        conn.rollback()
        for h in hosts:
            host_attempts[h] = gov_rows.get(governor.host_scope(h), 0)

    # H5: distinct hosts/workers contributing HARD BLOCKS (the breadth gate for a lane pause).
    hb_hosts = sorted(h for h, g in host_failures.items() if g.get(_HARD_BLOCK, 0) > 0)
    hb_workers: set = set()
    for h in hb_hosts:
        hb_workers |= host_workers.get(h, set())

    return {
        "window_minutes": window_minutes,
        "host_failures": host_failures,
        "host_workers": {h: sorted(ws) for h, ws in host_workers.items()},
        "host_attempts": host_attempts,
        "url_failures": url_failures,
        "url_workers": {u: sorted(ws) for u, ws in url_workers.items()},
        "cluster_rows": list(cluster_map.values()),
        "lane_failures": lane_total,
        "lane_hard_block_rate": (lane_hard / lane_total) if lane_total else 0.0,
        "hb_hosts": hb_hosts,
        "hb_distinct_hosts": len(hb_hosts),
        "hb_distinct_workers": len(hb_workers),
    }


# ---------------------------------------------------------------------------
# DECIDE -- turn clusters into planned actions (each tagged 'auto' | 'recommend').
# ---------------------------------------------------------------------------
def _cluster_key(reason: str, host: str | None, machine: str | None, lane: str | None) -> str:
    """Stable idempotency key for a diagnosis/cluster (reason|host|machine|lane)."""
    return "|".join(str(x or "-") for x in (reason, host, machine, lane))


# Human-readable templates per reason group (no LLM). Each yields (diagnosis, recommendation).
def _template(group: str, host: str | None, n: int) -> tuple[str, str]:
    h = host or "unknown host"
    return {
        _HARD_BLOCK: (
            f"{n} hard-block failure(s) on {h} (captcha/cloudflare/site-block/rate-limit).",
            "Host is actively blocking the fleet. Auto: un-approve queued rows for this host "
            "+ record a 24h host_skip knob. Human: investigate whether the host should be "
            "dropped from the search set.",
        ),
        _TIMEOUT: (
            f"{n} timeout failure(s) clustered on {h} (the apply agent hit the wall-clock cap).",
            "Slow pages, not a block. Auto: raise the apply-agent timeout within the ceiling "
            "(conservative). Human: confirm the host isn't simply slow-loading by design.",
        ),
        _AGENT: (
            f"{n} agent failure(s) on {h} (no_result_line / stuck / suspicious_page).",
            "The agent could not complete the form. NOT auto-fixable conservatively -- review "
            "the worker log tail; the prompt/profile may need a human edit (never auto-edited).",
        ),
        _DEAD: (
            f"{n} dead-posting failure(s) on {h} (expired).",
            "Postings are stale. NOT a fleet fault. Human: no action needed; jobs are retained "
            "for training (never deleted).",
        ),
        _AUTH: (
            f"{n} auth-wall failure(s) on {h} (email-verification / login / auth_required).",
            "An auth wall needs a human (the catastrophe-avoidance design already parks these). "
            "Review the captcha/auth inbox; do NOT auto-drive auth walls.",
        ),
        _LOCATION: (
            f"{n} location-ineligible failure(s) on {h}.",
            "The role's location gate rejected the profile. Human: tune the search filters; the "
            "Doctor will not auto-change result counts or filters.",
        ),
        "other": (
            f"{n} unclassified failure(s) on {h}.",
            "Unrecognized failure token. Human review only -- the Doctor never auto-acts on an "
            "unclassified cluster.",
        ),
    }[group]


def _host_skip_recurrence(conn, host: str, *, days: int = 7) -> int:
    """H13: count prior host_skip diagnoses for ``host`` in the last ``days`` (recurrence
    linkage). A chronic re-skipping host should escalate to a 'drop host?' recommendation,
    not silently re-skip every cycle. Read-only."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM fleet_diagnoses "
            "WHERE auto_action='host_skip' AND host=%s "
            "  AND created_at >= now() - make_interval(days => %s)",
            (host, days))
        n = int(cur.fetchone()["n"])
    conn.rollback()
    return n


def decide(conn, clusters: dict) -> list[dict]:
    """Turn the ``analyze`` output into a list of planned actions, each tagged
    ``mode='auto'`` (one of the four conservative fixes) or ``mode='recommend'`` (a human
    row). Pure: reads a little governor/config state but mutates NOTHING. Every auto action
    is shaped so ``_assert_conservative`` will accept it.

    HARDENED (red-team):
      H2  -- a SYSTEMIC pass (>M hosts or >X% of hosts-with-traffic crossing threshold)
             downgrades ALL host_skips/paces to recommend-only and emits ONE coarse pause;
             per-pass blast-radius caps (<=N host_skips, <=N paces) push the overflow to
             recommend, host_skip FIRST under pressure.
      H5  -- a lane PAUSE needs ATTEMPTS-based breadth (>=K hosts AND >=K workers, >=N attempts)
             + a 2-pass debounce, not 5 failures on one host.
      H6  -- host_skip needs a RATE with a denominator (governor attempts), not an absolute count.
      H7  -- timeout_bump requires a PURE timeout cluster (no agent failures on the host) and
             bumps additively (cur+90) within a ceiling strictly below the watchdog kill.
      H10 -- quarantine requires the poison span >=2 workers (not one flaky box).
      H13 -- a chronic re-skipping host escalates to a 'drop host?' recommendation.
    """
    planned: list[dict] = []
    host_failures = clusters["host_failures"]
    host_attempts = clusters.get("host_attempts", {})
    host_workers = clusters.get("host_workers", {})
    url_failures = clusters["url_failures"]
    url_workers = clusters.get("url_workers", {})

    # ---- H2: SYSTEMIC correlation detector (evaluated BEFORE any host_skip) ----
    # Hosts that would individually trip the host_skip rate gate this pass.
    tripping = []
    for host, groups in host_failures.items():
        if _mentions_linkedin(host):
            continue
        hb = groups.get(_HARD_BLOCK, 0)
        attempts = int(host_attempts.get(host, 0))
        if _host_skip_qualifies(hb, attempts):
            tripping.append(host)
    hosts_with_traffic = max(1, len([h for h in host_failures if not _mentions_linkedin(h)]))
    # A6: attribute the tripping hosts to distinct workers. If the correlated blocks concentrate on
    # too few workers it is an IP/worker problem (handled by the home_ip breaker + watchdog), NOT a
    # fleet-wide systemic event -- do NOT downgrade the per-host auto-fixes on the other workers.
    tripping_workers: set = set()
    for h in tripping:
        tripping_workers |= set(host_workers.get(h, []))
    # A6: relative-with-floor host threshold: max(absolute floor, ceil(fraction * hosts_with_traffic)).
    host_threshold = max(_SYSTEMIC_HOST_ABS,
                         _math.ceil(_SYSTEMIC_HOST_FRACTION * hosts_with_traffic))
    systemic = (len(tripping) >= host_threshold
                and len(tripping) >= 2
                and len(tripping_workers) >= _SYSTEMIC_MIN_WORKERS)
    # A6: streak counter -- advance on a systemic pass, reset on a non-systemic pass. The alert
    # fires only on the Nth consecutive systemic pass (one de-duplicated escalation, not per-pass).
    systemic_streak = _bump_systemic_streak(conn, systemic)

    if systemic:
        # A2/A6: a SYSTEMIC pass NEVER routes to a sticky breaker state. It emits ONE human-gated
        # recommendation and downgrades every per-host host_skip to recommend-only (a correlated
        # CDN/IP-pool blip must not auto-starve the whole approved queue). We do NOT mass-pause. If
        # a real coarse auto-action is ever wanted it MUST use the auto-expiring ats_paused lane-pause
        # (H1, LinkedIn-decoupled) or governor.trip_breaker -- never monitor.pause_scope.
        escalate = systemic_streak >= _SYSTEMIC_STREAK_ALERT
        # Stable cluster_key while merely systemic (deduped across passes); a DISTINCT escalation key
        # on the Nth consecutive pass so the loud "investigate" alert surfaces exactly once.
        ckey = (_cluster_key("systemic_escalated", "lane", None, LANE) if escalate
                else _cluster_key("systemic", "lane", None, LANE))
        diag = (f"SYSTEMIC: {len(tripping)} distinct hosts across {len(tripping_workers)} workers "
                "crossed the block threshold in one pass (CDN/proxy/IP-pool event likely). Per-host "
                "auto-skips suppressed to avoid starving the whole approved queue.")
        rec = ("Investigate the shared cause (proxy/IP pool/CDN). The Doctor refused to auto "
               "un-approve the queue under a correlated event; pace or pause manually if needed.")
        if escalate:
            diag = (f"SYSTEMIC for {systemic_streak} consecutive passes -- fleet-wide block persists; "
                    "the Doctor is recommend-only while breakers gate the queue. " + diag)
            rec = "ESCALATION: investigate NOW (proxy/IP pool/CDN) -- the lane is auto-gated. " + rec
        planned.append({
            "mode": "recommend", "knob_type": None, "scope_key": "lane", "host": None,
            "lane": LANE, "reason": "systemic_block", "sample_count": len(tripping),
            "severity": "severe",
            "diagnosis": diag,
            "recommendation": rec,
            "cluster_key": ckey,
            "distinct_hosts": len(tripping),
            "distinct_workers": len(tripping_workers),
        })

    # ---- 1) HOST_SKIP -- a host with a hard-block RATE over a denominator (H6) ----
    # Blast-radius cap (H2): at most _MAX_HOST_SKIPS_PER_PASS auto host_skips this pass; the
    # rest fall back to recommend FIRST (host_skip is the only operator-visible filter).
    host_skip_budget = 0 if systemic else _MAX_HOST_SKIPS_PER_PASS
    for host in sorted(host_failures.keys()):
        groups = host_failures[host]
        if _mentions_linkedin(host):
            continue
        hb = groups.get(_HARD_BLOCK, 0)
        attempts = int(host_attempts.get(host, 0))
        if not _host_skip_qualifies(hb, attempts):
            continue
        recurrence = _host_skip_recurrence(conn, host)
        rows_now = _queued_approved_count(conn, host)
        if recurrence >= 3:
            # H13: chronic re-skipping -> escalate to a human 'drop host?' recommendation.
            diag = (f"{host} has been host-skipped {recurrence}x in the last 7 days (chronic). "
                    "Re-approving just re-burns it.")
            rec = "Consider DROPPING this host from the search set (chronic blocker)."
            planned.append({
                "mode": "recommend", "knob_type": None, "scope_key": host, "host": host,
                "lane": LANE, "reason": _HARD_BLOCK, "sample_count": hb, "severity": "severe",
                "diagnosis": diag, "recommendation": rec, "prior_incident_count": recurrence,
                "cluster_key": _cluster_key("host_skip_chronic", host, None, LANE),
            })
            continue
        diag, rec = _template(_HARD_BLOCK, host, hb)
        mode = "auto" if host_skip_budget > 0 else "recommend"
        if mode == "auto":
            host_skip_budget -= 1
        else:
            rec = rec + " (deferred to recommend: per-pass host_skip cap reached.)"
        planned.append({
            "mode": mode,
            "knob_type": "host_skip" if mode == "auto" else None,
            "actuator": "doctor_skip_until" if mode == "auto" else None,  # A10
            "scope_key": host,
            "host": host,
            "lane": LANE,
            "reason": _HARD_BLOCK,
            "sample_count": hb,
            "severity": "severe" if hb >= 2 * _HOST_SKIP_MIN else "warn",
            "new_severity": "severe" if hb >= 2 * _HOST_SKIP_MIN else "warn",  # H12 escalation key
            "diagnosis": diag,
            "recommendation": rec,
            "prior_incident_count": recurrence,
            "rows_affected": rows_now,
            "cluster_key": _cluster_key(_HARD_BLOCK, host, None, LANE),
        })

    # ---- 2) TIMEOUT_BUMP -- a PURE timeout cluster on a host (H7) ----
    cur_default = _read_current_timeout(conn)
    bumped = False
    for host in sorted(host_failures.keys()):
        groups = host_failures[host]
        to = groups.get(_TIMEOUT, 0)
        agent = groups.get(_AGENT, 0)
        if to >= _TIMEOUT_BUMP_MIN and agent == 0 and not bumped:
            # H7: a PURE timeout cluster (no agent failures on the host in-window) -- an agent
            # failure means the agent is wedged, not the page slow; a longer timeout would only
            # let it hang longer. Bump ADDITIVELY (cur+90), clamped below the watchdog kill.
            new_timeout = min(_TIMEOUT_CEILING, cur_default + _TIMEOUT_BUMP_STEP)
            if new_timeout > cur_default:
                diag, rec = _template(_TIMEOUT, host, to)
                planned.append({
                    "mode": "auto",
                    "knob_type": "timeout_bump",
                    "actuator": "agent_timeout_override",  # A10
                    "scope_key": LANE,
                    "host": host,
                    "lane": LANE,
                    "reason": _TIMEOUT,
                    "sample_count": to,
                    "severity": "warn",
                    "new_timeout": new_timeout,
                    "current_default": cur_default,
                    "diagnosis": diag,
                    "recommendation": rec,
                    "cluster_key": _cluster_key(_TIMEOUT, None, None, LANE),
                })
                bumped = True

    # ---- 3) QUARANTINE -- a single url that failed >=K times across >=2 workers (H10) ----
    for url, info in url_failures.items():
        workers = url_workers.get(url, [])
        if (info["count"] >= _QUARANTINE_MIN and len(workers) >= _QUARANTINE_MIN_WORKERS
                and not _mentions_linkedin(url, info.get("host"))):
            host = info.get("host")
            diag = (f"URL failed {info['count']} times ({info.get('reason')}) across "
                    f"{len(workers)} workers -- poison pattern; re-leasing it just re-burns the host.")
            rec = ("Auto: quarantine this single url via the existing poison_jobs path (manual "
                   "one-shot; never deleted, retained for training). Human: review the url.")
            planned.append({
                "mode": "auto",
                "knob_type": "quarantine",
                "actuator": "poison_jobs.quarantined_at",  # A10
                "scope_key": url,
                "url": url,
                "host": host,
                "lane": LANE,
                "reason": info.get("reason") or "poison",
                "sample_count": info["count"],
                "distinct_workers": len(workers),
                "severity": "warn",
                "diagnosis": diag,
                "recommendation": rec,
                "cluster_key": _cluster_key("quarantine", host, url, LANE),
            })

    # ---- 4) PACE_OR_PAUSE -- attempts-based, breadth-gated, debounced (H5) ----
    rate = clusters["lane_hard_block_rate"]
    samples = clusters["lane_failures"]
    distinct_hosts = int(clusters.get("hb_distinct_hosts", 0))
    distinct_workers = int(clusters.get("hb_distinct_workers", 0))
    # ATTEMPTS-based pause rate over the governor (success+captcha+block on apply host scopes),
    # NOT failures-only -- a transient flaky host can no longer self-pause the whole lane (H5).
    attempt_rate, attempt_total = _lane_attempt_block_rate(conn)
    pause_breadth_ok = (distinct_hosts >= _LANE_PAUSE_MIN_HOSTS
                        and distinct_workers >= _LANE_PAUSE_MIN_WORKERS
                        and attempt_total >= _LANE_PAUSE_MIN_SAMPLES
                        and attempt_rate >= _LANE_PAUSE_RATE)

    if pause_breadth_ok and not systemic:
        # H5 debounce: require the breach on TWO consecutive passes before pausing (persist
        # doctor_pause_armed_at). The first qualifying pass ARMS; only a second within the debounce
        # window fires the pause. ALWAYS a recommendation on the arming pass so the human sees it.
        debounced = _pause_debounce_ready(conn)
        diag = (f"Apply-lane block rate {attempt_rate:.0%} over {attempt_total} attempts spanning "
                f"{distinct_hosts} hosts / {distinct_workers} workers -- severe, broad block.")
        if debounced:
            rec = ("Auto: PAUSE the apply lane (ATS-only flag; the LinkedIn lane is untouched). "
                   "RESUMING is human-only. Human: investigate the systemic block before resuming.")
            planned.append({
                "mode": "auto",
                "knob_type": "pace_or_pause",
                "op_kind": "pause",
                "actuator": _ALLOWED_PAUSE_ACTUATOR,   # H1: ATS-only, NEVER fleet_config.paused
                "scope_key": LANE,
                "lane": LANE,
                "reason": _HARD_BLOCK,
                "sample_count": attempt_total,
                "severity": "severe",
                "distinct_hosts": distinct_hosts,
                "distinct_workers": distinct_workers,
                "diagnosis": diag,
                "recommendation": rec,
                "cluster_key": _cluster_key("pace_or_pause", "lane", "pause", LANE),
            })
        else:
            planned.append({
                "mode": "recommend", "knob_type": None, "scope_key": "lane", "host": None,
                "lane": LANE, "reason": _HARD_BLOCK, "sample_count": attempt_total,
                "severity": "severe", "distinct_hosts": distinct_hosts,
                "distinct_workers": distinct_workers,
                "diagnosis": diag + " (ARMED -- will auto-pause if it persists next pass.)",
                "recommendation": "Lane-pause armed (1st pass). Pace/pause manually if urgent.",
                "cluster_key": _cluster_key("pace_or_pause", "lane", "arm", LANE),
            })
    else:
        # Not broad enough to pause: disarm (clear the debounce) so a one-off doesn't linger armed.
        _pause_disarm(conn)

    # Per-host PACE for the elevated (not pause-worthy) tier, with a per-pass cap (H2).
    if samples >= _LANE_MIN_SAMPLES and rate >= _LANE_HARD_BLOCK_RATE and not systemic:
        offending = sorted(
            h for h, g in host_failures.items()
            if g.get(_HARD_BLOCK, 0) > 0 and not _mentions_linkedin(h)
        )
        pace_budget = _MAX_PACE_PER_PASS
        for host in offending:
            scope_key = governor.host_scope(host)
            old_gap, new_gap = _planned_pace_gap(conn, scope_key)
            diag = (f"Apply-lane hard-block rate {rate:.0%} over {samples} failures -- "
                    f"elevated; pace {host} down to bleed off pressure.")
            rec = ("Auto: widen this host's min_gap floor (pace down; conservative). Human: it "
                   "auto-restores at TTL; no manual restore needed.")
            mode = "auto" if pace_budget > 0 else "recommend"
            if mode == "auto":
                pace_budget -= 1
            planned.append({
                "mode": mode,
                "knob_type": "pace_or_pause" if mode == "auto" else None,
                "actuator": "doctor_min_gap_floor" if mode == "auto" else None,  # A10
                "op_kind": "pace",
                "scope_key": scope_key if mode == "auto" else host,
                "host": host,
                "lane": LANE,
                "reason": _HARD_BLOCK,
                "sample_count": host_failures[host].get(_HARD_BLOCK, 0),
                "severity": "warn",
                "old_gap": old_gap,
                "new_gap": new_gap,
                "diagnosis": diag,
                "recommendation": rec,
                "cluster_key": _cluster_key("pace_or_pause", host, "pace", LANE),
            })

    # 5) RECOMMENDATIONS -- every non-auto-fixable cluster becomes a human row (status
    # recommended). agent / dead / auth / location / 'other' are NEVER auto-applied.
    for c in clusters["cluster_rows"]:
        group = c["group"]
        if group in (_HARD_BLOCK, _TIMEOUT):
            continue  # already handled by an auto path above (host-level)
        diag, rec = _template(group, c["host"], c["sample_count"])
        planned.append({
            "mode": "recommend",
            "knob_type": None,
            "scope_key": c["host"],
            "host": c["host"],
            "machine": c["machine"],
            "lane": c["lane"],
            "reason": group,
            "sample_count": c["sample_count"],
            "severity": "warn" if group in (_AGENT, _AUTH) else "info",
            "diagnosis": diag,
            "recommendation": rec,
            "cluster_key": _cluster_key(group, c["host"], c["machine"], c["lane"]),
        })

    return planned


def _read_current_timeout(conn) -> int:
    """Effective current apply-agent timeout default: the active override if set, else the
    env/default. Read-only."""
    with conn.cursor() as cur:
        cur.execute("SELECT agent_timeout_override FROM fleet_config WHERE id=1")
        row = cur.fetchone()
    conn.rollback()
    if row and row.get("agent_timeout_override") is not None:
        return int(row["agent_timeout_override"])
    try:
        return int(os.environ.get("APPLYPILOT_AGENT_TIMEOUT") or _DEFAULT_AGENT_TIMEOUT)
    except (TypeError, ValueError):
        return _DEFAULT_AGENT_TIMEOUT


def _planned_pace_gap(conn, scope_key: str) -> tuple[int | None, int]:
    """Compute (old_floor, new_floor) for a pace-down on ``scope_key`` (a per-host governor scope,
    'host:'||<host>). H4: the Doctor pace now writes its OWN ``doctor_min_gap_floor`` column (the
    lease takes GREATEST(min_gap_seconds, doctor_min_gap_floor)), so the watchdog breaker -- which
    owns min_gap_seconds -- can never clobber the Doctor's pace and vice-versa. We set the floor to
    base * _PACE_MULTIPLIER, monotone-by-construction (never below an existing floor). Read-only."""
    base = 90
    old_floor = None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT min_gap_seconds, base_min_gap_seconds, doctor_min_gap_floor "
            "FROM rate_governor WHERE scope_key=%s", (scope_key,))
        row = cur.fetchone()
    conn.rollback()
    if row:
        base = int(row.get("base_min_gap_seconds") or row.get("min_gap_seconds") or 90)
        if row.get("doctor_min_gap_floor") is not None:
            old_floor = int(row["doctor_min_gap_floor"])
    target = min(_PACE_GAP_CEILING, base * _PACE_MULTIPLIER)
    # Monotone: never narrow an existing floor; only widen (GREATEST).
    new_floor = max(target, old_floor or 0)
    return old_floor, new_floor


def _host_skip_qualifies(hb: int, attempts: int) -> bool:
    """H6: a host_skip needs a RATE over a denominator, not an absolute count. With a governor
    attempts denominator: >=K hard blocks AND >=N attempts AND hb/attempts >= ~0.3. With NO
    governor row (attempts==0) fall back to the absolute count (>=K hard blocks) so a host the
    governor has never seen is still protected on a strong hard-block burst."""
    if hb < _HOST_SKIP_MIN:
        return False
    if attempts <= 0:
        return True  # no denominator available -> absolute-count fallback
    if attempts < _HOST_SKIP_MIN_SAMPLES:
        return False
    return (hb / attempts) >= _HOST_SKIP_MIN_RATE


def _queued_approved_count(conn, host: str) -> int:
    """H19: count the host's queued+approved rows that the skip will gate (recorded on the audit
    row so a Reverse can report exactly how many vetted rows were affected). Read-only."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM apply_queue "
            "WHERE COALESCE(target_host, apply_domain)=%s AND status='queued' "
            "  AND approved_batch IS NOT NULL AND lane=%s",
            (host, LANE))
        n = int(cur.fetchone()["n"])
    conn.rollback()
    return n


def _lane_attempt_block_rate(conn) -> tuple[float, int]:
    """H5: the lane block rate computed over governor ATTEMPTS (success+captcha+block) across the
    apply HOST scopes -- NOT failures-only. Excludes the global / home_ip / board / account scopes.
    Returns (rate, total_attempts). Read-only."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(success_24h + captcha_24h + block_24h),0) AS total, "
            "       COALESCE(SUM(captcha_24h + block_24h),0) AS bad "
            "FROM rate_governor WHERE scope_key LIKE 'host:%'")
        row = cur.fetchone()
    conn.rollback()
    total = int(row["total"] or 0)
    bad = int(row["bad"] or 0)
    return (bad / total if total else 0.0), total


def _pause_debounce_ready(conn) -> bool:
    """H5: 2-pass debounce for a lane pause. The first qualifying pass ARMS (stamps
    doctor_pause_armed_at); only a subsequent pass at least _PAUSE_DEBOUNCE_SECONDS later FIRES.
    This is a stateful read-then-write: it stamps the arm time if unset and returns whether the
    debounce window has elapsed. Commits its own small write."""
    with conn.cursor() as cur:
        cur.execute("SELECT doctor_pause_armed_at FROM fleet_config WHERE id=1")
        row = cur.fetchone()
        armed = row.get("doctor_pause_armed_at") if row else None
        if armed is None:
            cur.execute("UPDATE fleet_config SET doctor_pause_armed_at=now(), updated_at=now() WHERE id=1")
            conn.commit()
            return False
        cur.execute(
            "SELECT (now() - %s) >= make_interval(secs => %s) AS ready",
            (armed, _PAUSE_DEBOUNCE_SECONDS))
        ready = bool(cur.fetchone()["ready"])
    conn.rollback()
    return ready


def _bump_systemic_streak(conn, systemic: bool) -> int:
    """A6: advance the consecutive-systemic-pass counter on a systemic pass, reset it on a
    non-systemic pass. Returns the NEW streak value (the post-update count). Commits its own
    small write. The N4 alert fires only when this reaches _SYSTEMIC_STREAK_ALERT."""
    with conn.cursor() as cur:
        if systemic:
            cur.execute("UPDATE fleet_config SET doctor_systemic_streak = "
                        "COALESCE(doctor_systemic_streak,0) + 1, updated_at=now() WHERE id=1 "
                        "RETURNING doctor_systemic_streak")
            row = cur.fetchone()
            streak = int(row["doctor_systemic_streak"]) if row else 1
        else:
            cur.execute("UPDATE fleet_config SET doctor_systemic_streak = 0, updated_at=now() WHERE id=1")
            streak = 0
    conn.commit()
    return streak


def _pause_disarm(conn) -> None:
    """Clear a pending pause-arm (the breach did not recur / was not broad enough). Idempotent."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE fleet_config SET doctor_pause_armed_at=NULL, updated_at=now() "
            "WHERE id=1 AND doctor_pause_armed_at IS NOT NULL")
    conn.commit()


# ---------------------------------------------------------------------------
# APPLY -- pass the gate, mutate via EXISTING mechanisms, write the audit row.
# Each auto-fix is IDEMPOTENT: it skips when an equivalent active knob already exists.
# ---------------------------------------------------------------------------
def _has_active_knob(conn, knob_type: str, scope_key: str | None) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM fleet_knobs WHERE active AND knob_type=%s "
            "AND scope_key IS NOT DISTINCT FROM %s AND (expires_at IS NULL OR expires_at > now()) LIMIT 1",
            (knob_type, scope_key),
        )
        found = cur.fetchone() is not None
    conn.rollback()
    return found


# H12: knob conservativeness ordering so idempotency suppresses only an EQUAL-OR-MILDER repeat,
# while allowing an ESCALATION (a stronger fix) to supersede an active milder one. Higher == more
# conservative (more restrictive of fleet activity). host_skip can supersede a pace on the host.
_KNOB_STRENGTH = {"pace_or_pause": 1, "timeout_bump": 1, "quarantine": 2, "host_skip": 3}


def _suppressed_by_active(conn, action: dict) -> bool:
    """H12 severity-aware idempotency. Suppress (skip) ONLY when a genuinely equal-or-stronger
    fix already covers THIS exact target -- never let an UNRELATED knob that merely shares a scope
    string (e.g. a timeout_bump and an ats-pause both on scope='ats') suppress each other, and
    always allow an ESCALATION through:

      * SAME (knob_type, scope) with an active knob -> suppress (don't double-write / re-pace the
        identical lever). A severity escalation to a STRONGER knob_type is handled below.
      * a PACE on a host is suppressed by an active HOST_SKIP on the same host (host_skip is the
        stronger fix -- no need to also pace). The reverse (host_skip when a pace is active) is NOT
        suppressed: host_skip supersedes the pace (escalation)."""
    knob = action["knob_type"]
    scope = action.get("scope_key")
    with conn.cursor() as cur:
        # Same lever already active on the same scope -> idempotent skip.
        cur.execute(
            "SELECT 1 FROM fleet_knobs WHERE active AND knob_type=%s AND scope_key IS NOT DISTINCT FROM %s "
            "AND (expires_at IS NULL OR expires_at > now()) LIMIT 1",
            (knob, scope))
        if cur.fetchone() is not None:
            conn.rollback()
            return True
        # A pace is redundant when a host_skip already covers the host (host_skip is stronger).
        # A5: host_skip + pace now share the CANONICAL host scope 'host:<h>', so this lookup uses
        # governor.host_scope(host) -- the bare-host vs 'host:'-prefixed split that silently defeated
        # this cross-knob suppression (and the H9 partial-unique index) is gone.
        if knob == "pace_or_pause" and action.get("op_kind") == "pace" and action.get("host"):
            cur.execute(
                "SELECT 1 FROM fleet_knobs WHERE active AND knob_type='host_skip' "
                "AND scope_key=%s AND (expires_at IS NULL OR expires_at > now()) LIMIT 1",
                (governor.host_scope(action["host"]),))
            if cur.fetchone() is not None:
                conn.rollback()
                return True
    conn.rollback()
    return False


def _has_open_diagnosis(conn, cluster_key: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM fleet_diagnoses WHERE cluster_key=%s "
            "AND status IN ('auto_applied','recommended','open') LIMIT 1",
            (cluster_key,),
        )
        found = cur.fetchone() is not None
    conn.rollback()
    return found


def _record_knob(cur, *, knob_type, scope_key, value_text, reason, ttl_seconds) -> bool:
    """Insert an active Doctor knob. H9: ON CONFLICT DO NOTHING against the partial-unique index
    (one active knob per knob_type+scope) makes a racing duplicate a no-op. Returns True if a row
    was actually inserted (False == a concurrent pass already holds the active knob)."""
    cur.execute(
        "INSERT INTO fleet_knobs (knob_type, scope_key, value_text, reason, created_by, active, expires_at) "
        "VALUES (%s,%s,%s,%s,'doctor',TRUE, now() + make_interval(secs => %s)) "
        "ON CONFLICT (knob_type, COALESCE(scope_key, '')) WHERE active DO NOTHING",
        (knob_type, scope_key, value_text, _scrub(reason), ttl_seconds),
    )
    return cur.rowcount > 0


def _execute_actuator(cur, actuator: str, **kw) -> None:
    """A10: the SINGLE dispatcher every Doctor mutation routes through. Each branch is one of the
    CLOSED _ALLOWED_ACTUATORS; there is NO branch that writes fleet_config.paused, a breaker
    column, account:linkedin, or halted_until -- and the static test (test_no_forbidden_mutation_
    outside_dispatcher) fails if any such mutation appears anywhere ELSE in this module. Centralizing
    the actuation here is what makes the allow-set gate's promise actually enforceable.

    kw per actuator:
      doctor_skip_until      -> scope_key, ttl_seconds   (set host skip lease)
      doctor_min_gap_floor   -> scope_key, new_floor      (raise the Doctor pace floor, monotone)
      ats_paused             -> (no kw)                   (ATS-only lane pause via pgqueue, source=doctor)
      agent_timeout_override -> new_timeout               (raise the apply-agent timeout)
    """
    if actuator == "doctor_skip_until":
        cur.execute("INSERT INTO rate_governor (scope_key) VALUES (%s) ON CONFLICT (scope_key) DO NOTHING",
                    (kw["scope_key"],))
        cur.execute("UPDATE rate_governor SET doctor_skip_until = now() + make_interval(secs => %s), "
                    "updated_at=now() WHERE scope_key=%s", (kw["ttl_seconds"], kw["scope_key"]))
    elif actuator == "doctor_min_gap_floor":
        cur.execute("INSERT INTO rate_governor (scope_key) VALUES (%s) ON CONFLICT (scope_key) DO NOTHING",
                    (kw["scope_key"],))
        cur.execute(
            "UPDATE rate_governor SET doctor_min_gap_floor = GREATEST(COALESCE(doctor_min_gap_floor,0), %s), "
            "updated_at = now() WHERE scope_key=%s", (kw["new_floor"], kw["scope_key"]))
    elif actuator == "agent_timeout_override":
        cur.execute("UPDATE fleet_config SET agent_timeout_override=%s, updated_at=now() WHERE id=1",
                    (kw["new_timeout"],))
    else:
        raise ConservativeViolation(f"_execute_actuator: unknown/forbidden actuator {actuator!r}")


def _record_diagnosis(cur, action, *, status, auto_action, how_to_reverse, ttl_seconds):
    cur.execute(
        "INSERT INTO fleet_diagnoses (cluster_key, reason, host, machine, lane, sample_count, "
        "severity, diagnosis, recommendation, auto_action, how_to_reverse, status, "
        "rows_affected, prior_incident_count, distinct_hosts, distinct_workers, "
        "expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, "
        "now() + make_interval(secs => %s))",
        (action.get("cluster_key"), action.get("reason"), action.get("host"),
         action.get("machine"), action.get("lane"), action.get("sample_count"),
         action.get("severity"), _scrub(action.get("diagnosis")), _scrub(action.get("recommendation")),
         auto_action, how_to_reverse, status,
         action.get("rows_affected"), action.get("prior_incident_count"),
         action.get("distinct_hosts"), action.get("distinct_workers"), ttl_seconds),
    )


def apply_auto(conn, action: dict) -> dict:
    """Apply ONE auto action: pass the D1 gate, perform the (conservative) mutation through
    an EXISTING mechanism, record the knob + the fleet_diagnoses audit row. Idempotent: a
    no-op (skipped) result if an equivalent active knob / open diagnosis already exists.

    Returns {"applied": bool, "knob_type": str, "scope_key": str, "skipped": bool, "detail": str}.
    Raises ConservativeViolation (via the gate) on a forbidden action -- the caller must NOT
    swallow that for a real action; it is the safety boundary.
    """
    _assert_conservative(action)  # D1: never mutate without passing the gate.

    knob = action["knob_type"]
    scope = action.get("scope_key")
    cluster_key = action.get("cluster_key")

    # Idempotency (D4 + H12): skip ONLY if an EQUAL-OR-STRONGER active knob already covers this
    # scope (so an escalation -- e.g. host_skip superseding a pace, or a severe re-diagnosis -- is
    # NOT swallowed), OR the exact cluster_key already has an open diagnosis (dedup the audit row).
    if _suppressed_by_active(conn, action) or (cluster_key and _has_open_diagnosis(conn, cluster_key)):
        return {"applied": False, "skipped": True, "knob_type": knob, "scope_key": scope,
                "detail": "equal-or-stronger active knob / open diagnosis already present"}

    ttl = _KNOB_TTL[knob]

    if knob == "host_skip":
        return _apply_host_skip(conn, action, ttl)
    if knob == "timeout_bump":
        return _apply_timeout_bump(conn, action, ttl)
    if knob == "quarantine":
        return _apply_quarantine(conn, action, ttl)
    if knob == "pace_or_pause":
        return _apply_pace_or_pause(conn, action, ttl)
    # Unreachable: the gate already constrained knob to AUTO_KNOBS.
    raise ConservativeViolation(f"unhandled knob_type {knob!r}")


def _apply_host_skip(conn, action, ttl) -> dict:
    """H6: host_skip is now a SELF-EXPIRING LEASE FILTER, not an irreversible un-approve. We set
    rate_governor.doctor_skip_until on the host scope (the lease already LEFT JOINs that scope and
    gates on `doctor_skip_until < now()`), so the host's queued+approved rows simply stop being
    leasable until the TTL passes -- the vetted approved_batch token is PRESERVED, the skip
    auto-reverts at TTL exactly like the breaker, and Reverse just clears the column. No
    operator-irreversible state is destroyed (was: approved_batch=NULL, H19)."""
    # A5: the host_skip action carries the BARE host in scope_key; canonicalize to the governor
    # host scope 'host:<h>' for BOTH the rate_governor row AND the knob, so host_skip and pace share
    # one scope_key convention (the cross-knob suppression + H9 index now see across the pair).
    host = action["scope_key"]
    scope_key = governor.host_scope(host)
    rows_affected = action.get("rows_affected")
    if rows_affected is None:
        rows_affected = _queued_approved_count(conn, host)
    with conn.cursor() as cur:
        # H9 race-proof: record the knob FIRST; if a concurrent pass already holds the active knob
        # the INSERT is a no-op and we skip the mutation (don't extend an existing skip blindly).
        inserted = _record_knob(cur, knob_type="host_skip", scope_key=scope_key,
                                 value_text="skip", reason=action.get("reason"), ttl_seconds=ttl)
        if not inserted:
            conn.rollback()
            return {"applied": False, "skipped": True, "knob_type": "host_skip", "scope_key": scope_key,
                    "detail": "host_skip already active (concurrent pass)"}
        _execute_actuator(cur, "doctor_skip_until", scope_key=scope_key, ttl_seconds=ttl)  # A10
        how = ("Console 'Reverse' clears doctor_skip_until (the host leases again immediately). "
               "Approved rows were NEVER un-approved -- vetted approval is preserved; the skip also "
               "auto-reverts at its TTL.")
        _record_diagnosis(cur, action, status="auto_applied", auto_action="host_skip",
                          how_to_reverse=how, ttl_seconds=ttl)
    conn.commit()
    return {"applied": True, "skipped": False, "knob_type": "host_skip", "scope_key": scope_key,
            "detail": f"host_skip filter set ({rows_affected} approved row(s) gated, NOT un-approved); 24h TTL"}


def _apply_timeout_bump(conn, action, ttl) -> dict:
    """Raise fleet_config.agent_timeout_override to the clamped new value [default..900].
    The apply worker prefers this over the env. Conservative: a longer timeout only lets a
    slow page finish instead of being killed + retried (which would re-hit the host)."""
    new_timeout = action["new_timeout"]
    cur_default = action.get("current_default", _DEFAULT_AGENT_TIMEOUT)
    # Defensive clamp (the gate already enforced new <= ceiling and new > current). The ceiling is
    # strictly below the watchdog kill (H7 invariant) so a bumped agent is never killed at job_max.
    new_timeout = max(cur_default + 1, min(_TIMEOUT_CEILING, int(new_timeout)))
    # A7 + A8: record the knob FIRST and gate the override UPDATE on inserted==True (mirror
    # _apply_host_skip). timeout_bump is fleet-wide (scope='ats', ONE active row via the H9 unique
    # index): a second slow host in the same pass hits ON CONFLICT DO NOTHING (inserted=False) -- it
    # must NOT raise the override a second time with no governing knob (invisible to Reverse + the
    # sweep). A8: SELECT fleet_config FOR UPDATE serializes count+NULL (sweep) against insert+set
    # (this apply) so the override can't be NULLed under a concurrent fresh bump.
    with conn.cursor() as cur:
        cur.execute("SELECT agent_timeout_override FROM fleet_config WHERE id=1 FOR UPDATE")
        prev = cur.fetchone().get("agent_timeout_override")
        prev_txt = "" if prev is None else str(int(prev))
        inserted = _record_knob(cur, knob_type="timeout_bump", scope_key=LANE,
                                value_text=f"{new_timeout}|{prev_txt}", reason=action.get("reason"),
                                ttl_seconds=ttl)
        if not inserted:
            conn.rollback()
            return {"applied": False, "skipped": True, "knob_type": "timeout_bump", "scope_key": LANE,
                    "detail": "timeout_bump already active (concurrent pass); override left unchanged"}
        _execute_actuator(cur, "agent_timeout_override", new_timeout=new_timeout)  # A10
        how = ("Console 'Reverse' on the timeout_bump knob restores agent_timeout_override to its "
               "pre-bump value (NULL/env-default if none was set).")
        _record_diagnosis(cur, action, status="auto_applied", auto_action="timeout_bump",
                          how_to_reverse=how, ttl_seconds=ttl)
    conn.commit()
    return {"applied": True, "skipped": False, "knob_type": "timeout_bump", "scope_key": LANE,
            "detail": f"agent_timeout_override -> {new_timeout}s (was default {cur_default}s)"}


def _apply_quarantine(conn, action, ttl) -> dict:
    """Quarantine a single poison url via the EXISTING heartbeat.quarantine_job manual
    one-shot (pulls it from the pool WITHOUT polluting crash_count; never deletes). Records
    a quarantine knob for the audit/reverse surface.

    H16: if the url is ALREADY quarantined (e.g. the watchdog stamped it first), the Doctor
    skips the stamp and records an AUDIT-ONLY knob -- it never re-stamps or pollutes the signal."""
    url = action["scope_key"]
    already = heartbeat.is_quarantined(conn, url)
    newly = False
    if not already:
        newly = heartbeat.quarantine_job(conn, url, worker="doctor", reason="poison_pattern",
                                         manual=True, commit=False)
    with conn.cursor() as cur:
        _record_knob(cur, knob_type="quarantine", scope_key=url,
                     value_text="quarantined", reason=action.get("reason"), ttl_seconds=ttl)
        how = ("Console 'Reverse' clears poison_jobs.quarantined_at for this url (manual: prefix "
               "only) + deactivates the knob. The job is retained -- never deleted.")
        _record_diagnosis(cur, action, status="auto_applied", auto_action="quarantine",
                          how_to_reverse=how, ttl_seconds=ttl)
    conn.commit()
    detail = ("audit-only (already quarantined)" if already
              else f"quarantined (newly={newly}) via manual poison one-shot")
    return {"applied": True, "skipped": False, "knob_type": "quarantine", "scope_key": url,
            "detail": detail}


def _apply_pace_or_pause(conn, action, ttl) -> dict:
    """Pace down (raise the Doctor's OWN min-gap floor) OR, if severe, PAUSE the APPLY lane via
    the ATS-only ats_paused flag. Both are conservative; RESUMING / restoring the gap is
    human-or-TTL only (never an activity-increasing auto action).

    H1: the pause writes pgqueue.set_ats_paused (ATS-only, source='doctor'), NEVER set_paused --
    so a Doctor pause can never halt the LinkedIn lane. H4: the pace writes doctor_min_gap_floor
    (Doctor-owned), so the watchdog breaker can never clobber it and vice-versa."""
    from applypilot.apply import pgqueue
    sub = action["op_kind"]
    if sub == "pause":
        # H1: ATS-ONLY pause. The LinkedIn lane reads linkedin_should_halt() (shared kill switch) only and
        # never ats_paused, so this can NOT stop LinkedIn. source='doctor' records provenance so
        # the Doctor auto-reverts only its OWN pause and the console can label it.
        pgqueue.set_ats_paused(conn, True, source="doctor")
        with conn.cursor() as cur:
            _record_knob(cur, knob_type="pace_or_pause", scope_key=LANE,
                         value_text="paused", reason=action.get("reason"), ttl_seconds=ttl)
            how = ("Console Resume (human-only) clears ats_paused. The Doctor auto-reverts its OWN "
                   "ATS pause at the knob TTL (sweep) but never touches an operator/cost pause.")
            _record_diagnosis(cur, action, status="auto_applied", auto_action="pace_or_pause:pause",
                              how_to_reverse=how, ttl_seconds=ttl)
        conn.commit()
        return {"applied": True, "skipped": False, "knob_type": "pace_or_pause", "scope_key": LANE,
                "detail": "APPLY lane paused via ats_paused (LinkedIn UNAFFECTED; resume human/TTL)"}

    # pace: raise the Doctor-owned min-gap FLOOR on the per-host governor scope (H4). The lease
    # takes GREATEST(min_gap_seconds, doctor_min_gap_floor), so this is monotone-by-construction
    # and the watchdog breaker's min_gap restore can never wipe it. scope_key='host:'||<host>.
    scope_key = action["scope_key"]
    new_floor = action["new_gap"]
    with conn.cursor() as cur:
        _execute_actuator(cur, "doctor_min_gap_floor", scope_key=scope_key, new_floor=new_floor)  # A10
        _record_knob(cur, knob_type="pace_or_pause", scope_key=scope_key,
                     value_text=str(new_floor), reason=action.get("reason"), ttl_seconds=ttl)
        how = ("Console 'Reverse' clears doctor_min_gap_floor (the host runs at its breaker-owned "
               "gap again). The floor also auto-clears at the knob TTL via the sweep.")
        _record_diagnosis(cur, action, status="auto_applied", auto_action="pace_or_pause:pace",
                          how_to_reverse=how, ttl_seconds=ttl)
    conn.commit()
    return {"applied": True, "skipped": False, "knob_type": "pace_or_pause", "scope_key": scope_key,
            "detail": f"{scope_key} doctor_min_gap_floor raised to {new_floor}s (pace down)"}


def _record_recommendation(conn, action: dict) -> bool:
    """Record a human RECOMMENDATION row (status='recommended'). Never mutates apply state.
    Idempotent on cluster_key. Returns True if a new row was written."""
    cluster_key = action.get("cluster_key")
    if cluster_key and _has_open_diagnosis(conn, cluster_key):
        return False
    ttl = 24 * 3600
    with conn.cursor() as cur:
        _record_diagnosis(cur, action, status="recommended", auto_action=None,
                          how_to_reverse="Dismiss via the console (bookkeeping only).", ttl_seconds=ttl)
    conn.commit()
    return True


def parsing_drift_actions(brain_path: str | None = None) -> list[dict]:
    """Read the latest parse-quality drift rows from the brain and return recommendations."""
    try:
        from applypilot import config
    except Exception:
        return []

    path = Path(brain_path or config.DB_PATH).expanduser()
    if not path.exists():
        return []

    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT board, total, null_rate, short_rate, html_rate
                FROM desc_quality_drift d
                WHERE d.board <> '__all__'
                  AND d.snapshot_at = (
                      SELECT MAX(d2.snapshot_at)
                        FROM desc_quality_drift d2
                       WHERE d2.board = d.board
                  )
                """
            ).fetchall()
    except Exception:
        return []

    actions: list[dict] = []
    for row in rows:
        total = int(row["total"] or 0)
        if total <= 0:
            continue

        board = str(row["board"] or "unknown")
        for metric, rate, threshold, severity in (
            ("null", row["null_rate"], 0.02, "warn"),
            ("short", row["short_rate"], 0.05, "warn"),
            ("html", row["html_rate"], 0.01, "warn"),
        ):
            if rate is None:
                continue
            if float(rate) <= threshold:
                continue
            actions.append({
                "mode": "recommend",
                "reason": "parsing_drift",
                "host": board,
                "machine": None,
                "lane": None,
                "sample_count": total,
                "severity": severity,
                "diagnosis": (
                    f"Board '{board}' has elevated parse drift: {metric.replace('_', ' ')} "
                    f"rate={float(rate):.2%}."
                ),
                "recommendation": (
                    f"Review parsing for board '{board}' before continuing large-at-scale ATS pushes."
                ),
                "cluster_key": f"parsing_drift|{board}|{metric}",
            })

    return actions


# ---------------------------------------------------------------------------
# SWEEP -- TTL: deactivate expired knobs + expire stale diagnoses each run.
# ---------------------------------------------------------------------------
def sweep_expired(conn) -> dict:
    """N3/H3: the TTL sweep now MECHANICALLY UNDOES each expired knob's effect in ONE transaction
    (not just active=FALSE), so an auto-fix can never outlive its TTL. Per type:

      * pace_or_pause (pace, scope='host:'||<h>) -> clear doctor_min_gap_floor (narrowing the gap
        back is activity-INCREASING but the cause has aged out; the Doctor owns this column so
        clearing it can't fight the watchdog -- FOR UPDATE locks the governor scope to avoid the
        watchdog-restore race).
      * pace_or_pause (pause, scope='ats') -> if the Doctor still owns the ATS pause
        (ats_pause_source='doctor') clear ats_paused (auto-revert its OWN pause, H8); if an
        operator/cost pause supervened (source<>'doctor') LEAVE it and record an audit note.
      * host_skip (scope=<host>) -> clear doctor_skip_until on the host scope (auto-revert the
        leasable filter, H6) AND write a 'host_skip expired -- re-approve N rows?' recommend row
        (we never auto re-approve; approval was preserved so this is informational).
      * timeout_bump (scope='ats') -> restore agent_timeout_override to the knob's captured pre-bump
        value ONLY if no OTHER active timeout_bump remains (single shared column).
      * quarantine -> leave poison_jobs.quarantined_at (genuinely permanent; see H15 surface).

    Reversibility is REAL for the auto path here, not merely active=FALSE."""
    reversed_effects: dict[str, int] = {}
    recommends: list[dict] = []
    with conn.cursor() as cur:
        # Lock + fetch the expired knobs (FOR UPDATE serializes against a concurrent revert/sweep;
        # the governor scope is re-locked per row below to avoid the watchdog-restore race).
        cur.execute(
            "SELECT id, knob_type, scope_key, value_text FROM fleet_knobs "
            "WHERE active AND expires_at IS NOT NULL AND expires_at <= now() FOR UPDATE")
        expired = [dict(r) for r in cur.fetchall()]

        # A1: pre-lock the target rate_governor scopes in ONE canonical order (ORDER BY scope_key)
        # BEFORE the per-knob undo loop. evaluate_breakers selects/UPDATEs rate_governor in the SAME
        # ORDER BY scope_key order, so the sweep and the watchdog breaker can never take these row
        # locks in opposite orders -> no AB-BA deadlock (SQLSTATE 40P01) under a correlated outage.
        gov_scopes = sorted({
            k["scope_key"] for k in expired
            if k["knob_type"] == "pace_or_pause" and (k["scope_key"] or "").startswith("host:")
        } | {
            k["scope_key"] for k in expired
            if k["knob_type"] == "host_skip" and k["scope_key"]
        })
        if gov_scopes:
            cur.execute(
                "SELECT scope_key FROM rate_governor WHERE scope_key = ANY(%s) ORDER BY scope_key FOR UPDATE",
                (gov_scopes,))
            cur.fetchall()

        for k in expired:
            ktype, scope, vtext = k["knob_type"], k["scope_key"], k.get("value_text")
            if ktype == "pace_or_pause" and scope and scope.startswith("host:"):
                cur.execute("SELECT 1 FROM rate_governor WHERE scope_key=%s FOR UPDATE", (scope,))
                # A12 last-writer guard: only clear doctor_min_gap_floor if no OTHER active pace knob
                # still covers this scope (mirrors the timeout_bump undo's single-shared-column
                # discipline). Otherwise a second still-active pace on the same host would be silently
                # un-paced when the FIRST one expires.
                cur.execute(
                    "SELECT 1 FROM fleet_knobs WHERE active AND knob_type='pace_or_pause' "
                    "AND scope_key=%s AND id<>%s AND (expires_at IS NULL OR expires_at > now()) LIMIT 1",
                    (scope, k["id"]))
                if cur.fetchone() is None:
                    cur.execute(
                        "UPDATE rate_governor SET doctor_min_gap_floor=NULL, updated_at=now() WHERE scope_key=%s",
                        (scope,))
                    reversed_effects["pace_floor_cleared"] = reversed_effects.get("pace_floor_cleared", 0) + 1
                else:
                    reversed_effects["pace_floor_kept_other_active"] = \
                        reversed_effects.get("pace_floor_kept_other_active", 0) + 1
            elif ktype == "pace_or_pause" and scope == LANE:
                # H8: auto-revert ONLY the Doctor's own ATS pause; never an operator/cost pause.
                cur.execute("SELECT ats_paused, ats_pause_source FROM fleet_config WHERE id=1 FOR UPDATE")
                cfg = cur.fetchone()
                if cfg and cfg.get("ats_paused") and (cfg.get("ats_pause_source") == "doctor"):
                    cur.execute(
                        "UPDATE fleet_config SET ats_paused=FALSE, ats_pause_source=NULL, "
                        "doctor_pause_armed_at=NULL, updated_at=now() WHERE id=1")
                    reversed_effects["ats_pause_cleared"] = reversed_effects.get("ats_pause_cleared", 0) + 1
                else:
                    reversed_effects["pause_left_operator"] = reversed_effects.get("pause_left_operator", 0) + 1
            elif ktype == "host_skip" and scope:
                # A5/A9: the host_skip knob scope_key is ALREADY the canonical governor host scope
                # ('host:<h>'), so the sweep matches the governor row directly -- no bare-vs-prefixed
                # re-derivation that would miss the row and leave doctor_skip_until set past TTL.
                host_scope = scope
                bare_host = scope[len("host:"):] if scope.startswith("host:") else scope
                cur.execute("SELECT 1 FROM rate_governor WHERE scope_key=%s FOR UPDATE", (host_scope,))
                cur.execute(
                    "UPDATE rate_governor SET doctor_skip_until=NULL, updated_at=now() WHERE scope_key=%s",
                    (host_scope,))
                reversed_effects["host_skip_cleared"] = reversed_effects.get("host_skip_cleared", 0) + 1
                recommends.append({
                    "cluster_key": _cluster_key("host_skip_expired", bare_host, None, LANE),
                    "reason": _HARD_BLOCK, "host": bare_host, "lane": LANE, "severity": "info",
                    "diagnosis": f"host_skip on {bare_host} expired (TTL). The block presumably cleared.",
                    "recommendation": ("The host leases again automatically (approval was preserved). "
                                       "Nothing to re-approve; review if it re-blocks."),
                })
            elif ktype == "timeout_bump":
                # A8: lock the fleet_config row FIRST so this count+NULL is mutually exclusive with a
                # concurrent _apply_timeout_bump's insert+set (which also takes FOR UPDATE). Without
                # this the sweep could see count==0, then a fresh bump inserts K2 + sets override,
                # then the sweep NULLs it -> a live in-TTL knob with a NULL override (job_over_max poison).
                cur.execute("SELECT 1 FROM fleet_config WHERE id=1 FOR UPDATE")
                # Restore the pre-bump override only if NO other active timeout_bump remains.
                cur.execute(
                    "SELECT count(*) AS n FROM fleet_knobs WHERE active AND knob_type='timeout_bump' AND id<>%s",
                    (k["id"],))
                if int(cur.fetchone()["n"]) == 0:
                    prev = None
                    if vtext and "|" in str(vtext):
                        tail = str(vtext).split("|", 1)[1].strip()
                        prev = int(tail) if tail.isdigit() else None
                    cur.execute("UPDATE fleet_config SET agent_timeout_override=%s, updated_at=now() WHERE id=1",
                                (prev,))
                    reversed_effects["timeout_restored"] = reversed_effects.get("timeout_restored", 0) + 1
            # quarantine: intentionally permanent (no auto un-quarantine); H15 surfaces it.

            cur.execute("UPDATE fleet_knobs SET active=FALSE WHERE id=%s", (k["id"],))

        cur.execute(
            "UPDATE fleet_diagnoses SET status='expired', updated_at=now() "
            "WHERE status IN ('auto_applied','open','recommended') "
            "AND expires_at IS NOT NULL AND expires_at <= now() "
            "RETURNING id")
        diags = len(cur.fetchall())

        # Write the 'host_skip expired' recommend rows (idempotent on cluster_key).
        rec_written = 0
        for rec in recommends:
            cur.execute(
                "SELECT 1 FROM fleet_diagnoses WHERE cluster_key=%s "
                "AND status IN ('auto_applied','recommended','open') "
                "AND (expires_at IS NULL OR expires_at > now()) LIMIT 1", (rec["cluster_key"],))
            if cur.fetchone() is None:
                _record_diagnosis(cur, rec, status="recommended", auto_action=None,
                                  how_to_reverse="Dismiss via the console (bookkeeping only).",
                                  ttl_seconds=24 * 3600)
                rec_written += 1
    conn.commit()
    return {"knobs_expired": len(expired), "diagnoses_expired": diags,
            "reversed": reversed_effects, "recommendations_written": rec_written}


# ---------------------------------------------------------------------------
# LLM hook (v1: inert). A future explain pass MAY enrich diagnosis text. It is given the
# already-templated, already-SCRUBBED action and MUST be non-mutating + best-effort.
# ---------------------------------------------------------------------------
def _llm_explain_hook(action: dict) -> str | None:  # pragma: no cover - reserved seam
    """RESERVED for a future LLM explain pass. v1 returns None (rule templates only).
    Any future implementation MUST: (1) never mutate DB/fleet state, (2) operate only on
    already-scrubbed text, (3) be best-effort (a failure here never blocks an auto-fix)."""
    return None


# ---------------------------------------------------------------------------
# H9 SINGLETON GUARD + H2 DAILY BUDGET helpers.
# ---------------------------------------------------------------------------
_DOCTOR_LOCK_KEY = "applypilot:fleet_doctor"   # byte-identical with the test's probe
_DOCTOR_WORKER_ID = "fleet_doctor"
# A11: bound how long any single Doctor query / idle transaction may hold the session (and thus the
# advisory lock). Without this a query wedged on a FOR UPDATE wait or a half-open socket holds the
# lock for minutes-to-hours on Windows defaults, so every later pass returns skipped_singleton and
# the TTL sweep never runs -- the Doctor is dead while reporting healthy.
_STATEMENT_TIMEOUT_MS = 120_000
_IDLE_IN_TX_TIMEOUT_MS = 120_000


def _prepare_pass_connection(conn) -> None:
    """A11: set statement_timeout + idle_in_transaction_session_timeout on the Doctor pass
    connection so a wedged query/transaction aborts and RELEASES the advisory lock deterministically
    instead of starving every later pass. Best-effort (a transient set failure never blocks a pass)."""
    try:
        # SET does not accept a bound parameter; the values are our own int constants (safe literals).
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {int(_STATEMENT_TIMEOUT_MS)}")
            cur.execute(f"SET idle_in_transaction_session_timeout = {int(_IDLE_IN_TX_TIMEOUT_MS)}")
        conn.commit()
    except Exception:  # pragma: no cover - defensive
        logger.exception("failed to set Doctor pass connection timeouts (continuing)")
        try:
            conn.rollback()
        except Exception:
            pass


def _try_singleton_lock(conn) -> bool:
    """H9: pg_try_advisory_lock(hashtext('applypilot:fleet_doctor')). Returns True if this pass
    acquired the SESSION-level lock (mirrors linkedin_worker_main:44). A second concurrent
    run_doctor gets False and no-ops -- preventing interleaved double-writes / compounded bumps.
    The lock is released in run_doctor's finally (or on connection close)."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", (_DOCTOR_LOCK_KEY,))
        ok = bool(cur.fetchone()["ok"])
    conn.commit()
    return ok


def _release_singleton_lock(conn) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_DOCTOR_LOCK_KEY,))
        conn.commit()
    except Exception:  # pragma: no cover - best-effort
        pass


def _roll_and_read_budget(conn) -> dict:
    """H2: roll the per-day blast-radius counters at the UTC day boundary, then return the
    remaining budget {host_skips, pace}. Commits the roll. A mismatched/NULL anchor resets."""
    today = _dt.datetime.now(_dt.timezone.utc).date()
    with conn.cursor() as cur:
        cur.execute("SELECT doctor_budget_day, doctor_host_skips_today, doctor_pace_actions_today "
                    "FROM fleet_config WHERE id=1 FOR UPDATE")
        row = cur.fetchone() or {}
        if row.get("doctor_budget_day") != today:
            cur.execute(
                "UPDATE fleet_config SET doctor_budget_day=%s, doctor_host_skips_today=0, "
                "doctor_pace_actions_today=0, updated_at=now() WHERE id=1", (today,))
            used_skips, used_pace = 0, 0
        else:
            used_skips = int(row.get("doctor_host_skips_today") or 0)
            used_pace = int(row.get("doctor_pace_actions_today") or 0)
    conn.commit()
    return {"host_skips": max(0, _MAX_HOST_SKIPS_PER_DAY - used_skips),
            "pace": max(0, _MAX_PACE_PER_DAY - used_pace)}


def _charge_budget(conn, *, host_skips=0, pace=0) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE fleet_config SET doctor_host_skips_today = doctor_host_skips_today + %s, "
            "doctor_pace_actions_today = doctor_pace_actions_today + %s, updated_at=now() WHERE id=1",
            (host_skips, pace))
    conn.commit()


def _mark_pass(conn) -> None:
    """H18: stamp doctor_last_pass_at so the console's fast /api/status poll can show a Doctor
    card + 'last pass Ns ago' badge without folding the heavy diagnostics blob into the poll."""
    with conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET doctor_last_pass_at=now(), updated_at=now() WHERE id=1")
    conn.commit()


# ---------------------------------------------------------------------------
# RUN -- one full pass: analyze -> decide -> apply autos -> record recs -> sweep.
# ---------------------------------------------------------------------------
def run_doctor(conn, *, window_minutes: int = 60) -> dict:
    """One Doctor pass. Reads failures, decides actions, applies the conservative autos
    (each through the D1 gate), records the human recommendations, and runs the TTL sweep.
    Returns a structured summary. Never raises on a single bad action -- a per-action error
    is logged and skipped so one defect can't wedge the pass.

    H9: held under a pg advisory-lock singleton -- a second concurrent pass no-ops. H2: per-DAY
    blast-radius budget caps host_skip/pace; the overflow is downgraded to recommend-only."""
    _prepare_pass_connection(conn)  # A11: deterministic statement / idle-in-tx timeouts
    if not _try_singleton_lock(conn):
        logger.info("another Fleet Doctor pass holds the advisory lock; skipping this pass")
        return {"window_minutes": window_minutes, "skipped_singleton": True,
                "clusters": 0, "auto_applied": [], "auto_skipped": [], "recommendations": 0,
                "errors": 0, "sweep": {}}
    try:
        # A11: beat the Doctor's own liveness (worker_heartbeat) so a locked-out / wedged Doctor is
        # visible the way the watchdog beats itself, instead of silently dark.
        try:
            heartbeat.beat(conn, _DOCTOR_WORKER_ID, role="doctor", state="busy")
        except Exception:  # pragma: no cover - liveness is best-effort, never blocks a pass
            logger.exception("fleet_doctor heartbeat failed (continuing)")
        clusters = analyze(conn, window_minutes=window_minutes)
        planned = decide(conn, clusters)
        budget = _roll_and_read_budget(conn)

        summary = {
            "window_minutes": window_minutes,
            "clusters": len(clusters["cluster_rows"]),
            "lane_hard_block_rate": round(clusters["lane_hard_block_rate"], 4),
            "auto_applied": [],
            "auto_skipped": [],
            "recommendations": 0,
            "errors": 0,
            "budget_downgraded": 0,
        }
        spent = {"host_skips": 0, "pace": 0}

        for action in planned:
            try:
                if action.get("mode") == "auto":
                    # H2 daily budget: host_skip / pace each respect a per-day cap; on overflow
                    # downgrade the SAME action to a recommendation instead of applying it.
                    kind = ("host_skips" if action["knob_type"] == "host_skip"
                            else "pace" if (action["knob_type"] == "pace_or_pause"
                                            and action.get("op_kind") == "pace") else None)
                    if kind is not None and spent[kind] >= budget[kind]:
                        rec = dict(action, mode="recommend", knob_type=None,
                                   scope_key=action.get("host") or action.get("scope_key"))
                        rec["recommendation"] = ((action.get("recommendation") or "")
                                                 + " (deferred: daily Doctor budget reached.)")
                        if _record_recommendation(conn, rec):
                            summary["recommendations"] += 1
                            summary["budget_downgraded"] += 1
                        continue
                    res = apply_auto(conn, action)
                    if res.get("applied"):
                        if kind is not None:
                            spent[kind] += 1
                        summary["auto_applied"].append(
                            {"knob_type": res["knob_type"], "scope_key": res["scope_key"],
                             "detail": res["detail"]})
                    else:
                        summary["auto_skipped"].append(
                            {"knob_type": res["knob_type"], "scope_key": res["scope_key"]})
                else:
                    if _record_recommendation(conn, action):
                        summary["recommendations"] += 1
            except ConservativeViolation:
                # A planned auto that fails the gate is a BUG in decide(); log loudly, never apply.
                logger.exception("conservative gate REJECTED a planned auto action; skipping")
                summary["errors"] += 1
                try:
                    conn.rollback()
                except Exception:
                    pass
            except Exception:  # pragma: no cover - logged, never fatal to the pass
                logger.exception("doctor action failed; continuing")
                summary["errors"] += 1
                try:
                    conn.rollback()
                except Exception:
                    pass

        for action in parsing_drift_actions():
            try:
                if _record_recommendation(conn, action):
                    summary["recommendations"] += 1
            except Exception:  # pragma: no cover - logged, never fatal to the pass
                logger.exception("parsing-drift recommendation failed; continuing")
                summary["errors"] += 1
                try:
                    conn.rollback()
                except Exception:
                    pass

        if spent["host_skips"] or spent["pace"]:
            _charge_budget(conn, host_skips=spent["host_skips"], pace=spent["pace"])
        summary["sweep"] = sweep_expired(conn)
        _mark_pass(conn)
        return summary
    finally:
        _release_singleton_lock(conn)


# ---------------------------------------------------------------------------
# Entry point (loop). --once for a single pass; otherwise every --interval seconds.
# ---------------------------------------------------------------------------
def run_loop(conn_factory, *, interval: int = 300, window_minutes: int = 60,
             stop=None, max_passes=None) -> int:
    """Drive run_doctor on a cadence. Fresh connection per pass (a transient DB blip can't
    wedge the loop); a per-pass exception is swallowed. Returns the number of passes run."""
    passes = 0
    while True:
        if stop is not None and stop():
            break
        if max_passes is not None and passes >= max_passes:
            break
        try:
            with conn_factory() as conn:
                summary = run_doctor(conn, window_minutes=window_minutes)
            print(_format_summary(summary), flush=True)
        except Exception:  # pragma: no cover - logged, never fatal
            logger.exception("doctor pass failed; continuing")
        passes += 1
        if interval:
            time.sleep(interval)
    return passes


def _format_summary(summary: dict) -> str:
    """Compact, structured one-line-ish stdout summary (no secrets)."""
    autos = summary.get("auto_applied", [])
    return (
        "[fleet-doctor] window=%dm clusters=%d hb_rate=%.2f "
        "auto_applied=%d auto_skipped=%d recommendations=%d errors=%d "
        "sweep(knobs=%d,diag=%d)%s"
        % (
            summary.get("window_minutes", 0), summary.get("clusters", 0),
            summary.get("lane_hard_block_rate", 0.0), len(autos),
            len(summary.get("auto_skipped", [])), summary.get("recommendations", 0),
            summary.get("errors", 0),
            summary.get("sweep", {}).get("knobs_expired", 0),
            summary.get("sweep", {}).get("diagnoses_expired", 0),
            (" | " + "; ".join(f"{a['knob_type']}:{a['scope_key']}" for a in autos)) if autos else "",
        )
    )


def main(argv=None) -> int:  # pragma: no cover - long-running / thin arg glue
    p = argparse.ArgumentParser(
        prog="applypilot-fleet-doctor",
        description="Bounded, reversible, conservative fleet auto-remediation (FLEET DOCTOR v1).")
    p.add_argument("--dsn", default=os.environ.get("FLEET_PG_DSN"))
    p.add_argument("--interval", type=int, default=300, help="seconds between passes")
    p.add_argument("--window-minutes", type=int, default=60, help="rolling failure window")
    p.add_argument("--once", action="store_true", help="run a single pass and exit")
    args = p.parse_args(argv)
    if not args.dsn:
        raise SystemExit("set --dsn or FLEET_PG_DSN")
    from applypilot.apply import pgqueue
    from applypilot.fleet import schema as fleet_schema
    with pgqueue.connect(args.dsn) as schema_conn:
        fleet_schema.ensure_schema_v3(schema_conn)
    if args.once:
        with pgqueue.connect(args.dsn) as conn:
            summary = run_doctor(conn, window_minutes=args.window_minutes)
        print(_format_summary(summary), flush=True)
        # A11: a --once that found the lock CONTENDED did NOT actually run a pass; exit non-zero +
        # loud so a manual force-run can't masquerade as "looked, found nothing."
        if summary.get("skipped_singleton"):
            print("[fleet-doctor] ANOTHER PASS HOLDS THE ADVISORY LOCK; this --once did NOT run.",
                  flush=True)
            return 3
        return 0
    run_loop(lambda: pgqueue.connect(args.dsn), interval=args.interval,
             window_minutes=args.window_minutes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
