"""Tests for the deterministic Greenhouse apply adapter.

The adapter reads a Greenhouse job's application questions (public API), then
builds a complete answer plan deterministically -- calling the cheap-model
answerer ONLY for free-text questions, and refusing to fake any required field
it cannot map. No browser, no network in these tests (fetch + answerer injected).
"""

from applypilot.apply.greenhouse_adapter import (
    build_answer_plan,
    fetch_questions,
    parse_greenhouse_url,
    resolve_greenhouse_url,
)


# --- parse_greenhouse_url --------------------------------------------------

def test_parse_standard_boards_url():
    assert parse_greenhouse_url("https://boards.greenhouse.io/acme/jobs/4012345") == ("acme", "4012345")


def test_parse_job_boards_host_with_query():
    assert parse_greenhouse_url("https://job-boards.greenhouse.io/acme/jobs/4012345?utm=x") == ("acme", "4012345")


def test_parse_tolerates_trailing_slash():
    assert parse_greenhouse_url("https://boards.greenhouse.io/acme/jobs/4012345/") == ("acme", "4012345")


def test_parse_non_greenhouse_returns_none():
    assert parse_greenhouse_url("https://jobs.lever.co/acme/123") is None


def test_parse_greenhouse_without_job_id_returns_none():
    assert parse_greenhouse_url("https://boards.greenhouse.io/acme") is None


# --- fetch_questions -------------------------------------------------------

_GH_JOB = {
    "id": 4012345,
    "title": "Quant Developer",
    "questions": [
        {"required": True, "label": "First Name", "fields": [{"name": "first_name", "type": "input_text"}]},
        {"required": True, "label": "Resume", "fields": [{"name": "resume", "type": "input_file"}]},
    ],
}


def test_fetch_questions_hits_public_endpoint_and_returns_the_list():
    seen = {}

    def fake_fetch(url):
        seen["url"] = url
        return _GH_JOB

    qs = fetch_questions("acme", "4012345", fetch=fake_fetch)
    assert "boards-api.greenhouse.io/v1/boards/acme/jobs/4012345" in seen["url"]
    assert "questions=true" in seen["url"]
    assert [q["label"] for q in qs] == ["First Name", "Resume"]


def test_fetch_questions_returns_empty_when_absent():
    assert fetch_questions("acme", "1", fetch=lambda u: {"id": 1}) == []


def test_resolve_greenhouse_url_follows_supported_short_link():
    final = "https://job-boards.greenhouse.io/acme/jobs/123"

    assert resolve_greenhouse_url(
        "https://grnh.se/example",
        resolve=lambda url: final,
    ) == final


def test_resolve_greenhouse_url_rejects_non_greenhouse_redirect():
    assert resolve_greenhouse_url(
        "https://grnh.se/example",
        resolve=lambda url: "https://example.com/jobs/123",
    ) is None


# --- build_answer_plan -----------------------------------------------------

PROFILE = {
    "personal": {
        "full_name": "Jordan Rivera", "email": "jordan@example.com",
        "phone": "5551234567", "city": "Jersey City",
        "province_state": "NJ", "country": "USA", "address": "1 Main St",
        "preferred_name": "Jordy", "linkedin_url": "https://linkedin.com/in/jordan",
    },
    "work_authorization": {"legally_authorized_to_work": "Yes", "require_sponsorship": "No"},
    "compensation": {"salary_expectation": 175000},
    "experience": {"education_level": "Bachelor's degree", "years_of_experience_total": 12},
    "resume_facts": {"preserved_companies": ["Acme Capital", "Example Labs"]},
}
RESUME = "Quantitative Developer. Built Python risk models at a trading desk supporting a $300M book."
JOB = {"title": "Quant Developer", "site": "Acme Capital", "description": "Python pricing models."}


class _FakeAns:
    def __init__(self, verified, text=""):
        self.verified = verified
        self.text = text or "Your desk's Python pricing work matches my risk-modeling background."
        self.escalate = not verified
        self.checks = [] if verified else ["length"]
        self.model = "fake"
        self.attempts = 1
        self.retrieved = []


def _good_fn(question, **kw):
    return _FakeAns(True)


def _bad_fn(question, **kw):
    return _FakeAns(False)


def _identity_qs():
    return [
        {"required": True, "label": "First Name", "fields": [{"name": "first_name", "type": "input_text"}]},
        {"required": True, "label": "Last Name", "fields": [{"name": "last_name", "type": "input_text"}]},
        {"required": True, "label": "Email", "fields": [{"name": "email", "type": "input_text"}]},
        {"required": False, "label": "Phone", "fields": [{"name": "phone", "type": "input_text"}]},
        {"required": True, "label": "Resume", "fields": [{"name": "resume", "type": "input_file"}]},
    ]


def _plan(extra=None, answer_fn=_good_fn):
    qs = _identity_qs() + (extra or [])
    return build_answer_plan(qs, profile=PROFILE, resume_text=RESUME, answer_fn=answer_fn, job=JOB)


def test_maps_identity_fields_deterministically():
    plan = _plan()
    assert plan.fields["first_name"] == "Jordan"
    assert plan.fields["last_name"] == "Rivera"
    assert plan.fields["email"] == "jordan@example.com"
    assert plan.fields["phone"] == "5551234567"
    assert plan.resume_field == "resume"


def test_identity_only_job_is_ready():
    plan = _plan()
    assert plan.ready is True
    assert plan.unmapped_required == []


def test_maps_profile_backed_custom_text_questions():
    q = [
        {"required": True, "label": "Preferred First Name",
         "fields": [{"name": "question_1", "type": "input_text"}]},
        {"required": True, "label": "LinkedIn Profile",
         "fields": [{"name": "question_2", "type": "input_text"}]},
    ]

    plan = _plan(q)

    assert plan.fields["question_1"] == "Jordy"
    assert plan.fields["question_2"] == "https://linkedin.com/in/jordan"
    assert plan.ready is True


def test_maps_profile_backed_salary_address_and_education_questions():
    q = [
        {"required": True, "label": "What is your desired salary?",
         "fields": [{"name": "salary_q", "type": "input_text"}]},
        {"required": True, "label": "Legal First Name",
         "fields": [{"name": "legal_first", "type": "input_text"}]},
        {"required": True, "label": "Legal Last Name",
         "fields": [{"name": "legal_last", "type": "input_text"}]},
        {"required": True, "label": "Address Line 1",
         "fields": [{"name": "address_q", "type": "textarea"}]},
        {"required": True, "label": "City",
         "fields": [{"name": "city_q", "type": "textarea"}]},
        {"required": True, "label": "What is your highest level of education completed?",
         "fields": [{"name": "education_q", "type": "textarea"}]},
    ]

    plan = _plan(q, answer_fn=_bad_fn)

    assert plan.fields["salary_q"] == "175000"
    assert plan.fields["legal_first"] == "Jordan"
    assert plan.fields["legal_last"] == "Rivera"
    assert plan.fields["address_q"] == "1 Main St"
    assert plan.fields["city_q"] == "Jersey City"
    assert plan.fields["education_q"] == "Bachelor's degree"
    assert plan.ready is True


def test_maps_profile_backed_state_country_and_address_type_selects():
    q = [
        {"required": True, "label": "Which state do you currently reside in?",
         "fields": [{"name": "state_q", "type": "multi_value_single_select",
                     "values": [{"label": "New Jersey", "value": 10},
                                {"label": "New York", "value": 11}]}]},
        {"required": True, "label": "Country",
         "fields": [{"name": "country_q", "type": "multi_value_single_select",
                     "values": [{"label": "USA", "value": 20},
                                {"label": "Outside-USA", "value": 21}]}]},
        {"required": True, "label": "Address Type",
         "fields": [{"name": "address_type_q", "type": "multi_value_single_select",
                     "values": [{"label": "Home", "value": 30}]}]},
    ]

    plan = _plan(q, answer_fn=_bad_fn)

    assert plan.fields["state_q"] == 10
    assert plan.fields["country_q"] == 20
    assert plan.fields["address_type_q"] == 30
    assert plan.ready is True


def test_maps_exact_profile_city_for_location_multi_select():
    profile = {
        **PROFILE,
        "personal": {
            **PROFILE["personal"],
            "city": "San Francisco",
            "province_state": "California",
        },
    }
    q = _identity_qs() + [
        {"required": True,
         "label": "Which location is closest to where you currently live or are actively planning on relocating to?",
         "fields": [{"name": "location_q[]", "type": "multi_value_multi_select",
                     "values": [{"label": "CA | Los Angeles", "value": 40},
                                {"label": "CA | San Francisco", "value": 41},
                                {"label": "NY | New York", "value": 42}]}]},
    ]

    plan = build_answer_plan(q, profile=profile, resume_text=RESUME, answer_fn=_bad_fn, job=JOB)

    assert plan.fields["location_q[]"] == 41
    assert plan.ready is True


def test_maps_online_job_board_source_and_required_privacy_acknowledgement():
    q = _identity_qs() + [
        {"required": True, "label": "How did you hear about this job?",
         "fields": [{"name": "source_q", "type": "input_text"}]},
        {"required": True, "label": "Data Protection Notice",
         "fields": [{"name": "privacy_q[]", "type": "multi_value_multi_select",
                     "values": [{"label": "Acknowledge/Confirm", "value": 50}]}]},
    ]

    plan = build_answer_plan(q, profile=PROFILE, resume_text=RESUME, answer_fn=_bad_fn, job=JOB)

    assert plan.fields["source_q"] == "Online job board"
    assert plan.fields["privacy_q[]"] == 50
    assert plan.ready is True


def test_maps_profile_backed_residency_and_application_attestations():
    q = _identity_qs() + [
        {"required": True,
         "label": "Are you a resident of the following states in which we can employ? CA, NJ, NY, TX.",
         "fields": [{"name": "resident_q", "type": "multi_value_single_select",
                     "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
        {"required": True,
         "label": "I hereby declare that the given particulars are true to the best of my knowledge and belief",
         "fields": [{"name": "truth_q", "type": "multi_value_single_select",
                     "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
        {"required": True,
         "label": "If provided a job offer, I understand I must provide documents establishing identity and employment eligibility",
         "fields": [{"name": "documents_q", "type": "multi_value_single_select",
                     "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
    ]

    plan = build_answer_plan(q, profile=PROFILE, resume_text=RESUME, answer_fn=_bad_fn, job=JOB)

    assert plan.fields["resident_q"] == 1
    assert plan.fields["truth_q"] == 1
    assert plan.fields["documents_q"] == 1
    assert plan.ready is True


def test_maps_location_availability_leadership_and_overlapping_compensation():
    profile = {
        **PROFILE,
        "personal": {**PROFILE["personal"], "city": "San Francisco", "province_state": "California"},
        "availability": {"earliest_start_date": "Immediately"},
        "experience": {**PROFILE["experience"], "current_title": "COO"},
        "compensation": {"salary_range_min": "80000", "salary_range_max": "230000"},
    }
    job = {
        **JOB,
        "description": "Level: Individual Contributor. Compensation: $75,000 - $110,000.",
    }
    q = _identity_qs() + [
        {"required": True, "label": "Are you located in San Francisco, CA?",
         "fields": [{"name": "location_q", "type": "multi_value_single_select",
                     "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
        {"required": True, "label": "What is your preferred start date?",
         "fields": [{"name": "start_q", "type": "input_text"}]},
        {"required": True, "label": "Do you have leadership/management experience?",
         "fields": [{"name": "leadership_q", "type": "multi_value_single_select",
                     "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
        {"required": True, "label": "Does the listed compensation range match your expectation?",
         "fields": [{"name": "comp_q", "type": "multi_value_single_select",
                     "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
    ]

    plan = build_answer_plan(q, profile=profile, resume_text=RESUME, answer_fn=_bad_fn, job=job)

    assert plan.fields["location_q"] == 1
    assert plan.fields["start_q"] == "Immediately"
    assert plan.fields["leadership_q"] == 1
    assert plan.fields["comp_q"] == 1
    assert plan.ready is True


def test_maps_prior_employment_from_profile_history_and_opts_out_of_sms():
    job = {**JOB, "company": "doordashusa", "site": "doordashusa"}
    q = _identity_qs() + [
        {"required": True, "label": "Have you worked at DoorDash?",
         "fields": [{"name": "history_q", "type": "multi_value_single_select",
                     "values": [{"label": "I am a previous employee", "value": 60},
                                {"label": "I have not worked at DoorDash", "value": 61}]}]},
        {"required": True, "label": "Applicant Privacy Acknowledgement",
         "fields": [{"name": "privacy_yes_q", "type": "multi_value_single_select",
                     "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
        {"required": True,
         "label": "Would you like to receive communications via SMS and/or WhatsApp about your application process?",
         "fields": [{"name": "sms_q", "type": "multi_value_single_select",
                     "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
    ]

    plan = build_answer_plan(q, profile=PROFILE, resume_text=RESUME, answer_fn=_bad_fn, job=job)

    assert plan.fields["history_q"] == 61
    assert plan.fields["privacy_yes_q"] == 1
    assert plan.fields["sms_q"] == 0
    assert plan.ready is True


def test_maps_completed_bachelors_degree_from_profile_education():
    q = _identity_qs() + [
        {"required": True, "label": "Have you graduated with your Bachelor's Degree?",
         "fields": [{"name": "degree_q", "type": "multi_value_single_select",
                     "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
    ]

    plan = build_answer_plan(q, profile=PROFILE, resume_text=RESUME, answer_fn=_bad_fn, job=JOB)

    assert plan.fields["degree_q"] == 1
    assert plan.ready is True


def test_free_text_question_goes_through_the_answerer():
    q = [{"required": False, "label": "Why do you want to work here?",
          "fields": [{"name": "question_1", "type": "textarea"}]}]
    plan = _plan(q)
    assert plan.fields["question_1"].startswith("Your desk's Python")
    assert "question_1" in plan.free_text


def test_work_authorization_select_maps_to_yes():
    q = [{"required": True, "label": "Are you legally authorized to work in the US?",
          "fields": [{"name": "question_2", "type": "multi_value_single_select",
                      "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]}]
    assert _plan(q).fields["question_2"] == 1


def test_demographic_select_declines():
    q = [{"required": False, "label": "Gender",
          "fields": [{"name": "question_3", "type": "multi_value_single_select",
                      "values": [{"label": "Male", "value": 1}, {"label": "Female", "value": 2},
                                 {"label": "Decline To Self Identify", "value": 3}]}]}]
    assert _plan(q).fields["question_3"] == 3


def test_optional_free_text_is_skipped_not_faked_when_unverifiable():
    q = [{"required": False, "label": "Anything else?",
          "fields": [{"name": "question_x", "type": "textarea"}]}]
    plan = _plan(q, answer_fn=_bad_fn)
    assert "question_x" not in plan.fields   # never submit an unverified answer
    assert plan.ready is True                # optional -> doesn't block


def test_required_unverifiable_free_text_blocks_ready():
    q = [{"required": True, "label": "Describe a hard problem you solved.",
          "fields": [{"name": "q_req", "type": "textarea"}]}]
    plan = _plan(q, answer_fn=_bad_fn)
    assert "q_req" not in plan.fields
    assert plan.ready is False
    assert any("hard problem" in u.lower() for u in plan.unmapped_required)


def test_resume_text_textarea_uses_real_resume_not_the_answerer():
    # Greenhouse's "Resume/CV" offers a file (resume) AND a paste textarea
    # (resume_text). The textarea must be filled from the REAL resume, never
    # sent to the answerer (which would fabricate a resume).
    calls = []

    def spy_fn(question, **kw):
        calls.append(question)
        return _FakeAns(True, "FABRICATED RESUME TEXT")

    qs = [
        {"required": True, "label": "First Name", "fields": [{"name": "first_name", "type": "input_text"}]},
        {"required": True, "label": "Email", "fields": [{"name": "email", "type": "input_text"}]},
        {"required": True, "label": "Resume/CV",
         "fields": [{"name": "resume", "type": "input_file"},
                    {"name": "resume_text", "type": "textarea"}]},
    ]
    plan = build_answer_plan(qs, profile=PROFILE, resume_text=RESUME, answer_fn=spy_fn, job=JOB)
    assert plan.fields["resume_text"] == RESUME
    assert "resume_text" not in plan.free_text
    assert calls == []
    assert plan.resume_field == "resume"
    assert plan.ready is True


def test_unmappable_required_select_blocks_ready():
    q = [{"required": True, "label": "What is your favorite color?",
          "fields": [{"name": "q_c", "type": "multi_value_single_select",
                      "values": [{"label": "Red", "value": 1}, {"label": "Blue", "value": 2}]}]}]
    plan = _plan(q)
    assert plan.ready is False
    assert any("favorite color" in u.lower() for u in plan.unmapped_required)
