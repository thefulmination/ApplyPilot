"""Resolve LinkedIn apply URLs by matching already-discovered company/ATS jobs.

This keeps LinkedIn browser automation as a fallback. The fast path is matching
an unresolved LinkedIn row to a known non-LinkedIn row with the same company,
similar title, compatible location, and an external application URL.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
import re
import sqlite3
from typing import Iterable, Sequence

from applypilot.database import get_connection
from applypilot.linkedin_resolver import is_external_apply_url, is_linkedin_url


RESOLVED_STATUS = "resolved_company_match"
ATS_RECONSTRUCTION_NEXT_ACTION = "run_ats_reconstruction"
ATS_RECONSTRUCTION_UNRESOLVED_KINDS = frozenset(
    {"ats_reconstruction_needed", "apply_button_missing"}
)


@dataclass(frozen=True)
class CompanyResolverOptions:
    limit: int = 200
    tiers: tuple[str, ...] = ("priority", "recommended")
    include_low: bool = False
    refresh: bool = False
    dry_run: bool = False
    min_confidence: float = 0.86
    ambiguity_margin: float = 0.03


@dataclass(frozen=True)
class JobRow:
    url: str
    title: str | None
    company: str | None
    location: str | None
    site: str | None
    application_url: str | None
    audit_label: str | None = None
    audit_score: float | None = None
    fit_score: int | None = None


@dataclass(frozen=True)
class MatchDecision:
    status: str
    final_url: str | None = None
    matched_url: str | None = None
    confidence: float | None = None
    error: str | None = None


@dataclass
class CompanyResolverSummary:
    considered: int = 0
    dry_run: bool = False
    counts: dict[str, int] | None = None
    sample_urls: list[str] | None = None

    def __post_init__(self) -> None:
        if self.counts is None:
            self.counts = {}
        if self.sample_urls is None:
            self.sample_urls = []


def _normalize_tiers(tiers: Iterable[str] | None, include_low: bool) -> tuple[str, ...]:
    base = tuple(t.strip() for t in (tiers or ("priority", "recommended")) if t and t.strip())
    if include_low:
        return tuple(dict.fromkeys((*base, "review", "low")))
    return base or ("priority", "recommended")


def _clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def normalize_company(value: str | None) -> str:
    text = re.sub(r"[^a-z0-9& ]+", " ", _clean_text(value))
    suffixes = {
        "inc",
        "incorporated",
        "llc",
        "l.l.c",
        "ltd",
        "limited",
        "corp",
        "corporation",
        "co",
        "company",
        "plc",
        "group",
    }
    tokens = [token for token in text.split() if token not in suffixes]
    return " ".join(tokens)


def normalize_title(value: str | None) -> str:
    text = re.sub(r"[^a-z0-9+&/ ]+", " ", _clean_text(value))
    text = text.replace("&", " and ").replace("/", " ")
    stop = {"remote", "hybrid", "onsite"}
    tokens = [token for token in text.split() if token not in stop]
    return " ".join(tokens)


def normalize_location(value: str | None) -> str:
    text = re.sub(r"[^a-z0-9, ]+", " ", _clean_text(value))
    text = re.sub(r"\b(united states|usa|us)\b", "", text)
    return re.sub(r"\s+", " ", text).strip(" ,")


def _title_similarity(left: str | None, right: str | None) -> float:
    a = normalize_title(left)
    b = normalize_title(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    token_score = len(a_tokens & b_tokens) / max(1, len(a_tokens | b_tokens))
    sequence_score = SequenceMatcher(None, a, b).ratio()
    return max(token_score, sequence_score)


def _location_similarity(left: str | None, right: str | None) -> float:
    a = normalize_location(left)
    b = normalize_location(right)
    if not a or not b:
        return 0.65
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85
    return SequenceMatcher(None, a, b).ratio()


def _confidence(candidate: JobRow, match: JobRow) -> float:
    if normalize_company(candidate.company) != normalize_company(match.company):
        return 0.0
    title_score = _title_similarity(candidate.title, match.title)
    location_score = _location_similarity(candidate.location, match.location)
    return round((0.25 + (0.65 * title_score) + (0.10 * location_score)), 4)


def _row_from_sql(row: sqlite3.Row) -> JobRow:
    return JobRow(
        url=row["url"],
        title=row["title"],
        company=row["company"],
        location=row["location"],
        site=row["site"],
        application_url=row["application_url"],
        audit_label=row["audit_label"] if "audit_label" in row.keys() else None,
        audit_score=row["audit_score"] if "audit_score" in row.keys() else None,
        fit_score=row["fit_score"] if "fit_score" in row.keys() else None,
    )


def fetch_candidates(
    *,
    limit: int,
    tiers: Iterable[str] | None = ("priority", "recommended"),
    include_low: bool = False,
    refresh: bool = False,
    conn: sqlite3.Connection | None = None,
) -> list[JobRow]:
    if conn is None:
        conn = get_connection()
    if limit <= 0:
        return []

    wanted_tiers = _normalize_tiers(tiers, include_low)
    tier_marks = ",".join("?" for _ in wanted_tiers)
    resolution_filter = "" if refresh else "AND COALESCE(apply_url_resolution_strategy, '') != 'company_match'"
    metadata_filter = ""
    ats_kind_values = tuple(sorted(ATS_RECONSTRUCTION_UNRESOLVED_KINDS))
    if not refresh:
        kind_marks = ",".join("?" for _ in ats_kind_values)
        metadata_filter = f"""
           AND (
                COALESCE(linkedin_resolve_status, '') != 'unresolved'
                OR COALESCE(linkedin_next_action, '') = ?
                OR (
                    COALESCE(linkedin_next_action, '') = ''
                    AND (
                        COALESCE(linkedin_unresolved_kind, '') IN ({kind_marks})
                        OR COALESCE(linkedin_unresolved_kind, '') = ''
                    )
                )
           )
        """
    metadata_params: tuple[str, ...] = ()
    if not refresh:
        metadata_params = (
            ATS_RECONSTRUCTION_NEXT_ACTION,
            *ats_kind_values,
        )
    order_kind_marks = ",".join("?" for _ in ats_kind_values)
    rows = conn.execute(
        f"""
        SELECT url, title, company, location, site, application_url,
               audit_label, audit_score, fit_score
          FROM jobs
         WHERE (lower(COALESCE(site, '')) = 'linkedin' OR url LIKE '%linkedin.com/jobs%')
           AND duplicate_of_url IS NULL
           AND COALESCE(liveness_status, '') != 'dead'
           AND applied_at IS NULL
           AND COALESCE(audit_label, '') IN ({tier_marks})
           {resolution_filter}
           {metadata_filter}
         ORDER BY
           CASE
                WHEN COALESCE(linkedin_next_action, '') = ? THEN 0
                WHEN COALESCE(linkedin_next_action, '') = ''
                     AND COALESCE(linkedin_unresolved_kind, '') IN ({order_kind_marks}) THEN 0
                ELSE 1
           END,
           CASE COALESCE(audit_label, '')
                WHEN 'priority' THEN 0
                WHEN 'recommended' THEN 1
                WHEN 'review' THEN 2
                WHEN 'low' THEN 3
                ELSE 4
           END,
           COALESCE(audit_score, -1) DESC,
           COALESCE(fit_score, -1) DESC,
           COALESCE(discovered_at, '') DESC,
           url ASC
         LIMIT ?
        """,
        (
            *wanted_tiers,
            *metadata_params,
            ATS_RECONSTRUCTION_NEXT_ACTION,
            *ats_kind_values,
            limit * 5,
        ),
    ).fetchall()

    candidates = []
    for row in rows:
        item = _row_from_sql(row)
        if not is_external_apply_url(item.application_url):
            candidates.append(item)
        if len(candidates) >= limit:
            break
    return candidates


def fetch_match_pool(conn: sqlite3.Connection | None = None) -> list[JobRow]:
    if conn is None:
        conn = get_connection()
    rows = conn.execute(
        """
        SELECT url, title, company, location, site, application_url,
               audit_label, audit_score, fit_score
          FROM jobs
         WHERE duplicate_of_url IS NULL
           AND COALESCE(liveness_status, '') != 'dead'
           AND applied_at IS NULL
           AND application_url IS NOT NULL
           AND trim(application_url) != ''
           AND lower(COALESCE(site, '')) != 'linkedin'
           AND url NOT LIKE '%linkedin.com/jobs%'
         ORDER BY
           CASE WHEN full_description IS NULL OR full_description = '' THEN 1 ELSE 0 END,
           COALESCE(audit_score, -1) DESC,
           COALESCE(fit_score, -1) DESC,
           COALESCE(discovered_at, '') DESC
        """
    ).fetchall()
    return [
        item
        for item in (_row_from_sql(row) for row in rows)
        if is_external_apply_url(item.application_url)
    ]


def _index_match_pool(pool: Sequence[JobRow]) -> dict[str, list[JobRow]]:
    index: dict[str, list[JobRow]] = defaultdict(list)
    for row in pool:
        key = normalize_company(row.company)
        if key:
            index[key].append(row)
    return index


def decide_match(
    candidate: JobRow,
    pool_index: dict[str, list[JobRow]],
    *,
    min_confidence: float,
    ambiguity_margin: float,
) -> MatchDecision:
    company_key = normalize_company(candidate.company)
    if not company_key:
        return MatchDecision(status="no_match", error="missing_company")

    scored = [
        (row, _confidence(candidate, row))
        for row in pool_index.get(company_key, [])
        if row.url != candidate.url
    ]
    scored = [(row, score) for row, score in scored if score >= min_confidence]
    if not scored:
        return MatchDecision(status="no_match")

    scored.sort(key=lambda item: (-item[1], item[0].url))
    top_row, top_score = scored[0]
    equivalent = [
        row
        for row, score in scored
        if abs(score - top_score) <= ambiguity_margin
        and row.application_url != top_row.application_url
    ]
    if equivalent:
        return MatchDecision(
            status="ambiguous",
            error="ambiguous_company_match",
            confidence=top_score,
            matched_url=top_row.url,
        )

    return MatchDecision(
        status=RESOLVED_STATUS,
        final_url=top_row.application_url,
        matched_url=top_row.url,
        confidence=top_score,
    )


def record_company_resolution(
    url: str,
    decision: MatchDecision,
    *,
    dry_run: bool = False,
    conn: sqlite3.Connection | None = None,
) -> None:
    if dry_run:
        return
    if conn is None:
        conn = get_connection()

    now = datetime.now(timezone.utc).isoformat()
    if decision.status == RESOLVED_STATUS and is_external_apply_url(decision.final_url):
        conn.execute(
            """
            UPDATE jobs
               SET application_url = ?,
                   apply_url_resolved_at = ?,
                   apply_url_resolution_strategy = 'company_match',
                   apply_url_resolution_confidence = ?,
                   apply_url_resolution_source = ?,
                   apply_url_resolution_error = NULL,
                   apply_url_resolution_attempts = COALESCE(apply_url_resolution_attempts, 0) + 1,
                   apply_url_resolution_matched_url = ?
             WHERE url = ?
            """,
            (
                decision.final_url,
                now,
                decision.confidence,
                RESOLVED_STATUS,
                decision.matched_url,
                url,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE jobs
               SET apply_url_resolved_at = ?,
                   apply_url_resolution_strategy = 'company_match',
                   apply_url_resolution_confidence = ?,
                   apply_url_resolution_source = ?,
                   apply_url_resolution_error = ?,
                   apply_url_resolution_attempts = COALESCE(apply_url_resolution_attempts, 0) + 1,
                   apply_url_resolution_matched_url = ?
             WHERE url = ?
            """,
            (
                now,
                decision.confidence,
                decision.status,
                decision.error,
                decision.matched_url,
                url,
            ),
        )
    conn.commit()


def run_resolver(
    options: CompanyResolverOptions,
    *,
    conn: sqlite3.Connection | None = None,
) -> CompanyResolverSummary:
    if conn is None:
        conn = get_connection()

    candidates = fetch_candidates(
        limit=options.limit,
        tiers=options.tiers,
        include_low=options.include_low,
        refresh=options.refresh,
        conn=conn,
    )
    pool_index = _index_match_pool(fetch_match_pool(conn))
    counts: Counter[str] = Counter()
    sample_urls: list[str] = []

    for candidate in candidates:
        decision = decide_match(
            candidate,
            pool_index,
            min_confidence=options.min_confidence,
            ambiguity_margin=options.ambiguity_margin,
        )
        counts[decision.status] += 1
        record_company_resolution(candidate.url, decision, dry_run=options.dry_run, conn=conn)
        if len(sample_urls) < 10:
            sample_urls.append(decision.final_url or candidate.url)

    return CompanyResolverSummary(
        considered=len(candidates),
        dry_run=options.dry_run,
        counts=dict(counts),
        sample_urls=sample_urls,
    )
