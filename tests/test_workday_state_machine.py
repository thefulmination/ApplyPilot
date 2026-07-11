from __future__ import annotations

import pytest

from applypilot.apply.workday_adapter import WorkdayState, WorkdayStateMachine, detect_state


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        ({"automation_ids": ["signInContent"]}, WorkdayState.LOGIN),
        ({"automation_ids": ["file-upload-input-ref"], "heading": "Upload your resume"}, WorkdayState.RESUME),
        ({"automation_ids": ["contactInformationPage"]}, WorkdayState.PERSONAL_INFORMATION),
        ({"automation_ids": ["workExperienceSection"]}, WorkdayState.EXPERIENCE),
        ({"automation_ids": ["applicationQuestionsPage"]}, WorkdayState.QUESTIONS),
        ({"automation_ids": ["applyFlowPrimaryQuestionsPage"]}, WorkdayState.QUESTIONS),
        ({"automation_ids": ["voluntaryDisclosuresPage"]}, WorkdayState.DISCLOSURES),
        ({"automation_ids": ["selfIdentificationPage"]}, WorkdayState.SELF_ID),
        ({"automation_ids": ["reviewPage"], "buttons": ["Submit application"]}, WorkdayState.REVIEW),
        ({"submit_clicked": True}, WorkdayState.SUBMIT),
        ({"heading": "Application submitted", "text": "Thank you for applying"}, WorkdayState.CONFIRMATION),
        ({"heading": "Unexpected maintenance page"}, WorkdayState.UNSUPPORTED),
    ],
)
def test_detects_every_workday_state(snapshot, expected):
    assert detect_state(snapshot) == expected


def test_state_machine_accepts_normal_sequence_and_records_metadata():
    machine = WorkdayStateMachine()
    sequence = [
        {"automation_ids": ["signInContent"]},
        {"automation_ids": ["file-upload-input-ref"]},
        {"automation_ids": ["contactInformationPage"]},
        {"automation_ids": ["workExperienceSection"]},
        {"automation_ids": ["applicationQuestionsPage"]},
        {"automation_ids": ["voluntaryDisclosuresPage"]},
        {"automation_ids": ["selfIdentificationPage"]},
        {"automation_ids": ["reviewPage"]},
    ]
    assert all(machine.observe(snapshot).allowed for snapshot in sequence)
    assert machine.mark_submit_clicked().allowed
    assert machine.observe({"text": "Application submitted. Thank you for applying."}).allowed
    assert machine.terminal is True
    assert machine.metadata()["invalid_transitions"] == 0


def test_state_machine_rejects_backward_or_skipped_transition_without_mutating_state():
    machine = WorkdayStateMachine()
    assert machine.observe({"automation_ids": ["contactInformationPage"]}).allowed
    bad = machine.observe({"automation_ids": ["signInContent"]})
    assert bad.allowed is False
    assert bad.reason == "invalid_transition"
    assert machine.current == WorkdayState.PERSONAL_INFORMATION
    assert machine.metadata()["invalid_transitions"] == 1
