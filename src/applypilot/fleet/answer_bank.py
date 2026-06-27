"""Screening-question answer bank (R10 / spec 9.2).

Job-application forms gate submission behind screening questions ("Are you
authorized to work in the US?", "How many years of Python?", "Desired salary?").
A worker MUST answer them to submit -- but it must NEVER fabricate an answer
under Jonathan's name. This bank is the owner-vetted source of truth:

  * ``get_answer`` returns an owner-approved answer for a KNOWN question, else
    records the question as ``unknown_deferred`` and returns ``None`` so the
    worker DEFERS the application (fail-safe -- never guess).
  * ``set_answer`` / ``seed_known`` are how the owner populates known answers.
  * ``list_unknown`` is the owner's defer queue: questions the fleet hit that
    nobody has answered yet.

Questions are keyed by a NORMALIZED form (lowercase, punctuation stripped,
whitespace collapsed) so trivially different phrasings of the same question map
to one row -- "Are you authorized to work in the US?" and
"are you authorized to work in the us" share a key.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)  # drop punctuation, keep word chars + space
_WS = re.compile(r"\s+")


def normalize_question(q: str | None) -> str:
    """Canonical key for a screening question: lowercase, strip punctuation,
    collapse whitespace. ``None``/blank -> ``""``."""
    if not q:
        return ""
    s = q.strip().lower()
    s = _PUNCT.sub(" ", s)        # underscores survive (\w), other punctuation -> space
    return _WS.sub(" ", s).strip()


def get_answer(conn, raw_question: str) -> str | None:
    """Look up the answer for ``raw_question`` by its normalized key.

    KNOWN (``status='known'``) -> return its answer. Otherwise FAIL-SAFE: record
    the question as ``unknown_deferred`` (insert keyed by ``q_norm`` if absent,
    preserving any existing row) and return ``None`` so the worker defers. NEVER
    guesses.
    """
    q_norm = normalize_question(raw_question)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT answer, status FROM answer_bank WHERE q_norm = %s", (q_norm,)
        )
        row = cur.fetchone()
        if row is not None and row["status"] == "known":
            return row["answer"]
        # Unknown (absent OR a still-deferred row): record/keep the defer entry.
        # ON CONFLICT DO NOTHING preserves an existing row (incl. a 'known' one we
        # only failed to match because answer was NULL -- never downgrade it).
        cur.execute(
            "INSERT INTO answer_bank (q_norm, q_raw, status) "
            "VALUES (%s, %s, 'unknown_deferred') ON CONFLICT (q_norm) DO NOTHING",
            (q_norm, raw_question),
        )
    conn.commit()
    return None


def set_answer(conn, raw_question: str, answer: str, *, kind: str | None = None) -> None:
    """UPSERT an owner-vetted answer and mark the question ``known``."""
    q_norm = normalize_question(raw_question)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO answer_bank (q_norm, q_raw, answer, kind, status, updated_at) "
            "VALUES (%s, %s, %s, %s, 'known', now()) "
            "ON CONFLICT (q_norm) DO UPDATE SET "
            "  q_raw = EXCLUDED.q_raw, "
            "  answer = EXCLUDED.answer, "
            "  kind = COALESCE(EXCLUDED.kind, answer_bank.kind), "
            "  status = 'known', "
            "  updated_at = now()",
            (q_norm, raw_question, answer, kind),
        )
    conn.commit()


def seed_known(conn, pairs: Mapping[str, str]) -> int:
    """Bulk-load known answers from ``{raw_question: answer}``. Returns the count
    written. UPSERT semantics mirror :func:`set_answer`."""
    n = 0
    with conn.cursor() as cur:
        for raw_question, answer in pairs.items():
            cur.execute(
                "INSERT INTO answer_bank (q_norm, q_raw, answer, status, updated_at) "
                "VALUES (%s, %s, %s, 'known', now()) "
                "ON CONFLICT (q_norm) DO UPDATE SET "
                "  q_raw = EXCLUDED.q_raw, "
                "  answer = EXCLUDED.answer, "
                "  status = 'known', "
                "  updated_at = now()",
                (normalize_question(raw_question), raw_question, answer),
            )
            n += 1
    conn.commit()
    return n


def list_unknown(conn) -> list[dict[str, Any]]:
    """The owner's defer queue: questions recorded ``unknown_deferred`` (no vetted
    answer yet), oldest first."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT q_norm, q_raw, kind, status, updated_at FROM answer_bank "
            "WHERE status = 'unknown_deferred' ORDER BY updated_at ASC, q_norm ASC"
        )
        return [dict(r) for r in cur.fetchall()]
