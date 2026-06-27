"""applypilot-fleet-frontier: home-side frontier quality pass over the contested
backlog. Subscription backend is gated behind --enable-subscription (default off)."""
from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

from applypilot import config
from applypilot.scoring import scorer
from applypilot.fleet import frontier_db
from applypilot.fleet.frontier_pass import run_frontier_pass

_SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "reasoning": {"type": "string"},
    },
    "required": ["score"],
}


def make_schema_file(d) -> str:
    """Write the score JSON Schema to a temp file and return the path."""
    p = Path(d) / "score_schema.json"
    p.write_text(json.dumps(_SCORE_SCHEMA), encoding="utf-8")
    return str(p)


def _brain():
    c = sqlite3.connect(str(config.DB_PATH))
    c.row_factory = sqlite3.Row
    return c


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="applypilot-fleet-frontier")
    p.add_argument("--mode", choices=["backlog", "new", "urls"], default="backlog")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--floor", type=float, default=7.0)
    p.add_argument("--top-model", default="gpt-5.5")
    p.add_argument("--backlog-model", default="gpt-5.5")
    p.add_argument("--top-tier-floor", type=float, default=8.5)
    p.add_argument("--metered-provider", default="gpt-5.5")
    p.add_argument("--use-subscription", action="store_true")
    p.add_argument("--enable-subscription", action="store_true")
    p.add_argument("--min-gap", type=float, default=2.0)
    p.add_argument("--report", action="store_true")
    args = p.parse_args(argv)

    # Guardrail: subscription backend requires an explicit opt-in flag
    if args.use_subscription and not args.enable_subscription:
        raise SystemExit(
            "refusing the Codex subscription backend without --enable-subscription "
            "(it runs `codex exec` on your logged-in account). "
            "Pass --enable-subscription to opt in."
        )

    conn = _brain()
    if args.report:
        for r in frontier_db.disagreement_report(conn):
            print(r)
        return 0

    ctx = scorer.load_score_context()
    with tempfile.TemporaryDirectory() as td:
        res = run_frontier_pass(
            conn,
            resume_text=ctx["resume_text"],
            preference_profile=ctx.get("preference_profile"),
            kg_prompt=ctx.get("kg_prompt"),
            limit=args.limit,
            floor=args.floor,
            mode=args.mode,
            use_subscription=args.use_subscription,
            metered_provider=args.metered_provider,
            top_model=args.top_model,
            backlog_model=args.backlog_model,
            top_tier_floor=args.top_tier_floor,
            schema_path=make_schema_file(td),
            min_gap_seconds=args.min_gap,
        )
    print(res)
    return 0
