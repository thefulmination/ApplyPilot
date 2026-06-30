"""Fleet Diagnoser (Phase 1, advisory). Reads a worker's log tail and names the root
cause of its apply failures. Tier 0 = deterministic usage-limit guard; Tier 1 = DeepSeek
advisory. Writes advisory rows to fleet_diagnoses. Takes NO fleet actions."""
from __future__ import annotations
import re
from dataclasses import dataclass, field


@dataclass
class WorkerCtx:
    worker_id: str
    recent_log: str = ""
    last_error: str = ""
    recent_failures: list[dict] = field(default_factory=list)  # [{apply_error, host, n}]


@dataclass
class Diagnosis:
    worker_id: str
    root_cause: str
    confidence: float
    recommendation: str
    source: str                       # "tier0" | "deepseek" | "none"
    evidence: str = ""
    details: dict = field(default_factory=dict)


_USAGE_LIMIT_RE = re.compile(r"hit your usage limit", re.IGNORECASE)
_RESET_RE = re.compile(r"try again at\s+(\d{1,2}:\d{2}\s*[AP]M)", re.IGNORECASE)
_MODEL_RE = re.compile(r"usage limit for\s+([\w\-]+(?:\.[\w\-]+)*)", re.IGNORECASE)


def _excerpt(text: str, pattern: re.Pattern, width: int = 160) -> str:
    m = pattern.search(text)
    if not m:
        return text[:width].strip()
    start = max(0, m.start() - 40)
    return text[start:start + width].strip()


def tier0_diagnose(ctx: WorkerCtx) -> Diagnosis | None:
    """Deterministic guard for the action-critical usage-limit case. Returns None on no match
    so diagnose() falls through to Tier 1 (graceful degradation if the wording ever changes)."""
    text = f"{ctx.recent_log}\n{ctx.last_error}"
    if not _USAGE_LIMIT_RE.search(text):
        return None
    reset = _RESET_RE.search(text)
    model = _MODEL_RE.search(text)
    reset_s = reset.group(1) if reset else "unknown"
    model_s = model.group(1) if model else "the agent model"
    rec = (f"Agent quota exhausted ({model_s}). RE-QUEUE these jobs (do NOT quarantine — they "
           f"were never submitted); switch the worker's model or wait until {reset_s}.")
    return Diagnosis(
        worker_id=ctx.worker_id, root_cause="usage_limit", confidence=1.0,
        recommendation=rec, source="tier0", evidence=_excerpt(text, _USAGE_LIMIT_RE),
        details={"model": model_s, "reset_at": reset_s},
    )
