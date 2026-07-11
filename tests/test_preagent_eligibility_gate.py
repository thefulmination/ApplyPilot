from __future__ import annotations

from applypilot.apply import pgqueue
from applypilot.fleet import eligibility, queue


def test_remote_and_accepted_us_locations_are_eligible():
    policy = {"accept_any_us": True, "accept_patterns": ["San Francisco", "Remote"]}
    assert eligibility.evaluate_job_eligibility(
        location="Remote - United States",
        description="Work from home",
        location_policy=policy,
    ) == ("eligible", "remote")
    assert eligibility.evaluate_job_eligibility(
        location="Austin, Texas",
        description="Onsite",
        location_policy=policy,
    ) == ("eligible", "us_relocation_allowed")


def test_explicit_foreign_onsite_and_foreign_only_remote_are_rejected():
    status, reason = eligibility.evaluate_job_eligibility(
        location="London, UK",
        description="This position is onsite five days per week.",
    )
    assert status == "ineligible" and reason.startswith("not_eligible_location:")

    status, reason = eligibility.evaluate_job_eligibility(
        location="Remote - Canada only",
        description="Candidates must be based in Canada.",
    )
    assert status == "ineligible" and reason == "not_eligible_work_auth:canada_only"


def test_no_sponsorship_rejects_only_when_profile_requires_it():
    kwargs = {
        "location": "New York, United States",
        "description": "We are unable to provide visa sponsorship.",
    }
    assert eligibility.evaluate_job_eligibility(
        **kwargs,
        work_authorization={"require_sponsorship": "yes"},
    )[0] == "ineligible"
    assert eligibility.evaluate_job_eligibility(
        **kwargs,
        work_authorization={"require_sponsorship": "no"},
    )[0] == "eligible"


def test_unknown_location_is_not_falsely_rejected():
    assert eligibility.evaluate_job_eligibility(
        location=None,
        description="Operations role",
        location_policy={"reject_patterns": ["London"]},
    ) == ("eligible", "no_deterministic_exclusion")


def test_staging_terminates_ineligible_before_liveness_or_paid_lease(fleet_db):
    url = "https://example.com/jobs/foreign"
    with pgqueue.connect(fleet_db) as conn:
        queue.push_apply_jobs(
            conn,
            [{
                "url": url,
                "company": "Acme",
                "title": "Operator",
                "application_url": url,
                "score": 9.0,
                "target_host": "example.com",
                "eligibility_status": "ineligible",
                "eligibility_reason": "not_eligible_location:london",
            }],
            approved_batch="batch-e",
            require_liveness=True,
            require_eligibility=True,
        )
        assert queue.claim_liveness_check(conn, "preflight") is None
        assert queue.lease_apply(conn, "paid", home_ip="1.2.3.4") is None
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, apply_status, apply_error, attempts FROM apply_queue WHERE url=%s",
                (url,),
            )
            row = cur.fetchone()
    assert row["status"] == "failed"
    assert row["apply_status"] == "failed"
    assert row["apply_error"] == "not_eligible_location:london"
    assert row["attempts"] == 0
