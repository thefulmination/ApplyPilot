from __future__ import annotations

import re

from applypilot import database
from applypilot.discovery import desc_quality


def test_empty_description_flags_and_score() -> None:
    flags, score = desc_quality.assess_description("Engineer", "")

    assert "empty" in flags
    assert "stub_lt200" in flags
    assert "short_lt500" in flags
    assert score == 0


def test_stub_below_200_is_flagged_and_penalized() -> None:
    flags, score = desc_quality.assess_description("Engineer", "x" * 199)

    assert "stub_lt200" in flags
    assert "short_lt500" in flags
    assert score < 100
    assert score >= 0


def test_short_under_500_flagged_but_not_stub() -> None:
    title = "Staff Engineer"
    description = re.sub(r"\s", " ", "Requirements: build software. Qualifications: Python.")
    description = (description + " x") * 8
    flags, _ = desc_quality.assess_description(title, description)

    assert "stub_lt200" not in flags
    assert "short_lt500" in flags


def test_missing_requirements_marker() -> None:
    flags, _ = desc_quality.assess_description(
        "Staff Engineer",
        "You will collaborate with teams to deliver critical workflows on schedule.",
    )
    assert "no_requirements_marker" in flags


def test_html_residue_flag() -> None:
    text = "<p>Requirements:</p><p>" + ("x " * 300) + "</p>" + "<ul><li>One</li><li>Two</li></ul>" * 4
    flags, _ = desc_quality.assess_description("Engineer", text)

    assert "html_residue" in flags


def test_junk_boilerplate_flag() -> None:
    flags, _ = desc_quality.assess_description(
        "Engineer",
        "This job requires a cookie and click here to enable javascript and sign in for full details.",
    )
    assert "junk_boilerplate" in flags


def test_board_summary_stub_flag() -> None:
    summary = "Requirements Summary:\n" + ("A" * 20) * 5
    flags, _ = desc_quality.assess_description("Engineer", summary)

    assert "board_summary_stub" in flags


def test_title_echo_flag() -> None:
    title = "Lead QA Engineer"
    flags, _ = desc_quality.assess_description(title, title)
    assert "title_echo" in flags


def test_refresh_desc_quality_only_updates_outdated_rows(tmp_path) -> None:
    conn = database.init_db(tmp_path / "desc-refresh.db")
    conn.execute(
        "INSERT INTO jobs (url, title, site, full_description, discovered_at, duplicate_of_url) "
        "VALUES ('https://example.com/1', 'Engineer', 'Example', 'x' * 600, ?, NULL)",
        ("2026-07-01T00:00:00+00:00",),
    )
    conn.commit()

    first = database.refresh_desc_quality(conn, limit=None)
    assert first["scanned"] == 1
    assert first["updated"] == 1

    row1 = conn.execute(
        "SELECT desc_quality_flags, desc_quality_score, desc_quality_at FROM jobs WHERE url='https://example.com/1'"
    ).fetchone()
    assert row1["desc_quality_at"] is not None
    assert row1["desc_quality_score"] is not None

    second = database.refresh_desc_quality(conn, limit=None)
    assert second["scanned"] == 0
    assert second["updated"] == 0

    # if the scrape result refreshes later, it should be recomputed again.
    conn.execute(
        "UPDATE jobs SET detail_scraped_at='2026-07-04T00:00:00+00:00', "
        "full_description='x' * 800 WHERE url='https://example.com/1'"
    )
    conn.commit()
    third = database.refresh_desc_quality(conn, limit=None)
    assert third["scanned"] == 1
    assert third["updated"] == 1


def test_snapshot_desc_quality_includes_all_board(tmp_path) -> None:
    conn = database.init_db(tmp_path / "desc-snapshot.db")
    conn.execute(
        "INSERT INTO jobs (url, title, site, full_description, discovered_at, duplicate_of_url) "
        "VALUES ('https://example.com/1', 'Engineer', 'A', 'x' * 600, '2026-07-01T00:00:00+00:00', NULL),"
        "('https://example.com/2', 'Engineer', 'A', '', '2026-07-01T00:00:00+00:00', NULL),"
        "('https://example.com/3', 'Manager', 'B', '<p>Requirements:</p>' || 'x', '2026-07-01T00:00:00+00:00', NULL)"
    )
    conn.commit()

    database.refresh_desc_quality(conn)
    snapshot = database.snapshot_desc_quality(conn, window_days=30)
    assert len(snapshot["rows"]) >= 3

    by_board = {row["board"]: row for row in snapshot["rows"]}
    assert "A" in by_board and "B" in by_board and "__all__" in by_board
    assert by_board["__all__"]["total"] == 3
    assert by_board["A"]["null_rate"] == 1 / 2
    assert by_board["A"]["short_rate"] > 0
    assert by_board["A"]["board_summary_rate"] >= 0


def test_refresh_then_parse_health_thresholds(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from applypilot import cli, config

    db_path = tmp_path / "parse-health.db"
    conn = database.init_db(db_path)

    # 3 empty rows -> 100% null
    rows = [
        ("https://example.com/a", "A", "Engineer", ""),
        ("https://example.com/b", "A", "Engineer", ""),
        ("https://example.com/c", "A", "Engineer", ""),
    ]
    # 100 short rows to trip short/html thresholds
    rows += [
        (f"https://example.com/{i}", "A", "Engineer", "x" * 200)
        for i in range(3, 104)
    ]
    conn.executemany(
        "INSERT INTO jobs (url, title, site, full_description, discovered_at, duplicate_of_url) "
        "VALUES (?, ?, ?, ?, '2026-07-04T00:00:00+00:00', NULL)",
        rows,
    )
    conn.commit()

    database.refresh_desc_quality(conn)

    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)

    result = CliRunner().invoke(cli.app, ["parse-health"])
    assert result.exit_code == 0
    assert "ALERT" in result.output
    assert "null_rate" in result.output
    assert "short_rate" in result.output
    assert "HTML" in result.output
