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
    client = get_client(stage="score", provider_override=provider)
    cost = estimate_cost(getattr(client, "model", None), getattr(client, "last_usage", None)) or 0.0
    return raw, float(cost)


def make_score_fn(ctx: ComputeContext) -> Callable[[dict], tuple[dict, float]]:
    primary = ctx.providers[0] if ctx.providers else None

    def score_fn(payload: dict) -> tuple[dict, float]:
        job = _job_from_payload(payload)
        raw, cost = _score_once(ctx, job, primary)
        provider = raw.get("provider") or primary
        if raw.get("error") or int(raw.get("score") or 0) <= 0:
            return ({"task": "score", "research_fit_score": None, "research_decision": None,
                     "keywords": "", "reasoning": raw.get("reasoning") or raw.get("error") or "",
                     "model": raw.get("model"), "provider": provider, "status": "failed"}, cost)
        return ({"task": "score", "research_fit_score": int(raw["score"]), "research_decision": None,
                 "keywords": raw.get("keywords", ""), "reasoning": raw.get("reasoning", ""),
                 "model": raw.get("model"), "provider": provider, "status": "done"}, cost)

    return score_fn


def make_audit_fn(ctx: ComputeContext) -> Callable[[dict], tuple[dict, float]]:
    def audit_fn(payload: dict) -> tuple[dict, float]:
        job = _job_from_payload(payload)
        a = audit_job(job, ctx.search_cfg)
        return ({"task": "audit", "research_fit_score": None, "research_decision": a.audit_label,
                 "audit_score": a.audit_score, "role_fit_score": a.role_fit_score,
                 "flags": list(a.flags), "reason": a.reason, "status": "done"}, 0.0)
    return audit_fn
