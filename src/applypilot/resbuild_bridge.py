"""Bridge: promote res_build's curated apply-list into ApplyPilot's apply gate.

res_build (the TypeScript review tree) exports the jobs the user reviewed and
KEPT as an apply-list JSONL (one approved job per ``url``, via
``src/cli/applypilotExportApplyList.ts``). This module ingests a SCOPED,
REVERSIBLE subset of that list and makes it authoritative for the apply gate.
It reuses ``import_decisions``' write path (``audit_score`` + ``decision_source``,
which the gate prefers via ``COALESCE(audit_score, fit_score)``) but adds the
safety rails the raw importer lacks:

  * host exclusion   -- keep the LinkedIn catastrophe lane OUT by default
  * applyable-only   -- skip rows already applied or marked ``duplicate_of_url``
  * limit            -- promote the top-N by the user's own score (smallest safe
                        canary batch first)
  * gate floor       -- the gate write is max(res score, prior effective score,
                        apply threshold): approval always clears the gate and never
                        demotes a row the production ranker already had eligible.
                        The raw res score is kept in ``external_decision_score``.
  * snapshot+revert  -- capture each touched row's prior state BEFORE the write
                        so the whole promotion is one-command reversible
  * dry-run          -- preview (how many are promotable, and how many the bridge
                        UNLOCKS -- currently below the apply threshold) with NO writes

Promotion only STAGES jobs as apply-eligible in the brain; nothing applies until
the fleet is run, so staging is safe.
"""
from __future__ import annotations

import json
import tempfile
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

from applypilot import config
from applypilot import import_decisions as _imp
from applypilot.database import get_connection, init_db
from applypilot.apply import pgqueue

DEFAULT_EXCLUDE_HOSTS = ("linkedin.com",)
DEFAULT_SOURCE = "res_build"

# Columns import_decisions overwrites; snapshot/restore EXACTLY these so a revert
# is a faithful undo (mirrors _DECISION_UPDATE in import_decisions.py).
_SNAPSHOT_COLS = (
    "audit_score", "audit_label", "audit_reason", "audited_at",
    "decision_source", "decision_verdict", "external_decision_score",
    "decision_at", "application_url",
)


def _host(url: str) -> str:
    # .hostname is lowercased + port-stripped; rstrip a trailing FQDN dot so the
    # 'linkedin.com.' form cannot slip past the exclusion (catastrophe-lane invariant).
    try:
        return (urlsplit(url).hostname or "").rstrip(".").removeprefix("www.")
    except Exception:
        return ""


def _excluded(url: str, exclude_hosts: Iterable[str]) -> bool:
    h = _host(url)
    return any(h == e or h.endswith("." + e) for e in exclude_hosts)


def _approved_url_records(path: Path) -> list[tuple[str, dict]]:
    """(url, record) for every APPROVED record carrying a url. Reuses
    import_decisions' verdict + field parsing so the bridge and the importer
    agree on what 'approved' means."""
    out: list[tuple[str, dict]] = []
    for rec in _imp._load_records(path):
        approved, _ = _imp._is_approved(rec)
        if not approved:
            continue
        url = _imp._field(rec, "url", "jobUrl", "job_url", "sourceUrl")
        if not url:
            continue
        out.append((str(url).strip(), rec))
    return out


def _score_of(rec: dict) -> float:
    v = _imp._field(rec, "decision_score", "decisionScore", "score", "fitScore")
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("-inf")


def _existing(conn, urls: list[str]) -> dict:
    """url -> brain row (the columns we filter and snapshot on), chunked for SQLite's
    variable limit."""
    out: dict = {}
    cols = (
        "url, applied_at, duplicate_of_url, fit_score, apply_status, apply_error, "
        + ", ".join(_SNAPSHOT_COLS)
    )
    CHUNK = 400
    for i in range(0, len(urls), CHUNK):
        chunk = urls[i:i + CHUNK]
        qmarks = ",".join("?" * len(chunk))
        for row in conn.execute(f"SELECT {cols} FROM jobs WHERE url IN ({qmarks})", chunk):
            out[row["url"]] = row
    return out


def _eff(row) -> float | None:
    """Effective rank the apply gate sees: audit_score wins, else fit_score."""
    return row["audit_score"] if row["audit_score"] is not None else row["fit_score"]


def _fleet_cross_checked_urls(pg_conn, urls: list[str]) -> set[str]:
    if not urls:
        return set()

    urls = sorted(set(urls))
    out: set[str] = set()
    chunk = 300
    with pg_conn.cursor() as cur:
        for i in range(0, len(urls), chunk):
            batch = urls[i:i + chunk]
            cur.execute(
                """
                SELECT url
                FROM apply_queue
                WHERE status IN ('leased', 'applied', 'crash_unconfirmed')
                  AND url = ANY(%s)
                """,
                (batch,),
            )
            out.update(r["url"] for r in cur.fetchall())
            cur.execute(
                """
                SELECT url
                FROM linkedin_queue
                WHERE status IN ('leased', 'applied', 'crash_unconfirmed')
                  AND url = ANY(%s)
                """,
                (batch,),
            )
            out.update(r["url"] for r in cur.fetchall())
    return out


def _filter(conn, recs, *, exclude_hosts, only_applyable, limit, fleet_applied_urls: set[str] | None = None):
    kept = [(u, r) for (u, r) in recs if not _excluded(u, exclude_hosts)]
    excluded_fleet_applied = 0
    if only_applyable and kept:
        rows = _existing(conn, [u for (u, _) in kept])
        applyable_urls: set[str] = set()
        excluded = fleet_applied_urls or set()
        for u, row in rows.items():
            if row["applied_at"] is not None:
                continue
            if row["duplicate_of_url"] is not None:
                continue
            status = (row["apply_status"] or "").strip().lower()
            if status in {"applied", "in_progress", "crash_unconfirmed"}:
                continue
            error = (row["apply_error"] or "").strip().lower()
            if error in {"no_confirmation", "crash_unconfirmed"}:
                continue
            if u in excluded:
                excluded_fleet_applied += 1
                continue
            applyable_urls.add(u)
        kept = [(u, r) for (u, r) in kept if u in applyable_urls]
    kept.sort(key=lambda ur: _score_of(ur[1]), reverse=True)   # the user's own score, best first
    if limit is not None:
        kept = kept[:limit]
    return kept, excluded_fleet_applied


def promote(path, *, source: str = DEFAULT_SOURCE, scale: str = "ten",
            exclude_hosts: Iterable[str] = DEFAULT_EXCLUDE_HOSTS,
            only_applyable: bool = True, limit: int | None = None,
            snapshot_path=None, dry_run: bool = False) -> dict:
    """Promote a scoped subset of the apply-list into the apply gate.

    Returns a counts report. With ``dry_run`` nothing is written (and no snapshot
    is taken). Otherwise a snapshot is written FIRST (if ``snapshot_path`` given),
    then the subset is imported via import_decisions.
    """
    init_db()
    conn = get_connection()
    path = Path(path)
    recs = _approved_url_records(path)
    fleet_cross_check = "skipped_no_dsn"
    fleet_applied_urls: set[str] = set()
    if os.environ.get("FLEET_PG_DSN"):
        try:
            pg_conn = pgqueue.connect()
            try:
                kept_urls_hint = [u for (u, _r) in recs if not _excluded(u, exclude_hosts)]
                existing_hint = _existing(conn, kept_urls_hint)
                if existing_hint:
                    fleet_applied_urls = _fleet_cross_checked_urls(pg_conn, list(existing_hint))
            finally:
                pg_conn.close()
            fleet_cross_check = "ok"
        except Exception:
            raise

    kept, excluded_fleet_applied = _filter(
        conn,
        recs,
        exclude_hosts=exclude_hosts,
        only_applyable=only_applyable,
        limit=limit,
        fleet_applied_urls=fleet_applied_urls,
    )
    kept_urls = [u for (u, _) in kept]
    threshold = config.get_min_score()

    existing = _existing(conn, kept_urls)
    would_raise = sum(
        1 for u in kept_urls
        if u in existing and (_eff(existing[u]) is None or _eff(existing[u]) < threshold)
    )

    report = {
        "source": source,
        "input_records": len(recs),
        "after_filter": len(kept),
        "excluded_hosts": list(exclude_hosts),
        "apply_threshold": threshold,
        "would_raise": would_raise,
        "excluded_fleet_applied": excluded_fleet_applied,
        "fleet_cross_check": fleet_cross_check,
        "dry_run": dry_run,
    }
    if dry_run:
        report["would_promote"] = len(kept)
        report["sample"] = kept_urls[:10]
        return report

    # Snapshot BEFORE the write so revert is a faithful undo.
    snap_rows = [{"url": u, **{c: existing[u][c] for c in _SNAPSHOT_COLS}}
                 for u in kept_urls if u in existing]
    if snapshot_path is not None:
        snapshot_path = Path(snapshot_path)
        snapshot_path.write_text(json.dumps({
            "source": source,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "rows": snap_rows,
        }, indent=2), encoding="utf-8")

    # Reuse import_decisions for the actual UPDATE (DRY): write the filtered subset
    # to a temp jsonl tagged with our source, then import it. The human APPROVAL is
    # the decision; the score is only a rank. So the gate write is floored at
    # max(res score, prior effective score, apply threshold): an approved job always
    # clears the gate and is never DEMOTED below where the production ranker had it.
    # The raw res score is preserved in external_decision_score as the benchmark.
    fd, tmpname = tempfile.mkstemp(prefix="resbuild_promote_", suffix=".jsonl")
    os.close(fd)
    tmp = Path(tmpname)
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            for u, r in kept:
                out = dict(r)
                out["url"] = u
                out["source"] = source
                raw = _imp._rescale_score(
                    _imp._field(r, "decision_score", "decisionScore", "score", "fitScore"),
                    scale,
                )
                if raw is not None:
                    prior = _eff(existing[u]) if u in existing else None
                    floors = [raw, float(threshold)]
                    if prior is not None:
                        floors.append(float(prior))
                    out["decision_score"] = raw          # ten band; benchmark score
                    out["gate_score"] = max(floors)      # ten band; what the gate sees
                fh.write(json.dumps(out) + "\n")
        # decision_score/gate_score were pre-rescaled to the ten band above.
        counts = _imp.import_decisions(tmp, scale="ten", default_source=source)
    finally:
        tmp.unlink(missing_ok=True)

    report["promoted"] = counts["updated"] + counts["inserted"]
    report["import_counts"] = counts
    report["snapshot_path"] = str(snapshot_path) if snapshot_path is not None else None
    return report


def revert(snapshot_path, *, source: str = DEFAULT_SOURCE) -> int:
    """Restore each snapshotted row to its pre-promotion state -- but ONLY rows that
    still bear our ``decision_source`` tag (never clobber a row re-decided since).
    Returns the number of rows reverted."""
    init_db()
    conn = get_connection()
    data = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    src = data.get("source", source)
    set_clause = ", ".join(f"{c}=?" for c in _SNAPSHOT_COLS)
    n = 0
    for row in data.get("rows", []):
        params = [row.get(c) for c in _SNAPSHOT_COLS] + [row["url"], src]
        cur = conn.execute(
            f"UPDATE jobs SET {set_clause} WHERE url=? AND decision_source=?", params)
        n += cur.rowcount
    conn.commit()
    return n
