"""Deterministic Lever apply adapter (DOM-discovery read + map).

Unlike Greenhouse, Lever exposes no structured questions API -- custom questions
render as ``cards[uuid][fieldN]`` inputs whose meaning lives only in the DOM
label. So fields are DISCOVERED from the live page, then mapped: standard fields
(name/email/phone/org/location/urls[...]/resume) come from the profile, free-text
goes to the verifier-gated answerer, work-auth / demographic selects map or
decline, and any required field we can't confidently fill lands in
``unmapped_required`` (never faked).

Lever's submit is hCaptcha-gated, so this module only computes WHAT to submit;
the existing apply agent still owns the actual submission (captcha + submit).
The shared select/demographic/name primitives are reused from greenhouse_adapter.
"""

from __future__ import annotations

import re
import urllib.parse

from applypilot.apply.greenhouse_adapter import (
    AnswerPlan,
    _DECLINE,
    _DEMOGRAPHIC,
    _SPONSOR,
    _WORK_AUTH,
    _has,
    _pick_value,
)

# Lever plumbing that must never be treated as an answerable question.
_SYSTEM_FIELDS = {
    "accountId", "linkedInData", "origin", "referer", "timezone",
    "socialReferralKey", "socialSource", "resumeStorageId", "h-captcha-response",
    "source", "selectedLocation",
}
_SYSTEM_SUFFIXES = ("[baseTemplate]", "[surveyId]", "[candidateSelectedLocation]")

_URLS_RE = re.compile(r"urls\[(\w+)\]", re.I)
_URL_ROLE = {
    "linkedin": "linkedin_url", "github": "github_url", "portfolio": "portfolio_url",
    "website": "website_url", "other": "website_url", "twitter": "twitter_url",
}

# Enumerates visible form fields + their DOM label + select options. Runs in the
# browser via page.evaluate; unit tests inject the result instead.
_DISCOVERY_JS = r"""() => {
  const out = [];
  for (const el of document.querySelectorAll('input, textarea, select')) {
    const name = el.getAttribute('name');
    if (!name || el.type === 'hidden') continue;
    let label = '';
    const card = el.closest('.application-question, .form-field, li');
    if (card) {
      const t = card.querySelector('.application-label, .text, label');
      if (t) label = (t.innerText || '').trim();
    }
    let options = [];
    if (el.tagName === 'SELECT') {
      for (const o of el.options) {
        if (o.value) options.push({ label: (o.text || '').trim(), value: o.value });
      }
    }
    out.push({
      name,
      type: el.tagName === 'SELECT' ? 'select' : (el.type || el.tagName.toLowerCase()),
      label,
      required: el.required === true,
      options,
    });
  }
  return out;
}"""


def parse_lever_url(url: str) -> tuple[str, str] | None:
    """Return (site, posting_id) for a Lever job/apply URL, else None."""
    try:
        p = urllib.parse.urlparse(url)
    except (ValueError, TypeError):
        return None
    if (p.hostname or "").lower() != "jobs.lever.co":
        return None
    parts = [x for x in p.path.split("/") if x and x != "apply"]
    if len(parts) < 2:
        return None
    return (parts[0], parts[1])


def _is_system(name: str) -> bool:
    return name in _SYSTEM_FIELDS or name.endswith(_SYSTEM_SUFFIXES)


def normalize_fields(raw) -> list[dict]:
    """Drop Lever's hidden/system plumbing, keep answerable fields."""
    out: list[dict] = []
    for f in raw or []:
        name = f.get("name", "")
        if not name or _is_system(name) or f.get("type") == "hidden":
            continue
        out.append({
            "name": name,
            "type": f.get("type", ""),
            "label": f.get("label", ""),
            "required": bool(f.get("required")),
            "options": f.get("options") or [],
        })
    return out


def discover_fields(page) -> list[dict]:
    """Enumerate the live apply form's answerable fields (via page.evaluate)."""
    return normalize_fields(page.evaluate(_DISCOVERY_JS))


def build_lever_plan(fields, *, profile, resume_text, corpus=None,
                     answer_fn=None, job=None) -> AnswerPlan:
    """Map discovered Lever fields into a deterministic AnswerPlan."""
    if answer_fn is None:
        from applypilot.apply.answerer import answer_question
        answer_fn = answer_question

    personal = (profile or {}).get("personal", {})
    work_auth = (profile or {}).get("work_authorization", {})
    exp = (profile or {}).get("experience", {})
    full = personal.get("full_name", "")

    out: dict = {}
    free_text: dict = {}
    resume_field: str | None = None
    unmapped: list = []

    for f in fields or []:
        name = f.get("name", "")
        ftype = f.get("type", "")
        required = bool(f.get("required"))
        label = (f.get("label") or "").strip()
        low = label.lower()
        options = f.get("options") or []
        url_key = _URLS_RE.match(name)
        mapped = False

        if name == "resume":
            resume_field = "resume"
            mapped = True
        elif name == "name" and full:
            out[name] = full
            mapped = True
        elif name == "email" and personal.get("email"):
            out[name] = personal["email"]
            mapped = True
        elif name == "phone" and personal.get("phone"):
            out[name] = personal["phone"]
            mapped = True
        elif name == "org" and exp.get("current_company"):
            out[name] = exp["current_company"]
            mapped = True
        elif name == "location":
            loc = ", ".join(
                x for x in (personal.get("city"), personal.get("province_state"),
                            personal.get("country")) if x
            )
            if loc:
                out[name] = loc
                mapped = True
        elif url_key:
            role = _URL_ROLE.get(url_key.group(1).lower())
            value = personal.get(role) if role else None
            if value:
                out[name] = value
                mapped = True
        elif ftype == "textarea":
            res = answer_fn(label or "Additional information", job=job or {}, profile=profile,
                            resume_text=resume_text, corpus=corpus, kind="open")
            if getattr(res, "verified", False):
                out[name] = res.text
                free_text[name] = res.text
                mapped = True
        elif options or ftype == "select":
            value = None
            if _has(low, _DEMOGRAPHIC):
                value = _pick_value(options, lambda label_l: _has(label_l, _DECLINE))
            elif _has(low, _SPONSOR):
                if str(work_auth.get("require_sponsorship", "")).strip().lower() in ("no", "false", "n"):
                    value = _pick_value(options, lambda label_l: label_l == "no")
            elif _has(low, _WORK_AUTH):
                if str(work_auth.get("legally_authorized_to_work", "")).strip().lower() in ("yes", "true", "y"):
                    value = _pick_value(options, lambda label_l: label_l == "yes")
            if value is not None:
                out[name] = value
                mapped = True

        if required and not mapped:
            unmapped.append(label or name)

    ready = bool(out.get("name") and out.get("email")) and not unmapped
    return AnswerPlan(fields=out, resume_field=resume_field, free_text=free_text,
                      unmapped_required=unmapped, ready=ready)
