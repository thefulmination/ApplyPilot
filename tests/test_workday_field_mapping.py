from __future__ import annotations

import pytest

from applypilot.apply.workday_adapter import (
    WorkdayField,
    WorkdayFieldAction,
    build_canonical_resume,
    build_field_plan,
)


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


def test_dynamic_resume_fields_use_canonical_records_and_role_bullets():
    canonical = build_canonical_resume(
        profile={
            "resume_facts": {
                "preserved_companies": ["Acme Capital"],
                "work_locations": {"Acme Capital": "Hicksville, NY"},
                "preserved_school": "State University",
                "education": {
                    "school": "State University",
                    "degree": "Bachelor of Science",
                    "discipline": "Quantitative Finance",
                    "location": "Hoboken, NJ",
                },
            }
        },
        resume_text="""Work Experience
Acme Capital, Strategy Manager January 2022 - Present
Led the strategy function and built operating dashboards.

EDUCATION
State University, Hoboken, NJ January 2016 - May 2020
Bachelor of Science: Quantitative Finance
""",
    )
    fields = [
        WorkdayField("workExperience-98--jobTitle", "Job Title*", required=True),
        WorkdayField("workExperience-98--company", "Company*", required=True),
        WorkdayField("workExperience-98--location", "Location*", required=True),
        WorkdayField("workExperience-98--roleDescription", "Role Description*", required=True),
        WorkdayField("education-99--school", "School or University*", required=True),
        WorkdayField("education-99--degree", "Degree*", required=True),
        WorkdayField("education-99--fieldOfStudy", "Field of Study*", required=True),
    ]

    plan = build_field_plan(fields, profile=PROFILE, canonical_resume=canonical)

    assert plan.unresolved_required == ()
    assert [(action.key, action.value, action.source) for action in plan.actions] == [
        ("workExperience-98--jobTitle", "Strategy Manager", "canonical_resume"),
        ("workExperience-98--company", "Acme Capital", "canonical_resume"),
        ("workExperience-98--location", "Hicksville, NY", "canonical_resume"),
        ("workExperience-98--roleDescription", "Led the strategy function and built operating dashboards.", "canonical_resume"),
        ("education-99--school", "State University", "canonical_resume"),
        ("education-99--degree", "Bachelor of Science", "canonical_resume"),
        ("education-99--fieldOfStudy", "Quantitative Finance", "canonical_resume"),
    ]


def test_dynamic_resume_field_without_canonical_fact_remains_unresolved():
    fields = [WorkdayField("workExperience-98--location", "Location*", required=True)]

    plan = build_field_plan(fields, profile=PROFILE, canonical_resume={"work_history": []})

    assert plan.actions == ()
    assert plan.unresolved_required == ("Location*",)


def test_canonical_controlled_value_uses_approved_closest_option():
    plan = build_field_plan([
        WorkdayField(
            "education-8--fieldOfStudy", "Field of Study*", required=True,
            field_type="combobox", options=(
                "Accounting", "Applied Mathematics", "Applied Finance, Investment"
            ),
        ),
    ], profile=PROFILE, canonical_resume={
        "work_history": [],
        "education": [{"school": "State University", "field_of_study": "Quantitative Finance"}],
    })
    assert [(a.value, a.source) for a in plan.actions] == [
        ("Applied Finance, Investment", "canonical_resume_approximation")
    ]
    assert plan.unresolved_required == ()


def test_visa_uses_approved_closest_option_when_workday_omits_options():
    profile = dict(PROFILE)
    profile["_application_context"] = {"target_host": "visa.wd5.myworkdayjobs.com"}
    plan = build_field_plan([
        WorkdayField("education-8--fieldOfStudy", "Field of Study*", required=True,
                     field_type="combobox"),
    ], profile=profile, canonical_resume={
        "work_history": [],
        "education": [{"school": "State University", "field_of_study": "Quantitative Finance"}],
    })
    assert plan.actions[0].value == "Applied Finance, Investment"
    assert plan.actions[0].source == "canonical_resume_approximation"


def test_dynamic_resume_dates_and_current_work_checkbox_use_canonical_values():
    canonical = {
        "work_history": [{
            "company": "Acme Capital",
            "title": "Strategy Manager",
            "start_date": "January 2022",
            "end_date": "Present",
            "currently_working": True,
        }],
        "education": [],
    }
    fields = [
        WorkdayField("workExperience-98--currentlyWorkHere", "I currently work here", field_type="checkbox"),
        WorkdayField("workExperience-98--startDate-dateSectionMonth-input", "From*", required=True),
        WorkdayField("workExperience-98--startDate-dateSectionYear-input", "From*", required=True),
        WorkdayField("workExperience-98--endDate-dateSectionMonth-input", "To*", required=True),
        WorkdayField("workExperience-98--endDate-dateSectionYear-input", "To*", required=True),
    ]

    plan = build_field_plan(fields, profile=PROFILE, canonical_resume=canonical)

    assert plan.unresolved_required == ()
    assert [(action.key, action.value) for action in plan.actions] == [
        ("workExperience-98--currentlyWorkHere", "Yes"),
        ("workExperience-98--startDate-dateSectionMonth-input", "01"),
        ("workExperience-98--startDate-dateSectionYear-input", "2022"),
    ]


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


def test_hiring_cafe_source_uses_external_job_board_other_path():
    profile = dict(PROFILE)
    profile["_application_context"] = {"source_board": "hiringcafe"}
    plan = build_field_plan([{
        "key": "source--source",
        "label": "How Did You Hear About Us?",
        "type": "combobox",
        "required": True,
    }], profile=profile)
    assert plan.actions[0].value == "Job Board/Website/Social Network > Other"
    assert plan.ready is True


@pytest.mark.parametrize(("host", "expected"), [
    ("visa.wd5.myworkdayjobs.com", "Other"),
])
def test_hiring_cafe_source_uses_verified_tenant_path(host, expected):
    profile = dict(PROFILE)
    profile["_application_context"] = {
        "source_board": "hiringcafe",
        "target_host": host,
    }

    plan = build_field_plan([{
        "key": "source--source", "label": "How Did You Hear About Us?",
        "type": "combobox", "required": True,
    }], profile=profile)

    assert plan.actions[0].value == expected


@pytest.mark.parametrize("host", [
    "lendingclub.wd1.myworkdayjobs.com",
    "mufgub.wd3.myworkdayjobs.com",
    "iqvia.wd1.myworkdayjobs.com",
])
def test_unverified_hiringcafe_source_uses_deterministic_category_fallback(host):
    profile = dict(PROFILE)
    profile["_application_context"] = {
        "source_board": "hiringcafe",
        "target_host": host,
    }
    plan = build_field_plan([{
        "key": "source--source", "label": "How Did You Hear About Us?",
        "type": "combobox", "required": True,
    }], profile=profile)

    assert plan.actions[0].value == "Job Boards/Websites"
    assert plan.actions[0].source == "application_source_approximation"
    assert plan.unresolved_required == ()


def test_mufg_hiringcafe_source_uses_deterministic_category_fallback():
    profile = dict(PROFILE)
    profile["_application_context"] = {
        "source_board": "hiringcafe",
        "target_host": "mufgub.wd3.myworkdayjobs.com",
    }
    plan = build_field_plan([{
        "key": "source--source", "label": "How Did You Hear About Us?",
        "type": "combobox", "required": True,
    }], profile=profile)
    assert plan.actions[0].value == "Job Boards/Websites"
    assert plan.actions[0].source == "application_source_approximation"


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


def test_self_identification_checkbox_group_selects_only_decline_option():
    fields = [
        {"key": "race-a-ethnicityMulti", "label": "Asian (United States)",
         "type": "checkbox", "required": True},
        {"key": "race-decline-ethnicityMulti", "label": "I do not wish to answer. (United States)",
         "type": "checkbox", "required": True},
        {"key": "race-w-ethnicityMulti", "label": "White (United States)",
         "type": "checkbox", "required": True},
    ]
    plan = build_field_plan(fields, profile=PROFILE)
    assert plan.ready is True
    assert plan.actions == (
        WorkdayFieldAction("check_box", "race-decline-ethnicityMulti", "Yes", "privacy_default"),
    )


def test_labeled_unchecked_ethnicity_checkbox_is_not_treated_as_filled():
    plan = build_field_plan([{
        "key": "race-decline-ethnicityMulti",
        "label": "I do not wish to answer. (United States)",
        "type": "checkbox",
        "value": "I do not wish to answer. (United States)",
        "required": True,
    }], profile=PROFILE)
    assert plan.actions == (
        WorkdayFieldAction("check_box", "race-decline-ethnicityMulti", "Yes", "privacy_default"),
    )


def test_required_terms_checkbox_is_acknowledged_deterministically():
    plan = build_field_plan([{
        "key": "acceptTermsAndAgreements",
        "label": "I have read and consent to the terms and conditions.",
        "type": "checkbox",
        "required": True,
    }], profile=PROFILE)
    assert plan.ready is True
    assert plan.actions == (
        WorkdayFieldAction("check_box", "acceptTermsAndAgreements", "Yes", "required_acknowledgement"),
    )


def test_self_identification_comboboxes_use_verified_decline_values():
    plan = build_field_plan([
        {"key": "gender", "label": "Gender", "type": "combobox", "required": True},
        {"key": "veteranStatus", "label": "Veteran Status", "type": "combobox", "required": True},
    ], profile=PROFILE)
    assert plan.ready is True
    assert [action.value for action in plan.actions] == [
        "Do Not Wish To Disclose (United States of America)",
        "I do not wish to self-identify",
    ]


def test_hispanic_question_without_decline_option_remains_factual_exception():
    plan = build_field_plan([{
        "key": "hispanicOrLatino", "label": "Are you Hispanic/Latino?",
        "type": "combobox", "required": True,
    }], profile=PROFILE)
    assert plan.actions == ()
    assert plan.unresolved_required == ("Are you Hispanic/Latino?",)


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


@pytest.mark.parametrize("question", [
    "Are you a previous Happen Bank employee?",
    "Have you ever been a regular employee or contingent worker for IQVIA?",
])
def test_previous_worker_key_maps_unfamiliar_tenant_wording(question):
    plan = build_field_plan([
        {"key": "candidateIsPreviousWorker", "label": f"{question} option Yes",
         "type": "radio", "required": True},
        {"key": "candidateIsPreviousWorker", "label": f"{question} option No",
         "type": "radio", "required": True},
    ], profile=PROFILE)

    assert plan.actions == (
        WorkdayFieldAction("check", "candidateIsPreviousWorker", "No", "resume_facts"),
    )


def test_previous_worker_key_wins_over_cybersource_source_wording():
    question = (
        "Have you ever worked for Visa Inc. or any wholly/majority-owned subsidiaries "
        "of Visa Inc. (e.g., CyberSource, Fundamo, etc.) in any capacity? If Yes, "
        "please answer the questions below. If No, please continue.*"
    )
    plan = build_field_plan([
        {"key": "candidateIsPreviousWorker", "label": f"{question} option Yes", "type": "radio"},
        {"key": "candidateIsPreviousWorker", "label": f"{question} option No", "type": "radio"},
    ], profile=PROFILE)

    assert plan.actions == (
        WorkdayFieldAction("check", "candidateIsPreviousWorker", "No", "resume_facts"),
    )


def test_phone_type_key_wins_over_generic_phone_number_mapping():
    plan = build_field_plan([{
        "key": "phoneType", "label": "Phone Device Type", "type": "combobox", "required": True,
    }], profile=PROFILE)

    assert plan.actions == (
        WorkdayFieldAction("select", "phoneType", "Mobile", "profile"),
    )
