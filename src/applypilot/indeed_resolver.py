"""Indeed company-site apply URL resolver.

This module classifies Indeed job pages deterministically. It does not submit
applications and does not use LLM/OCR.
"""

from __future__ import annotations

from dataclasses import dataclass

from applypilot.aggregator_resolver import (
    is_external_apply_url,
    next_action_for_unresolved_kind,
    source_platform_from_url,
)


CHECKPOINT_TEXTS = (
    "captcha",
    "verify you are human",
    "verify that you are human",
    "security check",
    "unusual activity",
)

UNAVAILABLE_TEXTS = (
    "job is no longer available",
    "this job is no longer available",
    "job has expired",
    "this job has expired",
    "position has been filled",
)


@dataclass(frozen=True)
class ApplyControl:
    text: str
    href: str | None
    selector: str
    aria_label: str | None = None


@dataclass(frozen=True)
class PageSnapshot:
    url: str
    body_text: str
    controls: tuple[ApplyControl, ...]


@dataclass(frozen=True)
class PageDecision:
    status: str
    final_url: str | None = None
    control: ApplyControl | None = None
    error: str | None = None
    unresolved_kind: str | None = None
    next_action: str | None = None


def unresolved_decision(
    kind: str,
    *,
    error: str | None = None,
    control: ApplyControl | None = None,
) -> PageDecision:
    return PageDecision(
        status="unresolved",
        error=error,
        control=control,
        unresolved_kind=kind,
        next_action=next_action_for_unresolved_kind(kind),
    )


def _control_text(control: ApplyControl) -> str:
    return f"{control.text or ''} {control.aria_label or ''}".lower()


def _snapshot_text(snapshot: PageSnapshot) -> str:
    return (snapshot.body_text or "").lower()


def classify_snapshot(snapshot: PageSnapshot) -> PageDecision:
    text = _snapshot_text(snapshot)
    controls = tuple(snapshot.controls or ())

    if any(token in text for token in CHECKPOINT_TEXTS):
        return unresolved_decision("checkpoint_or_captcha", error="indeed_checkpoint")

    if any(token in text for token in UNAVAILABLE_TEXTS):
        return PageDecision(status="unavailable", error="indeed_unavailable")

    for control in controls:
        label = _control_text(control)
        if is_external_apply_url(control.href):
            return PageDecision(
                status="resolved_offsite",
                final_url=control.href,
                control=control,
            )
        if "apply on company site" in label:
            return PageDecision(status="needs_click", control=control)
        if "apply now" in label or "easily apply" in label:
            return PageDecision(status="hosted_apply", control=control)

    if "apply on company site" in text:
        return PageDecision(status="needs_click")

    return unresolved_decision(
        "apply_button_missing",
        error="no_primary_apply_button",
    )


def _locator_for_control(page, control: ApplyControl):
    if control.selector:
        try:
            locator = page.locator(control.selector)
            if locator.count() == 1:
                return locator.first
        except Exception:
            pass
    control_text = (control.text or "Apply").strip() or "Apply"
    return page.get_by_text(control_text, exact=False).first


def _click_and_capture_external(
    page,
    control: ApplyControl,
    timeout_ms: int = 5000,
) -> PageDecision:
    try:
        locator = _locator_for_control(page, control)
        with page.expect_popup(timeout=timeout_ms) as popup_info:
            locator.click(timeout=timeout_ms)
        popup = popup_info.value
        try:
            popup.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            if is_external_apply_url(popup.url):
                return PageDecision(
                    status="resolved_offsite",
                    final_url=popup.url,
                    control=control,
                )
            if source_platform_from_url(popup.url) is not None:
                return unresolved_decision(
                    "outbound_still_source_platform",
                    error=popup.url,
                    control=control,
                )
            return unresolved_decision(
                "malformed_outbound_url",
                error=popup.url,
                control=control,
            )
        finally:
            try:
                popup.close()
            except Exception:
                pass
    except Exception as exc:
        return unresolved_decision(
            "outbound_not_observed",
            error=str(exc)[:200],
            control=control,
        )
