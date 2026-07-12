from __future__ import annotations

from applypilot.apply.workday_adapter import WorkdayAdapterRunner


PROFILE = {
    "personal": {
        "full_name": "Jane Candidate",
        "email": "jane@example.com",
        "phone": "555-0100",
    }
}


class SyntheticDriver:
    def __init__(self, *, persistent_validation=False, unresolved=False):
        self.pages = [
            {"automation_ids": ["file-upload-input-ref"]},
            {"automation_ids": ["contactInformationPage"]},
            {"automation_ids": ["applicationQuestionsPage"]},
            {"automation_ids": ["reviewPage"]},
        ]
        self.index = 0
        self.actions = []
        self.submits = 0
        self.validation_reads = 0
        self.persistent_validation = persistent_validation
        self.unresolved = unresolved

    def snapshot(self):
        return self.pages[self.index]

    def upload_resume(self, path):
        self.actions.append(("upload", path))

    def parsed_resume(self):
        return {"work_history": [], "education": [], "links": {}}

    def apply_resume_correction(self, action):
        self.actions.append(("resume", action.action, action.field))

    def fields(self):
        if self.index == 1:
            return [
                {"key": "first", "label": "First Name", "required": True},
                {"key": "last", "label": "Last Name", "required": True},
                {"key": "email", "label": "Email", "required": True},
            ]
        if self.index == 2 and self.unresolved:
            return [{"key": "novel", "label": "Novel strategy", "required": True}]
        return []

    def apply_field_action(self, action):
        self.actions.append((action.action, action.key, action.value))

    def next(self):
        self.index += 1

    def validation_issues(self):
        if not self.persistent_validation or self.index != 2:
            return []
        self.validation_reads += 1
        return [{"key": "email", "message": "Invalid"}]

    def submit(self):
        self.submits += 1

    def wait_after_submit(self):
        return None

    def final_url(self):
        return "https://acme.wd5.myworkdayjobs.com/application/confirmation"

    def page_text(self):
        return "Application submitted. Thank you for applying."


def test_runner_completes_synthetic_workday_with_one_submit_and_no_model():
    driver = SyntheticDriver()
    runner = WorkdayAdapterRunner(driver, profile=PROFILE, resume_path="resume.pdf")
    result = runner.execute(submit=True)
    assert result.status == "applied"
    assert driver.submits == 1
    assert result.metadata["confirmation_evidence"]
    assert result.metadata["invalid_transitions"] == 0


def test_runner_stops_at_review_in_shadow_mode():
    driver = SyntheticDriver()
    result = WorkdayAdapterRunner(driver, profile=PROFILE).execute(submit=False)
    assert result.status == "dry_run"
    assert result.reason == "review_ready"
    assert driver.submits == 0


def test_runner_parks_unmapped_required_question():
    driver = SyntheticDriver(unresolved=True)
    result = WorkdayAdapterRunner(driver, profile=PROFILE).execute(submit=True)
    assert result.status == "parked"
    assert result.reason == "unmapped_required_fields"
    assert driver.submits == 0


def test_runner_records_actionable_exception_and_reuses_approved_answer():
    recorded = []
    driver = SyntheticDriver(unresolved=True)
    runner = WorkdayAdapterRunner(
        driver,
        profile=PROFILE,
        answer_resolver=lambda field: "Approved response" if field.key == "novel" else None,
        exception_sink=lambda fields: recorded.extend(fields) or [17],
    )
    result = runner.execute(submit=False)
    assert result.status == "dry_run"
    assert result.reason == "review_ready"
    assert recorded == []
    assert ("fill", "novel", "Approved response") in driver.actions


def test_runner_reports_approved_answer_cache_hits_and_misses():
    driver = SyntheticDriver(unresolved=True)
    calls = []

    def resolver(field):
        calls.append(field.key)
        return "Approved response" if field.key == "novel" else None

    result = WorkdayAdapterRunner(
        driver,
        profile=PROFILE,
        answer_resolver=resolver,
    ).execute(submit=False)

    assert result.reason == "review_ready"
    assert calls == ["novel"]
    assert result.metadata["answer_cache"] == {
        "lookups": 1,
        "hits": 1,
        "misses": 0,
        "avoided_model_calls": 1,
    }


def test_runner_exception_metadata_contains_queue_ids_and_safe_field_context():
    driver = SyntheticDriver(unresolved=True)
    runner = WorkdayAdapterRunner(
        driver,
        profile=PROFILE,
        exception_sink=lambda fields: [23] if fields[0].key == "novel" else [],
    )
    result = runner.execute(submit=False)
    assert result.reason == "unmapped_required_fields"
    assert result.metadata["exception_ids"] == [23]
    assert result.metadata["exceptions"] == [{
        "key": "novel", "label": "Novel strategy", "field_type": "text", "options": [],
    }]


def test_runner_reconciles_mapped_fields_but_not_unresolved_question():
    batches = []
    driver = SyntheticDriver(unresolved=True)
    result = WorkdayAdapterRunner(
        driver,
        profile=PROFILE,
        exception_reconciler=lambda fields: batches.append([field.label for field in fields]),
    ).execute(submit=False)
    assert result.reason == "unmapped_required_fields"
    assert any("First Name" in batch and "Email" in batch for batch in batches)
    assert all("Novel strategy" not in batch for batch in batches)


def test_runner_applies_checkboxes_after_rerendering_dropdowns():
    class DisclosureDriver(SyntheticDriver):
        def fields(self):
            if self.index == 2:
                return [
                    {"key": "decline-ethnicityMulti", "label": "I do not wish to answer",
                     "type": "checkbox", "required": True},
                    {"key": "acceptTermsAndAgreements", "label": "Terms and conditions",
                     "type": "checkbox", "required": True},
                    {"key": "gender", "label": "Gender", "type": "combobox", "required": True},
                ]
            return super().fields()

    driver = DisclosureDriver()
    result = WorkdayAdapterRunner(driver, profile=PROFILE).execute(submit=False)
    assert result.reason == "review_ready"
    disclosure_actions = [action for action in driver.actions if action[1] in {
        "gender", "acceptTermsAndAgreements", "decline-ethnicityMulti"
    }]
    assert [action[1] for action in disclosure_actions] == [
        "gender", "acceptTermsAndAgreements", "decline-ethnicityMulti"
    ]


def test_runner_parks_persistent_validation_after_one_repair():
    driver = SyntheticDriver(persistent_validation=True)
    result = WorkdayAdapterRunner(driver, profile=PROFILE).execute(submit=True)
    assert result.status == "parked"
    assert result.reason == "validation_repair_exhausted"
    assert result.metadata["validation_repairs"] == ["email"]
    assert driver.submits == 0


def test_runner_converts_driver_exception_to_parked_result():
    driver = SyntheticDriver()
    driver.snapshot = lambda: (_ for _ in ()).throw(RuntimeError("detached page"))
    result = WorkdayAdapterRunner(driver, profile=PROFILE).execute(submit=True)
    assert result.status == "parked"
    assert result.reason == "driver_error:RuntimeError"
    assert "detached page" in result.metadata["driver_error"]
