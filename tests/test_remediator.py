from applypilot.fleet import remediator


class _FakeCursor:
    def __init__(self, script):
        self.script = script; self.executed = []; self._last = None
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        for needle, rows in self.script.items():
            if needle in sql: self._last = list(rows); return
        self._last = []
    def fetchone(self): return self._last[0] if self._last else None
    def fetchall(self): return self._last


class _FakeConn:
    def __init__(self, script=None):
        self._cur = _FakeCursor(script or {}); self.committed = False
    def cursor(self): return self._cur
    def commit(self): self.committed = True


def test_candidate_dataclass_fields():
    c = remediator.Candidate(url="u", worker_id="m2-3", dedup_key="dk",
                             status="crash_unconfirmed", attempts=99,
                             apply_error="crash_unconfirmed", reason="usage_limit")
    assert c.url == "u" and c.worker_id == "m2-3" and c.attempts == 99


def test_ensure_remediation_table_creates_idempotently():
    conn = _FakeConn()
    remediator.ensure_remediation_table(conn)
    sql = " ".join(s for s, _ in conn._cur.executed)
    assert "CREATE TABLE IF NOT EXISTS remediation_actions" in sql
    assert conn.committed is True


import sqlite3


def test_in_applied_set_true_and_false():
    hit = _FakeConn({"FROM applied_set": [{"?column?": 1}]})
    miss = _FakeConn({"FROM applied_set": []})
    assert remediator.in_applied_set(hit, "dk") is True
    assert remediator.in_applied_set(miss, "dk") is False


def test_in_applied_set_none_dedup_key_is_false_without_query():
    conn = _FakeConn({"FROM applied_set": [{"?column?": 1}]})
    assert remediator.in_applied_set(conn, None) is False
    assert conn._cur.executed == []  # short-circuits; never queries on a null key


def test_has_confirming_email_true_when_row_present(tmp_path):
    p = tmp_path / "brain.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE email_events (message_id TEXT PRIMARY KEY, job_url TEXT)")
    c.execute("INSERT INTO email_events VALUES ('m1', 'https://job/1')")
    c.commit(); c.close()
    assert remediator.has_confirming_email(str(p), "https://job/1") is True
    assert remediator.has_confirming_email(str(p), "https://job/2") is False


def test_has_confirming_email_graceful_when_table_absent(tmp_path):
    p = tmp_path / "noet.db"
    sqlite3.connect(p).close()  # valid db, no email_events table
    assert remediator.has_confirming_email(str(p), "https://job/1") is False  # no veto, no crash


def test_has_confirming_email_graceful_when_file_missing(tmp_path):
    assert remediator.has_confirming_email(str(tmp_path / "nope.db"), "u") is False


def test_select_candidates_maps_rows_to_dataclass():
    rows = [{"url": "https://job/1", "worker_id": "m2-3", "dedup_key": "dk1",
             "status": "failed", "attempts": 99,
             "apply_error": "failed:usage_limit", "reason": "usage_limit"}]
    conn = _FakeConn({"FROM apply_queue": rows})
    cands = remediator.select_candidates(conn, window_minutes=30, max_per_job=2)
    assert len(cands) == 1
    assert cands[0].url == "https://job/1" and cands[0].reason == "usage_limit"
    # the query must scope to ATS lane, own apply_error safety gate, and the per-job cap
    sql = conn._cur.executed[0][0]
    assert "lane = 'ats'" in sql
    assert "status = 'failed'" in sql
    assert "usage_limit" in sql
    assert "dedup_key IS NOT NULL" in sql
    assert "remediation_actions" in sql


def test_candidate_sql_excludes_may_have_submitted():
    """The emitted SQL must NOT reference the may-have-submitted bucket or diagnosis join."""
    conn = _FakeConn({})
    remediator.select_candidates(conn, window_minutes=30, max_per_job=2)
    sql = conn._cur.executed[0][0]
    assert "crash_unconfirmed" not in sql
    assert "no_result_line" not in sql
    assert "fleet_diagnoses" not in sql


def test_pre_touch_backfill_requires_latest_durable_zero_tool_event():
    rows = [{"url": "https://job/1", "worker_id": "m2-3", "dedup_key": "dk1",
             "status": "failed", "attempts": 1, "apply_error": "failed:no_browser_tool",
             "reason": "pre_touch_backfill"}]
    conn = _FakeConn({"FROM apply_queue": rows})

    candidates = remediator.select_pre_touch_backfill_candidates(conn, max_per_job=2)

    assert [candidate.url for candidate in candidates] == ["https://job/1"]
    sql = conn._cur.executed[0][0]
    assert "apply_result_events" in sql
    assert "application_tool_calls = 0" in sql
    assert "no_browser_tool" in sql
    assert "ORDER BY e.created_at DESC, e.id DESC" in sql


def test_has_confirming_email_graceful_on_corrupt_brain(tmp_path):
    """A corrupt (non-SQLite) brain file must degrade to False, not raise."""
    p = tmp_path / "corrupt.db"
    p.write_bytes(b"not a database at all")
    assert remediator.has_confirming_email(str(p), "u") is False  # no veto, no crash


class _RowcountCursor(_FakeCursor):
    """Like _FakeCursor but reports rowcount=1 for the UPDATE so requeue_job sees success."""
    def __init__(self, script):
        super().__init__(script); self.rowcount = 0
    def execute(self, sql, params=None):
        super().execute(sql, params)
        self.rowcount = 1 if sql.strip().upper().startswith("UPDATE") else 0


class _RowcountConn(_FakeConn):
    def __init__(self, script=None):
        super().__init__(script); self._cur = _RowcountCursor(script or {})


class _LostRaceCursor(_RowcountCursor):
    """Like _RowcountCursor but reports rowcount=0 for the UPDATE to simulate a lost race."""
    def execute(self, sql, params=None):
        super().execute(sql, params)
        self.rowcount = 0 if sql.strip().upper().startswith("UPDATE") else 0


class _LostRaceConn(_FakeConn):
    def __init__(self, script=None):
        super().__init__(script); self._cur = _LostRaceCursor(script or {})


def test_requeue_job_updates_to_queued_and_audits():
    conn = _RowcountConn()
    c = remediator.Candidate(url="https://job/1", worker_id="m2-3", dedup_key="dk1",
                             status="crash_unconfirmed", attempts=99,
                             apply_error="crash_unconfirmed", reason="usage_limit")
    assert remediator.requeue_job(conn, c) is True and conn.committed is True
    upd = [e for e in conn._cur.executed if e[0].strip().upper().startswith("UPDATE APPLY_QUEUE")]
    ins = [e for e in conn._cur.executed if "INSERT INTO remediation_actions" in e[0]]
    assert len(upd) == 1 and len(ins) == 1
    assert "status='queued'" in upd[0][0].replace(" ", "") or "status = 'queued'" in upd[0][0]
    # audit row carries the PRIOR state for reversal
    assert "crash_unconfirmed" in ins[0][1] and 99 in ins[0][1]


def test_requeue_job_lost_race_writes_nothing():
    """When the UPDATE rowcount is 0 (race: status changed underneath), requeue_job must return
    False and write NOTHING (no audit row, no commit). This proves the lost-race safety path."""
    conn = _LostRaceConn()
    c = remediator.Candidate(url="https://job/1", worker_id="m2-3", dedup_key="dk1",
                             status="crash_unconfirmed", attempts=99,
                             apply_error="crash_unconfirmed", reason="usage_limit")
    assert remediator.requeue_job(conn, c) is False
    ins = [e for e in conn._cur.executed if "INSERT INTO remediation_actions" in e[0]]
    assert len(ins) == 0
    assert conn.committed is False


def _cand(url, dk="dk"):
    return remediator.Candidate(url=url, worker_id="m2-3", dedup_key=dk,
                                status="crash_unconfirmed", attempts=99,
                                apply_error="crash_unconfirmed", reason="usage_limit")


def test_remediate_applies_guards_and_caps(monkeypatch):
    cands = [_cand("u-clean"), _cand("u-applied"), _cand("u-emailed"), _cand("u-overflow")]
    monkeypatch.setattr(remediator, "ensure_remediation_table", lambda conn: None)
    monkeypatch.setattr(remediator, "select_candidates", lambda conn, **k: cands)
    # guard 2 vetoes the candidate whose dedup_key == "applied"; guard 3 vetoes url "u-emailed"
    monkeypatch.setattr(remediator, "in_applied_set",
                        lambda conn, dk: dk == "applied")
    monkeypatch.setattr(remediator, "has_confirming_email",
                        lambda bp, url: url == "u-emailed")
    requeued = []
    monkeypatch.setattr(remediator, "requeue_job",
                        lambda conn, c: (requeued.append(c.url) or True))
    cands[1] = _cand("u-applied", dk="applied")  # guard-2 veto target
    out = remediator.remediate(object(), brain_path="x", max_requeue=1, max_per_job=2)
    # only u-clean re-queued (u-applied vetoed, u-emailed vetoed, then max_requeue=1 caps the rest)
    assert requeued == ["u-clean"]
    assert out["requeued"] == 1 and out["vetoed_applied_set"] == 1 and out["vetoed_email"] == 1
    assert out["capped"] >= 1


# ---------------------------------------------------------------------------
# Adversarial INTEGRATION test (real Postgres, fleet_db fixture) -- locks the
# double-apply invariant against the live candidate SQL, not a string assertion.
#
# Only a PROVABLY-never-submitted job (status='failed', apply_error contains 'usage_limit')
# may be a candidate. Every "may-have-submitted" park MUST be excluded:
#   * apply_error='crash_unconfirmed' -- reclaim_stale_leases parks a HARD crash (possibly
#     mid-submit, "may carry the user's name") and never writes applied_set;
#   * 'failed:no_result_line' / 'failed:timeout' -- the agent RAN and may have reached the form.
# A usage_limit diagnosis is seeded for the worker so this test ALSO fails (correctly) if a
# future change re-introduces a worker-diagnosis JOIN that re-admits the no_result_line bucket.
# This is the regression guard for the gap where status='crash_unconfirmed' was accepted alone.
# ---------------------------------------------------------------------------
from applypilot.apply import pgqueue


def _seed_usage_limit_diagnosis(conn, machine):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fleet_diagnoses (cluster_key, reason, machine, status, created_at) "
            "VALUES (%s, 'usage_limit', %s, 'open', now())",
            (f"logdiag:usage_limit|{machine}", machine))
    conn.commit()


def _seed_ats_job(conn, *, url, worker_id, status, apply_error, dedup_key):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, score, lane, status, "
            "apply_error, worker_id, dedup_key, attempts, updated_at) "
            "VALUES (%s, %s, 1.0, 'ats', %s, %s, %s, %s, 99, now())",
            (url, url, status, apply_error, worker_id, dedup_key))
    conn.commit()


def test_select_candidates_only_provably_never_submitted(fleet_db):
    """Only the provably-never-submitted bucket (status='failed', apply_error~usage_limit) is a
    candidate; every may-have-submitted park (reclaim crash_unconfirmed, no_result_line, timeout)
    on the SAME usage-limited worker is excluded -- so no re-queue can double-apply."""
    with pgqueue.connect(fleet_db) as conn:
        remediator.ensure_remediation_table(conn)
        _seed_usage_limit_diagnosis(conn, "m2-3")
        # DANGEROUS may-have-submitted parks (all carry a non-null dedup_key, so exclusion is
        # driven by status/apply_error, not by the dedup_key IS NOT NULL guard):
        _seed_ats_job(conn, url="u-reclaim", worker_id="m2-3", status="crash_unconfirmed",
                      apply_error="crash_unconfirmed", dedup_key="dk-reclaim")
        _seed_ats_job(conn, url="u-nores", worker_id="m2-3", status="crash_unconfirmed",
                      apply_error="failed:no_result_line", dedup_key="dk-nores")
        _seed_ats_job(conn, url="u-timeout", worker_id="m2-3", status="crash_unconfirmed",
                      apply_error="failed:timeout", dedup_key="dk-timeout")
        # PROVABLY never submitted (the only legitimate candidate):
        _seed_ats_job(conn, url="u-usage", worker_id="m2-3", status="failed",
                      apply_error="failed:usage_limit", dedup_key="dk-usage")
        cands = remediator.select_candidates(conn, window_minutes=30, max_per_job=2)
    urls = {c.url for c in cands}
    assert urls == {"u-usage"}        # exactly the provable bucket
    assert "u-reclaim" not in urls    # reclaim park excluded (the original gap)
    assert "u-nores" not in urls      # no_result_line is may-have-submitted -> excluded
    assert "u-timeout" not in urls    # timeout is may-have-submitted -> excluded


# ---------------------------------------------------------------------------
# Phase 2.4 / C12 -- (1) applied_set cleanup on re-queue, real Postgres.
#
# queue.write_apply_result seeds applied_set (keyed by dedup_key) whenever a job's status
# lands on 'applied' or 'crash_unconfirmed'. A job can cycle through crash_unconfirmed (seeding
# applied_set) on one lease and land on status='failed'+usage_limit on a LATER lease of the
# SAME dedup_key/url -- select_candidates only looks at the row's CURRENT status/apply_error, so
# it can legitimately select such a row. requeue_job flips it back to 'queued', but the lease
# path excludes any dedup_key present in applied_set (queue.py:71,422) -- so the row is
# unleasable until the stale applied_set row is cleared. Fix: requeue_job must delete the
# matching applied_set row in the SAME transaction as the status flip.
# ---------------------------------------------------------------------------

def _seed_applied_set(conn, *, dedup_key, company="Acme", applied_url="https://job/x"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO applied_set (dedup_key, company, applied_url) VALUES (%s, %s, %s)",
            (dedup_key, company, applied_url))
    conn.commit()


def _in_applied_set_real(conn, dedup_key) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM applied_set WHERE dedup_key=%s", (dedup_key,))
        return cur.fetchone() is not None


def test_requeue_job_deletes_matching_applied_set_row(fleet_db):
    """requeue_job must delete the candidate's OWN applied_set row (by dedup_key) in the same
    transaction as the status flip, so the row becomes leasable again (queue.py's lease query
    excludes any dedup_key present in applied_set)."""
    with pgqueue.connect(fleet_db) as conn:
        remediator.ensure_remediation_table(conn)
        _seed_ats_job(conn, url="u-stale", worker_id="m2-3", status="failed",
                      apply_error="failed:usage_limit", dedup_key="dk-stale")
        _seed_applied_set(conn, dedup_key="dk-stale")
        c = remediator.Candidate(url="u-stale", worker_id="m2-3", dedup_key="dk-stale",
                                 status="failed", attempts=1,
                                 apply_error="failed:usage_limit", reason="usage_limit")
        ok = remediator.requeue_job(conn, c)
        assert ok is True
        assert _in_applied_set_real(conn, "dk-stale") is False  # stale entry cleared
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM apply_queue WHERE url=%s", ("u-stale",))
            assert cur.fetchone()["status"] == "queued"


def test_requeue_job_does_not_touch_other_dedup_keys_in_applied_set(fleet_db):
    """A NON-requeued row's applied_set entry (different dedup_key) must be untouched."""
    with pgqueue.connect(fleet_db) as conn:
        remediator.ensure_remediation_table(conn)
        _seed_ats_job(conn, url="u-stale", worker_id="m2-3", status="failed",
                      apply_error="failed:usage_limit", dedup_key="dk-stale")
        _seed_applied_set(conn, dedup_key="dk-stale")
        _seed_applied_set(conn, dedup_key="dk-other", applied_url="https://job/other")
        c = remediator.Candidate(url="u-stale", worker_id="m2-3", dedup_key="dk-stale",
                                 status="failed", attempts=1,
                                 apply_error="failed:usage_limit", reason="usage_limit")
        remediator.requeue_job(conn, c)
        assert _in_applied_set_real(conn, "dk-other") is True  # untouched


# ---------------------------------------------------------------------------
# Phase 2.4 -- (2) --usage-limit-backfill status-keyed selection (no time window).
#
# C12: the live 59 usage-limit casualties are ~62h old; the windowed default (30 min, or even
# 720 min) selects nothing. These rows are DETERMINISTICALLY never-submitted regardless of age
# (the agent hit its quota wall before touching the page), so age is irrelevant to safety. Add a
# status-keyed query with NO time window, reusing the exact same 3-guard pipeline.
# ---------------------------------------------------------------------------

def test_select_backfill_candidates_finds_old_usage_limit_row(fleet_db):
    """A 3-day-old failed+usage_limit row (older than any sane window) must be selected by the
    backfill query, since it is provably never-submitted regardless of age."""
    with pgqueue.connect(fleet_db) as conn:
        remediator.ensure_remediation_table(conn)
        _seed_ats_job(conn, url="u-old", worker_id="m2-3", status="failed",
                      apply_error="failed:usage_limit", dedup_key="dk-old")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE apply_queue SET updated_at = now() - interval '3 days' WHERE url=%s",
                ("u-old",))
        conn.commit()
        # windowed mode (even a generous 720 min) must NOT find it -- this is the C12 gap.
        windowed = remediator.select_candidates(conn, window_minutes=720, max_per_job=2)
        backfill = remediator.select_backfill_candidates(conn, max_per_job=2)
    assert "u-old" not in {c.url for c in windowed}
    assert {c.url for c in backfill} == {"u-old"}


def test_backfill_excludes_crash_unconfirmed_and_no_result_line_even_with_usage_limit_text(fleet_db):
    """Guard the may-have-submitted invariant: backfill must NEVER select crash_unconfirmed or
    no_result_line rows, even if their apply_error text happens to contain 'usage_limit'."""
    with pgqueue.connect(fleet_db) as conn:
        remediator.ensure_remediation_table(conn)
        _seed_ats_job(conn, url="u-crash", worker_id="m2-3", status="crash_unconfirmed",
                      apply_error="crash_unconfirmed after usage_limit retry", dedup_key="dk-crash")
        _seed_ats_job(conn, url="u-nores", worker_id="m2-3", status="crash_unconfirmed",
                      apply_error="failed:no_result_line usage_limit", dedup_key="dk-nores")
        _seed_ats_job(conn, url="u-real", worker_id="m2-3", status="failed",
                      apply_error="failed:usage_limit", dedup_key="dk-real")
        for url in ("u-crash", "u-nores", "u-real"):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE apply_queue SET updated_at = now() - interval '3 days' WHERE url=%s",
                    (url,))
            conn.commit()
        backfill = remediator.select_backfill_candidates(conn, max_per_job=2)
    urls = {c.url for c in backfill}
    assert urls == {"u-real"}
    assert "u-crash" not in urls
    assert "u-nores" not in urls


def test_remediate_backfill_mode_requeues_and_clears_applied_set(fleet_db):
    """End-to-end, GENUINE candidate (no applied_set collision): remediate(..., backfill=True)
    selects the old usage_limit row through the backfill query, runs it through the same
    3-guard pipeline, and re-queues it. Confirms the backfill wiring end-to-end (selection ->
    guards -> requeue_job) without touching the guard-2-veto scenario (covered separately)."""
    with pgqueue.connect(fleet_db) as conn:
        remediator.ensure_remediation_table(conn)
        _seed_ats_job(conn, url="u-old2", worker_id="m2-3", status="failed",
                      apply_error="failed:usage_limit", dedup_key="dk-old2")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE apply_queue SET updated_at = now() - interval '3 days' WHERE url=%s",
                ("u-old2",))
        conn.commit()
        out = remediator.remediate(conn, brain_path="C:/nonexistent/brain.db",
                                   max_requeue=50, backfill=True)
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM apply_queue WHERE url=%s", ("u-old2",))
            status = cur.fetchone()["status"]
        leasable = not _in_applied_set_real(conn, "dk-old2")
    assert out["requeued"] == 1
    assert status == "queued"
    assert leasable is True


def test_remediate_backfill_clears_stale_applied_set_from_sibling_dedup_collision(fleet_db):
    """Realistic collision (C12 / Fix 1's actual trigger): dedup_key is computed from
    (company, title) -- see fleet/dedup.py -- so TWO DIFFERENT urls at the same company+title
    can share one dedup_key. If url A (a DIFFERENT posting, same company+title) already landed
    crash_unconfirmed and seeded applied_set for that shared dedup_key, url B's own
    status='failed'+usage_limit row is STILL a legitimate remediator candidate (B itself never
    touched the form) -- but guard 2 (in_applied_set) correctly vetoes it, because the posting
    (by dedup_key) genuinely may already carry the user's name via sibling A. This test locks
    that guard-2 veto still fires even under backfill (Fix 1 must never bypass guard 2)."""
    with pgqueue.connect(fleet_db) as conn:
        remediator.ensure_remediation_table(conn)
        _seed_ats_job(conn, url="u-siblingA", worker_id="m2-3", status="crash_unconfirmed",
                      apply_error="crash_unconfirmed", dedup_key="dk-shared")
        _seed_applied_set(conn, dedup_key="dk-shared")  # real prior write, from sibling A
        _seed_ats_job(conn, url="u-siblingB", worker_id="m2-3", status="failed",
                      apply_error="failed:usage_limit", dedup_key="dk-shared")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE apply_queue SET updated_at = now() - interval '3 days' WHERE url=%s",
                ("u-siblingB",))
        conn.commit()
        out = remediator.remediate(conn, brain_path="C:/nonexistent/brain.db",
                                   max_requeue=50, backfill=True)
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM apply_queue WHERE url=%s", ("u-siblingB",))
            status = cur.fetchone()["status"]
        sibling_a_preserved = _in_applied_set_real(conn, "dk-shared")
    assert out["requeued"] == 0
    assert out["vetoed_applied_set"] == 1
    assert status == "failed"                    # untouched -- correctly vetoed
    assert sibling_a_preserved is True            # sibling A's real entry preserved
