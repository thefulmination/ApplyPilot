from __future__ import annotations

from applypilot.apply.workday_adapter import (
    PlaywrightWorkdayDriver,
    PlaywrightWorkdayPageDriver,
    WorkdayFieldAction,
    WorkdayField,
)


class ReviewPage:
    def __init__(self) -> None:
        self.role_lookups = 0

    def evaluate(self, script, *args, **kwargs):
        if "applyFlowReviewPage" in script:
            return True
        return {"url": "https://example.test/review", "step": "Review", "errors": ""}

    def get_by_role(self, *args, **kwargs):
        self.role_lookups += 1
        raise AssertionError("next control must not be located after Review is visible")


def test_next_is_noop_when_review_is_already_visible():
    page = ReviewPage()

    PlaywrightWorkdayPageDriver(page).next()

    assert page.role_lookups == 0


class _Locator:
    def __init__(self, name, count=1):
        self.name = name
        self._count = count

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    def count(self):
        return self._count


class _FieldContainer(_Locator):
    def __init__(self, control):
        super().__init__("container")
        self.control = control

    def locator(self, selector):
        return self.control


class _ScopedControlPage:
    def __init__(self, key):
        self.key = key
        self.stale = _Locator("stale-exact-key")
        self.active = _Locator("active-control")
        self.container = _FieldContainer(self.active)
        self.locator_calls = []

    def locator(self, selector):
        self.locator_calls.append(selector)
        if selector == f'[data-automation-id="{self.key}"]':
            return self.stale
        if selector == f'[data-automation-id="formField-{self.key}"]':
            return self.container
        return _Locator("missing", count=0)

    def get_by_label(self, *args, **kwargs):
        raise AssertionError("label fallback must not bypass the scoped Workday field")


def test_control_uses_visible_form_field_control_before_stale_exact_match():
    page = _ScopedControlPage("phoneType")
    field = WorkdayField(key="phoneType", label="Phone Type", field_type="combobox")

    control = PlaywrightWorkdayDriver(page)._control(field)

    assert control.name == "active-control"
    assert page.locator_calls[0] == '[data-automation-id="formField-phoneType"]'


def test_control_strips_workday_source_suffix_for_form_field_lookup():
    page = _ScopedControlPage("source")
    field = WorkdayField(
        key="source--source",
        label="How Did You Hear About Us?",
        field_type="combobox",
    )

    control = PlaywrightWorkdayDriver(page)._control(field)

    assert control.name == "active-control"
    assert page.locator_calls[0] == '[data-automation-id="formField-source"]'


class _DropdownButton(_Locator):
    def __init__(self):
        super().__init__("dropdown-button")
        self.events = []

    def get_attribute(self, name):
        return "listbox" if name == "aria-haspopup" else None

    def click(self):
        self.events.append("click")

    def press(self, key):
        self.events.append(("press", key))


class _PromptOption(_Locator):
    def __init__(self, events):
        super().__init__("prompt-option")
        self.events = events

    def click(self, **kwargs):
        self.events.append("prompt_option_click")


class _PromptOptionPage:
    def __init__(self):
        self.button = _DropdownButton()
        self.option_events = []
        self.container = _FieldContainer(self.button)

    def locator(self, selector, **kwargs):
        if selector == '[data-automation-id="formField-source"]':
            return self.container
        if selector == '[data-automation-id="promptOption"]':
            return _PromptOption(self.option_events)
        return _Locator("missing", count=0)

    def get_by_label(self, *args, **kwargs):
        raise AssertionError("label fallback must not be used")

    def get_by_role(self, *args, **kwargs):
        raise AssertionError("ARIA option fallback must not be needed")

    def wait_for_timeout(self, _milliseconds):
        return None


def test_button_backed_workday_dropdown_uses_role_options():
    page = _PromptOptionPage()
    field = WorkdayField(key="source--source", label="How Did You Hear About Us?")

    page.get_by_role = lambda *args, **kwargs: _PromptOption(page.option_events)
    PlaywrightWorkdayDriver(page).select(field, "Job Boards/Websites > Other")

    assert page.button.events == ["click", ("press", "Tab")]
    assert page.option_events == ["prompt_option_click", "prompt_option_click"]


class _SelectInputControl:
    def get_attribute(self, name):
        return "selectinput" if name == "data-uxi-widget-type" else None

    def evaluate(self, script):
        return "Other" if "formField-" in script else ""

    def input_value(self):
        return ""

    def inner_text(self):
        return ""


def test_selectinput_readback_falls_back_to_form_field_selection_label():
    driver = PlaywrightWorkdayDriver(object())
    driver._control = lambda field: _SelectInputControl()

    assert driver.read_value(WorkdayField(key="source", label="Source")) == "Other"


class _SearchSelectInputControl(_SelectInputControl):
    def __init__(self):
        self.events = []
        self.selected = ""

    def fill(self, value):
        self.events.append(("fill", value))

    def click(self):
        self.events.append("click")

    def press(self, key):
        self.events.append(("press", key))
        if key == "Enter":
            self.selected = "Other"

    def evaluate(self, script):
        return self.selected if "formField-" in script else ""


def test_selectinput_prefers_search_commit_for_dynamic_multiselects():
    control = _SearchSelectInputControl()
    page = type("Page", (), {"wait_for_timeout": lambda self, milliseconds: None})()
    driver = PlaywrightWorkdayDriver(page)
    driver._control = lambda field: control

    driver.select(WorkdayField(key="source", label="Source"), "Other")

    assert control.events == [
        ("fill", ""), "click", ("fill", "Other"), ("press", "Enter"), ("press", "Tab")
    ]


class _CheckboxControl(_Locator):
    def __init__(self):
        super().__init__("checkbox")
        self.checked = False

    def is_checked(self):
        return self.checked

    def click(self):
        self.checked = True

    def press(self, key):
        assert key == "Tab"


class _NonCheckboxControl(_Locator):
    def is_checked(self):
        raise RuntimeError("Not a checkbox or radio button")


class _CheckboxCollisionPage:
    def __init__(self):
        self.checkbox = _CheckboxControl()
        self.non_checkbox = _NonCheckboxControl("terms-text")

    def evaluate(self, script):
        if "document.querySelectorAll" in script:
            return [{
                "key": "acceptTermsAndAgreements",
                "label": "I have read and consent to the terms and conditions.",
                "field_type": "checkbox",
                "required": True,
                "value": "",
            }]
        raise AssertionError("unexpected page evaluation")

    def locator(self, selector, **kwargs):
        if selector == '[data-automation-id="formField-acceptTermsAndAgreements"]':
            return _Locator("missing", count=0)
        if selector == '[data-automation-id="acceptTermsAndAgreements"]:visible':
            return _Locator("missing", count=0)
        if selector == '[data-automation-id="acceptTermsAndAgreements"]':
            return _Locator("missing", count=0)
        if selector == '[id="acceptTermsAndAgreements"], [name="acceptTermsAndAgreements"]':
            return self.non_checkbox
        if 'input[type="checkbox"]' in selector:
            return self.checkbox
        return _Locator("missing", count=0)

    def wait_for_timeout(self, _milliseconds):
        return None


def test_checkbox_action_ignores_non_checkbox_id_name_collision():
    page = _CheckboxCollisionPage()

    PlaywrightWorkdayPageDriver(page).apply_field_action(
        WorkdayFieldAction(
            "check_box",
            "acceptTermsAndAgreements",
            "Yes",
            "required_acknowledgement",
        )
    )

    assert page.checkbox.checked is True


class _DelayedConfirmationPage:
    def __init__(self):
        self.url = "https://example.test/review"
        self.polls = 0

    def locator(self, selector):
        assert selector == "body"
        page = self

        class Body:
            def inner_text(self):
                return "Application submitted. Thank you for applying." if page.polls >= 2 else "Review"

        return Body()

    def wait_for_timeout(self, _milliseconds):
        self.polls += 1


def test_submit_confirmation_waits_for_delayed_positive_evidence():
    page = _DelayedConfirmationPage()

    PlaywrightWorkdayPageDriver(page).wait_after_submit()

    assert page.polls == 2
