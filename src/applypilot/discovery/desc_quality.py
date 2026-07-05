from __future__ import annotations

import re
from collections.abc import Iterable

_EMPTY_RE = re.compile(r"^\s*$")
_REQ_MARKER_RE = re.compile(
    r"(requirements|qualifications|responsibilities|what you\.?ll|who you are|must have|skills)",
    re.IGNORECASE,
)
_JUNK_RE = re.compile(r"(cookie|privacy|enable[- ]javascript|sign(?:-|\s)?in)", re.IGNORECASE)
_TITLE_HTML_TAG_RE = re.compile(r"<[^>]+>")

_TAG_PENALTIES: dict[str, int] = {
    "empty": 100,
    "stub_lt200": 35,
    "short_lt500": 20,
    "no_requirements_marker": 8,
    "html_residue": 20,
    "junk_boilerplate": 12,
    "board_summary_stub": 25,
    "title_echo": 10,
}


def _normalize_text(value: str | None) -> str:
    """Normalize plain text for cheap exact-match checks."""
    if not value:
        return ""
    cleaned = re.sub(r"[^a-z0-9]+", " ", str(value).lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _is_empty(value: str | None) -> bool:
    return bool(_EMPTY_RE.match(value or ""))


def assess_description(title: str | None, full_description: str | None) -> tuple[list[str], int]:
    """Return (flags, score) for a job description quality audit.

    Flags are deterministic and orthogonal. Score starts at 100 and subtracts a fixed
    per-flag penalty, then floors at zero.
    """
    title_norm = _normalize_text(title or "")
    desc_norm = (full_description or "").strip()
    desc_text = (full_description or "").strip()

    flags: list[str] = []
    if _is_empty(desc_text):
        flags.append("empty")
    if len(desc_text) < 200:
        flags.append("stub_lt200")
    if len(desc_text) < 500:
        flags.append("short_lt500")

    if full_description and not _REQ_MARKER_RE.search(full_description):
        flags.append("no_requirements_marker")

    tag_count = len(_TITLE_HTML_TAG_RE.findall(full_description or ""))
    text_len = max(1, len(desc_text))
    if (tag_count / text_len) * 1000 > 5:
        flags.append("html_residue")

    if _JUNK_RE.search(full_description or ""):
        flags.append("junk_boilerplate")

    if _is_empty(desc_text):
        desc_starts_with_requirements = False
    else:
        desc_starts_with_requirements = desc_text.startswith("Requirements Summary:")
    if desc_starts_with_requirements and len(desc_text) < 1500:
        flags.append("board_summary_stub")

    if title_norm and title_norm == _normalize_text(desc_text):
        flags.append("title_echo")

    score = max(0, 100 - sum(_TAG_PENALTIES[f] for f in flags if f in _TAG_PENALTIES))
    # preserve stable order for snapshots and tests
    return flags, score


def iter_flags(row: dict | None) -> Iterable[str]:
    """Helper kept for tests and callers that want list-style parsing."""
    if not row:
        return []
    flags = row.get("desc_quality_flags") or ""
    if not flags:
        return []
    return [f for f in str(flags).split(",") if f]
