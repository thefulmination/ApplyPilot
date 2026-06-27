# src/applypilot/fleet/frontier_pass.py
"""Home-side frontier pass: select the contested backlog, re-score each with the
Codex subscription (model by tier), fail over to a metered API on any limit, and
write advisory frontier_scores. Serial + governed. Never touches the jobs table."""
from __future__ import annotations

import random
import time

from applypilot.scoring.scorer import score_job, build_score_prompt_text
from applypilot.fleet import frontier_db
from applypilot.fleet.frontier_select import select_priority
from applypilot.fleet.cli_providers import score_via_codex, SubscriptionUnavailable
from applypilot.fleet.frontier_governor import FrontierGovernor


def _agreement(frontier, cheap):
    if frontier is None or cheap is None:
        return None
    return round(1 - abs(float(frontier) - float(cheap)) / 9.0, 3)


def run_frontier_pass(conn, *, resume_text, preference_profile=None, kg_prompt=None, limit=200,
                      floor=7.0, mode="backlog", hours=24, urls=None, use_subscription=True,
                      metered_provider="gpt-5.5", top_model="gpt-5.5", backlog_model="gpt-5.5",
                      top_tier_floor=8.5, schema_path=None, governor=None, min_gap_seconds=2.0) -> dict:
    jobs = select_priority(conn, limit=limit, floor=floor, mode=mode, hours=hours, urls=urls)
    gov = governor or FrontierGovernor("codex", min_gap_seconds=min_gap_seconds)
    scored = by_subscription = failed_over = 0
    for j in jobs:
        cheap = j.get("cheap_score")
        job = {"title": j["title"], "site": j.get("company"), "location": "N/A",
               "full_description": j.get("full_description")}
        model = top_model if (cheap is not None and cheap >= top_tier_floor) else backlog_model
        result, provider = None, None
        if use_subscription and gov.allow():
            try:
                prompt = build_score_prompt_text(resume_text, job, preference_profile, kg_prompt)
                result = score_via_codex(prompt, schema_path=schema_path, model=model)
                provider, by_subscription = "codex-subscription", by_subscription + 1
                gov.record("ok")
            except SubscriptionUnavailable:
                gov.record("limit")
                result = None
        if result is None:  # failover / subscription off / governor deny
            result = score_job(resume_text, job, preference_profile, kg_prompt, provider=metered_provider)
            provider, failed_over = metered_provider, failed_over + 1
        fscore = result.get("score")
        frontier_db.upsert_frontier_score(
            conn, url=j["url"], cheap_score=cheap, frontier_score=fscore, provider=provider,
            agreement=_agreement(fscore, cheap), reasoning=result.get("reasoning"),
        )
        scored += 1
        if min_gap_seconds:
            time.sleep(min_gap_seconds * (0.5 + random.random()))  # gap-jitter
    return {"scored": scored, "by_subscription": by_subscription, "failed_over": failed_over}
