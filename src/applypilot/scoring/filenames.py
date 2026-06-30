"""Filename helpers for generated application artifacts."""

from __future__ import annotations

import hashlib
import re
from typing import Any

_WINDOWS_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _safe_filename_component(value: Any, *, max_length: int, fallback: str) -> str:
    cleaned = _WINDOWS_INVALID_FILENAME_CHARS.sub(" ", str(value or ""))
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._- ")
    cleaned = cleaned[:max_length].rstrip("._- ")
    return cleaned or fallback


def safe_job_prefix(job: dict[str, Any]) -> str:
    """Readable, stable filename prefix that is safe on Windows."""
    safe_title = _safe_filename_component(job.get("title"), max_length=50, fallback="job")
    safe_site = _safe_filename_component(job.get("site"), max_length=20, fallback="site")
    fingerprint_source = job.get("url") or "|".join(
        str(job.get(key, "")) for key in ("site", "title", "location")
    )
    fingerprint = hashlib.sha1(fingerprint_source.encode("utf-8")).hexdigest()[:8]
    return f"{safe_site}_{safe_title}_{fingerprint}"
