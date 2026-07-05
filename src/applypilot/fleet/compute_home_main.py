"""applypilot-fleet-compute-home: fill the compute_queue from the brain backlog
(score/audit) and pull advisory results back. Runs on the home box."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import yaml

from applypilot import config
from applypilot.apply import pgqueue
from applypilot.fleet import compute_context as cc
from applypilot.fleet import sync

_BAD_REASONING_RE = "(without a resume|no resume|resume contains no|resume provided|missing resume)"
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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


def status_report(*, pg_conn=None, task="score") -> dict:
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
                SELECT state, count(*) AS n
                FROM worker_heartbeat
                WHERE role='compute' AND last_beat > now() - interval '5 minutes'
                GROUP BY state
                ORDER BY state
                """
            )
            for row in cur.fetchall():
                state = row["state"] or "unknown"
                report["active_workers"]["by_state"][state] = int(row["n"])
                report["active_workers"]["total"] += int(row["n"])

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
    print(f"bad_reasoning_unsynced_done {report['bad_reasoning_unsynced_done']}")
    print(f"recent_done_15m {report['recent_done_15m']} eta_seconds={report['eta_seconds']}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="applypilot-fleet-compute-home")
    p.add_argument("cmd", choices=["push", "pull", "reopen", "publish-context", "requeue", "status"])
    p.add_argument("--task", default="score")
    p.add_argument("--score-floor", type=int, default=7)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--unscored-only", action="store_true")
    p.add_argument("--app-dir", default=None)
    p.add_argument("--before-context-version", default=None)
    p.add_argument("--snapshot-name", default=None)
    p.add_argument("--no-snapshot", action="store_true")
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
    else:
        report = status_report(task=args.task)
        if args.json:
            print(json.dumps(report, sort_keys=True))
        else:
            _print_status(report)
    return 0
