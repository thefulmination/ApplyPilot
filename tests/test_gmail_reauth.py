"""build_gmail_service self-heals a revoked token: on RefreshError it falls through to
a fresh interactive consent instead of crashing on invalid_grant."""
import json


def test_revoked_token_falls_through_to_reconsent(tmp_path, monkeypatch):
    from google.auth.exceptions import RefreshError

    import applypilot.gmail_outcomes as go

    creds_file = tmp_path / "gmail_credentials.json"
    creds_file.write_text('{"installed": {"client_id": "x", "client_secret": "y"}}')
    tok_file = tmp_path / "gmail_token.json"
    tok_file.write_text('{"refresh_token": "r"}')

    class _Revoked:
        valid = False
        expired = True
        refresh_token = "r"

        def refresh(self, request):
            raise RefreshError("invalid_grant: Token has been expired or revoked.")

    class _Fresh:
        valid = True

        def to_json(self):
            return json.dumps({"token": "fresh"})

    flow_calls = {"n": 0}

    class _FakeFlow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            return cls()

        def run_local_server(self, port=0):
            flow_calls["n"] += 1
            return _Fresh()

    monkeypatch.setattr(
        "google.oauth2.credentials.Credentials.from_authorized_user_file",
        lambda p, s: _Revoked(),
    )
    monkeypatch.setattr("google_auth_oauthlib.flow.InstalledAppFlow", _FakeFlow)
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: "SERVICE")

    svc = go.build_gmail_service(credentials_path=creds_file, token_path=tok_file)

    assert svc == "SERVICE"
    assert flow_calls["n"] == 1  # re-consent flow ran (did NOT crash on RefreshError)
    assert json.loads(tok_file.read_text()) == {"token": "fresh"}  # fresh token written
