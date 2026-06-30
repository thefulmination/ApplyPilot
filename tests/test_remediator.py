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
