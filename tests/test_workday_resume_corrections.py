from __future__ import annotations

from applypilot.apply.workday_adapter import build_resume_correction_plan


CANONICAL = {
    "work_history": [
        {
            "company": "Acme Capital",
            "title": "Strategy Manager",
            "start_date": "January 2022",
            "end_date": "June 2025",
            "currently_working": False,
        },
        {
            "company": "Beta Labs",
            "title": "Analyst",
            "start_date": "July 2020",
            "end_date": "December 2021",
            "currently_working": False,
        },
    ],
    "education": [
        {
            "school": "State University",
            "degree": "Bachelor of Science",
            "field_of_study": "Economics",
            "graduation_date": "May 2020",
        }
    ],
    "links": {
        "linkedin": "https://linkedin.com/in/candidate",
        "portfolio": "https://candidate.example.com",
    },
}


def test_correction_plan_repairs_stable_parse_mismatches():
    parsed = {
        "work_history": [
            {
                "company": "Acme Capital",
                "title": "Strategy Manager",
                "start_date": "January 2022",
                "end_date": "June 2023",
                "currently_working": True,
            }
        ],
        "education": [
            {
                "school": "State University",
                "degree": "Bachelor of Science",
                "field_of_study": "Finance",
                "graduation_date": "May 2020",
            }
        ],
        "links": {"linkedin": "https://linkedin.com/in/wrong"},
    }
    plan = build_resume_correction_plan(parsed=parsed, canonical=CANONICAL)
    summary = {(a.section, a.action, a.field): a.value for a in plan.actions}
    assert summary[("work_history", "set", "end_date")] == "June 2025"
    assert summary[("work_history", "set", "currently_working")] is False
    assert summary[("education", "set", "field_of_study")] == "Economics"
    assert summary[("links", "set", "linkedin")] == "https://linkedin.com/in/candidate"
    assert summary[("links", "set", "portfolio")] == "https://candidate.example.com"
    assert any(action.action == "add_record" and action.section == "work_history" for action in plan.actions)


def test_exact_resume_parse_produces_no_actions():
    plan = build_resume_correction_plan(parsed=CANONICAL, canonical=CANONICAL)
    assert plan.actions == ()
    assert plan.changed is False


def test_plan_never_deletes_extra_parsed_records():
    parsed = {**CANONICAL, "work_history": CANONICAL["work_history"] + [{"company": "Extra"}]}
    plan = build_resume_correction_plan(parsed=parsed, canonical=CANONICAL)
    assert all(action.action != "delete" for action in plan.actions)
