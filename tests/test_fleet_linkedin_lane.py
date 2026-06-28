from applypilot.apply import pgqueue


def test_linkedin_schema_columns(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT linkedin_canary_enabled, linkedin_canary_remaining FROM fleet_config WHERE id=1")
        row = cur.fetchone()
        assert row["linkedin_canary_enabled"] is False and row["linkedin_canary_remaining"] is None
        cur.execute("INSERT INTO rate_governor (scope_key) VALUES ('account:linkedin')")
        cur.execute("SELECT halted_until FROM rate_governor WHERE scope_key='account:linkedin'")
        assert cur.fetchone()["halted_until"] is None
