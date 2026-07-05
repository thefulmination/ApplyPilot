"""Pure wiring between the fleet compute_queue and the real scorer/auditor.

A compute job payload (url/company/title/application_url/full_description) is mapped
to a score_job/audit_job call and back into the advisory result shape that
sync.pull_compute_results reads (research_fit_score / research_decision). No DB here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from applypilot.llm import get_client, _estimate_cost as estimate_cost
from applypilot.scoring.scorer import score_job
from applypilot.scoring.audit import audit_job


@dataclass
class ComputeContext:
    resume_text: str
    preference_profile: dict | None = None
    kg_prompt: str | None = None
    search_cfg: dict | None = None
    ctx_version: str = ""
    providers: list[str] = field(default_factory=list)  # ordered; providers[0] is primary
    fallback: list[str] = field(default_factory=list)    # tried in order on an error result
    ensemble: bool = False


def _job_from_payload(payload: dict) -> dict:
    return {
        "title": payload.get("title") or "",
        "site": payload.get("company") or payload.get("site") or "",
        "location": payload.get("location") or "N/A",
        "full_description": payload.get("full_description") or "",
        "fit_score": payload.get("fit_score"),
    }


def _score_once(ctx: ComputeContext, job: dict, provider: str | None) -> tuple[dict, float]:
    raw = score_job(ctx.resume_text, job, ctx.preference_profile, ctx.kg_prompt, provider=provider)
    # Cost is read from the process-wide get_client(...) singleton's last_usage
    # immediately after the scoring call.  This means a compute PROCESS must run
    # ONE scoring slot at a time (the standard deployment is one WorkerLoop per
    # process; scale = more processes, not threads).  A cleaner fix would be to
    # have score_job return its token usage directly so the adapter needn't
    # re-read a shared singleton — that refactor is tracked in
    # .superpowers/sdd/compute-lane-followups.md (do NOT change score_job's
    # return contract here).
    client = get_client(stage="score", provider_override=provider)
    cost = estimate_cost(getattr(client, "model", None), getattr(client, "last_usage", None)) or 0.0
    return raw, float(cost)


def make_score_fn(ctx: ComputeContext) -> Callable[[dict], tuple[dict, float]]:
    primary = ctx.providers[0] if ctx.providers else None
    chain = [primary] + list(ctx.fallback)

    def _build(raw, provider, cost, status):
        ok = status == "done"
        return ({"task": "score",
                 "research_fit_score": int(raw["score"]) if ok else None,
                 "research_decision": None,
                 "keywords": raw.get("keywords", "") if ok else "",
                 "reasoning": raw.get("reasoning") or raw.get("error") or "",
                 "model": raw.get("model"), "provider": provider, "status": status,
                 "ctx_version": ctx.ctx_version}, cost)

    def score_fn(payload: dict) -> tuple[dict, float]:
        job = _job_from_payload(payload)
        if ctx.ensemble and len(ctx.providers) >= 2:
            members, total_cost, scores = [], 0.0, []
            for provider in ctx.providers:
                raw, cost = _score_once(ctx, job, provider)
                total_cost += cost
                s = int(raw.get("score") or 0)
                if not raw.get("error") and s > 0:
                    members.append({"provider": raw.get("provider") or provider, "score": s})
                    scores.append(s)
            if not scores:
                return _build({"reasoning": "ensemble: all providers failed"}, ctx.providers[0], total_cost, "failed")
            mean = sum(scores) / len(scores)
            spread = (max(scores) - min(scores)) / 9.0  # 1-10 scale span
            res = {"task": "score", "research_fit_score": round(mean), "research_decision": None,
                   "keywords": "", "reasoning": "ensemble", "model": None,
                   "provider": "+".join(m["provider"] for m in members),
                   "ensemble": members, "agreement": round(1.0 - spread, 3), "status": "done",
                   "ctx_version": ctx.ctx_version}
            return res, total_cost
        # failover loop from Task 4
        total_cost = 0.0
        last = None
        for provider in chain:
            raw, cost = _score_once(ctx, job, provider)
            total_cost += cost
            prov = raw.get("provider") or provider
            if not raw.get("error") and int(raw.get("score") or 0) > 0:
                return _build(raw, prov, total_cost, "done")
            last = (raw, prov)
        raw, prov = last
        return _build(raw, prov, total_cost, "failed")

    return score_fn


def make_audit_fn(ctx: ComputeContext) -> Callable[[dict], tuple[dict, float]]:
    def audit_fn(payload: dict) -> tuple[dict, float]:
        job = _job_from_payload(payload)
        a = audit_job(job, ctx.search_cfg)
        return ({"task": "audit", "research_fit_score": None, "research_decision": a.audit_label,
                 "audit_score": a.audit_score, "role_fit_score": a.role_fit_score,
                 "flags": list(a.flags), "reason": a.reason, "status": "done",
                 "ctx_version": ctx.ctx_version}, 0.0)
    return audit_fn
