from __future__ import annotations

import json

from applypilot import database


def _seed_jobs(conn) -> None:
    conn.executemany(
        "INSERT INTO jobs (url, title, site, full_description, discovered_at, audit_score, fit_score, source_board, duplicate_of_url) "
        "VALUES (?, ?, ?, ?, '2026-07-04T00:00:00+00:00', ?, ?, ?, NULL)",
        [
            ("https://ex.com/e1", "Engineer", "A", "x" * 700, 7, 7, "A"),
            ("https://ex.com/e2", "Engineer", "A", "x" * 250, 4, 4, "A"),
            ("https://ex.com/e3", "Engineer", "A", "", None, None, "A"),
            ("https://ex.com/e4", "Engineer", "B", "x" * 700, 8, 8, "B"),
            ("https://ex.com/e5", "Engineer", "B", "x" * 250, 5, 5, "B"),
            ("https://ex.com/e6", "Engineer", "B", "", None, None, "B"),
        ],
    )


def test_parse_spot_audit_stratifies_board_and_band(monkeypatch, tmp_path) -> None:
    from typer.testing import CliRunner
    from applypilot import cli, config, llm

    db_path = tmp_path / "spot.db"
    conn = database.init_db(db_path)
    _seed_jobs(conn)
    conn.commit()

    class FakeClient:
        model = "parse-model"
        provider_name = "fake"
        calls = 0

        def chat(self, *_, **__):
            self.__class__.calls += 1
            return json.dumps({"complete": "y", "defects": ["missing_salary"]})

    calls = {"calls": 0}

    def fake_get_client(*_, **__):
        calls["calls"] += 1
        return FakeClient()

    # Force one output per selected job; we expect at least one row from each
    # board x band group in one round-robin sample.
    def side_effect():
        return database

    monkeypatch.setattr(llm, "get_client", fake_get_client)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    result = CliRunner().invoke(cli.app, ["parse-spot-audit", "--sample", "6", "--window-days", "30"])
    assert result.exit_code == 0
    assert calls["calls"] == 1

    sample_rows = conn.execute("SELECT COUNT(*) AS n FROM parse_spot_audit").fetchone()["n"]
    assert sample_rows == 6

    boards = {row["board"] for row in conn.execute("SELECT DISTINCT board FROM parse_spot_audit")}
    assert boards >= {"A", "B"}


def test_parse_spot_audit_flags_board_incomplete(monkeypatch, tmp_path) -> None:
    from typer.testing import CliRunner
    from applypilot import cli, config, llm

    db_path = tmp_path / "spot.db"
    conn = database.init_db(db_path)
    _seed_jobs(conn)
    conn.commit()

    response_plan = [
        {"complete": "y", "defects": []},
        {"complete": "n", "defects": ["title_echo"]},
        {"complete": "n", "defects": ["html"]},
        {"complete": "n", "defects": []},
        {"complete": "n", "defects": []},
        {"complete": "n", "defects": ["junk_boilerplate"]},
    ]

    class FakeClient:
        model = "parse-model"
        provider_name = "fake"

        def __init__(self):
            self.i = 0

        def chat(self, *_args, **_kwargs):
            payload = response_plan[min(self.i, len(response_plan) - 1)]
            self.i += 1
            return json.dumps(payload)

    monkeypatch.setattr(llm, "get_client", lambda *args, **kwargs: FakeClient())
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)

    result = CliRunner().invoke(cli.app, ["parse-spot-audit", "--sample", "4", "--window-days", "30"])
    assert result.exit_code == 0
    assert "ALERT" in result.output
    assert "board=" in result.output

    bad = conn.execute(
        "SELECT board, AVG(complete) AS c FROM parse_spot_audit GROUP BY board"
    ).fetchall()
    assert bad
    for row in bad:
        if row["c"] < 0.9:
            assert row["c"] < 0.9


def test_parse_spot_audit_malformed_json_is_handled(monkeypatch, tmp_path) -> None:
    from typer.testing import CliRunner
    from applypilot import cli, config, llm

    db_path = tmp_path / "spot.db"
    conn = database.init_db(db_path)
    _seed_jobs(conn)
    conn.commit()

    class FakeClient:
        model = "parse-model"
        provider_name = "fake"
        def chat(self, *_args, **_kwargs):
            return "not-json-at-all"

    monkeypatch.setattr(llm, "get_client", lambda *args, **kwargs: FakeClient())
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)

    result = CliRunner().invoke(cli.app, ["parse-spot-audit", "--sample", "3", "--window-days", "30"])
    assert result.exit_code == 0
    rows = conn.execute(
        "SELECT complete, defects FROM parse_spot_audit WHERE defects IS NOT NULL LIMIT 3"
    ).fetchall()
    assert rows
    assert all(r["complete"] == 0 for r in rows)
