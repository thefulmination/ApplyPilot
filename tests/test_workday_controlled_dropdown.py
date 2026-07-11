from __future__ import annotations

from applypilot.apply.workday_adapter import WorkdayField, select_controlled_option


class FakeDriver:
    def __init__(self, observations, *, fail_first=False):
        self.observations = iter(observations)
        self.fail_first = fail_first
        self.selections = []

    def select(self, field, value):
        self.selections.append((field.key, field.label, value))
        if self.fail_first and len(self.selections) == 1:
            raise RuntimeError("transient control detach")

    def read_value(self, field):
        return next(self.observations)


FIELD = WorkdayField(
    key="countryDropdown",
    label="Country",
    field_type="combobox",
    required=True,
    options=("Canada", "United States of America"),
)


def test_controlled_option_requires_exact_verified_readback():
    driver = FakeDriver(["United States of America"])
    result = select_controlled_option(driver, FIELD, "United States of America")
    assert result.ok is True
    assert result.attempts == 1
    assert result.reason == "verified"
    assert driver.selections == [
        ("countryDropdown", "Country", "United States of America")
    ]


def test_controlled_option_retries_one_readback_mismatch():
    driver = FakeDriver(["Canada", "United States of America"])
    result = select_controlled_option(driver, FIELD, "United States of America")
    assert result.ok is True
    assert result.attempts == 2
    assert len(driver.selections) == 2


def test_controlled_option_stops_after_two_mismatches():
    driver = FakeDriver(["Canada", "Canada"])
    result = select_controlled_option(driver, FIELD, "United States of America", max_attempts=99)
    assert result.ok is False
    assert result.attempts == 2
    assert result.reason == "readback_mismatch"
    assert len(driver.selections) == 2


def test_control_exception_gets_only_one_retry():
    driver = FakeDriver(["United States of America"], fail_first=True)
    result = select_controlled_option(driver, FIELD, "United States of America")
    assert result.ok is True
    assert result.attempts == 2
