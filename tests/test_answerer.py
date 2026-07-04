"""Tests for the Tier-0 free-text application answerer.

The answerer produces a short, verified free-text answer to an application
question using a cheap model, retrieval over past approved answers, and a
deterministic verification layer that NEVER lets an unverified answer through.
"""

from applypilot.apply import answerer as answerer_mod
from applypilot.apply.answerer import (
    AnswerCorpus,
    AnswerResult,
    answer_question,
    remember_answer,
    verify_answer,
)


# --- shared fixtures -------------------------------------------------------

RESUME = (
    "Jonathan Stallone. Quantitative finance professional, 9.5 years of "
    "experience. Software engineer at Bloomberg building risk analytics for a "
    "$250M portfolio. Reduced reporting costs by 18% and automated the "
    "reconciliation pipeline. BS in Quantitative Finance, Stevens Institute."
)

PROFILE = {
    "personal": {"full_name": "Jonathan Stallone", "city": "Hoboken"},
    "experience": {"years_of_experience_total": "9.5", "target_role": "Quant Developer"},
}

JOB = {"title": "Quant Developer", "site": "Acme Capital",
       "description": "Build pricing models in Python for a trading desk."}


def _verify(text, question="Why do you want this role?", kind="motivation"):
    return verify_answer(text, question, resume_text=RESUME, profile=PROFILE, job=JOB, kind=kind)


# --- verify_answer ---------------------------------------------------------

def test_verify_passes_a_grounded_specific_answer():
    good = (
        "Your trading-desk pricing work maps directly to my nine years building "
        "risk analytics at Bloomberg, where I automated a reconciliation pipeline "
        "and cut reporting costs by 18%. I'd bring that same Python-heavy, "
        "detail-first approach to your models."
    )
    assert _verify(good) == []


def test_verify_rejects_empty():
    assert "empty" in _verify("   ")


def test_verify_rejects_openai_mention():
    # Hard rule: never surface OpenAI in an application.
    assert "banned_openai" in _verify(
        "I'm excited because I have long admired OpenAI and your mission-driven work here."
    )


def test_verify_rejects_ai_tells_and_placeholders():
    assert "ai_tells" in _verify("As an AI language model, I am passionate about this role at your company.")
    assert "ai_tells" in _verify("I would be a great fit for the [Company] team because I love [role].")


def test_verify_rejects_too_short_for_open_ended():
    assert "length" in _verify("I love finance.")


def test_verify_rejects_fabricated_dollar_and_percent_metrics():
    # $4 billion and 45% appear nowhere in the resume -> fabricated.
    failed = _verify(
        "At Bloomberg I personally managed a $4 billion book and boosted returns by 45% in one year."
    )
    assert "fabricated_metric" in failed


def test_verify_allows_metrics_that_are_in_the_resume():
    # $250M and 18% are both grounded in the resume text.
    failed = _verify(
        "At Bloomberg I supported a $250M portfolio and reduced reporting costs by 18% through automation."
    )
    assert "fabricated_metric" not in failed


def test_verify_rejects_fabricated_employer_with_corporate_suffix():
    # "Vandelay Industries" is a company the candidate never worked at.
    failed = _verify(
        "I sharpened my modeling craft leading the analytics team at Vandelay Industries for six years."
    )
    assert "fabricated_company" in failed


def test_verify_does_not_flag_the_real_resume_employer():
    # Bloomberg is in the resume; the job's own company (Acme Capital) is context.
    failed = _verify(
        "My work at Bloomberg on risk analytics is exactly what Acme Capital needs for its trading desk models."
    )
    assert "fabricated_company" not in failed


# --- AnswerCorpus (lexical retrieval over past approved answers) -----------

def _corpus():
    c = AnswerCorpus()
    c.add("Why do you want to work at our company?", "Because your mission fits mine.")
    c.add("Tell us about a challenging project you led.", "I led the reconciliation rebuild.")
    c.add("What are your salary expectations?", "Targeting the midpoint of your range.")
    return c


def test_empty_corpus_returns_empty_list():
    assert AnswerCorpus().top_k("anything at all") == []


def test_top_k_ranks_the_most_lexically_similar_answer_first():
    hits = _corpus().top_k("What salary range should we discuss for this role?", k=1)
    assert len(hits) == 1
    assert "salary" in hits[0].question.lower()


def test_top_k_excludes_zero_overlap_records():
    # Nothing in the corpus shares a meaningful word with this question.
    assert _corpus().top_k("Explain quantum chromodynamics to a unicorn.") == []


def test_top_k_respects_k_limit():
    c = AnswerCorpus()
    for i in range(5):
        c.add(f"Describe your experience with project management tool {i}.", f"answer {i}")
    assert len(c.top_k("Describe your project management experience.", k=2)) == 2


def test_corpus_roundtrips_through_jsonl(tmp_path):
    path = tmp_path / "answers.jsonl"
    _corpus().save_jsonl(path)
    loaded = AnswerCorpus.from_jsonl(path)
    hits = loaded.top_k("What salary do you expect?", k=1)
    assert hits and "salary" in hits[0].question.lower()


# --- answer_question (retrieve -> cheap model -> verify -> retry) ----------

GOOD = (
    "Your trading-desk pricing work maps directly to my nine years building "
    "risk analytics at Bloomberg, where I automated a reconciliation pipeline "
    "and cut reporting costs by 18%. I'd bring that same Python-heavy, "
    "detail-first approach to your models."
)
# $4 billion and 45% are fabricated (not in the resume) -> fails verification.
BAD = "At Bloomberg I personally managed a $4 billion book and boosted returns by 45%."


class FakeClient:
    """Stands in for the LLMClient. Returns queued replies, sticking on the
    last one, and records every message list it was handed."""

    def __init__(self, replies, model="deepseek-chat"):
        self._replies = list(replies)
        self._i = 0
        self.model = model
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append(messages)
        reply = self._replies[min(self._i, len(self._replies) - 1)]
        self._i += 1
        return reply


def _answer(client, corpus=None, question="Why do you want this role?", **kw):
    return answer_question(
        question,
        job=JOB,
        profile=PROFILE,
        resume_text=RESUME,
        corpus=corpus,
        client=client,
        **kw,
    )


def test_returns_verified_answer_on_a_good_first_try():
    res = _answer(FakeClient([GOOD]))
    assert isinstance(res, AnswerResult)
    assert res.verified is True
    assert res.escalate is False
    assert res.text == GOOD
    assert res.attempts == 1
    assert res.model == "deepseek-chat"


def test_retries_after_a_failed_verification_then_succeeds():
    client = FakeClient([BAD, GOOD])
    res = _answer(client)
    assert res.verified is True
    assert res.text == GOOD
    assert res.attempts == 2
    assert len(client.calls) == 2


def test_escalates_and_returns_no_text_when_every_attempt_fails():
    res = _answer(FakeClient([BAD]), max_attempts=3)
    assert res.verified is False
    assert res.escalate is True
    assert res.text == ""
    assert res.attempts == 3
    assert "fabricated_metric" in res.checks


def test_injects_retrieved_past_answers_into_the_prompt():
    corpus = AnswerCorpus()
    corpus.add("Why are you interested in this analytics role?",
               "MISSION_FIT_MARKER: your analytics focus matches my Bloomberg work.")
    client = FakeClient([GOOD])
    res = _answer(client, corpus=corpus,
                  question="Why are you interested in this role?", kind="motivation")
    sent = str(client.calls[0])
    assert "MISSION_FIT_MARKER" in sent
    assert "Why are you interested in this analytics role?" in res.retrieved


def test_remember_answer_appends_a_retrievable_record(tmp_path):
    path = tmp_path / "answers.jsonl"
    remember_answer("Why do you want this salary range?", "Because it fits my level.",
                    job=JOB, path=path)
    remember_answer("Describe a leadership moment.", "I led the rebuild.", path=path)
    hits = AnswerCorpus.from_jsonl(path).top_k("What salary should we discuss?", k=1)
    assert hits and hits[0].answer == "Because it fits my level."


def test_remember_answer_appends_rather_than_overwrites(tmp_path):
    path = tmp_path / "answers.jsonl"
    remember_answer("q about salary", "a1", path=path)
    remember_answer("q about leadership", "a2", path=path)
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_defaults_to_the_cheap_answer_stage_client(monkeypatch):
    captured = {}

    def fake_get_client(*, stage=None, **kw):
        captured["stage"] = stage
        return FakeClient([GOOD])

    monkeypatch.setattr(answerer_mod, "get_client", fake_get_client)
    res = answer_question("Why do you want this role?", job=JOB, profile=PROFILE,
                          resume_text=RESUME)
    assert captured["stage"] == "answer"
    assert res.verified is True
