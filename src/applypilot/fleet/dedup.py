"""Posting-level dedup: collapse the same company+role across boards (R9).

The same role often appears on LinkedIn AND Greenhouse AND the company site;
URL-level dedup still sends three applications to one employer (reads as spam).
So we key applications by ``dedup_key = sha(normalized_company | normalized_role)``
and apply ONCE per company+role. The key also ties inbox responses (R8) back to
the application across boards.
"""
from __future__ import annotations

import hashlib
import re

_PARENS = re.compile(r"[(\[{].*?[)\]}]")  # (Remote), [Req 123], {US}
_REQID = re.compile(r"\b(?:req|requisition|job)\s*#?\s*\w*\d\w*\b", re.I)
_SENIORITY = re.compile(
    r"\b(?:jr|junior|sr|senior|staff|principal|lead|entry[- ]?level|mid[- ]?level)\b", re.I
)
_LEVEL = re.compile(r"\b(?:i{1,3}|iv|vi?|l\d|level\s*\d|[1-5])\b", re.I)  # II, III, IV, L4, level 3, 2
_LEGAL = re.compile(
    r"\b(?:inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|plc|gmbh|s\.a)\b\.?", re.I
)
_NONWORD = re.compile(r"[^a-z0-9]+")
_WS = re.compile(r"\s+")

# Small role-family canonicalization; extend with the existing role-family map.
_ROLE_SYNONYMS = {
    "chief of staff": "chief of staff",
    "cos": "chief of staff",
    "bizops": "business operations",
    "biz ops": "business operations",
    "business operations": "business operations",
    "strategy and operations": "strategy operations",
    "strategy operations": "strategy operations",
    "quant dev": "quantitative developer",
    "quantitative developer": "quantitative developer",
}


def normalize_company(company: str | None) -> str:
    if not company:
        return ""
    c = company.strip().lower()
    c = _PARENS.sub(" ", c)
    c = _LEGAL.sub(" ", c)
    c = _NONWORD.sub(" ", c)
    return _WS.sub(" ", c).strip()


def normalize_role(title: str | None) -> str:
    if not title:
        return ""
    t = title.strip().lower()
    t = _PARENS.sub(" ", t)
    t = _REQID.sub(" ", t)
    t = _NONWORD.sub(" ", t)
    t = _WS.sub(" ", t).strip()
    # Role-family canonicalization FIRST -- before stripping seniority/level -- so
    # "Chief of Staff" isn't mangled by the 'staff' seniority token (staff is a
    # seniority in "Staff Engineer" but part of the title in "Chief of Staff").
    for k in sorted(_ROLE_SYNONYMS, key=len, reverse=True):
        if k in t:
            return _ROLE_SYNONYMS[k]
    # Otherwise strip seniority + level decorations so "Engineer II" == "Engineer".
    t = _SENIORITY.sub(" ", t)
    t = _LEVEL.sub(" ", t)
    return _WS.sub(" ", t).strip()


def dedup_key(company: str | None, title: str | None) -> str:
    """Board-agnostic key for (company, role). Stable, 20 hex chars."""
    raw = f"{normalize_company(company)}\x1f{normalize_role(title)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
