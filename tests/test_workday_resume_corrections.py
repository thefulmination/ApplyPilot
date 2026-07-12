from __future__ import annotations

from applypilot.apply.workday_adapter import (
    build_canonical_resume,
    build_resume_correction_plan,
    parse_resume_control_groups,
)


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


def test_canonical_resume_parser_extracts_only_known_factual_records():
    profile = {
        "resume_facts": {
            "preserved_companies": ["Acme Capital", "Beta Labs"],
            "preserved_school": "State University",
        },
        "personal": {"linkedin_url": "https://linkedin.com/in/candidate"},
    }
    text = """Work Experience
Acme Capital, Strategy Manager (Remote) January 2022 - Present
Beta Labs, Analyst July 2020 - December 2021
CW Seafood/John's Farms, Customer Insights Analyst March 2018 - March 2020
Sabbatical January 2019 - December 2019

EDUCATION
State University, City January 2016 - May 2020
Bachelor of Science: Economics Major GPA 3.8
SKILLS
Python
"""
    canonical = build_canonical_resume(profile=profile, resume_text=text)
    assert canonical["work_history"] == [
        {
            "company": "Acme Capital",
            "title": "Strategy Manager",
            "start_date": "January 2022",
            "end_date": "Present",
            "currently_working": True,
        },
        {
            "company": "Beta Labs",
            "title": "Analyst",
            "start_date": "July 2020",
            "end_date": "December 2021",
            "currently_working": False,
        },
        {
            "company": "CW Seafood/John's Farms",
            "title": "Customer Insights Analyst",
            "start_date": "March 2018",
            "end_date": "March 2020",
            "currently_working": False,
        },
    ]
    assert canonical["education"] == [{
        "school": "State University",
        "degree": "Bachelor of Science",
        "field_of_study": "Economics",
        "graduation_date": "May 2020",
    }]
    assert canonical["links"] == {"linkedin": "https://linkedin.com/in/candidate"}


def test_canonical_resume_applies_authoritative_work_and_education_locations():
    profile = {
        "resume_facts": {
            "preserved_companies": ["Acme Capital"],
            "work_locations": {"Acme Capital": "Hicksville, NY"},
            "education": {
                "school": "State University",
                "degree": "Bachelor of Science",
                "discipline": "Quantitative Finance",
                "location": "Hoboken, NJ",
            },
            "preserved_school": "State University",
        }
    }
    canonical = build_canonical_resume(
        profile=profile,
        resume_text="""Work Experience
Acme Capital, Analyst March 2020 - March 2022
EDUCATION
State University, Hoboken, NJ Aug. 2012 - Aug. 2016
Bachelor of Science: Quantitative Finance
""",
    )

    assert canonical["work_history"][0]["location"] == "Hicksville, NY"
    assert canonical["education"][0]["location"] == "Hoboken, NJ"


def test_grouped_workday_resume_controls_are_parsed_without_cross_record_merging():
    parsed = parse_resume_control_groups([
        {"key": "companyName", "label": "Company", "group": "workExperienceItem-0", "value": "Acme"},
        {"key": "jobTitle", "label": "Job Title", "group": "workExperienceItem-0", "value": "Analyst"},
        {"key": "school", "label": "School or University", "group": "educationItem-0", "value": "State University"},
        {"key": "degree", "label": "Degree", "group": "educationItem-0", "value": "Bachelor"},
        {"key": "jobTitle", "label": "Job Title", "group": "workExperienceItem-1", "value": "Manager"},
    ])
    assert parsed == {
        "work_history": [
            {"company": "Acme", "title": "Analyst"},
            {"title": "Manager"},
        ],
        "education": [{"school": "State University", "degree": "Bachelor"}],
        "links": {},
    }
