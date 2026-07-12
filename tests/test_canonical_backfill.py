import json

from applypilot import database
from applypilot.canonical_backfill import accepted_reviewed_outcomes, backfill_research_artifacts


def test_backfill_is_deterministic_and_requires_review(tmp_path):
    conn = database.init_db(tmp_path / "brain.db")
    url = "https://jobs.example.com/1"
    conn.execute("INSERT INTO jobs (url,title) VALUES (?,?)", (url, "Role"))
    conn.execute(
        "INSERT INTO email_events (message_id,occurred_at,stage,scanned_at,job_url) "
        "VALUES ('mail-1','2026-01-01T00:00:00Z','interview','2026-01-01T00:00:00Z',?)",
        (url,),
    )
    (tmp_path / "pairwise.jsonl").write_text(
        "\n".join(
            json.dumps({"id": f"p{i}", "leftJobUrl": url, "rightJobUrl": url, "winner": winner})
            for i, winner in enumerate(("left", "right"))
        ),
        encoding="utf-8",
    )
    (tmp_path / "reviewed_outcomes.jsonl").write_text(
        json.dumps({"eventId": "mail-1", "jobUrl": url, "stage": "interview"}),
        encoding="utf-8",
    )

    first = backfill_research_artifacts(conn, tmp_path)
    second = backfill_research_artifacts(conn, tmp_path)

    assert first == second
    assert first["pairwise"]["written"] == 2
    assert first["pairwise"]["sha256"] == second["pairwise"]["sha256"]
    assert conn.execute("SELECT review_status FROM reviewed_outcomes").fetchone()[0] == "needs_review"
    assert accepted_reviewed_outcomes(conn) == []


def test_only_explicit_accepted_outcomes_enter_model_input(tmp_path):
    conn = database.init_db(tmp_path / "brain.db")
    url = "https://jobs.example.com/accepted"
    conn.execute("INSERT INTO jobs (url,title) VALUES (?,?)", (url, "Role"))
    conn.execute(
        "INSERT INTO email_events (message_id,occurred_at,stage,scanned_at,job_url) "
        "VALUES ('mail-a','2026-01-01T00:00:00Z','screen','2026-01-01T00:00:00Z',?)",
        (url,),
    )
    conn.execute(
        "INSERT INTO email_event_reviews (message_id,review_action,reviewed_at,resolution) "
        "VALUES ('mail-a','confirm','2026-01-02T00:00:00Z','trusted')"
    )
    (tmp_path / "outcomes.jsonl").write_text(
        json.dumps({
            "eventId": "mail-a", "jobUrl": url, "normalizedStage": "screen",
            "reviewStatus": "accepted", "reviewer": "owner",
            "attribution": {"messageId": "mail-a", "match": "exact-url"},
        }),
        encoding="utf-8",
    )
    backfill_research_artifacts(conn, tmp_path)
    rows = accepted_reviewed_outcomes(conn)
    assert len(rows) == 1
    assert rows[0]["event_id"] == "mail-a"
    assert json.loads(rows[0]["attribution_json"])["messageId"] == "mail-a"


def test_pairwise_ab_artifact_resolves_item_ids_and_kg_schema_version(tmp_path):
    conn = database.init_db(tmp_path / "brain.db")
    urls = ("https://example.com/a", "https://example.com/b")
    for url in urls:
        conn.execute("INSERT INTO jobs (url,title) VALUES (?,?)", (url, "Role"))
    for event_id, item_id, url in (("l-a", "item-a", urls[0]), ("l-b", "item-b", urls[1])):
        conn.execute(
            "INSERT INTO research_labels (id,item_id,job_url,raw_event_json) VALUES (?,?,?,?)",
            (event_id, item_id, url, "{}"),
        )
    conn.commit()
    (tmp_path / "pairwise.jsonl").write_text(
        json.dumps({"id": "pw-1", "jobAItemId": "item-a", "jobBItemId": "item-b", "result": "b"}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "knowledge_graph.json").write_text(
        json.dumps({"schemaVersion": "applypilot_knowledge_graph_v1", "generatedAt": "2026-01-01T00:00:00Z"}),
        encoding="utf-8",
    )

    report = backfill_research_artifacts(conn, tmp_path)

    assert report["pairwise"]["written"] == 1
    row = conn.execute(
        "SELECT left_job_url,right_job_url,winner FROM research_pairwise_labels WHERE id='pw-1'"
    ).fetchone()
    assert tuple(row) == (urls[0], urls[1], "right")
    assert report["kg"]["written"] == 1
