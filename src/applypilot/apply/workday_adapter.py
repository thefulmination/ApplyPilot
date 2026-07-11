"""Deterministic Workday application state machine and DOM contracts."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable
import re


class WorkdayState(str, Enum):
    LOGIN = "login"
    RESUME = "resume"
    PERSONAL_INFORMATION = "personal_information"
    EXPERIENCE = "experience"
    QUESTIONS = "questions"
    DISCLOSURES = "disclosures"
    SELF_ID = "self_id"
    REVIEW = "review"
    SUBMIT = "submit"
    CONFIRMATION = "confirmation"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class WorkdaySnapshot:
    url: str = ""
    heading: str = ""
    text: str = ""
    automation_ids: tuple[str, ...] = ()
    buttons: tuple[str, ...] = ()
    submit_clicked: bool = False

    @classmethod
    def from_dict(cls, raw: dict) -> "WorkdaySnapshot":
        return cls(
            url=str(raw.get("url") or ""),
            heading=str(raw.get("heading") or ""),
            text=str(raw.get("text") or ""),
            automation_ids=tuple(str(value) for value in raw.get("automation_ids") or ()),
            buttons=tuple(str(value) for value in raw.get("buttons") or ()),
            submit_clicked=bool(raw.get("submit_clicked")),
        )


@dataclass(frozen=True)
class Transition:
    previous: WorkdayState | None
    current: WorkdayState
    allowed: bool
    reason: str


_ALLOWED_NEXT = {
    WorkdayState.LOGIN: {WorkdayState.LOGIN, WorkdayState.RESUME, WorkdayState.PERSONAL_INFORMATION},
    WorkdayState.RESUME: {WorkdayState.RESUME, WorkdayState.PERSONAL_INFORMATION, WorkdayState.EXPERIENCE},
    WorkdayState.PERSONAL_INFORMATION: {
        WorkdayState.PERSONAL_INFORMATION,
        WorkdayState.EXPERIENCE,
        WorkdayState.QUESTIONS,
    },
    WorkdayState.EXPERIENCE: {
        WorkdayState.EXPERIENCE,
        WorkdayState.QUESTIONS,
        WorkdayState.DISCLOSURES,
    },
    WorkdayState.QUESTIONS: {
        WorkdayState.QUESTIONS,
        WorkdayState.DISCLOSURES,
        WorkdayState.SELF_ID,
        WorkdayState.REVIEW,
    },
    WorkdayState.DISCLOSURES: {
        WorkdayState.DISCLOSURES,
        WorkdayState.SELF_ID,
        WorkdayState.REVIEW,
    },
    WorkdayState.SELF_ID: {WorkdayState.SELF_ID, WorkdayState.REVIEW},
    WorkdayState.REVIEW: {WorkdayState.REVIEW, WorkdayState.SUBMIT, WorkdayState.CONFIRMATION},
    WorkdayState.SUBMIT: {WorkdayState.SUBMIT, WorkdayState.CONFIRMATION},
    WorkdayState.CONFIRMATION: {WorkdayState.CONFIRMATION},
    WorkdayState.UNSUPPORTED: {WorkdayState.UNSUPPORTED},
}


def _contains_any(value: str, needles: Iterable[str]) -> bool:
    low = value.lower()
    return any(needle in low for needle in needles)


def detect_state(snapshot: WorkdaySnapshot | dict) -> WorkdayState:
    """Classify a Workday page from stable DOM text/automation IDs only."""
    if isinstance(snapshot, dict):
        snapshot = WorkdaySnapshot.from_dict(snapshot)
    ids = " ".join(snapshot.automation_ids).lower()
    page = f"{snapshot.heading}\n{snapshot.text}".lower()
    url = snapshot.url.lower()
    buttons = " ".join(snapshot.buttons).lower()

    if _contains_any(page, ("application submitted", "thank you for applying", "application received")) or (
        "/candidate/home" in url and _contains_any(page, ("submitted", "application"))
    ):
        return WorkdayState.CONFIRMATION
    if snapshot.submit_clicked:
        return WorkdayState.SUBMIT
    if _contains_any(ids, ("signincontent", "signinpage", "createaccount")) or _contains_any(
        page, ("sign in to your account", "create a candidate account")
    ):
        return WorkdayState.LOGIN
    if "applyflowmyexppage" in ids:
        return WorkdayState.EXPERIENCE
    if _contains_any(ids, ("file-upload-input-ref", "resumeupload", "quickapply")) or _contains_any(
        snapshot.heading, ("upload your resume", "my information")
    ) and "resume" in page:
        return WorkdayState.RESUME
    if _contains_any(ids, ("contactinformationpage", "personalinformationpage", "applyflowmyinfopage")) or _contains_any(
        snapshot.heading, ("personal information", "contact information")
    ):
        return WorkdayState.PERSONAL_INFORMATION
    if _contains_any(ids, ("workexperiencesection", "educationsection", "experiencepage")) or _contains_any(
        snapshot.heading, ("experience", "work history", "education")
    ):
        return WorkdayState.EXPERIENCE
    if _contains_any(ids, ("applicationquestionspage", "applyflowprimaryquestionspage", "questionnairepage")) or _contains_any(
        snapshot.heading, ("application questions", "questionnaire")
    ):
        return WorkdayState.QUESTIONS
    if _contains_any(ids, ("voluntarydisclosurespage", "disclosurespage")) or _contains_any(
        snapshot.heading, ("voluntary disclosures", "terms and conditions")
    ):
        return WorkdayState.DISCLOSURES
    if _contains_any(ids, ("selfidentificationpage", "selfidentify")) or _contains_any(
        snapshot.heading, ("self identification", "self-identification")
    ):
        return WorkdayState.SELF_ID
    if _contains_any(ids, ("reviewpage", "reviewapplication")) or _contains_any(
        snapshot.heading, ("review your application", "review")
    ) or "submit application" in buttons:
        return WorkdayState.REVIEW
    return WorkdayState.UNSUPPORTED


@dataclass
class WorkdayStateMachine:
    current: WorkdayState | None = None
    transitions: list[Transition] = field(default_factory=list)
    visited: list[WorkdayState] = field(default_factory=list)

    def observe(self, snapshot: WorkdaySnapshot | dict) -> Transition:
        state = detect_state(snapshot)
        previous = self.current
        allowed = previous is None or state in _ALLOWED_NEXT.get(previous, set())
        reason = "initial" if previous is None else ("allowed" if allowed else "invalid_transition")
        transition = Transition(previous, state, allowed, reason)
        self.transitions.append(transition)
        if allowed:
            self.current = state
            if not self.visited or self.visited[-1] != state:
                self.visited.append(state)
        return transition

    def mark_submit_clicked(self) -> Transition:
        return self.observe(WorkdaySnapshot(submit_clicked=True))

    @property
    def terminal(self) -> bool:
        return self.current in {WorkdayState.CONFIRMATION, WorkdayState.UNSUPPORTED}

    def metadata(self) -> dict:
        return {
            "current_state": self.current.value if self.current else None,
            "visited": [state.value for state in self.visited],
            "terminal": self.terminal,
            "invalid_transitions": sum(not transition.allowed for transition in self.transitions),
        }


@dataclass(frozen=True)
class WorkdayField:
    key: str
    label: str
    field_type: str = "text"
    required: bool = False
    options: tuple[str, ...] = ()
    value: str | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "WorkdayField":
        raw_value = None if raw.get("value") is None else str(raw.get("value"))
        if raw_value and re.fullmatch(r"0\s+items?\s+selected", raw_value.strip(), re.I):
            raw_value = ""
        return cls(
            key=str(raw.get("key") or raw.get("automation_id") or raw.get("name") or ""),
            label=str(raw.get("label") or ""),
            field_type=str(raw.get("field_type") or raw.get("type") or "text"),
            required=bool(raw.get("required")),
            options=tuple(str(value) for value in raw.get("options") or ()),
            value=raw_value,
        )


@dataclass(frozen=True)
class WorkdayFieldAction:
    action: str
    key: str
    value: str
    source: str = "profile"


@dataclass(frozen=True)
class WorkdayFieldPlan:
    actions: tuple[WorkdayFieldAction, ...]
    unresolved_required: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.unresolved_required


def _normalize_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _profile_value(profile: dict, *paths: tuple[str, str]) -> str | None:
    for section, key in paths:
        value = (profile.get(section) or {}).get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _name_parts(profile: dict) -> tuple[str, str]:
    personal = profile.get("personal") or {}
    first = str(personal.get("first_name") or "").strip()
    last = str(personal.get("last_name") or "").strip()
    if first and last:
        return first, last
    tokens = str(personal.get("full_name") or "").split()
    return (tokens[0] if tokens else "", tokens[-1] if len(tokens) > 1 else "")


def _option(options: tuple[str, ...], wanted: Iterable[str]) -> str | None:
    wanted_normalized = {_normalize_label(value) for value in wanted}
    for option in options:
        if _normalize_label(option) in wanted_normalized:
            return option
    return None


_US_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}


def _map_factual_field(field: WorkdayField, profile: dict) -> tuple[str | None, str]:
    label = _normalize_label(f"{field.key} {field.label}")
    first, last = _name_parts(profile)
    mappings = (
        (("first name", "given name"), first),
        (("last name", "family name", "surname"), last),
        (("email", "email address"), _profile_value(profile, ("personal", "email"))),
        (("phone", "telephone", "mobile"), _profile_value(profile, ("personal", "phone"))),
        (("address line 1", "street address", "address1"), _profile_value(profile, ("personal", "address"), ("personal", "address_line_1"))),
        (("city",), _profile_value(profile, ("personal", "city"))),
        (("postal code", "zip code", "zipcode"), _profile_value(profile, ("personal", "postal_code"), ("personal", "zip_code"))),
        (("linkedin",), _profile_value(profile, ("personal", "linkedin_url"), ("links", "linkedin"))),
        (("portfolio", "website"), _profile_value(profile, ("personal", "portfolio_url"), ("personal", "website_url"), ("links", "portfolio"))),
    )
    for markers, value in mappings:
        if any(marker in label for marker in markers) and value:
            return value, "profile"

    work_auth = profile.get("work_authorization") or {}
    if any(marker in label for marker in (
        "legally authorized", "authorized to work", "work authorization", "eligible to work"
    )):
        raw = str(work_auth.get("legally_authorized_to_work", "")).lower()
        wanted = "Yes" if raw in {"yes", "true", "y", "1"} else "No" if raw in {"no", "false", "n", "0"} else None
        return (_option(field.options, (wanted,)) if wanted and field.options else wanted), "profile"
    if any(marker in label for marker in ("sponsorship", "visa sponsor", "require sponsor")):
        raw = str(work_auth.get("require_sponsorship", "")).lower()
        wanted = "Yes" if raw in {"yes", "true", "y", "1"} else "No" if raw in {"no", "false", "n", "0"} else None
        return (_option(field.options, (wanted,)) if wanted and field.options else wanted), "profile"

    if "country" in label and not any(marker in label for marker in ("state", "province", "region")):
        value = _profile_value(profile, ("personal", "country"))
        if value and _normalize_label(value) in {"us", "usa", "united states"}:
            value = "United States of America"
        return (_option(field.options, (value, "United States of America", "United States", "USA")) if field.options and value else value), "profile"
    if any(marker in label for marker in ("state", "province", "region")):
        value = _profile_value(profile, ("personal", "province_state"), ("personal", "state"))
        value = _US_STATE_NAMES.get(str(value).upper(), value) if value else value
        return (_option(field.options, (value,)) if field.options and value else value), "profile"

    if any(marker in label for marker in ("gender", "race", "ethnicity", "veteran", "disability")):
        decline = _option(field.options, ("Decline to Self Identify", "I do not wish to answer", "Prefer not to say", "Decline"))
        return decline, "profile"
    if any(marker in label for marker in ("how did you hear", "source")):
        source = str((profile.get("_application_context") or {}).get("source_board") or "").lower()
        source_labels = {
            "indeed": "Indeed", "linkedin": "LinkedIn", "glassdoor": "Glassdoor",
            "builtin": "BuiltIn", "corporate": "Corporate Website",
            "company": "Corporate Website",
        }
        wanted = source_labels.get(source)
        if wanted:
            return (_option(field.options, (wanted,)) if field.options else wanted), "application_source"
        if source == "hiringcafe":
            return None, "unmapped"
        return (_option(field.options, ("Job Board", "Company Website", "Other"))
                if field.options else "Job Board"), "application_source"
    if any(marker in label for marker in (
        "previously been employed", "previously employed", "previous worker", "former employee"
    )):
        companies = (profile.get("resume_facts") or {}).get("preserved_companies") or []
        target_company = str((profile.get("_application_context") or {}).get("company") or "")
        target = _normalize_label(target_company)
        employed = bool(target) and any(
            target in _normalize_label(str(company)) or _normalize_label(str(company)) in target
            for company in companies if str(company).strip()
        )
        return ("Yes" if employed else "No"), "resume_facts"
    return None, "unmapped"


def build_field_plan(fields: Iterable[WorkdayField | dict], *, profile: dict,
                     answer_resolver=None) -> WorkdayFieldPlan:
    """Map factual Workday fields without model calls or guessed values."""
    normalized_fields = tuple(
        WorkdayField.from_dict(raw) if isinstance(raw, dict) else raw for raw in fields
    )
    actions: list[WorkdayFieldAction] = []
    unresolved: list[str] = []
    processed_radio_keys: set[str] = set()
    for field in normalized_fields:
        if not field.key:
            if field.required:
                unresolved.append(field.label or "unnamed_required_field")
            continue
        if field.field_type in {"file", "resume"}:
            continue
        if (field.value or "").strip():
            continue
        if field.field_type == "radio":
            if field.key in processed_radio_keys:
                continue
            processed_radio_keys.add(field.key)
            value, source = _map_factual_field(field, profile)
            group = [item for item in normalized_fields if item.field_type == "radio" and item.key == field.key]
            selected = next(
                (item for item in group
                 if _normalize_label(item.label).split(" option ")[-1] == _normalize_label(value or "")),
                None,
            )
            if value and selected is not None:
                actions.append(WorkdayFieldAction("check", field.key, str(value), source))
            elif any(item.required for item in group):
                unresolved.append(field.label or field.key)
            continue
        if field.field_type == "checkbox":
            if field.required and not (field.value or "").strip():
                unresolved.append(field.label or field.key)
            continue
        value, source = _map_factual_field(field, profile)
        if value is None and answer_resolver is not None:
            approved = answer_resolver(field)
            if approved is not None and str(approved).strip():
                value, source = str(approved).strip(), "approved_answer"
        if value is not None and str(value).strip():
            action = "select" if field.options or field.field_type in {"select", "combobox"} else "fill"
            actions.append(WorkdayFieldAction(action, field.key, str(value), source))
        elif field.required and not (field.value or "").strip():
            unresolved.append(field.label or field.key)
    return WorkdayFieldPlan(tuple(actions), tuple(unresolved))


@dataclass(frozen=True)
class ResumeCorrection:
    action: str
    section: str
    index: int
    field: str
    value: object
    reason: str


@dataclass(frozen=True)
class ResumeCorrectionPlan:
    actions: tuple[ResumeCorrection, ...]

    @property
    def changed(self) -> bool:
        return bool(self.actions)


_WORK_FIELDS = ("company", "title", "start_date", "end_date", "currently_working")
_EDUCATION_FIELDS = ("school", "degree", "field_of_study", "graduation_date")


def _same_value(left, right) -> bool:
    if isinstance(right, bool):
        return bool(left) is right
    return _normalize_label(str(left or "")) == _normalize_label(str(right or ""))


def _record_identity(record: dict, section: str) -> tuple[str, ...]:
    fields = ("company", "title") if section == "work_history" else ("school", "degree")
    return tuple(_normalize_label(str(record.get(field) or "")) for field in fields)


def _match_record(parsed: list[dict], expected: dict, section: str, fallback_index: int) -> int | None:
    identity = _record_identity(expected, section)
    if any(identity):
        for index, record in enumerate(parsed):
            if _record_identity(record, section) == identity:
                return index
    return fallback_index if fallback_index < len(parsed) else None


def build_resume_correction_plan(*, parsed: dict, canonical: dict) -> ResumeCorrectionPlan:
    """Diff Workday resume parsing against canonical factual resume data.

    The plan only adds missing records or sets stable factual fields. It never deletes
    parsed records and never generates content.
    """
    actions: list[ResumeCorrection] = []
    for section, fields in (("work_history", _WORK_FIELDS), ("education", _EDUCATION_FIELDS)):
        parsed_records = list(parsed.get(section) or [])
        expected_records = list(canonical.get(section) or [])
        for expected_index, expected in enumerate(expected_records):
            parsed_index = _match_record(parsed_records, expected, section, expected_index)
            if parsed_index is None:
                actions.append(
                    ResumeCorrection("add_record", section, expected_index, "record", dict(expected), "missing_record")
                )
                continue
            actual = parsed_records[parsed_index]
            for field_name in fields:
                expected_value = expected.get(field_name)
                if expected_value in (None, ""):
                    continue
                if not _same_value(actual.get(field_name), expected_value):
                    actions.append(
                        ResumeCorrection(
                            "set",
                            section,
                            parsed_index,
                            field_name,
                            expected_value,
                            "resume_parse_mismatch",
                        )
                    )

    parsed_links = parsed.get("links") or {}
    for field_name in ("linkedin", "portfolio", "website"):
        expected_value = (canonical.get("links") or {}).get(field_name)
        if expected_value and not _same_value(parsed_links.get(field_name), expected_value):
            actions.append(
                ResumeCorrection("set", "links", 0, field_name, expected_value, "link_mismatch")
            )
    return ResumeCorrectionPlan(tuple(actions))


@dataclass(frozen=True)
class ControlResult:
    ok: bool
    attempts: int
    expected: str
    observed: str | None
    reason: str


class PlaywrightWorkdayDriver:
    """Minimal accessible-control wrapper over a Playwright page."""

    def __init__(self, page) -> None:
        self.page = page

    def _control(self, field: WorkdayField):
        if field.key:
            by_id = self.page.locator(f'[data-automation-id="{field.key}"]')
            if by_id.count():
                return by_id.first
            by_dom_key = self.page.locator(
                f'[id="{field.key}"], [name="{field.key}"]'
            )
            if by_dom_key.count():
                return by_dom_key.first
        return self.page.get_by_label(field.label, exact=True)

    def select(self, field: WorkdayField, value: str) -> None:
        observed = _normalize_label(self.read_value(field) or "")
        expected = _normalize_label(value)
        if observed and (observed == expected or expected in observed or observed in expected):
            return
        control = self._control(field)
        if control.get_attribute("data-uxi-widget-type") == "selectinput":
            control.fill("")
            control.press_sequentially(value, delay=25)
            self.page.get_by_role("option", name=value, exact=True).click(timeout=5000)
            control.press("Tab")
            return
        control.click()
        self.page.get_by_role("option", name=value, exact=True).click(timeout=5000)

    def read_value(self, field: WorkdayField) -> str | None:
        control = self._control(field)
        if control.get_attribute("data-uxi-widget-type") == "selectinput":
            selected = control.evaluate("""el => {
              const root = el.closest('[data-automation-id="multiSelectContainer"]');
              if (!root) return '';
              const node = root.querySelector(
                '[data-automation-id="selectedItem"], [data-automation-id="promptSelectionLabel"]'
              );
              return node ? (node.innerText || node.textContent || '').trim() : '';
            }""")
            if selected:
                return str(selected)
        try:
            if control.evaluate("el => el.tagName") == "BUTTON":
                return control.inner_text().strip() or None
        except Exception:
            pass
        try:
            value = control.input_value()
            if value:
                return value
        except Exception:
            pass
        for attribute in ("data-automation-label", "aria-label", "value"):
            try:
                value = control.get_attribute(attribute)
                if value:
                    return value
            except Exception:
                continue
        try:
            return control.inner_text().strip() or None
        except Exception:
            return None


def select_controlled_option(
    driver,
    field: WorkdayField | dict,
    value: str,
    *,
    max_attempts: int = 2,
) -> ControlResult:
    """Select an exact option and require read-back equality after at most two tries."""
    field = WorkdayField.from_dict(field) if isinstance(field, dict) else field
    attempts = max(1, min(int(max_attempts), 2))
    observed = None
    expected_normalized = _normalize_label(value)
    for attempt in range(1, attempts + 1):
        try:
            driver.select(field, value)
            observed = driver.read_value(field)
        except Exception as exc:
            if attempt == attempts:
                return ControlResult(False, attempt, value, observed, f"control_error:{type(exc).__name__}")
            continue
        if _normalize_label(observed or "") == expected_normalized:
            return ControlResult(True, attempt, value, observed, "verified")
    return ControlResult(False, attempts, value, observed, "readback_mismatch")


@dataclass(frozen=True)
class ValidationIssue:
    key: str
    label: str
    message: str

    @classmethod
    def from_dict(cls, raw: dict) -> "ValidationIssue":
        return cls(
            key=str(raw.get("key") or raw.get("automation_id") or raw.get("name") or ""),
            label=str(raw.get("label") or ""),
            message=str(raw.get("message") or "invalid value"),
        )


@dataclass(frozen=True)
class ValidationDecision:
    action: str
    repairs: tuple[WorkdayFieldAction, ...]
    issues: tuple[ValidationIssue, ...]
    reason: str


_VALIDATION_JS = r"""() => {
  const issues = [];
  const seen = new Set();
  for (const error of document.querySelectorAll(
    '[data-automation-id="errorMessage"], [data-automation-id="inputAlert"]'
  )) {
    const container = error.closest('[data-automation-id^="formField-"], fieldset');
    const control = container && container.querySelector(
      'input, select, textarea, [role="combobox"], button[aria-haspopup="listbox"]'
    );
    if (!control) continue;
    const key = control.getAttribute('data-automation-id') || control.name || control.id || '';
    if (!key || seen.has(key)) continue;
    seen.add(key);
    const labelNode = container.querySelector('label, legend, [data-automation-id="formLabel"]');
    issues.push({
      key,
      label: labelNode ? (labelNode.innerText || '').trim() : '',
      message: (error.innerText || error.textContent || 'invalid value').trim(),
    });
  }
  return issues;
}"""


def collect_validation_issues(page) -> tuple[ValidationIssue, ...]:
    return tuple(ValidationIssue.from_dict(raw) for raw in (page.evaluate(_VALIDATION_JS) or []))


@dataclass
class ValidationGuard:
    attempted_keys: set[str] = field(default_factory=set)

    def decide(
        self,
        issues: Iterable[ValidationIssue | dict],
        field_plan: WorkdayFieldPlan,
    ) -> ValidationDecision:
        normalized_issues = tuple(
            ValidationIssue.from_dict(issue) if isinstance(issue, dict) else issue
            for issue in issues
        )
        if not normalized_issues:
            return ValidationDecision("clear", (), (), "no_validation_errors")

        action_by_key = {action.key: action for action in field_plan.actions}
        repairs: list[WorkdayFieldAction] = []
        for issue in normalized_issues:
            if not issue.key or issue.key not in action_by_key:
                return ValidationDecision("park", (), normalized_issues, "unmapped_validation_error")
            if issue.key in self.attempted_keys:
                return ValidationDecision("park", (), normalized_issues, "validation_repair_exhausted")
            repairs.append(action_by_key[issue.key])

        self.attempted_keys.update(issue.key for issue in normalized_issues)
        return ValidationDecision("repair", tuple(repairs), normalized_issues, "targeted_repair")


@dataclass(frozen=True)
class ConfirmationEvidence:
    kind: str
    value: str
    authoritative: bool = True


@dataclass(frozen=True)
class ConfirmationDecision:
    status: str
    confirmed: bool
    submit_clicked: bool
    evidence: tuple[ConfirmationEvidence, ...]
    reason: str


_CONFIRMATION_TEXT = (
    "application submitted",
    "thank you for applying",
    "application has been submitted",
    "we received your application",
)
_CONFIRMATION_URL_MARKERS = (
    "/application/submitted",
    "/application/thank-you",
    "/application/confirmation",
    "/candidate/home/submitted",
)
_INBOX_CONFIRMATION_STAGES = {
    "applied_confirmation",
    "acknowledged",
    "rejected",
    "screen",
    "assessment",
    "interview",
}


def evaluate_confirmation(
    *,
    final_url: str | None,
    page_text: str | None,
    submit_clicked: bool,
    inbox_events: Iterable[dict] = (),
    job_url: str | None = None,
) -> ConfirmationDecision:
    """Require positive URL, DOM, or attributed inbox evidence for ``applied``."""
    evidence: list[ConfirmationEvidence] = []
    normalized_url = (final_url or "").lower()
    normalized_text = _normalize_label(page_text or "")
    for marker in _CONFIRMATION_URL_MARKERS:
        if marker in normalized_url:
            evidence.append(ConfirmationEvidence("completion_url", marker))
            break
    for marker in _CONFIRMATION_TEXT:
        if _normalize_label(marker) in normalized_text:
            evidence.append(ConfirmationEvidence("dom_acknowledgement", marker))
            break
    for event in inbox_events or ():
        if event.get("stage") not in _INBOX_CONFIRMATION_STAGES:
            continue
        if event.get("match_status") not in (None, "attributed"):
            continue
        if job_url and event.get("job_url") != job_url:
            continue
        evidence.append(
            ConfirmationEvidence(
                "inbox_event",
                str(event.get("message_id") or event.get("stage") or "confirmation"),
            )
        )
        break

    if evidence:
        return ConfirmationDecision("applied", True, submit_clicked, tuple(evidence), "positive_confirmation")
    if submit_clicked:
        return ConfirmationDecision(
            "no_confirmation",
            False,
            True,
            (),
            "submit_clicked_without_confirmation",
        )
    return ConfirmationDecision("not_submitted", False, False, (), "submit_not_observed")


@dataclass
class WorkdayApplicationRun:
    machine: WorkdayStateMachine = field(default_factory=WorkdayStateMachine)
    submit_clicked: bool = False

    def observe(self, snapshot: WorkdaySnapshot | dict) -> Transition:
        return self.machine.observe(snapshot)

    def mark_submit_clicked(self) -> Transition:
        transition = self.machine.mark_submit_clicked()
        if transition.allowed:
            self.submit_clicked = True
        return transition

    def finish(
        self,
        *,
        final_url: str | None,
        page_text: str | None,
        inbox_events: Iterable[dict] = (),
        job_url: str | None = None,
    ) -> ConfirmationDecision:
        decision = evaluate_confirmation(
            final_url=final_url,
            page_text=page_text,
            submit_clicked=self.submit_clicked,
            inbox_events=inbox_events,
            job_url=job_url,
        )
        if decision.confirmed:
            self.observe({"text": "Application submitted"})
        return decision

    def metadata(self, confirmation: ConfirmationDecision | None = None) -> dict:
        result = self.machine.metadata()
        result["submit_clicked"] = self.submit_clicked
        if confirmation is not None:
            result["confirmation_status"] = confirmation.status
            result["confirmation_evidence"] = [
                {"kind": item.kind, "value": item.value, "authoritative": item.authoritative}
                for item in confirmation.evidence
            ]
        return result


@dataclass(frozen=True)
class WorkdayRunResult:
    status: str
    reason: str
    metadata: dict


class WorkdayAdapterRunner:
    """Bounded deterministic Workday workflow over an injectable page driver."""

    FORM_STATES = {
        WorkdayState.PERSONAL_INFORMATION,
        WorkdayState.EXPERIENCE,
        WorkdayState.QUESTIONS,
        WorkdayState.DISCLOSURES,
        WorkdayState.SELF_ID,
    }

    def __init__(self, driver, *, profile: dict, resume_path: str | None = None,
                 canonical_resume: dict | None = None, max_steps: int = 20,
                 budget=None, answer_resolver=None, exception_sink=None) -> None:
        from applypilot.apply.phase_budget import PhaseBudgetManager

        self.driver = driver
        self.profile = profile
        self.resume_path = resume_path
        self.canonical_resume = canonical_resume or {}
        self.max_steps = max(1, min(int(max_steps), 30))
        self.run = WorkdayApplicationRun()
        self.validation = ValidationGuard()
        self.budget = budget or PhaseBudgetManager()
        self.answer_resolver = answer_resolver
        self.exception_sink = exception_sink
        self.resume_corrections: ResumeCorrectionPlan | None = None

    def _metadata(self, **extra) -> dict:
        metadata = self.run.metadata()
        metadata["validation_repairs"] = sorted(self.validation.attempted_keys)
        metadata["resume_corrections"] = (
            len(self.resume_corrections.actions) if self.resume_corrections else 0
        )
        metadata["phase_budget"] = self.budget.metadata()
        metadata.update(extra)
        return metadata

    def _exception_result(self, fields, plan: WorkdayFieldPlan) -> WorkdayRunResult:
        unresolved_labels = set(plan.unresolved_required)
        unresolved_fields = tuple(
            field for field in fields
            if field.label in unresolved_labels or field.key in unresolved_labels
        )
        exception_ids = (
            list(self.exception_sink(unresolved_fields) or [])
            if self.exception_sink is not None else []
        )
        return WorkdayRunResult(
            "parked",
            "unmapped_required_fields",
            self._metadata(
                unresolved_required=list(plan.unresolved_required),
                exception_ids=exception_ids,
                exceptions=[{
                    "key": field.key,
                    "label": field.label,
                    "field_type": field.field_type,
                    "options": list(field.options),
                } for field in unresolved_fields],
            ),
        )

    def execute(self, *, submit: bool = False, inbox_events: Iterable[dict] = (),
                job_url: str | None = None) -> WorkdayRunResult:
        from applypilot.apply.phase_budget import PhaseBudgetExceeded

        try:
            return self._execute(submit=submit, inbox_events=inbox_events, job_url=job_url)
        except PhaseBudgetExceeded as exc:
            return WorkdayRunResult(
                "parked",
                f"budget_exhausted:{exc.phase}:{exc.dimension}",
                self._metadata(),
            )
        except Exception as exc:
            return WorkdayRunResult(
                "parked",
                f"driver_error:{type(exc).__name__}",
                self._metadata(driver_error=str(exc)[:200]),
            )

    def _execute(self, *, submit: bool = False, inbox_events: Iterable[dict] = (),
                 job_url: str | None = None) -> WorkdayRunResult:
        for _step in range(self.max_steps):
            with self.budget.track("form_fill"):
                snapshot = self.driver.snapshot()
            transition = self.run.observe(snapshot)
            if not transition.allowed:
                previous = transition.previous.value if transition.previous else "none"
                return WorkdayRunResult(
                    "parked",
                    f"invalid_state_transition:{previous}->{transition.current.value}",
                    self._metadata(
                        rejected_state=transition.current.value,
                        rejected_snapshot={
                            "url": snapshot.url,
                            "heading": snapshot.heading,
                            "automation_ids": list(snapshot.automation_ids)[-40:],
                            "buttons": list(snapshot.buttons)[-20:],
                            "text": snapshot.text[-800:],
                        },
                    ),
                )
            state = transition.current
            if state == WorkdayState.UNSUPPORTED:
                return WorkdayRunResult("parked", "unsupported_workday_state", self._metadata())
            if state == WorkdayState.LOGIN:
                return WorkdayRunResult("auth_required", "workday_login_required", self._metadata())
            if state == WorkdayState.CONFIRMATION:
                decision = self.run.finish(
                    final_url=self.driver.final_url(),
                    page_text=self.driver.page_text(),
                    inbox_events=inbox_events,
                    job_url=job_url,
                )
                return WorkdayRunResult(decision.status, decision.reason, self.run.metadata(decision))
            if state == WorkdayState.RESUME:
                if self.resume_path:
                    with self.budget.track("form_fill"):
                        uploaded = self.driver.upload_resume(self.resume_path)
                    if uploaded is False:
                        with self.budget.track("form_fill"):
                            self.driver.next()
                        continue
                with self.budget.track("form_fill"):
                    parsed = self.driver.parsed_resume() or {}
                self.resume_corrections = build_resume_correction_plan(
                    parsed=parsed,
                    canonical=self.canonical_resume,
                )
                for action in self.resume_corrections.actions:
                    with self.budget.track("form_fill"):
                        self.driver.apply_resume_correction(action)
                with self.budget.track("form_fill"):
                    self.driver.next()
                continue
            if state in self.FORM_STATES:
                ensure_attachment = getattr(self.driver, "ensure_resume_attachment", None)
                if state == WorkdayState.EXPERIENCE and self.resume_path and ensure_attachment:
                    with self.budget.track("form_fill"):
                        ensure_attachment(self.resume_path)
                with self.budget.track("form_fill"):
                    discovered_fields = tuple(
                        WorkdayField.from_dict(field) if isinstance(field, dict) else field
                        for field in self.driver.fields()
                    )
                field_plan = build_field_plan(
                    discovered_fields,
                    profile=self.profile,
                    answer_resolver=self.answer_resolver,
                )
                if not field_plan.ready:
                    return self._exception_result(discovered_fields, field_plan)
                for action in field_plan.actions:
                    with self.budget.track("form_fill"):
                        self.driver.apply_field_action(action)
                with self.budget.track("form_fill"):
                    self.driver.next()
                with self.budget.track("recovery"):
                    issues = self.driver.validation_issues()
                decision = self.validation.decide(issues, field_plan)
                if decision.action == "repair":
                    for action in decision.repairs:
                        with self.budget.track("recovery"):
                            self.driver.apply_field_action(action)
                    with self.budget.track("recovery"):
                        remaining_issues = self.driver.validation_issues()
                    remaining = self.validation.decide(remaining_issues, field_plan)
                    if remaining.action != "clear":
                        return WorkdayRunResult("parked", remaining.reason, self._metadata())
                elif decision.action == "park":
                    if decision.reason == "unmapped_validation_error":
                        with self.budget.track("recovery"):
                            validated_fields = tuple(
                                WorkdayField.from_dict(field) if isinstance(field, dict) else field
                                for field in self.driver.fields()
                            )
                        validated_plan = build_field_plan(
                            validated_fields,
                            profile=self.profile,
                            answer_resolver=self.answer_resolver,
                        )
                        if not validated_plan.ready:
                            return self._exception_result(validated_fields, validated_plan)
                        return WorkdayRunResult(
                            "parked",
                            decision.reason,
                            self._metadata(
                                validation_issues=[{
                                    "key": issue.key,
                                    "label": issue.label,
                                    "message": issue.message[:300],
                                } for issue in decision.issues],
                                current_fields=[{
                                    "key": field.key,
                                    "label": field.label,
                                    "field_type": field.field_type,
                                    "required": field.required,
                                    "has_value": bool((field.value or "").strip()),
                                } for field in validated_fields],
                            ),
                        )
                    return WorkdayRunResult("parked", decision.reason, self._metadata())
                continue
            if state == WorkdayState.REVIEW:
                if not submit:
                    return WorkdayRunResult("dry_run", "review_ready", self._metadata())
                with self.budget.track("form_fill"):
                    self.driver.submit()
                if not self.run.mark_submit_clicked().allowed:
                    return WorkdayRunResult("parked", "invalid_submit_transition", self._metadata())
                with self.budget.track("confirmation"):
                    self.driver.wait_after_submit()
                    final_url = self.driver.final_url()
                    page_text = self.driver.page_text()
                decision = self.run.finish(
                    final_url=final_url,
                    page_text=page_text,
                    inbox_events=inbox_events,
                    job_url=job_url,
                )
                return WorkdayRunResult(decision.status, decision.reason, self.run.metadata(decision))
        try:
            current_fields = self.driver.fields()
            current_plan = build_field_plan(current_fields, profile=self.profile)
            field_diagnostic = [
                {
                    "key": item.key,
                    "label": item.label,
                    "field_type": item.field_type,
                    "required": item.required,
                    "has_value": bool((item.value or "").strip()),
                    "option_count": len(item.options),
                }
                for item in current_fields
            ]
            extra = {
                "unresolved_required": list(current_plan.unresolved_required),
                "current_fields": field_diagnostic,
            }
        except Exception:
            extra = {}
        return WorkdayRunResult("parked", "workday_step_limit", self._metadata(**extra))


_SNAPSHOT_JS = r"""() => ({
  url: location.href,
  heading: ((document.querySelector('h1, h2, [role="heading"]') || {}).innerText || '').trim(),
  text: (document.body.innerText || '').slice(0, 12000),
  automation_ids: Array.from(document.querySelectorAll('[data-automation-id]'))
    .map(el => el.getAttribute('data-automation-id')).filter(Boolean),
  buttons: Array.from(document.querySelectorAll('button, [role="button"]'))
    .map(el => (el.innerText || el.getAttribute('aria-label') || '').trim()).filter(Boolean),
})"""

_FIELDS_JS = r"""() => {
  const fields = [];
  for (const el of document.querySelectorAll(
    'input, textarea, select, [role="combobox"], button[aria-haspopup="listbox"]'
  )) {
    if (el.type === 'hidden' || el.disabled) continue;
    const key = el.getAttribute('data-automation-id') || el.name || el.id || '';
    if (!key) continue;
    const container = el.closest('fieldset, [data-automation-id^="formField-"]')
      || el.closest('div') || el.parentElement;
    const labelNode = (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`))
      || (container && container.querySelector('label, legend, [data-automation-id="formLabel"]'));
    const options = el.tagName === 'SELECT'
      ? Array.from(el.options).map(o => (o.text || '').trim()).filter(Boolean)
      : [];
    const promptValue = container && Array.from(container.querySelectorAll(
      '[data-automation-id="promptSelectionLabel"], [data-automation-id="promptAriaInstruction"]'
    )).map(node => (node.innerText || node.textContent || '').trim()).find(Boolean);
    const isSelectInput = el.getAttribute('data-uxi-widget-type') === 'selectinput';
    const optionLabel = el.type === 'radio' && el.id
      ? ((document.querySelector(`label[for="${CSS.escape(el.id)}"]`) || {}).innerText || '').trim()
      : '';
    const questionLabel = container && ((container.querySelector(
      '[data-automation-id="formLabel"], legend, [id$="-label"]'
    ) || {}).innerText || '').trim();
    fields.push({
      key,
      label: el.type === 'radio' && questionLabel
        ? `${questionLabel} option ${optionLabel}`
        : (labelNode ? (labelNode.innerText || '').trim() : (el.getAttribute('aria-label') || '')),
      field_type: el.tagName === 'SELECT' || el.getAttribute('role') === 'combobox'
        || el.getAttribute('aria-haspopup') === 'listbox' || isSelectInput
        ? 'combobox' : (el.type || el.tagName.toLowerCase()),
      required: el.required || el.getAttribute('aria-required') === 'true'
        || Boolean(container && container.querySelector('[data-automation-id="inputAlert"]')),
      options,
      value: el.tagName === 'BUTTON'
        ? ((el.innerText || '').trim() === 'Select One' ? '' : (el.innerText || '').trim())
        : (['checkbox', 'radio'].includes(el.type) ? (el.checked ? el.value : '') : el.value)
        || el.getAttribute('data-automation-label')
        || promptValue || '',
    });
  }
  return fields;
}"""


class PlaywrightWorkdayPageDriver:
    """Concrete deterministic page operations for :class:`WorkdayAdapterRunner`."""

    def __init__(self, page) -> None:
        self.page = page
        self.controls = PlaywrightWorkdayDriver(page)

    def snapshot(self) -> WorkdaySnapshot:
        return WorkdaySnapshot.from_dict(self.page.evaluate(_SNAPSHOT_JS))

    def fields(self) -> tuple[WorkdayField, ...]:
        return tuple(WorkdayField.from_dict(raw) for raw in (self.page.evaluate(_FIELDS_JS) or []))

    def _field(self, key: str) -> WorkdayField:
        for field_item in self.fields():
            if field_item.key == key:
                return field_item
        return WorkdayField(key=key, label=key)

    def apply_field_action(self, action: WorkdayFieldAction) -> None:
        field_item = self._field(action.key)
        current = _normalize_label(field_item.value or "")
        expected = _normalize_label(action.value)
        if current and (current == expected or expected in current or current in expected):
            return
        if action.action == "check":
            boolean_value = "true" if _normalize_label(action.value) == "yes" else "false"
            control = self.page.locator(
                f'input[name="{action.key}"][value="{boolean_value}"]'
            ).first
            if control.count() == 0:
                control = self.page.get_by_label(action.value, exact=True).first
            control.check()
            if not control.is_checked():
                raise RuntimeError(f"workday_check_readback_mismatch:{action.key}")
            return
        if action.action == "select":
            result = select_controlled_option(self.controls, field_item, action.value)
            if not result.ok:
                expected_norm = _normalize_label(result.expected or "")
                observed_norm = _normalize_label(result.observed or "")
                comparison = {
                    "expected_length": len(expected_norm),
                    "observed_length": len(observed_norm),
                    "expected_in_observed": bool(expected_norm and expected_norm in observed_norm),
                    "observed_in_expected": bool(observed_norm and observed_norm in expected_norm),
                }
                raise RuntimeError(
                    f"workday_select_failed:{action.key}:{result.reason}:{comparison}"
                )
            return
        observed = None
        control = None
        for attempt in range(2):
            control = self.controls._control(field_item)
            control.fill("")
            control.press_sequentially(action.value, delay=25 * (attempt + 1))
            control.press("Tab")
            observed = self.controls.read_value(field_item)
            equivalent = _normalize_label(observed or "") == _normalize_label(action.value)
            if "phone" in action.key.lower() and "countryphonecode" not in action.key.lower():
                expected_digits = re.sub(r"\D", "", action.value)[-10:]
                observed_digits = re.sub(r"\D", "", observed or "")[-10:]
                equivalent = bool(expected_digits) and expected_digits == observed_digits
            if equivalent:
                return
        identity = {
            "tag": control.evaluate("el => el.tagName") if control is not None else None,
            "id": control.get_attribute("id") if control is not None else None,
            "name": control.get_attribute("name") if control is not None else None,
            "expected_length": len(action.value),
            "observed_length": len(observed or ""),
        }
        raise RuntimeError(f"workday_fill_readback_mismatch:{action.key}:{identity}")

    def upload_resume(self, path: str) -> bool:
        self.page.wait_for_function("""() => Boolean(
          document.querySelector('[data-automation-id="file-upload-input-ref"], input[type="file"], '
            + '[data-automation-id="contactInformationPage"], '
            + '[data-automation-id="personalInformationPage"], '
            + '[data-automation-id="workExperienceSection"], '
            + '[data-automation-id="applicationQuestionsPage"], '
            + '[data-automation-id="pageFooterNextButton"]')
        )""", timeout=15000)
        control = self.page.locator(
            '[data-automation-id="file-upload-input-ref"], input[type="file"]'
        ).first
        if control.count() == 0:
            return False
        control.set_input_files(path)
        return True

    def parsed_resume(self) -> dict:
        return {"work_history": [], "education": [], "links": {}}

    def ensure_resume_attachment(self, path: str) -> bool:
        container = self.page.locator('[data-automation-id="attachments-FileUpload"]')
        if container.count() == 0:
            return False
        control = container.first.locator('input[type="file"]').first
        if control.count() == 0:
            control = self.page.locator('[data-automation-id="file-upload-input-ref"]').first
        if control.count() == 0:
            raise RuntimeError("workday_attachment_input_missing")
        control.set_input_files(path)
        return True

    def apply_resume_correction(self, action: ResumeCorrection) -> None:
        if action.action == "add_record":
            label = "Add Work Experience" if action.section == "work_history" else "Add Education"
            self.page.get_by_role("button", name=re.compile(label, re.I)).click()
            return
        field_marker = re.sub(r"_([a-z])", lambda match: match.group(1).upper(), action.field)
        candidates = self.page.locator(
            f'[data-automation-id*="{field_marker}" i], [name*="{field_marker}" i]'
        )
        if candidates.count() <= action.index:
            raise RuntimeError(f"workday_resume_field_missing:{action.section}:{action.field}")
        control = candidates.nth(action.index)
        if isinstance(action.value, bool):
            if control.is_checked() != action.value:
                control.click()
        else:
            control.fill(str(action.value))

    def next(self) -> None:
        before = self.page.evaluate("""() => ({
          url: location.href,
          step: ((document.querySelector('[data-automation-id="progressBarActiveStep"]') || {}).innerText || '').trim(),
          errors: Array.from(document.querySelectorAll(
            '[data-automation-id="errorMessage"], [data-automation-id="inputAlert"], [role="alert"]'
          )).map(el => (el.innerText || '').trim()).filter(Boolean).join('|')
        })""")
        self.page.get_by_role("button", name=re.compile(r"^(save and continue|next|continue)$", re.I)).click()
        try:
            self.page.wait_for_function("""before => {
              const step = ((document.querySelector('[data-automation-id="progressBarActiveStep"]') || {}).innerText || '').trim();
              const errors = Array.from(document.querySelectorAll(
                '[data-automation-id="errorMessage"], [data-automation-id="inputAlert"], [role="alert"]'
              )).map(el => (el.innerText || '').trim()).filter(Boolean).join('|');
              return location.href !== before.url || step !== before.step || errors !== before.errors;
            }""", arg=before, timeout=20000)
        except Exception as exc:
            diagnostic = self.page.evaluate("""() => ({
              step: ((document.querySelector('[data-automation-id="progressBarActiveStep"]') || {}).innerText || '').trim(),
              ids: Array.from(document.querySelectorAll('[data-automation-id]'))
                .map(el => el.getAttribute('data-automation-id')).filter(Boolean).slice(-30),
              errors: Array.from(document.querySelectorAll(
                '[data-automation-id="errorMessage"], [data-automation-id="inputAlert"], [role="alert"]'
              ))
                .map(el => (el.innerText || '').trim()).filter(Boolean).slice(0, 5)
            })""")
            raise RuntimeError(f"workday_next_timeout:{diagnostic}") from exc

    def validation_issues(self) -> tuple[ValidationIssue, ...]:
        return collect_validation_issues(self.page)

    def submit(self) -> None:
        self.page.get_by_role("button", name=re.compile(r"^submit( application)?$", re.I)).click()

    def wait_after_submit(self) -> None:
        self.page.wait_for_timeout(1500)

    def final_url(self) -> str:
        return self.page.url

    def page_text(self) -> str:
        return self.page.locator("body").inner_text()
