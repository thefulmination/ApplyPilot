from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import console_diagnosis


def _seed_apply_job(
    conn,
    *,
    url: str,
    company: str = "Acme",
    title: str = "Engineer",
    status: str = "queued",
    approved_batch: str | None = "batch-1",
    dedup_key: str | None = None,
    score: float = 8.0,
    target_host: str = "boards.greenhouse.io",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue "
            "(url, company, title, application_url, score, lane, status, approved_batch, "
            "dedup_key, target_host, apply_domain, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,'ats',%s,%s,%s,%s,%s,now())",
            (
                url,
                company,
                title,
                url + "/apply",
                score,
                status,
                approved_batch,
                dedup_key or f"{company.lower()}::{title.lower()}",
                target_host,
                target_host,
            ),
        )


def test_ats_queue_diagnosis_counts_dedup_blocked_rows(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply_job(
            conn,
            url="https://boards.greenhouse.io/acme/jobs/1",
            dedup_key="acme::engineer",
        )
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO applied_set (dedup_key, company, applied_url) "
                "VALUES ('acme::engineer', 'Acme', 'https://already/applied')"
            )
        conn.commit()

        result = console_diagnosis.queue_diagnosis(conn)

    ats = result["ats"]
    assert ats["queued"] == 1
    assert ats["approved"] == 1
    assert ats["dedup_blocked"] == 1
    assert ats["leaseable"] == 0
    assert result["state"]["code"] == "idle_no_leasable_jobs"
    assert "dedup" in result["state"]["reason"].lower()


def test_ats_queue_diagnosis_counts_leaseable_rows(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply_job(
            conn,
            url="https://boards.greenhouse.io/acme/jobs/2",
            dedup_key="acme::analyst",
        )
        conn.commit()

        result = console_diagnosis.queue_diagnosis(conn)

    ats = result["ats"]
    assert ats["queued"] == 1
    assert ats["approved"] == 1
    assert ats["dedup_blocked"] == 0
    assert ats["leaseable"] == 1
    assert result["state"]["code"] == "ready_to_apply"


def test_linkedin_canary_exhaustion_is_lane_specific(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO linkedin_queue "
                "(url, company, title, application_url, score, lane, status, approved_batch, dedup_key, updated_at) "
                "VALUES ('https://www.linkedin.com/jobs/view/1','Beta','Analyst',"
                "'https://www.linkedin.com/jobs/view/1',8,'ats','queued','batch-li','beta::analyst',now())"
            )
            cur.execute(
                "UPDATE fleet_config SET linkedin_canary_enabled=TRUE, "
                "linkedin_canary_remaining=0, canary_enabled=FALSE, canary_remaining=NULL WHERE id=1"
            )
        conn.commit()

        result = console_diagnosis.queue_diagnosis(conn)

    assert result["linkedin"]["queued"] == 1
    assert result["linkedin"]["approved"] == 1
    assert result["linkedin"]["leaseable"] == 0
    assert result["linkedin"]["canary_exhausted"] is True
    assert result["ats"]["canary_exhausted"] is False
