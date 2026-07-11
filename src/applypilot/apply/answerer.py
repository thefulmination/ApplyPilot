"""Tier-0 free-text application answerer.

Produces a short, *verified* free-text answer to a job-application question
using a cheap model, retrieval over the candidate's own past approved answers,
and a deterministic verification layer that never lets an unverified answer
through. This is the isolated unit the deterministic ATS adapters call for
their free-text fields -- the expensive browsing agent is not involved.

Nothing here touches a browser or a GPU. The model call is injectable so the
whole module is unit-testable offline.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from applypilot.llm import get_client

# Open-ended question kinds get word-count bounds; short factual answers
# (yes/no, a city, a number) do not.
_OPEN_KINDS = {None, "motivation", "behavioral", "why", "open"}
_MIN_WORDS = 15
_MAX_WORDS = 150

# Phrases that mark robotic / template output. Lowercased substring match.
_AI_TELLS = (
    "as an ai",
    "as a language model",
    "i am passionate about",
    "i'm passionate about",
    "lorem ipsum",
)
# Unfilled template slots like "[Company]" or "[role]".
_PLACEHOLDER_RE = re.compile(r"\[[a-z][a-z /]*\]", re.I)

# "Name(s) + corporate suffix" -> a claimed employer/organization. Conservative
# on purpose: a bare invented company with no suffix won't trip this, but the
# blatant "led the team at Vandelay Industries" fabrication will.
_ORG_RE = re.compile(
    r"\b((?:[A-Z][A-Za-z&.\-]+\s+){1,4}"
    r"(?:Inc\.?|LLC|Corp\.?|Ltd\.?|Co\.?|Company|Technologies|Labs|Systems|"
    r"Solutions|Group|Industries|Bank|Capital|Partners|Ventures|Holdings))"
)

# Quantified claims that must be grounded in the resume.
_DOLLAR_RE = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?\s?(?:k|m|b|mm|bn|million|billion|thousand)?", re.I
)
_PERCENT_RE = re.compile(r"\b\d+(?:\.\d+)?\s?%")


def _digits(s: str) -> str:
    return re.sub(r"[^\d]", "", s or "")


def _flatten(obj) -> list:
    """Yield every scalar value inside a nested dict/list structure."""
    out: list = []
    if isinstance(obj, dict):
        for v in obj.values():
            out.extend(_flatten(v))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out.extend(_flatten(v))
    elif obj is not None:
        out.append(obj)
    return out


def verify_answer(
    text: str,
    question: str,
    *,
    resume_text: str,
    profile: dict,
    job: dict | None = None,
    kind: str | None = None,
) -> list[str]:
    """Return the names of failed checks ([] means the answer is clean).

    Deterministic and side-effect-free. Every "fabrication" check is grounded
    against the candidate's real resume + profile + the job's own context, so a
    figure or employer the candidate actually has is never flagged.
    """
    t = (text or "").strip()
    if not t:
        return ["empty"]

    failed: list[str] = []
    low = t.lower()

    if "openai" in low:
        failed.append("banned_openai")

    if any(tell in low for tell in _AI_TELLS) or _PLACEHOLDER_RE.search(t):
        failed.append("ai_tells")

    if kind in _OPEN_KINDS and not (_MIN_WORDS <= len(t.split()) <= _MAX_WORDS):
        failed.append("length")

    # Build the grounding text: resume + every profile value + job context.
    ground = resume_text or ""
    ground += " " + " ".join(str(v) for v in _flatten(profile))
    if job:
        ground += " " + " ".join(
            str(job.get(k, "")) for k in ("title", "site", "company", "description")
        )
    ground_low = ground.lower()
    ground_digits = _digits(ground)

    for m in list(_DOLLAR_RE.finditer(t)) + list(_PERCENT_RE.finditer(t)):
        d = _digits(m.group())
        if d and d not in ground_digits:
            failed.append("fabricated_metric")
            break

    for m in _ORG_RE.finditer(t):
        if m.group(1).strip().lower() not in ground_low:
            failed.append("fabricated_company")
            break

    return failed


# ---------------------------------------------------------------------------
# Retrieval over the candidate's own past approved answers
# ---------------------------------------------------------------------------

# Generic question words carry no retrieval signal. Tokens of length <= 2 are
# dropped separately, which already removes "to", "at", "we", "of", etc.
_STOPWORDS = {
    "the", "and", "for", "are", "you", "your", "our", "why", "what", "how",
    "who", "was", "were", "this", "that", "those", "these", "with", "about",
    "tell", "want", "would", "should", "could", "can", "will", "did", "does",
    "have", "has", "had", "from", "into", "been", "being", "they", "them",
    "their", "its", "which", "when", "where", "please", "give", "using", "use",
}


def _tokens(s: str) -> set[str]:
    return {
        tok
        for tok in re.findall(r"[a-z0-9]+", (s or "").lower())
        if len(tok) > 2 and tok not in _STOPWORDS
    }


@dataclass(frozen=True)
class PastAnswer:
    question: str
    answer: str
    job_title: str = ""
    company: str = ""
    kind: str = ""
    approved: bool = True


class AnswerCorpus:
    """A lexical retriever over the candidate's past approved answers.

    Deliberately dependency-free (no embeddings, no vector DB, no GPU): ranks by
    meaningful-token overlap with the query question and drops zero-overlap
    records so an unrelated past answer never leaks into the prompt. Backed by a
    JSONL file so it can grow every time an answer is approved.
    """

    def __init__(self, records: list[PastAnswer] | None = None) -> None:
        self._records: list[PastAnswer] = list(records or [])

    def add(self, question: str, answer: str, *, job_title: str = "", company: str = "",
            kind: str = "", approved: bool = True) -> None:
        self._records.append(PastAnswer(question, answer, job_title, company, kind, approved))

    def top_k(self, question: str, k: int = 3) -> list[PastAnswer]:
        q = _tokens(question)
        scored = [
            (len(q & _tokens(r.question)), r) for r in self._records
        ]
        ranked = sorted(
            (pair for pair in scored if pair[0] > 0),
            key=lambda pair: pair[0],
            reverse=True,
        )
        return [r for _, r in ranked[:k]]

    def find_compatible(
        self,
        question: str,
        *,
        job: dict,
        profile: dict,
        resume_text: str,
        kind: str | None,
        min_question_jaccard: float = 0.65,
        min_title_jaccard: float = 0.30,
    ) -> PastAnswer | None:
        """Return an approved answer that remains verified for the current job."""
        question_tokens = _tokens(question)
        title_tokens = _tokens(str(job.get("title") or ""))
        ranked: list[tuple[float, PastAnswer]] = []
        for record in self._records:
            if not record.approved:
                continue
            if record.kind and kind and record.kind != kind:
                continue
            record_question = _tokens(record.question)
            union = question_tokens | record_question
            question_score = len(question_tokens & record_question) / len(union) if union else 0
            if question_score < min_question_jaccard:
                continue
            record_title = _tokens(record.job_title)
            if title_tokens and record_title:
                title_union = title_tokens | record_title
                title_score = len(title_tokens & record_title) / len(title_union)
                if title_score < min_title_jaccard:
                    continue
            checks = verify_answer(
                record.answer,
                question,
                resume_text=resume_text,
                profile=profile,
                job=job,
                kind=kind,
            )
            if checks:
                continue
            ranked.append((question_score, record))
        return max(ranked, key=lambda item: item[0])[1] if ranked else None

    def save_jsonl(self, path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as fh:
            for r in self._records:
                fh.write(json.dumps(asdict(r)) + "\n")

    @classmethod
    def from_jsonl(cls, path) -> "AnswerCorpus":
        p = Path(path)
        records: list[PastAnswer] = []
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue  # skip a corrupt line, keep the rest
                records.append(
                    PastAnswer(
                        question=d.get("question", ""),
                        answer=d.get("answer", ""),
                        job_title=d.get("job_title", ""),
                        company=d.get("company", ""),
                        kind=d.get("kind", ""),
                        approved=bool(d.get("approved", True)),
                    )
                )
        return cls(records)


# ---------------------------------------------------------------------------
# The answerer: retrieve -> cheap model -> verify -> retry -> escalate
# ---------------------------------------------------------------------------

@dataclass
class AnswerResult:
    text: str                       # the answer to type ("" when unverifiable)
    verified: bool                  # passed every deterministic check
    checks: list[str]               # failed check names on the final attempt
    attempts: int                   # number of model calls made
    model: str                      # provider/model that produced it
    retrieved: list[str] = field(default_factory=list)  # exemplar questions used
    escalate: bool = False          # caller should leave blank / hand to human
    metadata: dict = field(default_factory=dict)


_SYSTEM_PROMPT = (
    "You are the candidate answering one question on a real job application. "
    "Write in the candidate's own first-person voice.\n"
    "Rules:\n"
    "- Be specific to THIS job; reference something concrete from the posting.\n"
    "- Use ONLY real achievements from the resume. NEVER invent a company, "
    "employer, metric, dollar figure, percentage, team, or outcome that is not "
    "in the resume.\n"
    "- For open-ended questions write 2-3 plain sentences. No fluff, no 'I am "
    "passionate about', no 'As an AI', no bracketed placeholders.\n"
    "- Output ONLY the answer text. No preamble, no quotes, no labels."
)


def _clean(raw: str) -> str:
    t = (raw or "").strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        t = t[1:-1].strip()
    return t


def _user_prompt(question, job, profile, resume_text, exemplars, kind, correction):
    job = job or {}
    personal = (profile or {}).get("personal", {})
    exp = (profile or {}).get("experience", {})

    parts = [
        "== JOB ==",
        f"Title: {job.get('title', '')}",
        f"Company: {job.get('site') or job.get('company', '')}",
        f"Description: {(job.get('description') or '')[:1200]}",
        "",
        "== ABOUT THE CANDIDATE ==",
        f"Name: {personal.get('full_name', '')}",
        f"Years of experience: {exp.get('years_of_experience_total', '')}",
        f"Target role: {exp.get('target_role', '')}",
        "",
        "== RESUME (the ONLY source of real achievements) ==",
        (resume_text or "")[:2500],
    ]

    if exemplars:
        parts.append("")
        parts.append("== HOW THE CANDIDATE HAS ANSWERED SIMILAR QUESTIONS (style + facts to reuse) ==")
        for e in exemplars:
            parts.append(f"Q: {e.question}\nA: {e.answer}")

    if correction:
        parts.append("")
        parts.append(
            "== FIX REQUIRED == Your previous answer failed these checks: "
            + ", ".join(correction)
            + ". Rewrite it. Do not invent any company, figure, or metric that is "
            "not in the resume above; drop unverifiable specifics; keep it to 2-3 "
            "sentences; never mention OpenAI."
        )

    parts.append("")
    parts.append(f"== QUESTION ==\n{question}")
    parts.append("\nWrite the answer now:")
    return "\n".join(parts)


def answer_question(
    question: str,
    *,
    job: dict,
    profile: dict,
    resume_text: str,
    corpus: AnswerCorpus | None = None,
    kind: str | None = None,
    client=None,
    max_attempts: int = 3,
) -> AnswerResult:
    """Produce a verified free-text answer, or escalate.

    Retrieves the candidate's most similar past answers, asks the (cheap) model,
    and runs the deterministic verifier. On failure it retries with a correction
    up to ``max_attempts``. If it still can't produce a clean answer it returns
    ``escalate=True`` with empty text -- an unverified answer is never returned
    as if it were good.
    """
    corpus = corpus if corpus is not None else AnswerCorpus()
    exemplars = corpus.top_k(question, k=3)
    retrieved = [e.question for e in exemplars]

    if client is None:
        client = get_client(stage="answer")
    model = getattr(client, "model", "unknown")

    correction: list[str] = []
    checks: list[str] = []
    for attempt in range(1, max_attempts + 1):
        user = _user_prompt(question, job, profile, resume_text, exemplars, kind, correction)
        raw = client.chat(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            temperature=0.4,
            max_tokens=400,
            stage="answer",
        )
        text = _clean(raw)
        checks = verify_answer(
            text, question, resume_text=resume_text, profile=profile, job=job, kind=kind
        )
        if not checks:
            return AnswerResult(
                text=text, verified=True, checks=[], attempts=attempt,
                model=model, retrieved=retrieved, escalate=False,
            )
        correction = checks

    return AnswerResult(
        text="", verified=False, checks=checks, attempts=max_attempts,
        model=model, retrieved=retrieved, escalate=True,
    )


def answer_question_bounded(
    question: str,
    *,
    job: dict,
    profile: dict,
    resume_text: str,
    corpus: AnswerCorpus | None = None,
    kind: str | None = None,
    client=None,
    local_transport=None,
    budget=None,
) -> AnswerResult:
    """Try local Qwen once, then allow at most one paid fallback call."""
    from applypilot.apply import qwen_provider
    from applypilot.apply.phase_budget import PhaseBudgetManager

    budget = budget or PhaseBudgetManager()

    corpus = corpus if corpus is not None else AnswerCorpus()
    exemplars = corpus.top_k(question, k=3)
    cached = corpus.find_compatible(
        question,
        job=job,
        profile=profile,
        resume_text=resume_text,
        kind=kind,
    )
    if cached is not None:
        return AnswerResult(
            text=cached.answer,
            verified=True,
            checks=[],
            attempts=0,
            model="cache/approved",
            retrieved=[cached.question],
            escalate=False,
            metadata={
                "cache_hit": True,
                "local_attempts": 0,
                "paid_fallback_calls": 0,
            },
        )
    local_result = None
    if qwen_provider.enabled():
        budget.check("answer")
        user_prompt = _user_prompt(
            question,
            job,
            profile,
            resume_text,
            exemplars,
            kind,
            correction=[],
        )
        with budget.track("answer"):
            local_result = qwen_provider.answer_locally(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                verify=lambda text: verify_answer(
                    text,
                    question,
                    resume_text=resume_text,
                    profile=profile,
                    job=job,
                    kind=kind,
                ),
                transport=local_transport,
            )
        if local_result.verified:
            return AnswerResult(
                text=local_result.text,
                verified=True,
                checks=[],
                attempts=1,
                model=f"local/{local_result.model}",
                retrieved=[item.question for item in exemplars],
                escalate=False,
                metadata={
                    "cache_hit": False,
                    "local_attempts": 1,
                    "paid_fallback_calls": 0,
                    "local_latency_ms": local_result.latency_ms,
                    "local_error": None,
                    "phase_budget": budget.metadata()["answer"],
                },
            )

    paid_reservation = float(os.environ.get("APPLYPILOT_PAID_ANSWER_RESERVATION_USD") or 0.02)
    budget.reserve("answer", turns=1, cost_usd=paid_reservation)
    with budget.track("answer"):
        paid = answer_question(
            question,
            job=job,
            profile=profile,
            resume_text=resume_text,
            corpus=corpus,
            kind=kind,
            client=client,
            max_attempts=1,
        )
    paid.metadata.update({
        "cache_hit": False,
        "local_attempts": 1 if local_result is not None else 0,
        "paid_fallback_calls": 1,
        "local_latency_ms": local_result.latency_ms if local_result else None,
        "local_error": local_result.error if local_result else "disabled",
        "local_checks": list(local_result.checks) if local_result else [],
        "phase_budget": budget.metadata()["answer"],
    })
    return paid


def default_corpus_path() -> Path:
    """The app-dir JSONL that accumulates approved answers across runs."""
    from applypilot import config

    return Path(config.APP_DIR) / "answers.jsonl"


def remember_answer(question, answer, *, job=None, path=None) -> Path:
    """Append one approved (question, answer, job) record to the corpus.

    This is the capture loop: every approved answer makes future retrieval
    richer. Appends a single JSONL line (never rewrites the file).
    """
    job = job or {}
    record = {
        "question": question,
        "answer": answer,
        "job_title": job.get("title", ""),
        "company": job.get("site") or job.get("company", ""),
    }
    p = Path(path) if path is not None else default_corpus_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return p
