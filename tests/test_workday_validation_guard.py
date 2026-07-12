from __future__ import annotations

from applypilot.apply.workday_adapter import (
    ValidationGuard,
    WorkdayFieldAction,
    WorkdayFieldPlan,
)


PLAN = WorkdayFieldPlan(
    actions=(
        WorkdayFieldAction("fill", "email", "candidate@example.com"),
        WorkdayFieldAction("select", "country", "United States of America"),
    ),
    unresolved_required=(),
)


def test_no_validation_errors_is_clear():
    decision = ValidationGuard().decide([], PLAN)
    assert decision.action == "clear"
    assert decision.repairs == ()


def test_first_mapped_error_gets_one_targeted_repair():
    guard = ValidationGuard()
    decision = guard.decide(
        [{"key": "email", "label": "Email", "message": "Enter a valid email"}],
        PLAN,
    )
    assert decision.action == "repair"
    assert decision.repairs == (WorkdayFieldAction("fill", "email", "candidate@example.com"),)
    assert guard.attempted_keys == {"email"}


def test_persistent_error_parks_after_one_repair():
    guard = ValidationGuard()
    issue = [{"key": "country", "label": "Country", "message": "Country is required"}]
    assert guard.decide(issue, PLAN).action == "repair"
    second = guard.decide(issue, PLAN)
    assert second.action == "park"
    assert second.reason == "validation_repair_exhausted"


def test_unmapped_error_parks_without_guessing():
    decision = ValidationGuard().decide(
        [{"key": "novelQuestion", "label": "Novel Question", "message": "Required"}],
        PLAN,
    )
    assert decision.action == "park"
    assert decision.reason == "unmapped_validation_error"
    assert decision.repairs == ()


def test_multiple_mapped_errors_get_one_bounded_batch():
    guard = ValidationGuard()
    decision = guard.decide(
        [
            {"key": "email", "message": "Invalid"},
            {"key": "country", "message": "Required"},
        ],
        PLAN,
    )
    assert decision.action == "repair"
    assert {repair.key for repair in decision.repairs} == {"email", "country"}
    assert guard.attempted_keys == {"email", "country"}


def test_ethnicity_group_error_repairs_selected_option_key():
    action = WorkdayFieldAction(
        "check_box", "decline-ethnicityMulti", "Yes", "privacy_default"
    )
    guard = ValidationGuard()
    decision = guard.decide(
        [{"key": "first-option-ethnicityMulti", "message": "Race is required"}],
        WorkdayFieldPlan(actions=(action,), unresolved_required=()),
    )
    assert decision.action == "repair"
    assert decision.repairs == (action,)
    assert guard.attempted_keys == {"first-option-ethnicityMulti", action.key}


def test_dynamic_group_validation_key_matches_unique_suffix():
    action = WorkdayFieldAction(
        "fill", "workExperience-4--jobTitle", "Strategy Manager", "canonical_resume"
    )
    decision = ValidationGuard().decide(
        [{"key": "jobTitle", "message": "Job Title is required"}],
        WorkdayFieldPlan(actions=(action,), unresolved_required=()),
    )
    assert decision.action == "repair"
    assert decision.repairs == (action,)


def test_dynamic_group_validation_suffix_ambiguity_stays_parked():
    plan = WorkdayFieldPlan(actions=(
        WorkdayFieldAction("fill", "workExperience-1--location", "Hicksville, NY"),
        WorkdayFieldAction("fill", "workExperience-2--location", "Remote"),
    ), unresolved_required=())
    decision = ValidationGuard().decide(
        [{"key": "location", "message": "Location is required"}], plan
    )
    assert decision.action == "park"
    assert decision.reason == "unmapped_validation_error"
