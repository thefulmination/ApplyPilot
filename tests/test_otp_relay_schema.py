"""otp_request gains the short-lived code-transport columns the relay uses."""
from applypilot.apply import pgqueue
from applypilot.fleet import schema as fleet_schema


def test_otp_request_has_transport_columns(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        fleet_schema.ensure_schema_v3(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'otp_request'"
            )
            cols = {r["column_name"] for r in cur.fetchall()}
    for needed in ("code", "code_kind", "expires_at", "answered_at",
                   "worker_id", "url", "sender_hint", "requested_at", "consumed_at"):
        assert needed in cols, f"missing column {needed}: {sorted(cols)}"


def test_otp_request_dml_roundtrip(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        fleet_schema.ensure_schema_v3(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO otp_request (worker_id, url, sender_hint, expires_at) "
                "VALUES ('mac-0', 'https://x/apply', 'greenhouse.io', now() + interval '5 min') "
                "RETURNING id"
            )
            rid = cur.fetchone()["id"]
            cur.execute("UPDATE otp_request SET code='123456', code_kind='code', "
                        "answered_at=now() WHERE id=%s", (rid,))
            cur.execute("SELECT code, code_kind FROM otp_request WHERE id=%s", (rid,))
            row = cur.fetchone()
        conn.commit()
    assert row["code"] == "123456" and row["code_kind"] == "code"
