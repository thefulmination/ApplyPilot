from __future__ import annotations

from applypilot.apply.answerer import AnswerCorpus, PastAnswer, answer_question_bounded


RESUME = "Built Python risk analytics at Bloomberg and automated reconciliation workflows."
PROFILE = {"personal": {"full_name": "Candidate"}}
JOB = {"title": "Quant Developer", "site": "Acme Capital", "description": "Python pricing models"}
ANSWER = (
    "My Bloomberg experience building Python risk analytics aligns directly with your pricing work. "
    "I would bring the same disciplined approach to automating reconciliation workflows for this team."
)


class PaidClient:
    model = "paid"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        return ANSWER


def test_compatible_approved_answer_skips_local_and_paid_calls(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_LOCAL_QWEN_ENABLED", "1")
    corpus = AnswerCorpus([PastAnswer(
        "Why do you want this role?",
        ANSWER,
        job_title="Quant Developer",
        company="Acme Capital",
        kind="motivation",
        approved=True,
    )])
    paid = PaidClient()
    local_calls = []
    result = answer_question_bounded(
        "Why do you want this role?",
        job=JOB,
        profile=PROFILE,
        resume_text=RESUME,
        corpus=corpus,
        kind="motivation",
        client=paid,
        local_transport=lambda payload, timeout: local_calls.append(1),
    )
    assert result.verified is True
    assert result.model == "cache/approved"
    assert result.metadata["cache_hit"] is True
    assert result.attempts == 0
    assert paid.calls == 0
    assert local_calls == []


def test_unapproved_or_role_incompatible_answer_is_not_reused(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_LOCAL_QWEN_ENABLED", raising=False)
    corpus = AnswerCorpus([
        PastAnswer("Why do you want this role?", ANSWER, job_title="Nurse", approved=True),
        PastAnswer("Why do you want this role?", ANSWER, job_title="Quant Developer", approved=False),
    ])
    paid = PaidClient()
    result = answer_question_bounded(
        "Why do you want this role?",
        job=JOB,
        profile=PROFILE,
        resume_text=RESUME,
        corpus=corpus,
        kind="motivation",
        client=paid,
    )
    assert result.metadata["cache_hit"] is False
    assert paid.calls == 1


def test_stale_company_grounding_fails_current_job_verification(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_LOCAL_QWEN_ENABLED", raising=False)
    stale = (
        "Acme Capital offers the exact environment I want for quantitative pricing work. "
        "My Bloomberg risk analytics experience would help its trading team automate reconciliation."
    )
    corpus = AnswerCorpus([PastAnswer(
        "Why do you want this role?", stale, job_title="Quant Developer", company="Acme Capital"
    )])
    paid = PaidClient()
    result = answer_question_bounded(
        "Why do you want this role?",
        job={"title": "Quant Developer", "site": "Beta Systems", "description": "Python pricing"},
        profile=PROFILE,
        resume_text=RESUME,
        corpus=corpus,
        kind="motivation",
        client=paid,
    )
    assert result.model == "paid"
    assert result.metadata["cache_hit"] is False
    assert paid.calls == 1


def test_cache_roundtrip_preserves_approval_and_kind(tmp_path):
    corpus = AnswerCorpus()
    corpus.add(
        "Why do you want this role?",
        ANSWER,
        job_title="Quant Developer",
        kind="motivation",
        approved=True,
    )
    path = tmp_path / "answers.jsonl"
    corpus.save_jsonl(path)
    loaded = AnswerCorpus.from_jsonl(path)
    match = loaded.find_compatible(
        "Why do you want this role?",
        job=JOB,
        profile=PROFILE,
        resume_text=RESUME,
        kind="motivation",
    )
    assert match is not None
    assert match.approved is True
    assert match.kind == "motivation"
