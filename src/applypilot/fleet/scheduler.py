"""Search-task scheduler (RF3 / spec 8.5).

Turns a flat search CONFIG -- ``{searches: [{query, boards, locations, ...}]}`` --
into the cartesian product of recurring ``search_tasks`` rows (one task per
``query x board x location``). The expansion is the *declarative* control surface:
the owner edits a YAML file, re-runs ``expand_search_config``, and the running
fleet picks up the changes on the next lease.

Claiming + re-scheduling is NOT reimplemented here: workers use
``queue.lease_search`` / ``queue.complete_search`` (board-governed, recurring).
This module only owns the task-set: create, enable/disable, and the coverage view.

Idempotency contract (the part that matters):
  * task_id = sha1(f"{query}|{board}|{location}")[:20] -- stable across re-runs.
  * Re-expanding REFRESHES cadence/params/enabled on a ``queued`` row.
  * Re-expanding NEVER touches a ``leased`` row (status, lease, attempts) and
    NEVER rewinds ``next_due_at`` -- so an in-flight scrape and the recurrence
    clock are never disturbed by an owner editing the config mid-run.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

DEFAULT_CADENCE_SECONDS = 21600  # 6h, matches search_tasks.cadence_seconds default


def task_id_for(query: str, board: str, location: str | None) -> str:
    """Stable 20-hex id for a (query, board, location) discovery task."""
    raw = f"{query}|{board}|{location or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


# ---------------------------------------------------------------------------
# Config expansion -- the cartesian product UPSERT (RF3).
# ---------------------------------------------------------------------------
# On a 'queued' row we refresh the declarative fields (cadence/params/query/board/
# location). We deliberately leave next_due_at alone so a re-expand never pulls the
# recurrence clock forward. ``enabled`` is OPERATOR-OWNED: a re-expand must NOT
# clobber a manual set_task_enabled(). The config only sets ``enabled`` when the
# entry EXPLICITLY provides the key (EXCLUDED.enabled is non-NULL); otherwise
# COALESCE preserves the existing row value -- so a manually-disabled task stays
# disabled across "edit searches.yaml and re-expand". On INSERT, EXCLUDED.enabled
# is itself COALESCEd to TRUE (the default) when the config omits the key.
# The WHERE status='queued' guard means a 'leased' row is invisible to the UPDATE
# branch -- its lease + next_due_at are untouched. A brand-new row is claimable
# immediately (next_due_at default now()).
_UPSERT_TASK = """
INSERT INTO search_tasks (task_id, query, board, location, params, cadence_seconds, enabled)
VALUES (%(task_id)s, %(query)s, %(board)s, %(location)s, %(params)s, %(cadence)s, COALESCE(%(enabled)s, TRUE))
ON CONFLICT (task_id) DO UPDATE SET
    query           = EXCLUDED.query,
    board           = EXCLUDED.board,
    location        = EXCLUDED.location,
    params          = EXCLUDED.params,
    cadence_seconds = EXCLUDED.cadence_seconds,
    enabled         = COALESCE(%(enabled)s, search_tasks.enabled),
    updated_at      = now()
WHERE search_tasks.status = 'queued';
"""


def expand_search_config(conn, config: dict[str, Any], *, default_cadence: int = DEFAULT_CADENCE_SECONDS,
                         commit: bool = True) -> int:
    """Expand ``config`` into ``search_tasks`` rows (idempotent).

    ``config`` shape::

        {"searches": [
            {"query": "chief of staff",
             "boards": ["linkedin", "greenhouse"],
             "locations": ["Remote", "New York"],   # optional; [None] if omitted
             "cadence_hours": 4,                      # optional; -> cadence_seconds
             "params": {"results_wanted": 50}},       # optional JSONB blob
            ...
        ]}

    For every (query x board x location) triple this UPSERTs one task. Returns the
    number of rows inserted-or-updated. Re-running with the same config leaves the
    row COUNT unchanged and never disturbs a leased task or its next_due_at.
    """
    searches = config.get("searches") or []
    n = 0
    seen: set[str] = set()  # dedupe task_ids within one expand so n == distinct rows
    with conn.cursor() as cur:
        for s in searches:
            query = s.get("query")
            if not query:
                continue
            boards = s.get("boards") or ([s["board"]] if s.get("board") else [])
            locations = s.get("locations")
            if locations is None:
                locations = [s["location"]] if s.get("location") else [None]
            if not locations:
                locations = [None]
            # cadence: explicit seconds > cadence_hours > the call default.
            if s.get("cadence_seconds") is not None:
                cadence = int(s["cadence_seconds"])
            elif s.get("cadence_hours") is not None:
                cadence = int(round(float(s["cadence_hours"]) * 3600))
            else:
                cadence = int(default_cadence)
            params = s.get("params")
            params_json = json.dumps(params) if params is not None else None
            # ``enabled`` is operator-owned: pass an explicit bool ONLY when the
            # config entry sets the key, else None so the UPSERT preserves the
            # existing row's value (and defaults to TRUE on a fresh INSERT). This
            # stops a re-expand from resurrecting a manually-disabled task.
            enabled = bool(s["enabled"]) if "enabled" in s else None
            for board in boards:
                for location in locations:
                    tid = task_id_for(query, board, location)
                    if tid in seen:   # a duplicate (query x board x location) -> one row, count once
                        continue
                    seen.add(tid)
                    cur.execute(_UPSERT_TASK, {
                        "task_id": tid,
                        "query": query, "board": board, "location": location,
                        "params": params_json, "cadence": cadence, "enabled": enabled,
                    })
                    n += cur.rowcount
    if commit:
        conn.commit()
    return n


def load_search_config_from_yaml(path) -> dict[str, Any]:
    """Load a search config YAML file into the dict ``expand_search_config`` wants.

    Accepts either a top-level ``{searches: [...]}`` mapping or a bare list of
    search entries (wrapped into ``{"searches": [...]}``)."""
    import yaml

    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {"searches": []}
    if isinstance(data, list):
        return {"searches": data}
    if "searches" not in data:
        return {"searches": []}
    return data


# ---------------------------------------------------------------------------
# Enable / disable a single task (removes it from lease_search eligibility).
# ---------------------------------------------------------------------------
def set_task_enabled(conn, task_id: str, enabled: bool, *, commit: bool = True) -> bool:
    """Flip ``enabled`` on one task. A disabled task is excluded from
    ``queue.lease_search`` (its WHERE clause requires ``enabled``). Returns True
    if a row was updated."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE search_tasks SET enabled=%s, updated_at=now() WHERE task_id=%s",
            (enabled, task_id),
        )
        updated = cur.rowcount
    if commit:
        conn.commit()
    return updated > 0


# ---------------------------------------------------------------------------
# Coverage view for the dashboard.
# ---------------------------------------------------------------------------
_COVERAGE_SQL = """
SELECT task_id, board, query, location, status, enabled,
       last_run_at, result_count, next_due_at, cadence_seconds, last_error
FROM search_tasks
ORDER BY board, query, location NULLS FIRST;
"""


def coverage_view(conn) -> list[dict[str, Any]]:
    """Per-task coverage rows for the discovery dashboard: which (board, query)
    pairs are scheduled, when each last ran, how many postings it found, and when
    it is next due. One row per task."""
    with conn.cursor() as cur:
        cur.execute(_COVERAGE_SQL)
        return [dict(r) for r in cur.fetchall()]
