"""Conservative re-audit of historical fleet outcomes recorded as applied."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import base64
import hashlib
from pathlib import Path
import sqlite3
import subprocess
from typing import Callable

from applypilot.apply.launcher import _parse_terminal_result

TranscriptLoader = Callable[[str], str]

_APPLICATION_EVIDENCE_STAGES = (
    "applied_confirmation",
    "acknowledged",
    "rejected",
    "screen",
    "assessment",
    "interview",
    "position_filled",
)


def _read_transcript(path: str) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _read_remote_transcript(host: str, path: str) -> str:
    escaped_path = path.replace("'", "''")
    script = f"[Convert]::ToBase64String([IO.File]::ReadAllBytes('{escaped_path}'))"
    encoded_command = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        [
            "ssh",
            host,
            "powershell",
            "-NoProfile",
            "-EncodedCommand",
            encoded_command,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or f"ssh exited {result.returncode}")
    try:
        text = base64.b64decode(result.stdout.strip()).decode("utf-8", errors="replace")
        return text.replace("\r\n", "\n").replace("\r", "\n")
    except (ValueError, UnicodeError) as exc:
        raise OSError(f"invalid base64 transcript from {host}: {exc}") from exc


def _confirmed_urls(home_db_path: str | None) -> set[str]:
    if not home_db_path or not Path(home_db_path).is_file():
        return set()
    conn = sqlite3.connect(home_db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT job_url FROM email_events "
            f"WHERE job_url IS NOT NULL AND stage IN ({','.join('?' for _ in _APPLICATION_EVIDENCE_STAGES)}) "
            "AND COALESCE(match_status, 'attributed') = 'attributed'",
            _APPLICATION_EVIDENCE_STAGES,
        ).fetchall()
        return {str(row[0]) for row in rows}
    except sqlite3.DatabaseError:
        return set()
    finally:
        conn.close()


def _candidate_events(
    conn,
    event_ids: list[int] | None = None,
    home_ips: list[str] | None = None,
) -> list[dict]:
    event_filter = " AND e.id = ANY(%s)" if event_ids else ""
    home_filter = " AND e.home_ip = ANY(%s)" if home_ips else ""
    params = tuple(value for value in (event_ids, home_ips) if value)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT e.id, e.queue_name, e.url, e.worker_id, e.status, e.apply_status, "
            "e.apply_error, e.target_host, e.home_ip, e.agent, e.agent_model, "
            "e.est_cost_usd, e.apply_duration_ms, e.application_tool_calls, "
            "e.job_log_path, e.transcript_digest, e.final_result_source, e.created_at "
            "FROM apply_result_events e "
            "WHERE e.source = 'worker' AND (e.status = 'applied' OR e.apply_status = 'applied') "
            + event_filter + home_filter +
            "AND NOT EXISTS ("
            "  SELECT 1 FROM apply_result_events correction "
            "  WHERE correction.source = 'historical_reaudit:' || e.id::text"
            ") ORDER BY e.id",
            params,
        )
        return [dict(row) for row in cur.fetchall()]


def _record_correction(conn, event: dict, parsed_status: str) -> bool:
    queue_name = event["queue_name"] or "apply_queue"
    if queue_name != "apply_queue":
        return False

    reason = f"historical_reaudit:{parsed_status}"
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE apply_queue SET status='crash_unconfirmed', "
            "apply_status='crash_unconfirmed', apply_error=%s, applied_at=NULL, updated_at=now() "
            "WHERE url=%s AND status='applied'",
            (reason, event["url"]),
        )
        if cur.rowcount != 1:
            return False
        cur.execute(
            "INSERT INTO apply_result_events ("
            "queue_name, url, worker_id, status, apply_status, apply_error, target_host, "
            "home_ip, agent, agent_model, est_cost_usd, apply_duration_ms, "
            "application_tool_calls, job_log_path, transcript_digest, final_result_source, "
            "result_line, source) "
            "VALUES (%s,%s,%s,'crash_unconfirmed','crash_unconfirmed',%s,%s,%s,%s,%s,"
            "%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                queue_name,
                event["url"],
                event["worker_id"],
                reason,
                event["target_host"],
                event["home_ip"],
                event["agent"],
                event["agent_model"],
                event["est_cost_usd"],
                event["apply_duration_ms"],
                event["application_tool_calls"],
                event["job_log_path"],
                event["transcript_digest"],
                "historical_reaudit",
                f"RESULT:{parsed_status}",
                f"historical_reaudit:{event['id']}",
            ),
        )
    return True


def reaudit_applied_outcomes(
    conn,
    *,
    home_db_path: str | None = None,
    transcript_loader: TranscriptLoader | None = None,
    remote_hosts: dict[str, str] | None = None,
    event_ids: list[int] | None = None,
    home_ips: list[str] | None = None,
    apply: bool = False,
) -> dict:
    """Re-evaluate recorded applied events from logs and attributed email receipts.

    Dry-run is the default. Mutation is limited to direct log contradictions without
    positive email evidence. Corrected rows remain in ``applied_set`` and become
    ``crash_unconfirmed``, preventing an automatic duplicate submission.
    """
    loader = transcript_loader or _read_transcript
    remote_hosts = remote_hosts or {}
    email_confirmed = _confirmed_urls(home_db_path)
    counts: Counter[str] = Counter()
    rows: list[dict] = []
    events = _candidate_events(conn, event_ids=event_ids, home_ips=home_ips)
    remote_results: dict[int, str | Exception] = {}

    remote_events = [
        event for event in events
        if transcript_loader is None
        and event["job_log_path"]
        and (event["home_ip"] or "") in remote_hosts
    ]
    if remote_events:
        with ThreadPoolExecutor(max_workers=min(8, len(remote_events))) as pool:
            futures = {
                pool.submit(
                    _read_remote_transcript,
                    remote_hosts[event["home_ip"]],
                    event["job_log_path"],
                ): event["id"]
                for event in remote_events
            }
            for future in as_completed(futures):
                event_id = futures[future]
                try:
                    remote_results[event_id] = future.result()
                except Exception as exc:
                    remote_results[event_id] = exc

    for event in events:
        parsed_status = None
        log_error = None
        if event["job_log_path"]:
            try:
                remote_host = remote_hosts.get(event["home_ip"] or "")
                remote_result = remote_results.get(event["id"])
                if isinstance(remote_result, Exception):
                    raise OSError(str(remote_result))
                transcript = remote_result if remote_host and transcript_loader is None else loader(event["job_log_path"])
                actual_digest = "sha256:" + hashlib.sha256(
                    transcript.encode("utf-8", errors="replace")
                ).hexdigest()
                if event["transcript_digest"] and actual_digest != event["transcript_digest"]:
                    log_error = "transcript_digest_mismatch"
                else:
                    parsed_status = _parse_terminal_result(transcript)
            except (OSError, KeyError, RuntimeError) as exc:
                log_error = f"{type(exc).__name__}: {exc}"

        has_email = event["url"] in email_confirmed
        evidence_conflict = has_email and parsed_status not in (None, "applied")
        if has_email:
            classification = "verified_email"
        elif parsed_status == "applied":
            classification = "verified_log"
        elif parsed_status is not None:
            classification = "correction_candidate"
        else:
            classification = "review_missing_evidence"

        row = {
            "event_id": event["id"],
            "url": event["url"],
            "worker_id": event["worker_id"],
            "recorded_status": event["status"] or event["apply_status"],
            "parsed_status": parsed_status,
            "email_confirmation": has_email,
            "evidence_conflict": evidence_conflict,
            "classification": classification,
            "job_log_path": event["job_log_path"],
            "log_error": log_error,
            "corrected": False,
        }
        counts[classification] += 1

        if apply and classification == "correction_candidate":
            row["corrected"] = _record_correction(conn, event, parsed_status)
            if row["corrected"]:
                counts["corrected"] += 1
            else:
                counts["mutation_skipped"] += 1
        rows.append(row)

    if apply:
        conn.commit()
    else:
        conn.rollback()
    return {
        "dry_run": not apply,
        "checked": len(rows),
        "counts": dict(sorted(counts.items())),
        "rows": rows,
    }


def format_text(report: dict) -> str:
    mode = "DRY RUN" if report["dry_run"] else "APPLIED"
    lines = [f"Historical applied-outcome re-audit ({mode})", f"Checked: {report['checked']}"]
    lines.extend(f"{name}: {count}" for name, count in report["counts"].items())
    for row in report["rows"]:
        lines.append(
            f"[{row['classification']}] event={row['event_id']} "
            f"parsed={row['parsed_status'] or 'none'} url={row['url']}"
        )
    return "\n".join(lines)
