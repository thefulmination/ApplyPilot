from __future__ import annotations

import json
import pytest
from pathlib import Path


def _setup(monkeypatch, tmp_path: Path):
    """Wire both import_decisions and resbuild_bridge to one disposable SQLite brain."""
    from applypilot import database, import_decisions, resbuild_bridge

    conn = database.init_db(tmp_path / "applypilot.db")
    for mod in (import_decisions, resbuild_bridge):
        monkeypatch.setattr(mod, "init_db", lambda *a, **k: conn)
        monkeypatch.setattr(mod, "get_connection", lambda: conn)
    return conn, resbuild_bridge


def _insert_job(conn, url, *, title="Engineer", site="Co", fit_score=5,
                audit_score=None, audit_label=None, full_description="desc",
                applied_at=None, duplicate_of_url=None, apply_status=None,
                apply_error=None):
    conn.execute(
        "INSERT INTO jobs (url, title, site, full_description, fit_score, audit_score, "
        "audit_label, applied_at, duplicate_of_url, apply_status, apply_error) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (url, title, site, full_description, fit_score, audit_score, audit_label,
         applied_at, duplicate_of_url, apply_status, apply_error),
    )
    conn.commit()


def _write(tmp_path, records, name="applylist.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return path


def _src(conn, url):
    return conn.execute("SELECT decision_source FROM jobs WHERE url=?", (url,)).fetchone()["decision_source"]


def test_excludes_linkedin_by_default(monkeypatch, tmp_path):
    conn, mod = _setup(monkeypatch, tmp_path)
    _insert_job(conn, "https://www.linkedin.com/jobs/view/1")
    _insert_job(conn, "https://job-boards.greenhouse.io/x/jobs/2")
    path = _write(tmp_path, [
        {"url": "https://www.linkedin.com/jobs/view/1", "verdict": "approve", "decision_score": 9},
        {"url": "https://job-boards.greenhouse.io/x/jobs/2", "verdict": "approve", "decision_score": 9},
    ])
    r = mod.promote(path, source="res_build", snapshot_path=tmp_path / "snap.json")
    assert r["promoted"] == 1
    assert _src(conn, "https://www.linkedin.com/jobs/view/1") is None       # catastrophe lane untouched
    gh = conn.execute("SELECT decision_source, audit_score FROM jobs WHERE url=?",
                      ("https://job-boards.greenhouse.io/x/jobs/2",)).fetchone()
    assert gh["decision_source"] == "res_build" and gh["audit_score"] == 9.0


def test_excludes_linkedin_trailing_dot_fqdn(monkeypatch, tmp_path):
    # The fully-qualified 'linkedin.com.' form must NOT slip past the exclusion.
    conn, mod = _setup(monkeypatch, tmp_path)
    url = "https://www.linkedin.com./jobs/view/9"
    _insert_job(conn, url)
    path = _write(tmp_path, [{"url": url, "verdict": "approve", "decision_score": 9}])
    r = mod.promote(path, source="res_build", snapshot_path=tmp_path / "snap.json")
    assert r["promoted"] == 0
    assert _src(conn, url) is None


def test_only_applyable_skips_applied_and_duplicate(monkeypatch, tmp_path):
    conn, mod = _setup(monkeypatch, tmp_path)
    _insert_job(conn, "https://x.io/clean")
    _insert_job(conn, "https://x.io/applied", applied_at="2026-06-01T00:00:00+00:00")
    _insert_job(conn, "https://x.io/dup", duplicate_of_url="https://x.io/clean")
    path = _write(tmp_path, [
        {"url": "https://x.io/clean", "verdict": "approve", "decision_score": 9},
        {"url": "https://x.io/applied", "verdict": "approve", "decision_score": 9},
        {"url": "https://x.io/dup", "verdict": "approve", "decision_score": 9},
    ])
    r = mod.promote(path, source="res_build", snapshot_path=tmp_path / "snap.json")
    assert r["promoted"] == 1
    assert _src(conn, "https://x.io/clean") == "res_build"
    assert _src(conn, "https://x.io/applied") is None
    assert _src(conn, "https://x.io/dup") is None


def test_limit_takes_top_by_score(monkeypatch, tmp_path):
    conn, mod = _setup(monkeypatch, tmp_path)
    for u in ("https://x.io/a", "https://x.io/b", "https://x.io/c"):
        _insert_job(conn, u, fit_score=3)
    path = _write(tmp_path, [
        {"url": "https://x.io/a", "verdict": "approve", "decision_score": 9},
        {"url": "https://x.io/b", "verdict": "approve", "decision_score": 7},
        {"url": "https://x.io/c", "verdict": "approve", "decision_score": 5},
    ])
    r = mod.promote(path, source="res_build", limit=2, snapshot_path=tmp_path / "snap.json")
    assert r["promoted"] == 2
    promoted = {row["url"] for row in conn.execute(
        "SELECT url FROM jobs WHERE decision_source='res_build'")}
    assert promoted == {"https://x.io/a", "https://x.io/b"}


def test_dry_run_writes_nothing(monkeypatch, tmp_path):
    conn, mod = _setup(monkeypatch, tmp_path)
    _insert_job(conn, "https://x.io/j", fit_score=5)
    path = _write(tmp_path, [{"url": "https://x.io/j", "verdict": "approve", "decision_score": 9}])
    snap = tmp_path / "snap.json"
    r = mod.promote(path, source="res_build", dry_run=True, snapshot_path=snap)
    assert r["dry_run"] is True and r["would_promote"] == 1
    row = conn.execute("SELECT audit_score, decision_source FROM jobs WHERE url=?",
                      ("https://x.io/j",)).fetchone()
    assert row["audit_score"] is None and row["decision_source"] is None
    assert not snap.exists()   # dry run never snapshots


def test_dry_run_counts_only_jobs_it_unlocks(monkeypatch, tmp_path):
    conn, mod = _setup(monkeypatch, tmp_path)
    _insert_job(conn, "https://x.io/low", fit_score=5)    # eff 5 < 7 -> unlocked by bridge
    _insert_job(conn, "https://x.io/high", fit_score=8)   # eff 8 >= 7 -> already qualifies
    path = _write(tmp_path, [
        {"url": "https://x.io/low", "verdict": "approve", "decision_score": 9},
        {"url": "https://x.io/high", "verdict": "approve", "decision_score": 9},
    ])
    r = mod.promote(path, source="res_build", dry_run=True)
    assert r["would_promote"] == 2 and r["would_raise"] == 1


def test_promote_then_revert_restores_prior(monkeypatch, tmp_path):
    conn, mod = _setup(monkeypatch, tmp_path)
    _insert_job(conn, "https://x.io/j", fit_score=5, audit_score=4.0, audit_label="audit")
    path = _write(tmp_path, [{"url": "https://x.io/j", "verdict": "approve", "decision_score": 9}])
    snap = tmp_path / "snap.json"
    mod.promote(path, source="res_build", snapshot_path=snap)
    row = conn.execute("SELECT audit_score, decision_source FROM jobs WHERE url=?",
                      ("https://x.io/j",)).fetchone()
    assert row["audit_score"] == 9.0 and row["decision_source"] == "res_build"
    assert mod.revert(snap) == 1
    row2 = conn.execute("SELECT audit_score, audit_label, decision_source FROM jobs WHERE url=?",
                       ("https://x.io/j",)).fetchone()
    assert row2["audit_score"] == 4.0 and row2["audit_label"] == "audit" and row2["decision_source"] is None


def test_revert_restores_null_prior(monkeypatch, tmp_path):
    conn, mod = _setup(monkeypatch, tmp_path)
    _insert_job(conn, "https://x.io/j", fit_score=5)   # no prior audit at all
    path = _write(tmp_path, [{"url": "https://x.io/j", "verdict": "approve", "decision_score": 9}])
    snap = tmp_path / "snap.json"
    mod.promote(path, source="res_build", snapshot_path=snap)
    assert conn.execute("SELECT audit_score FROM jobs WHERE url=?", ("https://x.io/j",)).fetchone()["audit_score"] == 9.0
    mod.revert(snap)
    row = conn.execute("SELECT audit_score, audit_label, decision_source FROM jobs WHERE url=?",
                      ("https://x.io/j",)).fetchone()
    assert row["audit_score"] is None and row["audit_label"] is None and row["decision_source"] is None


def test_scale_ten_preserves_decimal(monkeypatch, tmp_path):
    conn, mod = _setup(monkeypatch, tmp_path)
    _insert_job(conn, "https://x.io/j", fit_score=5)
    path = _write(tmp_path, [{"url": "https://x.io/j", "verdict": "approve", "decision_score": 9.9}])
    mod.promote(path, source="res_build", scale="ten", snapshot_path=tmp_path / "snap.json")
    assert conn.execute("SELECT audit_score FROM jobs WHERE url=?", ("https://x.io/j",)).fetchone()["audit_score"] == 9.9


def test_approval_never_demotes_prior_effective(monkeypatch, tmp_path):
    # A kept job the production ranker already had ABOVE the gate must stay there,
    # even when the res_build score is lower (approval is the decision; the score
    # is only a rank). The raw res score lands in external_decision_score.
    conn, mod = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr("applypilot.config.get_min_score", lambda: 6)
    _insert_job(conn, "https://x.io/j", fit_score=5, audit_score=8.2, audit_label="audit")
    path = _write(tmp_path, [{"url": "https://x.io/j", "verdict": "approve", "decision_score": 4.5}])
    r = mod.promote(path, source="res_build", snapshot_path=tmp_path / "snap.json")
    assert r["promoted"] == 1
    row = conn.execute(
        "SELECT audit_score, external_decision_score, decision_source FROM jobs WHERE url=?",
        ("https://x.io/j",)).fetchone()
    assert row["audit_score"] == 8.2
    assert row["external_decision_score"] == 4.5
    assert row["decision_source"] == "res_build"


def test_approval_floors_gate_at_apply_threshold(monkeypatch, tmp_path):
    # A kept job the ranker buries must become apply-eligible: the gate write is
    # floored at the apply threshold even when the res_build score is below it.
    conn, mod = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr("applypilot.config.get_min_score", lambda: 6)
    _insert_job(conn, "https://x.io/j", fit_score=3)
    path = _write(tmp_path, [{"url": "https://x.io/j", "verdict": "approve", "decision_score": 4.5}])
    r = mod.promote(path, source="res_build", snapshot_path=tmp_path / "snap.json")
    assert r["promoted"] == 1
    row = conn.execute(
        "SELECT audit_score, external_decision_score FROM jobs WHERE url=?",
        ("https://x.io/j",)).fetchone()
    assert row["audit_score"] == 6.0
    assert row["external_decision_score"] == 4.5


def test_unit_scale_floor_and_benchmark_score(monkeypatch, tmp_path):
    # unit-scale input: raw rescales to the ten band BEFORE flooring, so the floor
    # can never be re-rescaled into a clamped 10 by the importer.
    conn, mod = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr("applypilot.config.get_min_score", lambda: 6)
    _insert_job(conn, "https://x.io/j", fit_score=3)
    path = _write(tmp_path, [{"url": "https://x.io/j", "verdict": "approve", "decision_score": 0.45}])
    mod.promote(path, source="res_build", scale="unit", snapshot_path=tmp_path / "snap.json")
    row = conn.execute(
        "SELECT audit_score, external_decision_score FROM jobs WHERE url=?",
        ("https://x.io/j",)).fetchone()
    assert row["audit_score"] == 6.0
    assert row["external_decision_score"] == 4.5


def test_revert_skips_rows_no_longer_ours(monkeypatch, tmp_path):
    conn, mod = _setup(monkeypatch, tmp_path)
    _insert_job(conn, "https://x.io/j", fit_score=5)
    path = _write(tmp_path, [{"url": "https://x.io/j", "verdict": "approve", "decision_score": 9}])
    snap = tmp_path / "snap.json"
    mod.promote(path, source="res_build", snapshot_path=snap)
    conn.execute("UPDATE jobs SET decision_source='other', audit_score=6 WHERE url=?", ("https://x.io/j",))
    conn.commit()
    assert mod.revert(snap) == 0   # not ours anymore -> leave it alone
    assert conn.execute("SELECT audit_score FROM jobs WHERE url=?", ("https://x.io/j",)).fetchone()["audit_score"] == 6


class _FakePgCursor:
    def __init__(self, apply_queue_urls=(), linkedin_queue_urls=()):
        self.apply_queue_urls = list(apply_queue_urls)
        self.linkedin_queue_urls = list(linkedin_queue_urls)
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def execute(self, sql, params=()):
        if "apply_queue" in sql:
            self._rows = [{"url": u} for u in self.apply_queue_urls]
        elif "linkedin_queue" in sql:
            self._rows = [{"url": u} for u in self.linkedin_queue_urls]
        else:
            self._rows = []
        return self

    def fetchall(self):
        return self._rows

    def close(self):
        return None


class _FakePgConn:
    def __init__(self, apply_queue_urls=(), linkedin_queue_urls=()):
        self._cursor = _FakePgCursor(apply_queue_urls, linkedin_queue_urls)

    def cursor(self):
        return self._cursor

    def close(self):
        return None


def test_promote_excludes_fleet_applied_rows_and_counts(monkeypatch, tmp_path):
    conn, mod = _setup(monkeypatch, tmp_path)
    monkeypatch.delenv("FLEET_PG_DSN", raising=False)
    _insert_job(conn, "https://x.io/normal", fit_score=5)
    _insert_job(conn, "https://x.io/fleet", fit_score=5)
    path = _write(
        tmp_path,
        [
            {"url": "https://x.io/normal", "verdict": "approve", "decision_score": 9},
            {"url": "https://x.io/fleet", "verdict": "approve", "decision_score": 9},
        ],
    )

    monkeypatch.setenv("FLEET_PG_DSN", "postgresql://unit-test")
    monkeypatch.setattr(
        "applypilot.resbuild_bridge.pgqueue.connect",
        lambda: _FakePgConn(apply_queue_urls=["https://x.io/fleet"]),
    )
    r = mod.promote(path, source="res_build", snapshot_path=tmp_path / "snap.json")

    assert r["fleet_cross_check"] == "ok"
    assert r["excluded_fleet_applied"] == 1
    assert r["promoted"] == 1
    assert conn.execute("SELECT decision_source FROM jobs WHERE url=?", ("https://x.io/normal",)).fetchone()["decision_source"] == "res_build"
    assert conn.execute("SELECT decision_source FROM jobs WHERE url=?", ("https://x.io/fleet",)).fetchone()["decision_source"] is None


@pytest.mark.parametrize("field,value", [("apply_status", "applied"), ("apply_status", "in_progress"),
                                        ("apply_status", "crash_unconfirmed"),
                                        ("apply_error", "no_confirmation"),
                                        ("apply_error", "crash_unconfirmed")])
def test_promote_excludes_submit_markers(monkeypatch, tmp_path, field, value):
    conn, mod = _setup(monkeypatch, tmp_path)
    monkeypatch.delenv("FLEET_PG_DSN", raising=False)
    url = f"https://x.io/{field}-{value}"
    _insert_job(conn, url, fit_score=5, **{field: value})
    path = _write(tmp_path, [{"url": url, "verdict": "approve", "decision_score": 9}])

    r = mod.promote(path, source="res_build", snapshot_path=tmp_path / "snap.json")
    assert r["promoted"] == 0
    assert r["fleet_cross_check"] == "skipped_no_dsn"
    assert r["excluded_fleet_applied"] == 0
    assert conn.execute("SELECT decision_source FROM jobs WHERE url=?", (url,)).fetchone()["decision_source"] is None


def test_promote_without_dsn_skips_fleet_cross_check(monkeypatch, tmp_path):
    conn, mod = _setup(monkeypatch, tmp_path)
    monkeypatch.delenv("FLEET_PG_DSN", raising=False)
    _insert_job(conn, "https://x.io/normal", fit_score=5)
    path = _write(tmp_path, [{"url": "https://x.io/normal", "verdict": "approve", "decision_score": 9}])
    monkeypatch.setattr(
        "applypilot.resbuild_bridge.pgqueue.connect",
        lambda: (_ for _ in ()).throw(RuntimeError("should not be called")),
    )

    r = mod.promote(path, source="res_build", snapshot_path=tmp_path / "snap.json")
    assert r["fleet_cross_check"] == "skipped_no_dsn"
    assert r["excluded_fleet_applied"] == 0
    assert r["promoted"] == 1
    assert conn.execute("SELECT decision_source FROM jobs WHERE url=?", ("https://x.io/normal",)).fetchone()["decision_source"] == "res_build"
