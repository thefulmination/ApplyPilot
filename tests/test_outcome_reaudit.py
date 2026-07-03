from __future__ import annotations


def _brain(tmp_path):
    from applypilot import database
    conn = database.init_db(tmp_path / "brain.db")
    conn.execute("INSERT INTO jobs (url, title, site, company, apply_status, applied_at) VALUES (?,?,?,?,?,?)",
                 ("https://boards.greenhouse.io/checkr/jobs/1", "Analyst", "Checkr", "Checkr",
                  "applied", "2026-06-28T12:00:00+00:00"))
    conn.commit()
    return conn


def _event(conn, message_id, job_url, occurred_at, subject="Your application to Checkr"):
    conn.execute(
        "INSERT INTO email_events (message_id, job_url, occurred_at, sender, subject, stage, body_text, scanned_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (message_id, job_url, occurred_at, "no-reply@us.greenhouse-mail.io", subject,
         "applied_confirmation", "Thank you for applying to Checkr.", "2026-07-01T00:00:00+00:00"))
    conn.commit()


def test_reaudit_flips_predating_attribution_and_is_idempotent(tmp_path):
    from applypilot.outcome_reaudit import reaudit_email_events
    conn = _brain(tmp_path)
    _event(conn, "bad1", "https://boards.greenhouse.io/checkr/jobs/1", "2026-06-20T12:00:00+00:00")
    _event(conn, "good1", "https://boards.greenhouse.io/checkr/jobs/1", "2026-06-29T12:00:00+00:00")

    r = reaudit_email_events(conn)
    assert r["flipped"]["predates_application"] == 1 and "bad1" in r["flipped_ids"]
    assert r["backfilled"] >= 1
    bad = conn.execute("SELECT job_url, prev_job_url, match_status, match_reason FROM email_events WHERE message_id='bad1'").fetchone()
    assert bad["job_url"] is None and bad["prev_job_url"] == "https://boards.greenhouse.io/checkr/jobs/1"
    assert bad["match_status"] == "needs_review" and bad["match_reason"] == "predates_application"
    good = conn.execute("SELECT match_status FROM email_events WHERE message_id='good1'").fetchone()
    assert good["match_status"] == "attributed"

    r2 = reaudit_email_events(conn)
    assert sum(r2["flipped"].values()) == 0   # idempotent


def test_reaudit_empty_brain_ok(tmp_path):
    from applypilot.outcome_reaudit import reaudit_email_events
    conn = _brain(tmp_path)
    r = reaudit_email_events(conn)
    assert r["checked"] == 0


def test_reaudit_rematch_mismatch_flips_with_dedicated_reason(tmp_path):
    """A stored attribution to job A, where re-matching today resolves to a
    DIFFERENT job (or no job), is not any of the three named guard reasons --
    it gets the deliberate extension reason 'rematch_mismatch'."""
    from applypilot.outcome_reaudit import reaudit_email_events
    conn = _brain(tmp_path)
    # Insert a second applied job so the stored row can plausibly point at the
    # "wrong" one relative to what a fresh match would pick.
    conn.execute("INSERT INTO jobs (url, title, site, company, apply_status, applied_at) VALUES (?,?,?,?,?,?)",
                 ("https://boards.greenhouse.io/checkr/jobs/2", "Analyst II", "Checkr", "Checkr",
                  "applied", "2026-06-20T12:00:00+00:00"))
    conn.commit()
    # Stored attribution points at job 1, but occurred_at/content will re-match
    # (via ats_domain + company hint) onto the same company with 2 eligible
    # jobs and no distinguishing title token -> ambiguous_company, still a
    # flip, just via the needs_review path (covers the "different job" family
    # more directly with an unmatched-sender case below).
    _event(conn, "orphan1", "https://boards.greenhouse.io/checkr/jobs/1", "2026-06-29T12:00:00+00:00",
           subject="This has nothing to do with any employer")
    conn.execute(
        "UPDATE email_events SET sender = ? WHERE message_id = ?",
        ("someone@example.com", "orphan1"),
    )
    conn.execute(
        "UPDATE email_events SET body_text = ? WHERE message_id = ?",
        ("no company signal here at all", "orphan1"),
    )
    conn.commit()

    r = reaudit_email_events(conn)
    assert r["flipped"].get("rematch_mismatch") == 1
    assert "orphan1" in r["flipped_ids"]
    row = conn.execute(
        "SELECT job_url, prev_job_url, match_status, match_reason FROM email_events WHERE message_id='orphan1'"
    ).fetchone()
    assert row["job_url"] is None
    assert row["prev_job_url"] == "https://boards.greenhouse.io/checkr/jobs/1"
    assert row["match_status"] == "needs_review"
    assert row["match_reason"] == "rematch_mismatch"
