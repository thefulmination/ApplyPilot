import json

from applypilot.apply.ashby_adapter import (
    apply_ashby,
    build_answer_plan,
    extract_form_definition,
    parse_ashby_url,
)


def _html(fields):
    data = {
        "posting": {"applicationForm": {"formDefinition": {"sections": [
            {"title": "Application", "fields": fields}
        ]}}}
    }
    return f"<script>window.__appData = {json.dumps(data)};</script>"


def _field(title, path, kind="String", required=True, values=None):
    field = {"title": title, "path": path, "type": kind}
    if values is not None:
        field["selectableValues"] = [{"label": value} for value in values]
    return {"field": field, "isRequired": required}


PROFILE = {
    "personal": {
        "full_name": "Jane Candidate", "email": "jane@example.com",
        "phone": "555-0100", "city": "Hoboken", "province_state": "NJ",
        "country": "United States", "linkedin": "https://linkedin.com/in/jane",
    },
    "work_authorization": {
        "legally_authorized_to_work": True, "require_sponsorship": False,
    },
}


class Locator:
    def __init__(self, calls, label):
        self.calls, self.label = calls, label

    def fill(self, value): self.calls.append(("fill", self.label, value))
    def set_input_files(self, value): self.calls.append(("file", self.label, value))
    def select_option(self, **kwargs): self.calls.append(("select", self.label, kwargs))
    def check(self): self.calls.append(("check", self.label))
    def click(self): self.calls.append(("click", self.label))


class Page:
    def __init__(self, html, url="https://jobs.ashbyhq.com/acme/job/application"):
        self._html, self.url, self.calls = html, url, []

    def content(self): return self._html
    def get_by_label(self, label, exact=True): return Locator(self.calls, label)
    def get_by_role(self, role, name, exact=True): return Locator(self.calls, name)


def test_parse_and_extract_structured_form_definition():
    assert parse_ashby_url("https://jobs.ashbyhq.com/acme/abc/application") == ("acme", "abc")
    form = extract_form_definition(_html([_field("Email", "_systemfield_email", "Email")]))
    assert form[0].title == "Email" and form[0].required is True


def test_plan_maps_facts_and_verified_free_text_only():
    fields = extract_form_definition(_html([
        _field("Legal Name", "_systemfield_name"),
        _field("Email", "_systemfield_email", "Email"),
        _field("Resume", "_systemfield_resume", "File"),
        _field("Why this role?", "why", "LongText"),
    ]))
    answer = type("Answer", (), {"verified": True, "text": "Grounded answer"})()
    plan = build_answer_plan(fields, profile=PROFILE, resume_text="resume", answer_fn=lambda *_a, **_k: answer)
    assert plan.ready is True
    assert plan.values["_systemfield_name"] == "Jane Candidate"
    assert plan.values["why"] == "Grounded answer"


def test_unknown_required_field_stops_before_submit():
    fields = extract_form_definition(_html([_field("Secret clearance code", "secret")]))
    unverified = type("Answer", (), {"verified": False, "text": "guess"})()
    plan = build_answer_plan(fields, profile=PROFILE, resume_text="", answer_fn=lambda *_a, **_k: unverified)
    assert plan.ready is False
    assert plan.unmapped_required == ["Secret clearance code"]


def test_submit_without_positive_confirmation_is_owned_no_confirmation():
    html = _html([
        _field("Legal Name", "_systemfield_name"),
        _field("Email", "_systemfield_email", "Email"),
    ])
    page = Page(html)
    result = apply_ashby(
        "https://jobs.ashbyhq.com/acme/job/application", page=page,
        profile=PROFILE, resume_text="", resume_path=None, dry_run=False,
    )
    assert result["submit_attempted"] is True
    assert result["status"] == "failed:no_confirmation"
    assert ("click", "Submit Application") in page.calls


def test_positive_confirmation_is_applied():
    html = _html([
        _field("Legal Name", "_systemfield_name"),
        _field("Email", "_systemfield_email", "Email"),
    ]) + " Thank you for applying. Your application was submitted."
    result = apply_ashby(
        "https://jobs.ashbyhq.com/acme/job/application", page=Page(html),
        profile=PROFILE, resume_text="", resume_path=None, dry_run=False,
    )
    assert result["status"] == "applied"
