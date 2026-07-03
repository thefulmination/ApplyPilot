from __future__ import annotations

"""Re-audit stored email_events attributions against today's match guards.

`reaudit_email_events` replays `match_email_to_job` over rows already persisted
in `email_events` -- no Gmail calls. Any row whose stored attribution no longer
passes the temporal/ambiguity guards (Task 1-4) is flipped to needs_review with
a reversible `prev_job_url` audit trail. Rows that pass and are missing
`match_status` (legacy pre-Task-3 rows) are backfilled to 'attributed'.

Idempotent: a flipped row has job_url = NULL afterward, so it is skipped (not
re-checked) on the next run; a backfilled row has match_status set, so the
backfill UPDATE's `AND match_status IS NULL` guard no-ops.
"""

from typing import Any

from applypilot.gmail_outcomes import get_applied_jobs, match_email_to_job

# Deliberate extension beyond the brief's three named reasons: a stored row
# whose re-match now resolves to a DIFFERENT job (or to no job at all) isn't
# captured by predates_application/ambiguous_company/no_timestamp -- those are
# reasons match_email_to_job returns for a *fresh* (unattributed) match. Record
# that case with a dedicated reason so it isn't misreported as a guard the
# result never actually named.
REAUDIT_MISMATCH_REASON = "rematch_mismatch"


def reaudit_email_events(conn) -> dict[str, Any]:
    """Replay match guards over stored email_events rows. Returns:
    {"checked": N, "flipped": {reason: count}, "backfilled": B, "flipped_ids": [...]}
    """
    applied_jobs = get_applied_jobs(conn)

    rows = conn.execute(
        "SELECT message_id, job_url, occurred_at, sender, subject, body_text, match_status "
        "FROM email_events "
        "WHERE job_url IS NOT NULL AND (match_status IS NULL OR match_status != 'needs_review')"
    ).fetchall()

    checked = 0
    flipped: dict[str, int] = {}
    flipped_ids: list[str] = []
    backfilled = 0

    for row in rows:
        checked += 1
        stored_url = row["job_url"]

        result = match_email_to_job(
            row["sender"] or "",
            row["subject"] or "",
            row["body_text"] or "",
            applied_jobs,
            occurred_at=row["occurred_at"],
        )

        if result.status == "needs_review":
            reason = result.reason
        elif result.status == "unmatched" or (
            result.status == "attributed"
            and (result.job or {}).get("url") != stored_url
        ):
            reason = REAUDIT_MISMATCH_REASON
        else:
            reason = None

        if reason is not None:
            conn.execute(
                "UPDATE email_events SET prev_job_url = job_url, job_url = NULL, "
                "match_status = 'needs_review', match_reason = ? WHERE message_id = ?",
                (reason, row["message_id"]),
            )
            flipped[reason] = flipped.get(reason, 0) + 1
            flipped_ids.append(row["message_id"])
        else:
            cur = conn.execute(
                "UPDATE email_events SET match_status = 'attributed' "
                "WHERE message_id = ? AND match_status IS NULL",
                (row["message_id"],),
            )
            if cur.rowcount:
                backfilled += cur.rowcount

    conn.commit()

    return {
        "checked": checked,
        "flipped": flipped,
        "backfilled": backfilled,
        "flipped_ids": flipped_ids,
    }
