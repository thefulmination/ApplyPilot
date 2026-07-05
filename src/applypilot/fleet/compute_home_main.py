"""applypilot-fleet-compute-home: fill the compute_queue from the brain backlog
(score/audit) and pull advisory results back. Runs on the home box."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from applypilot import config
from applypilot.apply import pgqueue
from applypilot.fleet import compute_context as cc
from applypilot.fleet import sync

_BAD_REASONING_RE = "(without a resume|no resume|resume contains no|resume provided|missing resume)"
_RATE_LIMIT_RE = (
    r"(^|[^A-Za-z0-9_])"
    r"(rate[- ]?limit(ed|ing)?|too many requests|http\s*429|429|quota|throttl(ed|ing|e))"
    r"([^A-Za-z0-9_]|$)"
)
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_SCORE_WORKERS = 16
MAX_SCORE_WORKERS = 16
_SCORE_RESULT_KEYS = ("research_fit_score", "fit_score", "score", "audit_score")


def push_backlog(*, sqlite_conn=None, pg_conn=None, task="score", score_floor=7,
                 limit=None, unscored_only=False) -> int:
    # score_floor=7 here is intentional: the home driver applies a quality floor
    # so only jobs worth a second LLM pass are queued for compute.  The underlying
    # sync.push_compute_eligible defaults to score_floor=0 (score everything) — that
    # default is intentional for the raw sync function, which callers may use with
    # their own floor.  Do not change sync.py to match this default.
    own_pg = pg_conn is None
    pg = pg_conn or pgqueue.connect()
    try:
        if task == "score":
            _require_score_context(pg)
        return sync.push_compute_eligible(sqlite_conn=sqlite_conn, pg_conn=pg,
                                          task=task, score_floor=score_floor, limit=limit,
                                          unscored_only=unscored_only)
    finally:
        if own_pg:
            pg.close()


def pull_results(*, sqlite_conn=None, pg_conn=None) -> int:
    return sync.pull_compute_results(sqlite_conn=sqlite_conn, pg_conn=pg_conn)


def reopen_results(*, pg_conn=None) -> int:
    return sync.reopen_compute_results(pg_conn=pg_conn)


def _require_score_context(pg_conn) -> str:
    ctx, version = cc.load_context(pg_conn, providers=[])
    if not ctx.resume_text.strip():
        raise RuntimeError(
            "cannot push score jobs without published ctx:resume; run "
            "applypilot-fleet-compute-home publish-context first"
        )
    return version


def _default_app_dir() -> Path:
    local = Path.cwd() / ".applypilot"
    return local if local.exists() else config.APP_DIR


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def publish_context_from_app_dir(*, app_dir: str | Path | None = None, pg_conn=None) -> str:
    root = Path(app_dir) if app_dir is not None else _default_app_dir()
    resume_path = root / "resume.txt"
    if not resume_path.exists():
        raise FileNotFoundError(f"missing compute resume text: {resume_path}")
    resume_text = resume_path.read_text(encoding="utf-8", errors="ignore")
    if not resume_text.strip():
        raise ValueError(f"empty compute resume text: {resume_path}")

    preference_profile = _load_json(root / "job_preference_profile.json")
    kg_prompt_path = root / "job_knowledge_graph_prompt.md"
    kg_prompt = kg_prompt_path.read_text(encoding="utf-8", errors="ignore") if kg_prompt_path.exists() else ""
    search_cfg = _load_yaml(root / "searches.yaml")

    version_input = "\0".join([
        resume_text,
        json.dumps(preference_profile, sort_keys=True, ensure_ascii=True),
        kg_prompt,
        json.dumps(search_cfg, sort_keys=True, ensure_ascii=True),
    ])
    version = "ctx-" + hashlib.sha256(version_input.encode("utf-8")).hexdigest()[:16]
    own_pg = pg_conn is None
    pg = pg_conn or pgqueue.connect()
    try:
        cc.publish_context(pg, resume_text=resume_text, preference_profile=preference_profile,
                           kg_prompt=kg_prompt, search_cfg=search_cfg, version=version)
    finally:
        if own_pg:
            pg.close()
    return version


def _safe_identifier(name: str) -> str:
    if not _SAFE_IDENTIFIER_RE.match(name or ""):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


def requeue_results(*, pg_conn=None, task="score", before_context_version: str,
                    snapshot: bool = True, snapshot_name: str | None = None,
                    statuses: tuple[str, ...] = ("done", "failed")) -> int:
    """Requeue terminal compute results not produced with *before_context_version*.

    Rows are selected by result JSON metadata rather than hand-written timestamps:
    any terminal row whose result.ctx_version differs from the target version is
    considered stale. Snapshotting preserves the old advisory result before reset.
    """
    if not before_context_version:
        raise ValueError("before_context_version is required")
    version_slug = re.sub(r"[^A-Za-z0-9_]", "_", before_context_version)
    table = _safe_identifier(snapshot_name or f"compute_queue_rescore_snapshot_{version_slug}")
    own_pg = pg_conn is None
    pg = pg_conn or pgqueue.connect()
    params = {
        "task": task,
        "version": before_context_version,
        "statuses": list(statuses),
    }
    stale_where = (
        "task=%(task)s AND status = ANY(%(statuses)s) "
        "AND COALESCE(result->>'ctx_version','') <> %(version)s"
    )
    try:
        with pg.cursor() as cur:
            if snapshot:
                cur.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} AS "
                    "SELECT *, now() AS snapshotted_at, %s::text AS target_ctx_version "
                    "FROM compute_queue WHERE false",
                    (before_context_version,),
                )
                cur.execute(
                    f"INSERT INTO {table} "
                    "SELECT cq.*, now() AS snapshotted_at, %(version)s::text AS target_ctx_version "
                    f"FROM compute_queue cq WHERE {stale_where} "
                    f"AND NOT EXISTS (SELECT 1 FROM {table} s WHERE s.url = cq.url AND s.task = cq.task)",
                    params,
                )
            cur.execute(
                "UPDATE compute_queue SET status='queued', lease_owner=NULL, lease_expires_at=NULL, "
                "attempts=0, result=NULL, est_cost_usd=0, synced_to_home_at=NULL, updated_at=now() "
                f"WHERE {stale_where}",
                params,
            )
            n = cur.rowcount
        pg.commit()
        return n
    finally:
        if own_pg:
            pg.close()


def status_report(*, pg_conn=None, task="score", expected_score_workers: int | None = DEFAULT_SCORE_WORKERS) -> dict:
    own_pg = pg_conn is None
    pg = pg_conn or pgqueue.connect()
    try:
        ctx, version = cc.load_context(pg, providers=[])
        report = {
            "task": task,
            "context": {
                "version": version,
                "resume_chars": len(ctx.resume_text or ""),
                "kg_chars": len(ctx.kg_prompt or ""),
            },
            "queue": {},
            "active_workers": {"total": 0, "by_state": {}},
            "score_workers": {
                "expected": expected_score_workers,
                "cap": MAX_SCORE_WORKERS,
                "active": 0,
                "within_expected": None,
                "by_owner": {},
                "recent_done_15m": 0,
                "recent_failed_15m": 0,
                "recent_rate_limited_15m": 0,
                "failure_rate_15m": None,
            },
            "bad_reasoning_unsynced_done": 0,
            "eta_seconds": None,
            "recent_done_15m": 0,
        }
        with pg.cursor() as cur:
            cur.execute(
                """
                SELECT status, count(*) AS n,
                       count(*) FILTER (WHERE synced_to_home_at IS NULL) AS unsynced
                FROM compute_queue
                WHERE task=%s
                GROUP BY status
                ORDER BY status
                """,
                (task,),
            )
            for row in cur.fetchall():
                report["queue"][row["status"]] = {"count": int(row["n"]), "unsynced": int(row["unsynced"])}

            cur.execute(
                """
                SELECT COALESCE(machine_owner, 'unknown') AS owner, state, count(*) AS n
                FROM worker_heartbeat
                WHERE role='compute' AND last_beat > now() - interval '5 minutes'
                GROUP BY owner, state
                ORDER BY owner, state
                """
            )
            for row in cur.fetchall():
                state = row["state"] or "unknown"
                owner = row["owner"] or "unknown"
                report["active_workers"]["by_state"][state] = (
                    report["active_workers"]["by_state"].get(state, 0) + int(row["n"])
                )
                report["active_workers"]["total"] += int(row["n"])
                report["score_workers"]["by_owner"][owner] = (
                    report["score_workers"]["by_owner"].get(owner, 0) + int(row["n"])
                )
                report["score_workers"]["active"] += int(row["n"])
            if expected_score_workers is not None:
                report["score_workers"]["within_expected"] = (
                    report["score_workers"]["active"] == expected_score_workers
                    and report["score_workers"]["active"] <= MAX_SCORE_WORKERS
                )

            cur.execute(
                """
                SELECT count(*) AS n
                FROM compute_queue
                WHERE task=%s AND status='done' AND synced_to_home_at IS NULL
                  AND (
                    COALESCE(result->>'reasoning','') ~* %s OR
                    COALESCE(result->>'verdict','') ~* %s OR
                    COALESCE(result->>'research_reasoning','') ~* %s
                  )
                """,
                (task, _BAD_REASONING_RE, _BAD_REASONING_RE, _BAD_REASONING_RE),
            )
            report["bad_reasoning_unsynced_done"] = int(cur.fetchone()["n"])

            cur.execute(
                """
                SELECT count(*) AS n
                FROM compute_queue
                WHERE task=%s AND status='done' AND updated_at >= now() - interval '15 minutes'
                """,
                (task,),
            )
            recent_done = int(cur.fetchone()["n"])
            report["recent_done_15m"] = recent_done
            report["score_workers"]["recent_done_15m"] = recent_done

            cur.execute(
                """
                SELECT count(*) AS n
                FROM compute_queue
                WHERE task=%s AND status='failed' AND updated_at >= now() - interval '15 minutes'
                """,
                (task,),
            )
            recent_failed = int(cur.fetchone()["n"])
            report["score_workers"]["recent_failed_15m"] = recent_failed
            recent_terminal = recent_done + recent_failed
            if recent_terminal:
                report["score_workers"]["failure_rate_15m"] = round(recent_failed / recent_terminal, 4)

            cur.execute(
                """
                SELECT count(*) AS n
                FROM compute_queue
                WHERE task=%s AND status='failed' AND updated_at >= now() - interval '15 minutes'
                  AND (
                    COALESCE(result->>'error','') ~* %s OR
                    COALESCE(result->>'reasoning','') ~* %s OR
                    COALESCE(result->>'verdict','') ~* %s
                  )
                """,
                (task, _RATE_LIMIT_RE, _RATE_LIMIT_RE, _RATE_LIMIT_RE),
            )
            report["score_workers"]["recent_rate_limited_15m"] = int(cur.fetchone()["n"])
            remaining = sum(report["queue"].get(s, {}).get("count", 0) for s in ("queued", "leased"))
            if recent_done > 0 and remaining > 0:
                report["eta_seconds"] = int(remaining / (recent_done / 900.0))
        pg.rollback()
        return report
    finally:
        if own_pg:
            pg.close()


def _print_status(report: dict) -> None:
    print(f"task {report['task']}")
    ctx = report["context"]
    print(f"context {ctx['version'] or 'MISSING'} resume_chars={ctx['resume_chars']} kg_chars={ctx['kg_chars']}")
    for status, data in report["queue"].items():
        print(f"{status} {data['count']} unsynced={data['unsynced']}")
    workers = report["active_workers"]
    print(f"active_workers {workers['total']} by_state={workers['by_state']}")
    score_workers = report["score_workers"]
    print(
        "score_workers "
        f"active={score_workers['active']} expected={score_workers['expected']} cap={score_workers['cap']} "
        f"within_expected={score_workers['within_expected']} by_owner={score_workers['by_owner']}"
    )
    print(
        "score_errors_15m "
        f"done={score_workers['recent_done_15m']} failed={score_workers['recent_failed_15m']} "
        f"rate_limited={score_workers['recent_rate_limited_15m']} "
        f"failure_rate={score_workers['failure_rate_15m']}"
    )
    print(f"bad_reasoning_unsynced_done {report['bad_reasoning_unsynced_done']}")
    print(f"recent_done_15m {report['recent_done_15m']} eta_seconds={report['eta_seconds']}")


def _result_dict(result: Any) -> dict:
    if isinstance(result, dict):
        return result
    if isinstance(result, str) and result.strip():
        try:
            loaded = json.loads(result)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _score_from_result(result: Any) -> float | None:
    data = _result_dict(result)
    for key in _SCORE_RESULT_KEYS:
        value = data.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _ctx_version_from_result(result: Any) -> str:
    value = _result_dict(result).get("ctx_version")
    return str(value) if value else "missing"


def _round_float(value: float | None) -> float | None:
    return None if value is None else round(float(value), 3)


def score_delta_audit(*, pg_conn=None, snapshot_name: str, task="score",
                      material_change: float = 2.0, high_threshold: float = 7.0,
                      top: int = 20) -> dict:
    if not snapshot_name:
        raise ValueError("snapshot_name is required")
    table = _safe_identifier(snapshot_name)
    own_pg = pg_conn is None
    pg = pg_conn or pgqueue.connect()
    try:
        with pg.cursor() as cur:
            cur.execute(
                f"""
                SELECT s.url, s.task, s.result AS old_result,
                       q.status AS current_status, q.result AS new_result
                FROM {table} s
                LEFT JOIN compute_queue q ON q.url = s.url AND q.task = s.task
                WHERE s.task = %s
                ORDER BY s.url
                """,
                (task,),
            )
            rows = cur.fetchall()
        pg.rollback()

        old_ctx_versions: Counter[str] = Counter()
        new_ctx_versions: Counter[str] = Counter()
        compared: list[dict[str, Any]] = []
        pending_rows = 0
        current_done_rows = 0
        old_scores: list[float] = []
        new_scores: list[float] = []

        for row in rows:
            old_result = row["old_result"]
            new_result = row["new_result"]
            old_ctx_versions[_ctx_version_from_result(old_result)] += 1
            if row["current_status"] == "done":
                current_done_rows += 1
            if new_result:
                new_ctx_versions[_ctx_version_from_result(new_result)] += 1

            old_score = _score_from_result(old_result)
            new_score = _score_from_result(new_result) if row["current_status"] == "done" else None
            if old_score is None or new_score is None:
                pending_rows += 1
                continue

            delta = new_score - old_score
            old_scores.append(old_score)
            new_scores.append(new_score)
            compared.append({
                "url": row["url"],
                "old_score": _round_float(old_score),
                "new_score": _round_float(new_score),
                "delta": _round_float(delta),
                "abs_delta": _round_float(abs(delta)),
                "old_ctx_version": _ctx_version_from_result(old_result),
                "new_ctx_version": _ctx_version_from_result(new_result),
            })

        changed = [r for r in compared if (r["abs_delta"] or 0) >= material_change]
        improved = [r for r in compared if (r["delta"] or 0) >= material_change]
        worsened = [r for r in compared if (r["delta"] or 0) <= -material_change]
        crossed_high_up = [
            r for r in compared
            if (r["old_score"] or 0) < high_threshold <= (r["new_score"] or 0)
        ]
        crossed_high_down = [
            r for r in compared
            if (r["old_score"] or 0) >= high_threshold > (r["new_score"] or 0)
        ]
        top_changes = sorted(
            (r for r in compared if (r["abs_delta"] or 0) > 0),
            key=lambda r: (-(r["abs_delta"] or 0), r["url"]),
        )[:max(0, top)]

        old_avg = sum(old_scores) / len(old_scores) if old_scores else None
        new_avg = sum(new_scores) / len(new_scores) if new_scores else None
        return {
            "task": task,
            "snapshot": table,
            "snapshot_rows": len(rows),
            "current_done_rows": current_done_rows,
            "compared_rows": len(compared),
            "pending_rows": pending_rows,
            "material_change": material_change,
            "high_threshold": high_threshold,
            "changed_count": len(changed),
            "improved_count": len(improved),
            "worsened_count": len(worsened),
            "crossed_high_up": len(crossed_high_up),
            "crossed_high_down": len(crossed_high_down),
            "old_avg_score": _round_float(old_avg),
            "new_avg_score": _round_float(new_avg),
            "avg_delta": _round_float((new_avg - old_avg) if old_avg is not None and new_avg is not None else None),
            "old_ctx_versions": dict(sorted(old_ctx_versions.items())),
            "new_ctx_versions": dict(sorted(new_ctx_versions.items())),
            "top_changes": top_changes,
        }
    finally:
        if own_pg:
            pg.close()


def _print_delta_audit(report: dict) -> None:
    print(f"task {report['task']} snapshot={report['snapshot']}")
    print(
        f"rows snapshot={report['snapshot_rows']} current_done={report['current_done_rows']} "
        f"compared={report['compared_rows']} pending={report['pending_rows']}"
    )
    print(
        f"changed>={report['material_change']} {report['changed_count']} "
        f"improved={report['improved_count']} worsened={report['worsened_count']}"
    )
    print(
        f"crossed_high_up={report['crossed_high_up']} "
        f"crossed_high_down={report['crossed_high_down']} threshold={report['high_threshold']}"
    )
    print(
        f"avg old={report['old_avg_score']} new={report['new_avg_score']} "
        f"delta={report['avg_delta']}"
    )
    print(f"old_ctx_versions {report['old_ctx_versions']}")
    print(f"new_ctx_versions {report['new_ctx_versions']}")
    for row in report["top_changes"]:
        print(f"change {row['delta']:+} {row['old_score']}->{row['new_score']} {row['url']}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="applypilot-fleet-compute-home")
    p.add_argument("cmd", choices=["push", "pull", "reopen", "publish-context", "requeue", "status", "delta-audit"])
    p.add_argument("--task", default="score")
    p.add_argument("--score-floor", type=int, default=7)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--unscored-only", action="store_true")
    p.add_argument("--app-dir", default=None)
    p.add_argument("--before-context-version", default=None)
    p.add_argument("--snapshot-name", default=None)
    p.add_argument("--snapshot", default=None)
    p.add_argument("--no-snapshot", action="store_true")
    p.add_argument("--expected-score-workers", type=int, default=DEFAULT_SCORE_WORKERS)
    p.add_argument("--material-change", type=float, default=2.0)
    p.add_argument("--high-threshold", type=float, default=7.0)
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    if args.cmd == "push":
        print("pushed", push_backlog(task=args.task, score_floor=args.score_floor,
                                     limit=args.limit, unscored_only=args.unscored_only))
    elif args.cmd == "pull":
        print("pulled", pull_results())
    elif args.cmd == "reopen":
        print("reopened", reopen_results())
    elif args.cmd == "publish-context":
        print("published-context", publish_context_from_app_dir(app_dir=args.app_dir))
    elif args.cmd == "requeue":
        print("requeued", requeue_results(task=args.task, before_context_version=args.before_context_version,
                                          snapshot=not args.no_snapshot, snapshot_name=args.snapshot_name))
    elif args.cmd == "status":
        report = status_report(task=args.task, expected_score_workers=args.expected_score_workers)
        if args.json:
            print(json.dumps(report, sort_keys=True))
        else:
            _print_status(report)
    else:
        report = score_delta_audit(
            snapshot_name=args.snapshot or args.snapshot_name,
            task=args.task,
            material_change=args.material_change,
            high_threshold=args.high_threshold,
            top=args.top,
        )
        if args.json:
            print(json.dumps(report, sort_keys=True))
        else:
            _print_delta_audit(report)
    return 0
