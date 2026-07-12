from types import SimpleNamespace

import pytest

from applypilot.apply.ashby_adapter import (
    build_ashby_plan,
    execute_ashby_actions,
    parse_ashby_url,
    plan_ashby_actions,
    verify_ashby_submission,
)
from applypilot.apply.greenhouse_submit import RequiredFormFieldsError


PROFILE = {
    "personal": {
        "full_name": "Jordan Rivera",
        "email": "j@example.com",
        "city": "San Francisco",
        "province_state": "California",
        "country": "USA",
        "linkedin_url": "https://linkedin.com/in/jordan",
    },
    "work_authorization": {
        "legally_authorized_to_work": True,
        "require_sponsorship": False,
    },
    "compensation": {"salary_range_min": "80000", "salary_range_max": "230000"},
}

FIELDS = [
    {"path": "_systemfield_name", "label": "Name", "type": "text", "required": True},
    {"path": "_systemfield_email", "label": "Email", "type": "email", "required": True},
    {"path": "link", "label": "LinkedIn or Personal Website Link", "type": "text", "required": True},
    {"path": "_systemfield_resume", "label": "Resume", "type": "file", "required": True},
    {"path": "_systemfield_location", "label": "Where are you located?", "type": "location", "required": True},
    {"path": "why", "label": "Why us?", "type": "textarea", "required": True},
    {"path": "salary", "label": "What compensation are you seeking?", "type": "text", "required": True},
    {"path": "auth", "label": "Are you authorized to work in the United States?", "type": "boolean", "required": True},
    {"path": "sponsor", "label": "Will you require visa sponsorship?", "type": "boolean", "required": True},
]


def test_parse_ashby_application_url():
    assert parse_ashby_url("https://jobs.ashbyhq.com/acme/123/application") == ("acme", "123")
    assert parse_ashby_url("https://example.com/acme/123") is None


def test_build_ashby_plan_maps_profile_and_verified_free_text():
    answer = lambda *args, **kwargs: SimpleNamespace(verified=True, text="Verified answer")
    plan = build_ashby_plan(FIELDS, profile=PROFILE, resume_text="resume", answer_fn=answer)
    assert plan.ready is True
    assert plan.fields["_systemfield_name"] == "Jordan Rivera"
    assert plan.fields["_systemfield_location"] == "San Francisco, California, United States"
    assert plan.fields["auth"] is True
    assert plan.fields["sponsor"] is False
    assert plan.fields["salary"] == "$80000-$230000"
    assert plan.free_text == {"why": "Verified answer"}


def test_build_ashby_plan_fails_closed_on_unverified_required_text():
    answer = lambda *args, **kwargs: SimpleNamespace(verified=False, text="")
    plan = build_ashby_plan(FIELDS, profile=PROFILE, resume_text="resume", answer_fn=answer)
    assert plan.ready is False
    assert plan.unmapped_required == ["Why us?"]


def test_plan_ashby_actions_uses_stable_field_paths_and_submit_last():
    plan = build_ashby_plan(
        FIELDS,
        profile=PROFILE,
        resume_text="resume",
        answer_fn=lambda *args, **kwargs: SimpleNamespace(verified=True, text="Verified answer"),
    )
    actions = plan_ashby_actions(plan, FIELDS, resume_path="resume.pdf")
    assert actions[0].kind == "file"
    assert actions[0].selector == '[id="_systemfield_resume"]'
    assert any(action.kind == "ashby_location" for action in actions)
    assert any(action.kind == "ashby_boolean" and action.value is False for action in actions)
    assert actions[-1].kind == "submit"


def test_execute_validates_then_checkpoints_immediately_before_single_submit():
    events = []

    class InvalidLocator:
        def evaluate_all(self, expression):
            events.append("validate")
            return []

    class FormLocator:
        def locator(self, selector):
            assert selector == ":invalid"
            return InvalidLocator()

    class Button:
        def click(self):
            events.append("click")

    class Page:
        def locator(self, selector):
            assert selector == "form"
            return FormLocator()

        def get_by_role(self, role, name, exact=True):
            return Button()

    submit = SimpleNamespace(kind="submit", selector="button", value=None)
    result = execute_ashby_actions(
        [submit], Page(), dry_run=False,
        before_submit=lambda: events.append("checkpoint"),
    )
    assert result["submitted"] is True
    assert events == ["validate", "checkpoint", "click"]


def test_execute_rejects_runtime_required_fields_before_checkpoint_or_click():
    events = []

    class InvalidLocator:
        def evaluate_all(self, expression):
            events.append("validate")
            return ["email"]

    class FormLocator:
        def locator(self, selector):
            return InvalidLocator()

    class Page:
        def locator(self, selector):
            return FormLocator()

        def get_by_role(self, *args, **kwargs):
            raise AssertionError("submit must not be reached")

    submit = SimpleNamespace(kind="submit", selector="button", value=None)
    with pytest.raises(RequiredFormFieldsError):
        execute_ashby_actions(
            [submit], Page(), dry_run=False,
            before_submit=lambda: events.append("checkpoint"),
        )
    assert events == ["validate"]


def test_verify_ashby_submission_is_fail_closed():
    class Page:
        def __init__(self, succeeds):
            self.succeeds = succeeds

        def wait_for_function(self, expression, *, timeout):
            assert "application submitted" in expression
            assert timeout == 15_000
            if not self.succeeds:
                raise TimeoutError

    assert verify_ashby_submission(Page(True)) is True
    assert verify_ashby_submission(Page(False)) is False
