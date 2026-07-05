"""applypilot-fleet-compute-home: fill the compute_queue from the brain backlog
(score/audit) and pull advisory results back. Runs on the home box."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import yaml

from applypilot import config
from applypilot.apply import pgqueue
from applypilot.fleet import compute_context as cc
from applypilot.fleet import sync


def push_backlog(*, sqlite_conn=None, pg_conn=None, task="score", score_floor=7,
                 limit=None, unscored_only=False) -> int:
    # score_floor=7 here is intentional: the home driver applies a quality floor
    # so only jobs worth a second LLM pass are queued for compute.  The underlying
    # sync.push_compute_eligible defaults to score_floor=0 (score everything) — that
    # default is intentional for the raw sync function, which callers may use with
    # their own floor.  Do not change sync.py to match this default.
    return sync.push_compute_eligible(sqlite_conn=sqlite_conn, pg_conn=pg_conn,
                                      task=task, score_floor=score_floor, limit=limit,
                                      unscored_only=unscored_only)


def pull_results(*, sqlite_conn=None, pg_conn=None) -> int:
    return sync.pull_compute_results(sqlite_conn=sqlite_conn, pg_conn=pg_conn)


def reopen_results(*, pg_conn=None) -> int:
    return sync.reopen_compute_results(pg_conn=pg_conn)


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


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="applypilot-fleet-compute-home")
    p.add_argument("cmd", choices=["push", "pull", "reopen", "publish-context"])
    p.add_argument("--task", default="score")
    p.add_argument("--score-floor", type=int, default=7)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--unscored-only", action="store_true")
    p.add_argument("--app-dir", default=None)
    args = p.parse_args(argv)
    if args.cmd == "push":
        print("pushed", push_backlog(task=args.task, score_floor=args.score_floor,
                                     limit=args.limit, unscored_only=args.unscored_only))
    elif args.cmd == "pull":
        print("pulled", pull_results())
    elif args.cmd == "reopen":
        print("reopened", reopen_results())
    else:
        print("published-context", publish_context_from_app_dir(app_dir=args.app_dir))
    return 0
