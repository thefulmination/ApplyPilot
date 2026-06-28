# tests/test_fleet_apply_lane.py
from applypilot.apply import pgqueue


def test_canary_columns_exist_and_default(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT canary_enabled, canary_remaining FROM fleet_config WHERE id=1")
        row = cur.fetchone()
    assert row["canary_enabled"] is False
    assert row["canary_remaining"] is None
