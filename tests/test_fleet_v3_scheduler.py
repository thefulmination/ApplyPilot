"""Tests for the search-task scheduler (RF3 / spec 8.5).

Real Postgres via the ``fleet_db`` fixture (conftest). Verifies the cartesian
expansion, the idempotency contract (re-expand is a no-op count-wise and NEVER
disturbs a leased task or its next_due_at), that disabling removes a task from
``queue.lease_search`` eligibility, and that the coverage view returns rows.
"""
from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import queue, scheduler


def _count_tasks(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM search_tasks")
        return cur.fetchone()["n"]


def _row(conn, task_id):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM search_tasks WHERE task_id=%s", (task_id,))
        return cur.fetchone()


def test_expand_dedupes_duplicate_triples_in_count(fleet_db):
    # Duplicate boards/locations within one config produce one physical row; the
    # returned count must equal the distinct task count (n == COUNT(*)).
    cfg = {"searches": [{"query": "cos",
                         "boards": ["greenhouse", "greenhouse"],
                         "locations": ["remote", "remote"]}]}
    with pgqueue.connect(fleet_db) as conn:
        n = scheduler.expand_search_config(conn, cfg)
        rows = _count_tasks(conn)
    assert n == rows == 1, f"duplicate triple must collapse to one task; n={n} rows={rows}"


# ---------------------------------------------------------------------------
# Expansion = cartesian product of (query x board x location).
# ---------------------------------------------------------------------------
def test_expand_creates_cartesian_product(fleet_db):
    config = {
        "searches": [
            {"query": "chief of staff", "boards": ["linkedin", "greenhouse"],
             "locations": ["Remote", "New York"], "cadence_hours": 4,
             "params": {"results_wanted": 50}},
            {"query": "quant developer", "boards": ["indeed"]},  # no locations -> [None]
        ]
    }
    with pgqueue.connect(fleet_db) as conn:
        n = scheduler.expand_search_config(conn, config)
        # 2 boards x 2 locations + 1 board x 1 (None) location = 5 tasks.
        assert n == 5
        assert _count_tasks(conn) == 5

        # task_id is the documented sha1(query|board|location)[:20]
        tid = scheduler.task_id_for("chief of staff", "linkedin", "Remote")
        r = _row(conn, tid)
        assert r is not None
        assert r["query"] == "chief of staff"
        assert r["board"] == "linkedin"
        assert r["location"] == "Remote"
        assert r["cadence_seconds"] == 4 * 3600
        assert r["params"] == {"results_wanted": 50}
        assert r["status"] == "queued" and r["enabled"] is True

        # the no-location search collapsed to a single NULL-location task
        tid2 = scheduler.task_id_for("quant developer", "indeed", None)
        r2 = _row(conn, tid2)
        assert r2 is not None and r2["location"] is None
        assert r2["cadence_seconds"] == scheduler.DEFAULT_CADENCE_SECONDS


# ---------------------------------------------------------------------------
# Re-expand is idempotent: count unchanged; declarative fields refreshed.
# ---------------------------------------------------------------------------
def test_reexpand_is_idempotent_and_refreshes_queued(fleet_db):
    base = {"searches": [{"query": "chief of staff", "boards": ["linkedin"],
                          "locations": ["Remote"], "cadence_hours": 6}]}
    with pgqueue.connect(fleet_db) as conn:
        scheduler.expand_search_config(conn, base)
        assert _count_tasks(conn) == 1
        tid = scheduler.task_id_for("chief of staff", "linkedin", "Remote")
        assert _row(conn, tid)["cadence_seconds"] == 6 * 3600

        # Re-expand with a changed cadence + params on the SAME triple.
        changed = {"searches": [{"query": "chief of staff", "boards": ["linkedin"],
                                 "locations": ["Remote"], "cadence_hours": 2,
                                 "params": {"hours_old": 24}}]}
        scheduler.expand_search_config(conn, changed)
        assert _count_tasks(conn) == 1                       # COUNT unchanged
        r = _row(conn, tid)
        assert r["cadence_seconds"] == 2 * 3600              # refreshed
        assert r["params"] == {"hours_old": 24}             # refreshed


# ---------------------------------------------------------------------------
# A LEASED task is NOT reset by re-expand (status, lease, next_due_at).
# ---------------------------------------------------------------------------
def test_reexpand_does_not_reset_leased_task(fleet_db):
    config = {"searches": [{"query": "chief of staff", "boards": ["linkedin"],
                            "locations": ["Remote"], "cadence_hours": 6}]}
    with pgqueue.connect(fleet_db) as conn:
        scheduler.expand_search_config(conn, config)
        tid = scheduler.task_id_for("chief of staff", "linkedin", "Remote")

        # Worker claims it via the REUSED lease (don't reimplement).
        leased = queue.lease_search(conn, "worker-A")
        assert leased is not None and leased["task_id"] == tid
        before = _row(conn, tid)
        assert before["status"] == "leased"
        assert before["lease_owner"] == "worker-A"
        due_before = before["next_due_at"]
        attempts_before = before["attempts"]

        # Owner edits the config and re-expands mid-scrape.
        edited = {"searches": [{"query": "chief of staff", "boards": ["linkedin"],
                                "locations": ["Remote"], "cadence_hours": 1}]}
        n = scheduler.expand_search_config(conn, edited)
        assert n == 0                                       # leased row not touched

        after = _row(conn, tid)
        assert after["status"] == "leased"                  # still leased
        assert after["lease_owner"] == "worker-A"           # same owner
        assert after["next_due_at"] == due_before           # recurrence clock untouched
        assert after["attempts"] == attempts_before
        assert after["cadence_seconds"] == 6 * 3600         # NOT refreshed while leased

        # And the worker can still close it via the reused completion path.
        assert queue.complete_search(conn, "worker-A", tid, result_count=12, board="linkedin") is True
        done = _row(conn, tid)
        assert done["status"] == "queued" and done["result_count"] == 12


# ---------------------------------------------------------------------------
# Disabling a task removes it from lease_search eligibility.
# ---------------------------------------------------------------------------
def test_disable_removes_from_lease_eligibility(fleet_db):
    config = {"searches": [{"query": "chief of staff", "boards": ["linkedin"],
                            "locations": ["Remote"]}]}
    with pgqueue.connect(fleet_db) as conn:
        scheduler.expand_search_config(conn, config)
        tid = scheduler.task_id_for("chief of staff", "linkedin", "Remote")

        # Disable -> lease_search must find nothing.
        assert scheduler.set_task_enabled(conn, tid, False) is True
        assert queue.lease_search(conn, "worker-A") is None

        # Re-enable -> now leasable again.
        assert scheduler.set_task_enabled(conn, tid, True) is True
        leased = queue.lease_search(conn, "worker-A")
        assert leased is not None and leased["task_id"] == tid

        # set_task_enabled on an unknown id returns False.
        assert scheduler.set_task_enabled(conn, "nope" * 5, False) is False


# ---------------------------------------------------------------------------
# Re-expand must NOT resurrect a manually-disabled task (enabled is operator-owned).
# ---------------------------------------------------------------------------
def test_reexpand_preserves_manual_disable(fleet_db):
    config = {"searches": [{"query": "chief of staff", "boards": ["linkedin"],
                            "locations": ["Remote"], "cadence_hours": 6}]}
    with pgqueue.connect(fleet_db) as conn:
        scheduler.expand_search_config(conn, config)
        tid = scheduler.task_id_for("chief of staff", "linkedin", "Remote")

        # Operator manually disables the task (YAML omits an `enabled` key).
        assert scheduler.set_task_enabled(conn, tid, False) is True
        assert _row(conn, tid)["enabled"] is False
        assert queue.lease_search(conn, "worker-A") is None  # excluded from lease

        # The documented "edit searches.yaml and re-expand" workflow re-runs with
        # the SAME config (which never carries an `enabled` key).
        scheduler.expand_search_config(conn, config)

        # The manual disable must survive the re-expand...
        assert _row(conn, tid)["enabled"] is False
        # ...and the task must STILL be out of lease eligibility.
        assert queue.lease_search(conn, "worker-A") is None


# ---------------------------------------------------------------------------
# coverage_view returns one row per task with the dashboard fields.
# ---------------------------------------------------------------------------
def test_coverage_view_returns_rows(fleet_db):
    config = {
        "searches": [
            {"query": "chief of staff", "boards": ["linkedin", "greenhouse"],
             "locations": ["Remote"]},
            {"query": "quant developer", "boards": ["indeed"]},
        ]
    }
    with pgqueue.connect(fleet_db) as conn:
        scheduler.expand_search_config(conn, config)

        # Run one task so last_run_at / result_count are populated.
        leased = queue.lease_search(conn, "worker-A")
        queue.complete_search(conn, "worker-A", leased["task_id"], result_count=7, board=leased["board"])

        rows = scheduler.coverage_view(conn)
        assert len(rows) == 3
        expected_cols = {"board", "query", "status", "last_run_at", "result_count", "next_due_at"}
        assert expected_cols <= set(rows[0].keys())
        # the completed task carries its coverage stats
        ran = [r for r in rows if r["result_count"] == 7]
        assert len(ran) == 1
        assert ran[0]["last_run_at"] is not None


# ---------------------------------------------------------------------------
# YAML loader round-trips into the expand-able dict shape.
# ---------------------------------------------------------------------------
def test_load_search_config_from_yaml(fleet_db, tmp_path):
    yaml_text = (
        "searches:\n"
        "  - query: chief of staff\n"
        "    boards: [linkedin, greenhouse]\n"
        "    locations: [Remote]\n"
        "    cadence_hours: 3\n"
        "    params:\n"
        "      results_wanted: 25\n"
    )
    p = tmp_path / "searches.yaml"
    p.write_text(yaml_text, encoding="utf-8")

    config = scheduler.load_search_config_from_yaml(str(p))
    assert isinstance(config, dict) and "searches" in config
    assert config["searches"][0]["query"] == "chief of staff"

    with pgqueue.connect(fleet_db) as conn:
        n = scheduler.expand_search_config(conn, config)
        assert n == 2  # 2 boards x 1 location
        tid = scheduler.task_id_for("chief of staff", "greenhouse", "Remote")
        r = _row(conn, tid)
        assert r["cadence_seconds"] == 3 * 3600
        assert r["params"] == {"results_wanted": 25}
