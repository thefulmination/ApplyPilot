"""Read-only Greenhouse form inventory using the public Job Board API."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

from applypilot import config
from applypilot.apply.greenhouse_adapter import (
    builtin_questions_from_payload,
    build_answer_plan,
    fetch_job,
    job_context_from_payload,
    parse_greenhouse_url,
    resolve_greenhouse_url,
)
from applypilot.apply.pgqueue import connect


@dataclass
class ShadowInventory:
    url: str
    board: str | None
    job_id: str | None
    question_count: int
    required_count: int
    unmapped_required: list[str]
    ready_without_free_text: bool
    error: str | None = None


def _no_model_answer(*args, **kwargs):
    return SimpleNamespace(verified=False, text="")


def inventory_url(url: str, *, profile: dict, fetch=None) -> ShadowInventory:
    resolved_url = resolve_greenhouse_url(url)
    parsed = parse_greenhouse_url(resolved_url or "")
    if not parsed:
        return ShadowInventory(url, None, None, 0, 0, [], False, "unsupported_url")
    board, job_id = parsed
    try:
        payload = fetch_job(board, job_id, fetch=fetch)
        questions = list(payload.get("questions") or [])
        questions.extend(builtin_questions_from_payload(payload, profile=profile))
        plan = build_answer_plan(
            questions,
            profile=profile,
            resume_text="",
            answer_fn=_no_model_answer,
            job=job_context_from_payload(payload, board=board),
        )
        return ShadowInventory(
            url=resolved_url,
            board=board,
            job_id=job_id,
            question_count=len(questions),
            required_count=sum(bool(q.get("required")) for q in questions),
            unmapped_required=list(plan.unmapped_required),
            ready_without_free_text=plan.ready,
        )
    except Exception as exc:
        return ShadowInventory(
            resolved_url, board, job_id, 0, 0, [], False,
            f"{type(exc).__name__}:{exc}",
        )


def candidate_urls(conn, *, limit: int, min_score: float) -> Iterable[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT application_url
            FROM apply_queue
            WHERE application_url IS NOT NULL
              AND score >= %s
              AND status IN ('queued', 'leased', 'blocked')
              AND lower(application_url) SIMILAR TO
                  'https://(boards|job-boards).greenhouse.io/%%/jobs/%%'
            ORDER BY application_url
            LIMIT %s
            """,
            (min_score, limit),
        )
        for row in cur.fetchall():
            yield row["application_url"] if isinstance(row, dict) else row[0]


def run(*, limit: int, min_score: float, output: Path | None = None) -> list[ShadowInventory]:
    profile = config.load_profile()
    with connect() as conn:
        results = []
        successful = 0
        for url in candidate_urls(conn, limit=limit * 10, min_score=min_score):
            result = inventory_url(url, profile=profile)
            results.append(result)
            if result.error is None:
                successful += 1
            if successful >= limit:
                break
    lines = [json.dumps(asdict(result), sort_keys=True) for result in results]
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    for line in lines:
        print(line)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(prog="applypilot-greenhouse-shadow")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--min-score", type=float, default=0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    results = run(limit=max(1, args.limit), min_score=args.min_score, output=args.output)
    ok = sum(result.error is None for result in results)
    ready = sum(result.ready_without_free_text for result in results)
    print(f"greenhouse shadow inventory: total={len(results)} ok={ok} ready_without_free_text={ready}")


if __name__ == "__main__":
    main()
