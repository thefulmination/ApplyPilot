"""Tests for the deterministic Greenhouse submit path.

plan_form_actions turns an AnswerPlan into an ordered list of deterministic
browser actions; execute_form runs them against an injected page. The safety
contract: execute_form DEFAULTS to dry-run and never clicks submit unless
explicitly told to. No real browser and no real submission in these tests.
"""

import pytest

from applypilot.apply.greenhouse_adapter import AnswerPlan
from applypilot.apply.greenhouse_submit import (
    RequiredFormFieldsError,
    SubmitReport,
    _complete_greenhouse_email_challenge,
    adapter_enabled,
    apply_greenhouse,
    capture_answers,
    decide_route,
    detect_confirmation,
    execute_form,
    plan_form_actions,
    submit_enabled,
)


QUESTIONS = [
    {"fields": [{"name": "first_name", "type": "input_text"}]},
    {"fields": [{"name": "email", "type": "input_text"}]},
    {"fields": [{"name": "resume", "type": "input_file"}]},
    {"fields": [{"name": "resume_text", "type": "textarea"}]},
    {"fields": [{"name": "question_1", "type": "textarea"}]},
    {"fields": [{"name": "question_2", "type": "multi_value_single_select",
                 "values": [{"label": "Yes", "value": 1}]}]},
]


def _ready_plan():
    return AnswerPlan(
        fields={"first_name": "Jordan", "email": "j@x.com", "resume_text": "REAL RESUME",
                "question_1": "why text", "question_2": 1},
        resume_field="resume", free_text={"question_1": "why text"},
        unmapped_required=[], ready=True,
    )


class FakePage:
    def __init__(self):
        self.calls = []
        self._content = ""
        self.url = "https://boards.greenhouse.io/acme/jobs/123"

    def set_content(self, html):
        self._content = html

    def content(self):
        return self._content

    def fill(self, selector, value):
        self.calls.append(("fill", selector, value))

    def set_input_files(self, selector, value):
        self.calls.append(("file", selector, value))

    def select_option(self, selector, value):
        self.calls.append(("select", selector, value))

    def click(self, selector):
        self.calls.append(("click", selector))


# --- plan_form_actions -----------------------------------------------------

def test_plan_form_actions_maps_each_type_to_the_right_action():
    acts = [(a.kind, a.selector, a.value)
            for a in plan_form_actions(_ready_plan(), QUESTIONS, resume_path="/r.pdf")]
    assert ("file", "#resume", "/r.pdf") in acts
    assert ("fill", "#first_name", "Jordan") in acts
    assert not any(selector == "#resume_text" for _, selector, _ in acts)
    assert ("textarea", "#question_1", "why text") in acts
    assert ("select", "#question_2", 1) in acts


def test_plan_form_actions_keeps_manual_resume_when_no_file_is_available():
    acts = [(action.kind, action.selector, action.value)
            for action in plan_form_actions(_ready_plan(), QUESTIONS, resume_path=None)]

    assert ("textarea", "#resume_text", "REAL RESUME") in acts


def test_plan_form_actions_uses_select_for_multi_select_fields():
    plan = AnswerPlan(
        fields={"location_q[]": 41},
        resume_field=None,
        free_text={},
        unmapped_required=[],
        ready=True,
    )
    questions = [
        {"fields": [{"name": "location_q[]", "type": "multi_value_multi_select",
                     "values": [{"label": "CA | San Francisco", "value": 41}]}]},
    ]

    actions = plan_form_actions(plan, questions)

    assert ("select", '[id="location_q[]"]', 41, "CA | San Francisco") in [
        (action.kind, action.selector, action.value, action.option_label) for action in actions
    ]


def test_plan_form_actions_uses_attribute_selector_for_numeric_demographic_id():
    plan = AnswerPlan(
        fields={"864": 4660}, resume_field=None, free_text={},
        unmapped_required=[], ready=True,
    )
    questions = [{"fields": [{
        "name": "864", "type": "multi_value_multi_select",
        "values": [{"label": "I don't wish to answer", "value": 4660}],
    }]}]

    actions = plan_form_actions(plan, questions)

    assert ("select", '[id="864"]', 4660) in [
        (action.kind, action.selector, action.value) for action in actions
    ]


def test_plan_form_actions_maps_phone_country_widget():
    plan = AnswerPlan(
        fields={"country": "us"},
        resume_field=None,
        free_text={},
        unmapped_required=[],
        ready=True,
    )
    questions = [
        {"fields": [{"name": "country", "type": "phone_country_select"}]},
    ]

    actions = plan_form_actions(plan, questions)

    assert ("phone_country", "#country", "us") in [
        (action.kind, action.selector, action.value) for action in actions
    ]


def test_plan_form_actions_maps_location_and_coordinates_to_real_dom_controls():
    plan = AnswerPlan(
        fields={
            "location": "San Francisco, California, United States",
        },
        resume_field=None, free_text={}, unmapped_required=[], ready=True,
    )
    questions = [
        {"fields": [{"name": "location", "type": "location_autocomplete"}]},
        {"fields": [{"name": "latitude", "type": "location_virtual"}]},
        {"fields": [{"name": "longitude", "type": "location_virtual"}]},
    ]

    actions = plan_form_actions(plan, questions)

    assert ("location", "#candidate-location", "San Francisco, California, United States") in [
        (action.kind, action.selector, action.value) for action in actions
    ]
    assert not any(action.selector in {"#latitude", "#longitude"} for action in actions)


def test_execute_form_selects_location_autocomplete_option():
    class LocationInput:
        def __init__(self, calls):
            self.calls = calls

        def click(self):
            self.calls.append(("location_click",))

        def fill(self, value):
            self.calls.append(("location_fill", value))

        def press_sequentially(self, value, *, delay):
            self.calls.append(("location_type", value, delay))

    class LocationOption:
        def __init__(self, calls, name):
            self.calls = calls
            self.name = name

        def click(self):
            self.calls.append(("location_option", self.name))

    class LocationPage(FakePage):
        def locator(self, selector):
            assert selector == "#candidate-location"
            return LocationInput(self.calls)

        def get_by_role(self, role, *, name, exact):
            assert role == "option" and exact is True
            return LocationOption(self.calls, name)

    actions = [
        type("Action", (), {"kind": "location", "selector": "#candidate-location",
                             "value": "San Francisco, California, United States"})(),
    ]
    page = LocationPage()

    execute_form(actions, page)

    assert ("location_type", "San Francisco, California, United States", 50) in page.calls
    assert ("location_option", "San Francisco, California, United States") in page.calls


def test_plan_form_actions_puts_submit_last():
    acts = plan_form_actions(_ready_plan(), QUESTIONS, resume_path="/r.pdf")
    assert acts[-1].kind == "submit"
    assert [a for a in acts if a.kind == "submit"][0].selector == "button[type='submit']"


def test_execute_form_selects_react_combobox_option_by_label():
    class FakeLocator:
        def evaluate(self, script):
            return "input"

    class FakeOption:
        def __init__(self, calls, label):
            self.calls = calls
            self.label = label

        def click(self):
            self.calls.append(("option", self.label))

    class FakeReactPage(FakePage):
        def locator(self, selector):
            self.calls.append(("locator", selector))
            return FakeLocator()

        def get_by_role(self, role, *, name, exact):
            self.calls.append(("role", role, name, exact))
            return FakeOption(self.calls, name)

    plan = AnswerPlan(
        fields={"location_q[]": 41},
        resume_field=None,
        free_text={},
        unmapped_required=[],
        ready=True,
    )
    questions = [
        {"fields": [{"name": "location_q[]", "type": "multi_value_multi_select",
                     "values": [{"label": "CA | San Francisco", "value": 41}]}]},
    ]
    page = FakeReactPage()

    execute_form(plan_form_actions(plan, questions), page)

    assert ("click", '[id="location_q[]"]') in page.calls
    assert ("option", "CA | San Francisco") in page.calls


def test_execute_form_selects_phone_country_by_country_code():
    class ClickTarget:
        def __init__(self, calls, value):
            self.calls = calls
            self.value = value

        def click(self):
            self.calls.append(("click_target", self.value))

    class CountryContainer:
        def __init__(self, calls):
            self.calls = calls

        def get_by_role(self, role):
            return ClickTarget(self.calls, role)

    class InputLocator:
        def __init__(self, calls):
            self.calls = calls

        def locator(self, selector):
            self.calls.append(("ancestor", selector))
            return CountryContainer(self.calls)

        def get_attribute(self, name):
            assert name == "aria-controls"
            return "react-select-country-listbox"

    class CountryListbox:
        def __init__(self, calls):
            self.calls = calls

        def get_by_role(self, role, *, name, exact):
            self.calls.append(("country_option", role, name, exact))
            return ClickTarget(self.calls, name)

    class PhoneCountryPage(FakePage):
        def locator(self, selector):
            self.calls.append(("locator", selector))
            if selector == "#react-select-country-listbox":
                return CountryListbox(self.calls)
            return InputLocator(self.calls)

    action_plan = AnswerPlan(
        fields={"country": "us"}, resume_field=None, free_text={},
        unmapped_required=[], ready=True,
    )
    questions = [{"fields": [
        {"name": "country", "type": "phone_country_select", "values": [
            {"label": "United States +1", "value": "us"},
        ]},
    ]}]
    page = PhoneCountryPage()

    execute_form(plan_form_actions(action_plan, questions), page)

    assert ("click_target", "button") in page.calls
    assert ("click_target", "United States +1") in page.calls


def test_plan_form_actions_never_touches_unplanned_fields():
    acts = plan_form_actions(_ready_plan(), QUESTIONS, resume_path="/r.pdf")
    selectors = {a.selector for a in acts}
    assert "#question_3" not in selectors  # not in the plan -> no action


# --- execute_form (safety-critical) ----------------------------------------

def test_execute_form_dry_run_fills_but_NEVER_clicks_submit():
    page = FakePage()
    rep = execute_form(plan_form_actions(_ready_plan(), QUESTIONS, resume_path="/r.pdf"), page)
    assert rep.dry_run is True
    assert rep.submitted is False
    assert rep.skipped_submit is True
    assert ("click", "button[type='submit']") not in page.calls
    assert ("fill", "#first_name", "Jordan") in page.calls
    assert ("file", "#resume", "/r.pdf") in page.calls


def test_execute_form_clicks_submit_only_when_dry_run_false():
    page = FakePage()
    rep = execute_form(plan_form_actions(_ready_plan(), QUESTIONS, resume_path="/r.pdf"),
                       page, dry_run=False)
    assert rep.submitted is True
    assert ("click", "button[type='submit']") in page.calls


def test_execute_form_captures_expected_greenhouse_post_response():
    class Request:
        method = "POST"

    class Response:
        request = Request()
        url = "https://boards.greenhouse.io/acme/jobs/123"
        status = 200

        def header_value(self, name):
            return "request-123" if name == "x-request-id" else None

    class ExpectedResponse:
        value = Response()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class ResponsePage(FakePage):
        def expect_response(self, predicate, *, timeout):
            self.calls.append(("expect_response", timeout))
            assert predicate(Response())
            return ExpectedResponse()

    page = ResponsePage()
    report = execute_form(
        plan_form_actions(_ready_plan(), QUESTIONS, resume_path="/r.pdf"),
        page,
        dry_run=False,
        expected_submit_url="https://boards.greenhouse.io/acme/jobs/123",
    )

    assert report.response_status == 200
    assert report.response_url == "https://boards.greenhouse.io/acme/jobs/123"
    assert report.response_request_id == "request-123"


def test_execute_form_does_not_click_twice_when_response_wait_times_out():
    class TimeoutResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            raise TimeoutError("no matching response")

    class TimeoutPage(FakePage):
        def expect_response(self, predicate, *, timeout):
            return TimeoutResponse()

    page = TimeoutPage()
    report = execute_form(
        plan_form_actions(_ready_plan(), QUESTIONS, resume_path="/r.pdf"),
        page,
        dry_run=False,
        expected_submit_url="https://boards.greenhouse.io/acme/jobs/123",
    )

    assert page.calls.count(("click", "button[type='submit']")) == 1
    assert report.response_wait_error == "TimeoutError"


def test_greenhouse_email_challenge_uses_fresh_eight_digit_code_and_retries_submit():
    class Request:
        method = "POST"

    class Response:
        request = Request()
        url = "https://boards.greenhouse.io/alpaca/jobs/123"
        status = 200

        def header_value(self, name):
            return "request-456" if name == "x-request-id" else None

        def json(self):
            return {}

    class ExpectedResponse:
        value = Response()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class ChallengePage(FakePage):
        def wait_for_selector(self, selector, *, state, timeout):
            self.calls.append(("wait", selector, state, timeout))

        def expect_response(self, predicate, *, timeout):
            assert predicate(Response())
            return ExpectedResponse()

    page = ChallengePage()
    report = SubmitReport(
        dry_run=False,
        submitted=True,
        response_status=428,
        response_code="captcha-failed",
    )

    completed = _complete_greenhouse_email_challenge(
        page,
        board="alpaca",
        company_name="Alpaca",
        expected_submit_url="https://boards.greenhouse.io/alpaca/jobs/123",
        report=report,
        code_fn=lambda **kwargs: "12345678",
    )

    assert completed is True
    assert [(call[1], call[2]) for call in page.calls if call[0] == "fill"] == [
        (f"#security-input-{index}", digit) for index, digit in enumerate("12345678")
    ]
    assert page.calls.count(("click", "button[type='submit']")) == 1
    assert report.challenge_response_status == 428
    assert report.challenge_response_code == "captcha-failed"
    assert report.response_status == 200
    assert report.response_request_id == "request-456"
    assert report.security_code_used is True


def test_execute_form_checkpoints_immediately_before_submit():
    page = FakePage()

    def checkpoint():
        page.calls.append(("checkpoint",))

    execute_form(
        plan_form_actions(_ready_plan(), QUESTIONS, resume_path="/r.pdf"),
        page,
        dry_run=False,
        before_submit=checkpoint,
    )

    assert page.calls[-2:] == [("checkpoint",), ("click", "button[type='submit']")]


def test_execute_form_checkpoint_failure_prevents_submit():
    page = FakePage()

    def checkpoint():
        raise RuntimeError("checkpoint unavailable")

    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        execute_form(
            plan_form_actions(_ready_plan(), QUESTIONS, resume_path="/r.pdf"),
            page,
            dry_run=False,
            before_submit=checkpoint,
        )

    assert ("click", "button[type='submit']") not in page.calls


def test_execute_form_blocks_invalid_required_controls_before_checkpoint():
    class InvalidControls:
        def evaluate_all(self, expression):
            return ["Location (City)", "Gender"]

    class InvalidForm:
        def count(self):
            return 1

        def evaluate(self, expression):
            return False

        def locator(self, selector):
            assert selector == ":invalid"
            return InvalidControls()

    class InvalidPage(FakePage):
        def locator(self, selector):
            assert selector == "form"
            return InvalidForm()

    page = InvalidPage()
    checkpoints = []

    with pytest.raises(RequiredFormFieldsError) as raised:
        execute_form(
            [
                type("Action", (), {"kind": "fill", "selector": "#first_name", "value": "J"})(),
                type("Action", (), {"kind": "submit", "selector": "button[type='submit']"})(),
            ],
            page,
            dry_run=False,
            before_submit=lambda: checkpoints.append(True),
        )

    assert raised.value.fields == ["Location (City)", "Gender"]
    assert checkpoints == []
    assert ("click", "button[type='submit']") not in page.calls


# --- decide_route (the "both 1 and 2" glue) --------------------------------

def test_decide_route_is_deterministic_when_plan_ready():
    assert decide_route(_ready_plan())[0] == "deterministic"


def test_decide_route_is_agent_fallback_when_not_ready():
    plan = AnswerPlan(fields={"first_name": "J", "email": "e"}, resume_field="resume",
                      free_text={}, unmapped_required=["Describe a hard problem"], ready=False)
    route, reasons = decide_route(plan)
    assert route == "agent_fallback"
    assert "Describe a hard problem" in reasons


# --- apply_greenhouse orchestrator (both 1 and 2, end to end) --------------

_READY_QS = [
    {"required": True, "label": "First Name", "fields": [{"name": "first_name", "type": "input_text"}]},
    {"required": True, "label": "Last Name", "fields": [{"name": "last_name", "type": "input_text"}]},
    {"required": True, "label": "Email", "fields": [{"name": "email", "type": "input_text"}]},
    {"required": True, "label": "Resume", "fields": [{"name": "resume", "type": "input_file"}]},
    {"required": False, "label": "Why?", "fields": [{"name": "question_1", "type": "textarea"}]},
]
_UNREADY_QS = _READY_QS + [
    {"required": True, "label": "Favorite color?",
     "fields": [{"name": "q_c", "type": "multi_value_single_select", "values": [{"label": "Red", "value": 1}]}]},
]
_PROFILE = {"personal": {"full_name": "Jordan Rivera", "email": "j@x.com",
                         "phone": "5551234567", "country": "USA"},
            "work_authorization": {}}
_RESUME = "Quant developer, Python, supported a $300M book."


class _Ans:
    def __init__(self):
        self.verified = True
        self.text = "Because your Python risk work matches my background."
        self.escalate = False
        self.checks = []
        self.model = "fake"
        self.attempts = 1
        self.retrieved = []


def _good(question, **kw):
    return _Ans()


def test_apply_greenhouse_deterministic_path_is_dry_run_by_default():
    page = FakePage()
    res = apply_greenhouse(
        "https://boards.greenhouse.io/acme/jobs/123",
        profile=_PROFILE, resume_text=_RESUME, resume_path="/r.pdf", page=page,
        fetch=lambda u: {"questions": _READY_QS}, answer_fn=_good,
    )
    assert res["route"] == "deterministic"
    assert res["report"].submitted is False
    assert res["report"].skipped_submit is True
    assert ("fill", "#first_name", "Jordan") in page.calls
    assert ("click", "button[type='submit']") not in page.calls


def test_apply_greenhouse_result_includes_route_for_deterministic_dry_run():
    page = FakePage()
    res = apply_greenhouse(
        "https://boards.greenhouse.io/acme/jobs/123",
        profile=_PROFILE,
        resume_text=_RESUME,
        resume_path="/r.pdf",
        page=page,
        fetch=lambda u: {"questions": _READY_QS},
        answer_fn=_good,
    )

    assert res["route"] == "deterministic"
    assert res["ready"] is True


def test_apply_greenhouse_falls_back_to_agent_and_never_touches_the_form():
    page = FakePage()
    res = apply_greenhouse(
        "https://boards.greenhouse.io/acme/jobs/123",
        profile=_PROFILE, resume_text=_RESUME, resume_path="/r.pdf", page=page,
        fetch=lambda u: {"questions": _UNREADY_QS}, answer_fn=_good,
    )
    assert res["route"] == "agent_fallback"
    assert any("favorite color" in u.lower() for u in res["unmapped"])
    assert page.calls == []


def test_apply_greenhouse_ignores_non_greenhouse_urls():
    res = apply_greenhouse(
        "https://jobs.lever.co/acme/1",
        profile=_PROFILE, resume_text=_RESUME, resume_path=None, page=FakePage(),
        fetch=lambda u: {}, answer_fn=_good,
    )
    assert res["route"] == "not_greenhouse"


# --- adapter_enabled (default OFF opt-in gate for the live-flow hook) -------

def test_adapter_disabled_by_default(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_GREENHOUSE_ADAPTER", raising=False)
    assert adapter_enabled() is False


def test_adapter_enabled_when_flag_truthy(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_GREENHOUSE_ADAPTER", "on")
    assert adapter_enabled() is True
    monkeypatch.setenv("APPLYPILOT_GREENHOUSE_ADAPTER", "0")
    assert adapter_enabled() is False


# --- submit ownership: second gate + confirmation detection ----------------

def test_submit_ownership_disabled_by_default(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_GREENHOUSE_ADAPTER_SUBMIT", raising=False)
    assert submit_enabled() is False


def test_submit_ownership_enabled_when_flag_on(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_GREENHOUSE_ADAPTER_SUBMIT", "yes")
    assert submit_enabled() is True


def test_detect_confirmation_recognizes_a_success_state():
    page = FakePage()
    page.set_content("<h1>Thank you for applying!</h1> Your application has been submitted.")
    assert detect_confirmation(page) == "applied"


def test_detect_confirmation_defaults_to_no_confirmation_when_unproven():
    page = FakePage()
    page.set_content("<form>...<span>This field is required</span></form>")
    assert detect_confirmation(page) == "failed:no_confirmation"


def test_detect_confirmation_never_raises():
    class Boom:
        def content(self):
            raise RuntimeError("boom")
    assert detect_confirmation(Boom()) == "failed:no_confirmation"


def test_apply_greenhouse_owns_submit_and_reports_applied_on_confirmation():
    page = FakePage()
    page.set_content("Your application has been submitted. Thanks for applying!")
    res = apply_greenhouse(
        "https://boards.greenhouse.io/acme/jobs/123",
        profile=_PROFILE, resume_text=_RESUME, resume_path="/r.pdf", page=page,
        fetch=lambda u: {"questions": _READY_QS}, answer_fn=_good, dry_run=False,
    )
    assert res["report"].submitted is True
    assert ("click", "button[type='submit']") in page.calls
    assert res["status"] == "applied"


def test_apply_greenhouse_waits_for_async_confirmation_before_verifying():
    class DelayedConfirmationPage(FakePage):
        def wait_for_function(self, expression, *, timeout):
            self.calls.append(("wait_for_function", timeout))
            self.url = "https://job-boards.greenhouse.io/acme/jobs/123/confirmation"
            self.set_content("Thank you for applying. Your application has been received.")

    page = DelayedConfirmationPage()
    page.set_content("<form>Application form</form>")

    res = apply_greenhouse(
        "https://boards.greenhouse.io/acme/jobs/123",
        profile=_PROFILE, resume_text=_RESUME, resume_path="/r.pdf", page=page,
        fetch=lambda u: {"questions": _READY_QS}, answer_fn=_good, dry_run=False,
    )

    assert ("wait_for_function", 15_000) in page.calls
    assert res["status"] == "applied"
    assert res["verification_status"] == "verified"


def test_apply_greenhouse_quarantines_when_success_not_seen_after_submit():
    page = FakePage()
    page.set_content("<form>Application form</form>")
    res = apply_greenhouse(
        "https://boards.greenhouse.io/acme/jobs/123",
        profile=_PROFILE, resume_text=_RESUME, resume_path="/r.pdf", page=page,
        fetch=lambda u: {"questions": _READY_QS}, answer_fn=_good, dry_run=False,
    )
    assert res["report"].submitted is True
    assert res["status"] == "crash_unconfirmed"
    assert res["verification_status"] == "unverified"


def test_apply_greenhouse_creates_prepared_context_only_for_ready_live_submit():
    page = FakePage()
    page.set_content("Your application has been submitted. Thanks for applying!")
    calls = []

    def on_plan_ready(plan, actions):
        calls.append(("prepared", plan.ready, len(actions)))
        return "attempt-1"

    def before_submit(context):
        calls.append(("submit_started", context))

    res = apply_greenhouse(
        "https://boards.greenhouse.io/acme/jobs/123",
        profile=_PROFILE,
        resume_text=_RESUME,
        resume_path="/r.pdf",
        page=page,
        fetch=lambda u: {"questions": _READY_QS},
        answer_fn=_good,
        dry_run=False,
        on_plan_ready=on_plan_ready,
        before_submit=before_submit,
    )

    assert calls[0][0] == "prepared"
    assert calls[1] == ("submit_started", "attempt-1")
    assert res["attempt_context"] == "attempt-1"


def test_unready_greenhouse_plan_creates_no_attempt_context():
    calls = []
    res = apply_greenhouse(
        "https://boards.greenhouse.io/acme/jobs/123",
        profile=_PROFILE,
        resume_text=_RESUME,
        resume_path="/r.pdf",
        page=FakePage(),
        fetch=lambda u: {"questions": _UNREADY_QS},
        answer_fn=_good,
        dry_run=False,
        on_plan_ready=lambda *args: calls.append(args),
    )

    assert res["route"] == "agent_fallback"
    assert calls == []


# --- remember_answer capture loop ------------------------------------------

def test_capture_answers_stores_question_label_and_answer():
    calls = []

    def spy(question, answer, **kw):
        calls.append((question, answer))

    plan = AnswerPlan(fields={"question_1": "my grounded answer"}, resume_field=None,
                      free_text={"question_1": "my grounded answer"},
                      unmapped_required=[], ready=True)
    questions = [{"label": "Why do you want this role?",
                  "fields": [{"name": "question_1", "type": "textarea"}]}]
    n = capture_answers(plan, questions, {"site": "acme"}, remember_fn=spy)
    assert n == 1
    assert ("Why do you want this role?", "my grounded answer") in calls


def _apply(page, *, dry_run, remember_fn):
    return apply_greenhouse(
        "https://boards.greenhouse.io/acme/jobs/123",
        profile=_PROFILE, resume_text=_RESUME, resume_path="/r.pdf", page=page,
        fetch=lambda u: {"questions": _READY_QS}, answer_fn=_good,
        dry_run=dry_run, remember_fn=remember_fn,
    )


def test_capture_happens_only_on_a_confirmed_submit():
    calls = []
    page = FakePage()
    page.set_content("Your application has been submitted. Thanks for applying!")
    _apply(page, dry_run=False, remember_fn=lambda q, a, **k: calls.append(q))
    assert calls  # the free-text answer was captured


def test_no_capture_on_dry_run():
    calls = []
    _apply(FakePage(), dry_run=True, remember_fn=lambda q, a, **k: calls.append(q))
    assert calls == []


def test_no_capture_when_submit_not_confirmed():
    calls = []
    page = FakePage()
    page.set_content("<form>This field is required</form>")
    _apply(page, dry_run=False, remember_fn=lambda q, a, **k: calls.append(q))
    assert calls == []
