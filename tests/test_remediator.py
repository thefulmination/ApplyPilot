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
             "status": "crash_unconfirmed", "attempts": 99,
             "apply_error": "crash_unconfirmed", "reason": "usage_limit"}]
    conn = _FakeConn({"FROM apply_queue": rows})
    cands = remediator.select_candidates(conn, window_minutes=30, max_per_job=2)
    assert len(cands) == 1
    assert cands[0].url == "https://job/1" and cands[0].reason == "usage_limit"
    # the query must scope to ATS lane, usage_limit diagnoses, and the per-job cap
    sql = conn._cur.executed[0][0]
    assert "lane = 'ats'" in sql and "usage_limit" in sql and "remediation_actions" in sql


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
