"""Tests for the deterministic Lever apply adapter (DOM-discovery read + map).

Lever has no clean questions API: custom questions live in cards[uuid][fieldN]
whose meaning is only in the DOM label, so fields are discovered from the page.
The mapper reuses the answerer for free-text and never fakes a required field.
Submitting is captcha-gated on Lever, so the deterministic path fills and the
agent still owns submission -- out of scope for these read+map tests.
No browser here: the page's field-discovery result and the answerer are injected.
"""

from applypilot.apply.lever_adapter import (
    build_lever_plan,
    discover_fields,
    normalize_fields,
    parse_lever_url,
)


# --- parse_lever_url -------------------------------------------------------

def test_parse_standard_lever_url():
    assert parse_lever_url("https://jobs.lever.co/match/abc-123-uuid") == ("match", "abc-123-uuid")


def test_parse_lever_apply_url():
    assert parse_lever_url("https://jobs.lever.co/match/abc-123-uuid/apply") == ("match", "abc-123-uuid")


def test_parse_non_lever_returns_none():
    assert parse_lever_url("https://boards.greenhouse.io/acme/jobs/1") is None


def test_parse_lever_without_id_returns_none():
    assert parse_lever_url("https://jobs.lever.co/match") is None


# --- normalize_fields (drop Lever's system/hidden plumbing) -----------------

def test_normalize_drops_system_and_hidden_fields_keeps_answerable():
    raw = [
        {"name": "name", "type": "text", "label": "Full name", "required": True, "options": []},
        {"name": "accountId", "type": "text", "label": "", "required": False, "options": []},
        {"name": "cards[u][baseTemplate]", "type": "text", "label": "", "required": False, "options": []},
        {"name": "h-captcha-response", "type": "text", "label": "", "required": False, "options": []},
        {"name": "origin", "type": "hidden", "label": "", "required": False, "options": []},
        {"name": "cards[u][field0]", "type": "textarea", "label": "Why?", "required": False, "options": []},
    ]
    names = [f["name"] for f in normalize_fields(raw)]
    assert names == ["name", "cards[u][field0]"]


def test_discover_fields_normalizes_the_pages_raw_result():
    class FakePage:
        def evaluate(self, js):
            return [
                {"name": "email", "type": "text", "label": "Email", "required": True, "options": []},
                {"name": "resumeStorageId", "type": "text", "label": "", "required": False, "options": []},
            ]
    assert [f["name"] for f in discover_fields(FakePage())] == ["email"]


# --- build_lever_plan ------------------------------------------------------

PROFILE = {
    "personal": {"full_name": "Jordan Rivera", "email": "j@x.com", "phone": "5551234567",
                 "city": "Jersey City", "province_state": "NJ", "country": "USA",
                 "linkedin_url": "https://linkedin.com/in/jordan",
                 "portfolio_url": "https://jordan.dev"},
    "work_authorization": {"legally_authorized_to_work": "Yes", "require_sponsorship": "No"},
    "experience": {"current_company": "Acme Quant"},
}
RESUME = "Quant developer, Python, supported a $300M book."


class _Ans:
    def __init__(self):
        self.verified = True
        self.text = "Because your Python risk work matches my background."


def _good(question, **kw):
    return _Ans()


def _bad(question, **kw):
    a = _Ans()
    a.verified = False
    return a


def _std_fields():
    return [
        {"name": "name", "type": "text", "label": "Full name", "required": True, "options": []},
        {"name": "email", "type": "text", "label": "Email", "required": True, "options": []},
        {"name": "phone", "type": "text", "label": "Phone", "required": False, "options": []},
        {"name": "resume", "type": "file", "label": "Resume", "required": True, "options": []},
        {"name": "urls[LinkedIn]", "type": "text", "label": "LinkedIn URL", "required": False, "options": []},
    ]


def _plan(extra=None, answer_fn=_good):
    return build_lever_plan(_std_fields() + (extra or []), profile=PROFILE,
                            resume_text=RESUME, answer_fn=answer_fn, job={"site": "match"})


def test_maps_lever_standard_fields_and_urls():
    plan = _plan()
    assert plan.fields["name"] == "Jordan Rivera"
    assert plan.fields["email"] == "j@x.com"
    assert plan.fields["phone"] == "5551234567"
    assert plan.fields["urls[LinkedIn]"] == "https://linkedin.com/in/jordan"
    assert plan.resume_field == "resume"


def test_standard_only_lever_form_is_ready():
    plan = _plan()
    assert plan.ready is True
    assert plan.unmapped_required == []


def test_card_textarea_goes_through_the_answerer():
    q = [{"name": "cards[u][field0]", "type": "textarea", "label": "Why do you want this role?",
          "required": False, "options": []}]
    plan = _plan(q)
    assert plan.fields["cards[u][field0]"].startswith("Because your Python")
    assert "cards[u][field0]" in plan.free_text


def test_card_work_authorization_select_maps_to_yes():
    q = [{"name": "cards[u][field1]", "type": "select",
          "label": "Are you legally authorized to work in the US?", "required": True,
          "options": [{"label": "Yes", "value": "Yes"}, {"label": "No", "value": "No"}]}]
    assert _plan(q).fields["cards[u][field1]"] == "Yes"


def test_card_demographic_select_declines():
    q = [{"name": "cards[u][field2]", "type": "select", "label": "Gender", "required": False,
          "options": [{"label": "Male", "value": "Male"},
                      {"label": "Decline to self identify", "value": "Decline"}]}]
    assert _plan(q).fields["cards[u][field2]"] == "Decline"


def test_required_unmappable_card_blocks_ready():
    q = [{"name": "cards[u][field3]", "type": "text", "label": "What is your favorite framework?",
          "required": True, "options": []}]
    plan = _plan(q)
    assert plan.ready is False
    assert any("favorite framework" in u.lower() for u in plan.unmapped_required)
