from applypilot.apply.workday_onboarding import bootstrap_workday_account


class Locator:
    def __init__(self, page, selector): self.page, self.selector = page, selector
    @property
    def first(self): return self
    @property
    def last(self): return self
    def count(self):
        return 1 if any(token in self.selector or self.selector in token
                        for token in self.page.visible) else 0
    def is_visible(self): return self.count() > 0
    def inner_text(self): return self.page.text
    def evaluate(self, _js):
        if "click" in _js:
            self.page.clicked.append(self.selector)
        return True
    def fill(self, value): self.page.fills.append((self.selector, value))
    def press_sequentially(self, value, delay=0): self.page.values[self.selector] = value
    def input_value(self): return self.page.values.get(self.selector, "")
    def press(self, _key): return None
    def locator(self, selector): return Locator(self.page, selector)
    def is_checked(self): return False
    def check(self): self.page.checked = True
    def click(self, **_kwargs):
        self.page.clicked.append(self.selector)
        if "Checkbox" in self.selector:
            self.page.checked = True
    def wait_for(self, **kwargs):
        if kwargs.get("state") == "detached":
            self.page.visible.discard(self.selector)
            self.page.visible.add('input[type="file"]')


class Page:
    def __init__(self, *, ready=False, captcha=False):
        self.visible = {'[data-automation-id="signInContent"]', "body",
                        '[data-automation-id="email"]', '[data-automation-id="password"]',
                        '[data-automation-id="verifyPassword"]',
                        '[data-automation-id="createAccountCheckbox"][type="checkbox"]',
                        '[data-automation-id="click_filter"]',
                        '[data-automation-id="createAccountSubmitButton"]'}
        if ready:
            self.visible.add('input[type="file"]')
        if captcha:
            self.visible.add(
                'iframe[src*="recaptcha" i], iframe[src*="hcaptcha" i], .g-recaptcha, .h-captcha'
            )
        self.text, self.fills, self.clicked, self.checked, self.values = (
            "Create Account", [], [], False, {}
        )
    def locator(self, selector): return Locator(self, selector)
    def get_by_role(self, role, name, exact=True): return Locator(self, f"{role}:{name}")
    def wait_for_timeout(self, _ms): self.visible.add('input[type="file"]')


def test_missing_credentials_park_without_browser_actions():
    assert bootstrap_workday_account(Page(), email="", password="").reason == "credentials_missing"


def test_existing_application_session_is_ready():
    result = bootstrap_workday_account(Page(ready=True), email="x@y.com", password="secret")
    assert result.status == "ready"
    assert result.reason == "session_already_ready"


def test_current_my_information_step_is_a_ready_application_session():
    page = Page()
    page.visible.add('[data-automation-id="applyFlowMyInfoPage"]')

    result = bootstrap_workday_account(page, email="x@y.com", password="secret")

    assert result.status == "ready"
    assert result.reason == "session_already_ready"
    assert page.fills == []


def test_captcha_is_explicit_boundary():
    result = bootstrap_workday_account(Page(captcha=True), email="x@y.com", password="secret")
    assert result.status == "captcha"


def test_account_creation_fills_credentials_but_result_contains_no_secret():
    page = Page()
    result = bootstrap_workday_account(page, email="x@y.com", password="secret-value")
    assert result.status == "ready"
    assert page.checked is True
    assert "secret-value" not in repr(result)


class _ExplodingEmailPage(Page):
    def locator(self, selector):
        if selector == '[data-automation-id="email"]':
            raise RuntimeError("email control rejected secret-value")
        return super().locator(selector)


def test_account_creation_driver_error_keeps_redacted_first_line():
    result = bootstrap_workday_account(
        _ExplodingEmailPage(), email="x@y.com", password="secret-value"
    )

    assert result.reason == "create_account_driver_error:RuntimeError:email control rejected [redacted]"
    assert "secret-value" not in result.reason
