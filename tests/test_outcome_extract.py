from applypilot.outcome_extract import ExtractedOutcome, extract_outcome


class FakeClient:
    def __init__(self, reply): self._reply = reply
    def chat(self, messages, **kw): return self._reply


def test_extract_parses_valid_json():
    reply = (
        '{"stage": "rejected", "outcome": "rejected", '
        '"reason": "position was filled internally", '
        '"title": "Quant Analyst", "company": "Acme", "confidence": "high"}'
    )
    r = extract_outcome("Update on your application", "We went another direction.",
                        "careers@acme.com", client=FakeClient(reply))
    assert r.stage == "rejected"
    assert r.outcome == "rejected"
    assert r.reason == "position was filled internally"
    assert r.title == "Quant Analyst"
    assert r.company == "Acme"
    assert r.extracted_by != "heuristic_fallback"


def test_extract_handles_json_wrapped_in_text():
    reply = 'Here is the result:\n{"stage":"interview","outcome":null,"reason":null,"title":null,"company":null,"confidence":"medium"}\nDone.'
    r = extract_outcome("Interview invitation", "Use the calendly link.", client=FakeClient(reply))
    assert r.stage == "interview"
    assert r.outcome is None


def test_extract_clamps_invalid_stage_to_other():
    reply = '{"stage":"banana","outcome":"banana","reason":null,"title":null,"company":null,"confidence":"low"}'
    r = extract_outcome("hi", "hi", client=FakeClient(reply))
    assert r.stage == "other"
    assert r.outcome is None


def test_extract_falls_back_to_heuristic_when_client_raises():
    class Boom:
        def chat(self, messages, **kw): raise RuntimeError("model down")
    r = extract_outcome(
        "Unfortunately we won't be moving forward",
        "After careful consideration we have decided to pursue other candidates.",
        client=Boom(),
    )
    assert r.stage == "rejected"
    assert r.outcome == "rejected"
    assert r.extracted_by == "heuristic_fallback"
    assert r.reason is None


def test_extract_falls_back_on_unparseable_reply():
    class Junk:
        def chat(self, messages, **kw): return "no json here at all"
    r = extract_outcome("Thanks for applying", "We received your application.", client=Junk())
    assert r.stage == "acknowledged"
    assert r.extracted_by == "heuristic_fallback"
