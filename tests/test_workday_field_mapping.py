from __future__ import annotations

from applypilot.apply.workday_adapter import WorkdayField, WorkdayFieldAction, build_field_plan


PROFILE = {
    "personal": {
        "full_name": "Jonathan Smith",
        "email": "jonathan@example.com",
        "phone": "+1 555 0100",
        "address": "1 Main Street",
        "city": "San Francisco",
        "province_state": "California",
        "postal_code": "94105",
        "country": "United States",
        "linkedin_url": "https://linkedin.com/in/jonathan",
        "portfolio_url": "https://example.com",
    },
    "work_authorization": {
        "legally_authorized_to_work": "yes",
        "require_sponsorship": "no",
    },
}


def test_maps_identity_contact_address_and_urls_from_profile():
    fields = [
        {"key": "legalNameSection_firstName", "label": "First Name", "required": True},
        {"key": "legalNameSection_lastName", "label": "Last Name", "required": True},
        {"key": "email", "label": "Email Address", "required": True},
        {"key": "phone", "label": "Phone Number", "required": True},
        {"key": "addressLine1", "label": "Address Line 1", "required": True},
        {"key": "city", "label": "City", "required": True},
        {"key": "postalCode", "label": "Postal Code", "required": True},
        {"key": "linkedin", "label": "LinkedIn Profile", "required": False},
    ]
    plan = build_field_plan(fields, profile=PROFILE)
    values = {action.key: action.value for action in plan.actions}
    assert values["legalNameSection_firstName"] == "Jonathan"
    assert values["legalNameSection_lastName"] == "Smith"
    assert values["email"] == "jonathan@example.com"
    assert values["addressLine1"] == "1 Main Street"
    assert plan.ready is True


def test_maps_controlled_factual_options_by_visible_value():
    fields = [
        {"key": "country", "label": "Country", "type": "combobox", "required": True,
         "options": ["Canada", "United States of America"]},
        {"key": "state", "label": "State", "type": "combobox", "required": True,
         "options": ["California", "New York"]},
        {"key": "auth", "label": "Are you legally authorized to work?", "type": "select",
         "required": True, "options": ["Yes", "No"]},
        {"key": "sponsor", "label": "Will you require sponsorship?", "type": "select",
         "required": True, "options": ["Yes", "No"]},
        {"key": "gender", "label": "Gender", "type": "select", "required": True,
         "options": ["Female", "Male", "Decline to Self Identify"]},
    ]
    plan = build_field_plan(fields, profile=PROFILE)
    values = {action.key: action.value for action in plan.actions}
    assert values == {
        "country": "United States of America",
        "state": "California",
        "auth": "Yes",
        "sponsor": "No",
        "gender": "Decline to Self Identify",
    }
    assert all(action.action == "select" for action in plan.actions)


def test_hiring_cafe_source_parks_when_tenant_leaf_is_unknown():
    profile = dict(PROFILE)
    profile["_application_context"] = {"source_board": "hiringcafe"}
    plan = build_field_plan([{
        "key": "source--source",
        "label": "How Did You Hear About Us?",
        "type": "combobox",
        "required": True,
    }], profile=profile)
    assert plan.actions == ()
    assert plan.unresolved_required == ("How Did You Hear About Us?",)


def test_work_eligibility_question_is_not_misclassified_as_address_country():
    plan = build_field_plan([{
        "key": "primary-question-id",
        "label": "Are you eligible to work in the country to which you have applied?",
        "type": "combobox",
        "required": True,
        "options": ["Yes", "No"],
    }], profile=PROFILE)

    assert plan.ready is True
    assert plan.actions[0].value == "Yes"
    assert plan.actions[0].source == "profile"


def test_boolean_false_sponsorship_value_maps_to_no():
    profile = dict(PROFILE)
    profile["work_authorization"] = {"require_sponsorship": False}
    plan = build_field_plan([{
        "key": "sponsorship",
        "label": "Will you now or in the future require Visa sponsorship?",
        "type": "combobox",
        "required": True,
        "options": ["Yes", "No"],
    }], profile=profile)

    assert plan.ready is True
    assert plan.actions[0].value == "No"


def test_unknown_required_question_is_unresolved_not_invented():
    plan = build_field_plan(
        [{"key": "customQuestion", "label": "Describe a novel strategy", "required": True}],
        profile=PROFILE,
    )
    assert plan.actions == ()
    assert plan.unresolved_required == ("Describe a novel strategy",)
    assert plan.ready is False


def test_exact_approved_answer_resolves_otherwise_unknown_required_question():
    calls = []
    plan = build_field_plan(
        [{"key": "customQuestion", "label": "Are you an Acme customer?", "type": "combobox",
          "required": True, "options": ["Yes", "No"]}],
        profile=PROFILE,
        answer_resolver=lambda field: calls.append(field.label) or "No",
    )
    assert calls == ["Are you an Acme customer?"]
    assert plan.ready is True
    assert plan.actions[0].value == "No"
    assert plan.actions[0].source == "approved_answer"


def test_existing_required_value_does_not_trigger_unresolved():
    plan = build_field_plan(
        [{"key": "custom", "label": "Previously populated", "required": True, "value": "Existing"}],
        profile=PROFILE,
    )
    assert plan.ready is True


def test_empty_multiselect_accessibility_text_is_not_a_field_value():
    field = WorkdayField.from_dict({
        "key": "source--source",
        "label": "How Did You Hear About Us?",
        "type": "combobox",
        "value": "0 items selected",
    })
    assert field.value == ""


def test_optional_phone_sms_checkbox_is_left_untouched():
    plan = build_field_plan(
        [{"key": "phone-sms-opt-in", "label": "Phone SMS Opt In",
          "type": "checkbox", "required": False}],
        profile=PROFILE,
    )
    assert plan.actions == ()
    assert plan.ready is True


def test_previous_worker_radio_uses_resume_company_evidence():
    profile = dict(PROFILE)
    profile["resume_facts"] = {"preserved_companies": ["Different Employer"]}
    profile["_application_context"] = {"company": "Collectors"}
    question = "Have you previously been employed by Collectors?"
    plan = build_field_plan([
        {"key": "candidateIsPreviousWorker", "label": f"{question} option Yes",
         "type": "radio", "required": True},
        {"key": "candidateIsPreviousWorker", "label": f"{question} option No",
         "type": "radio", "required": True},
    ], profile=profile)
    assert plan.ready is True
    assert plan.actions[0].action == "check"
    assert plan.actions[0].value == "No"


def test_previous_worker_radio_recognizes_previously_employed_wording():
    profile = dict(PROFILE)
    profile["resume_facts"] = {"preserved_companies": ["Different Employer"]}
    profile["_application_context"] = {"company": "Mitsubishi UFJ Financial Group"}
    question = "Were you PREVIOUSLY employed by MUFG Bank or its affiliates?"
    plan = build_field_plan([
        {"key": "candidateIsPreviousWorker", "label": f"{question} option Yes", "type": "radio"},
        {"key": "candidateIsPreviousWorker", "label": f"{question} option No", "type": "radio"},
    ], profile=profile)
    assert plan.actions == (
        WorkdayFieldAction("check", "candidateIsPreviousWorker", "No", "resume_facts"),
    )
