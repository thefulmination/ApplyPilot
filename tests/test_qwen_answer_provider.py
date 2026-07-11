from __future__ import annotations

from applypilot.apply.answerer import answer_question_bounded


RESUME = (
    "Jonathan worked at Bloomberg building Python risk analytics for a $250M portfolio. "
    "He automated reconciliation and reduced reporting costs by 18%."
)
PROFILE = {"personal": {"full_name": "Jonathan"}, "experience": {"years_of_experience_total": "9"}}
JOB = {"title": "Quant Developer", "site": "Acme Capital", "description": "Python pricing models"}
GOOD = (
    "My Bloomberg experience building Python risk analytics for a $250M portfolio maps "
    "directly to your pricing work. I would bring the same disciplined automation approach "
    "that reduced reporting costs by 18%."
)
BAD = "I managed a $9 billion portfolio at Vandelay Industries."


class PaidClient:
    model = "paid-test"

    def __init__(self, reply=GOOD):
        self.reply = reply
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        return self.reply


def _answer(*, client, transport=None):
    return answer_question_bounded(
        "Why do you want this role?",
        job=JOB,
        profile=PROFILE,
        resume_text=RESUME,
        kind="motivation",
        client=client,
        local_transport=transport,
    )


def test_local_qwen_is_disabled_by_default_and_paid_call_is_bounded(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_LOCAL_QWEN_ENABLED", raising=False)
    paid = PaidClient()
    result = _answer(client=paid)
    assert result.verified is True
    assert paid.calls == 1
    assert result.metadata["local_attempts"] == 0
    assert result.metadata["paid_fallback_calls"] == 1


def test_verified_local_answer_avoids_paid_fallback(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_LOCAL_QWEN_ENABLED", "1")
    paid = PaidClient()
    result = _answer(
        client=paid,
        transport=lambda payload, timeout: {
            "message": {"content": f"<think>private reasoning</think>{GOOD}"}
        },
    )
    assert result.verified is True
    assert result.text == GOOD
    assert result.model == "local/qwen3:8b"
    assert paid.calls == 0
    assert result.metadata["paid_fallback_calls"] == 0
    assert "private reasoning" not in result.text


def test_unverified_local_answer_gets_exactly_one_paid_fallback(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_LOCAL_QWEN_ENABLED", "true")
    paid = PaidClient()
    result = _answer(
        client=paid,
        transport=lambda payload, timeout: {"message": {"content": BAD}},
    )
    assert result.verified is True
    assert paid.calls == 1
    assert result.metadata["local_attempts"] == 1
    assert result.metadata["paid_fallback_calls"] == 1
    assert result.metadata["local_checks"]


def test_local_timeout_gets_exactly_one_paid_fallback(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_LOCAL_QWEN_ENABLED", "1")
    paid = PaidClient()

    def timeout(_payload, _timeout):
        raise TimeoutError("local model unavailable")

    result = _answer(client=paid, transport=timeout)
    assert result.verified is True
    assert paid.calls == 1
    assert result.metadata["paid_fallback_calls"] == 1
    assert result.metadata["local_error"].startswith("TimeoutError:")


def test_failed_local_and_failed_single_paid_answer_escalate(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_LOCAL_QWEN_ENABLED", "1")
    paid = PaidClient(reply=BAD)
    result = _answer(
        client=paid,
        transport=lambda payload, timeout: {"message": {"content": BAD}},
    )
    assert result.verified is False
    assert result.escalate is True
    assert result.text == ""
    assert paid.calls == 1
