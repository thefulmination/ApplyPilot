# Email Authentication Defense-in-Depth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ApplyPilot email authentication use one request and one eligible message exactly once, with complete mailbox reads and demand-aware failure monitoring.

**Architecture:** Preserve the existing `mail_source` -> `inbox_auth` -> `otp_relay` -> apply-worker pipeline. Centralize request-relative candidate eligibility in `inbox_auth`, persist Gmail message idempotency in Postgres, serialize responder scans with an advisory lock, and make DeadMan evaluate actual pending demand.

**Tech Stack:** Python 3, pytest, psycopg/PostgreSQL, Gmail IMAP and Gmail API read-only clients, PowerShell operational scripts.

---

## File Map

- `src/applypilot/apply/launcher.py`: prearm TTL and local watcher context.
- `src/applypilot/fleet/apply_worker_main.py`: retain one prearmed request through the bounded post-run wait and one assisted retry.
- `src/applypilot/inbox_auth.py`: shared time/provider/message eligibility and busy-inbox query forwarding.
- `src/applypilot/fleet/otp_relay.py`: active-request reuse, persistent message idempotency, unique assignment, and responder singleton ownership.
- `src/applypilot/mail_source.py`: Gmail API pagination while preserving the normalized mail contract.
- `src/applypilot/fleet/schema_v3.sql`: `matched_message_id`, `wait_started_at`, and uniqueness index.
- `src/applypilot/fleet/deadman.py`: pending-demand and delivery-stall alerts.
- `docs/fleet-otp-relay-runbook.md`: migration, restart, canary, and live-cycle checks.
- `tests/test_launcher_inbox_relay.py`: prearm TTL regression.
- `tests/test_apply_channel.py`: one-request bounded retry regression.
- `tests/test_inbox_auth_mail_source.py`: local stale/provider filtering and query-budget regressions.
- `tests/test_otp_relay_schema.py`: schema and uniqueness contract.
- `tests/test_otp_relay_worker.py`: active-request reuse contract.
- `tests/test_otp_relay_responder.py`: cross-cycle idempotency, ambiguity, and singleton tests.
- `tests/test_mail_source_factory.py`: Gmail API pagination and exact budget tests.
- `tests/test_deadman_check.py`: missing responder and stalled delivery alerts.

### Task 1: Create the isolated execution worktree

**Files:**
- Reference: `docs/superpowers/specs/2026-07-10-email-auth-defense-in-depth-design.md`
- Reference: `docs/superpowers/plans/2026-07-10-email-auth-defense-in-depth.md`

- [ ] **Step 1: Invoke the worktree skill**

Use `superpowers:using-git-worktrees` before creating the branch or directory.

- [ ] **Step 2: Create the focused branch from the approved-plan commit**

Run from the main checkout:

```powershell
git worktree add .worktrees/email-auth-defense-in-depth -b codex/email-auth-defense-in-depth HEAD
```

Expected: a clean worktree on `codex/email-auth-defense-in-depth`; the original checkout retains all unrelated dirty files.

- [ ] **Step 3: Run the baseline authentication suite from the worktree**

```powershell
$env:PYTHONPATH = (Join-Path $PWD "src")
& "..\..\.conda-env\python.exe" -m pytest tests\test_inbox_auth_mail_source.py tests\test_inbox_auth_gmail.py tests\test_mail_source_imap.py tests\test_mail_source_factory.py tests\test_otp_relay_worker.py tests\test_otp_relay_responder.py tests\test_otp_responder_main.py tests\test_launcher_inbox_relay.py tests\test_apply_channel.py tests\test_deadman_check.py -q
```

Expected: all baseline tests pass. If a path differs, resolve the parent checkout's `.conda-env\python.exe` with `Resolve-Path` and rerun the same test list.

### Task 2: Keep one request through the complete assisted retry

**Files:**
- Modify: `src/applypilot/apply/launcher.py:2097`
- Modify: `src/applypilot/fleet/apply_worker_main.py:197`
- Test: `tests/test_launcher_inbox_relay.py:61`
- Test: `tests/test_apply_channel.py:164`

- [ ] **Step 1: Strengthen the failing TTL assertion**

Replace the current lower bound in `test_prearm_request_ttl_covers_agent_run_plus_postrun_poll` with the complete wait invariant:

```python
assert seen["ttl_seconds"] >= 900  # 600s agent + 300s complete auth wait
```

- [ ] **Step 2: Add the failing one-request retry test**

Add to `tests/test_apply_channel.py`:

```python
def test_auth_required_reuses_prearmed_request_for_full_bounded_wait(monkeypatch):
    from applypilot.apply import chrome, launcher
    from applypilot.fleet import apply_worker_main as awm

    calls = []
    consume_calls = []
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH_TIMEOUT", "300")
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH_POSTRUN_TIMEOUT", "45")
    monkeypatch.setattr(chrome, "launch_chrome", lambda *a, **k: object())
    monkeypatch.setattr(chrome, "cleanup_worker", lambda *a, **k: None)
    monkeypatch.setattr(awm, "_cdp_page_urls", lambda port: [])
    monkeypatch.setattr(launcher, "_should_prearm_inbox_auth", lambda job: True)
    monkeypatch.setattr(launcher, "_prearm_inbox_auth_request", lambda job: 73)
    ticks = iter([100.0, 145.0])
    monkeypatch.setattr(awm.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(
        launcher,
        "_poll_inbox_auth_hint",
        lambda job: (_ for _ in ()).throw(AssertionError("must not create a second request")),
    )

    def consume(request_id, *, timeout_seconds=None, poll_seconds=None):
        consume_calls.append((request_id, timeout_seconds))
        return None if len(consume_calls) == 1 else "code=246810\nsource=fleet_relay"

    def run_job(job, port, worker_id, model, agent, inbox_auth_hint=None):
        calls.append(inbox_auth_hint)
        monkeypatch.setattr(launcher, "_last_run_stats", {0: {}}, raising=False)
        return ("auth_required", 1.0) if len(calls) == 1 else ("applied", 1.0)

    monkeypatch.setattr(launcher, "_consume_prearmed_inbox_auth_hint", consume)
    monkeypatch.setattr(launcher, "run_job", run_job)

    result = awm.make_apply_fn("sonnet", "codex")(
        {"url": "https://jobs.test/1", "application_url": "https://greenhouse.io/a"}
    )

    assert [request_id for request_id, _ in consume_calls] == [73, 73]
    assert sum(timeout for _, timeout in consume_calls) <= 300
    assert calls == [None, "code=246810\nsource=fleet_relay"]
    assert result["run_status"] == "applied"
```

- [ ] **Step 3: Run both regressions and verify RED**

```powershell
& "..\..\.conda-env\python.exe" -m pytest tests\test_launcher_inbox_relay.py::test_prearm_request_ttl_covers_agent_run_plus_postrun_poll tests\test_apply_channel.py::test_auth_required_reuses_prearmed_request_for_full_bounded_wait -q
```

Expected: TTL assertion fails and the apply-worker test reaches `_poll_inbox_auth_hint` or does not perform the second same-ID poll.

- [ ] **Step 4: Implement the complete prearm TTL**

In `_prearm_inbox_auth_request` use:

```python
timeout = max(1, int(os.environ.get("APPLYPILOT_INBOX_AUTH_TIMEOUT", "300")))
agent_timeout = max(
    1,
    int(os.environ.get("APPLYPILOT_AGENT_TIMEOUT", str(AGENT_TIMEOUT_SECONDS))),
)
ttl_seconds = agent_timeout + timeout
```

Remove the old `postrun_timeout` TTL calculation.

- [ ] **Step 5: Poll the original request for the remaining budget**

In `make_apply_fn`, replace the fallback call that inserts another request with bounded same-row polling:

```python
auth_wait_started = time.monotonic()
total_auth_wait = max(1, int(os.environ.get("APPLYPILOT_INBOX_AUTH_TIMEOUT", "300")))
short_auth_wait = min(
    total_auth_wait,
    max(0, int(os.environ.get("APPLYPILOT_INBOX_AUTH_POSTRUN_TIMEOUT") or "45")),
)
inbox_hint = launcher._consume_prearmed_inbox_auth_hint(
    prearmed_request_id,
    timeout_seconds=short_auth_wait,
)
if not inbox_hint and launcher._is_auth_required_result(status):
    elapsed = max(0.0, time.monotonic() - auth_wait_started)
    remaining = max(0, int(total_auth_wait - elapsed))
    if remaining:
        inbox_hint = launcher._consume_prearmed_inbox_auth_hint(
            prearmed_request_id,
            timeout_seconds=remaining,
        )
```

Keep the existing one-retry `if inbox_hint:` block unchanged.

- [ ] **Step 6: Run the focused tests and verify GREEN**

```powershell
& "..\..\.conda-env\python.exe" -m pytest tests\test_launcher_inbox_relay.py tests\test_apply_channel.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit the request-lifecycle fix**

```powershell
git add src/applypilot/apply/launcher.py src/applypilot/fleet/apply_worker_main.py tests/test_launcher_inbox_relay.py tests/test_apply_channel.py
git commit -m "fix: reuse one OTP request through auth retry"
```

### Task 3: Share request-relative candidate eligibility

**Files:**
- Modify: `src/applypilot/inbox_auth.py:177,523,621`
- Modify: `src/applypilot/apply/launcher.py:2199`
- Modify: `src/applypilot/fleet/otp_relay.py:22,68`
- Test: `tests/test_inbox_auth_mail_source.py:144`
- Test: `tests/test_otp_relay_responder.py`

- [ ] **Step 1: Add stale and cross-provider local watcher tests**

Add to `tests/test_inbox_auth_mail_source.py`:

```python
def test_watch_rejects_prechallenge_and_wrong_provider_messages(monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)

    class _FakeSource:
        def fetch(self, *, since_days, max_messages, gmail_raw_query=None):
            assert "verification" in gmail_raw_query
            return [
                MailMessage(
                    id="stale-greenhouse", thread_id="1", subject="Verify your email",
                    sender="no-reply@greenhouse.io", date=_rfc(now - dt.timedelta(minutes=2)),
                    body="Use verification code 111111 to continue.",
                ),
                MailMessage(
                    id="fresh-workday", thread_id="2", subject="Verify your email",
                    sender="no-reply@workday.com", date=_rfc(now + dt.timedelta(seconds=5)),
                    body="Use verification code 222222 to continue.",
                ),
                MailMessage(
                    id="fresh-greenhouse", thread_id="3", subject="Verify your email",
                    sender="no-reply@greenhouse-mail.io", date=_rfc(now + dt.timedelta(seconds=10)),
                    body="Use verification code 333333 to continue.",
                ),
            ]

    monkeypatch.setattr("applypilot.mail_source.get_mail_source", lambda: _FakeSource())
    match = inbox_auth.watch_gmail_for_auth_code(
        timeout_seconds=1,
        poll_seconds=0,
        max_errors=1,
        minutes=15,
        max_messages=1000,
        not_before=now,
        provider_domain="greenhouse.io",
    )

    assert match is not None
    assert match.message_id == "fresh-greenhouse"
```

- [ ] **Step 2: Run the new watcher test and verify RED**

```powershell
& "..\..\.conda-env\python.exe" -m pytest tests\test_inbox_auth_mail_source.py::test_watch_rejects_prechallenge_and_wrong_provider_messages -q
```

Expected: `watch_gmail_for_auth_code` rejects the new keyword arguments or the fake source rejects the missing Gmail query.

- [ ] **Step 3: Add shared provider and eligibility helpers**

In `inbox_auth.py`, define the provider groups once:

```python
PROVIDER_DOMAIN_GROUPS = (
    ("oraclecloud.com", "oracle.com", "taleo.net"),
    ("myworkdayjobs.com", "myworkdaysite.com", "workdayjobs.com", "workday.com"),
    ("greenhouse.io", "greenhouse-mail.io"),
    ("adp.com", "workforcenow.adp.com"),
    ("amazon.jobs", "jobs.amazon.com"),
    ("eightfold.ai",),
)


def domains_related(left: str | None, right: str | None) -> bool:
    left = _normalize_domain(left or "")
    right = _normalize_domain(right or "")
    if not left or not right:
        return False
    if left == right or left.endswith(f".{right}") or right.endswith(f".{left}"):
        return True
    return any(
        any(left == d or left.endswith(f".{d}") for d in group)
        and any(right == d or right.endswith(f".{d}") for d in group)
        for group in PROVIDER_DOMAIN_GROUPS
    )


def match_belongs_to_provider(match: AuthEmailMatch, provider_domain: str | None) -> bool:
    if not provider_domain:
        return True
    evidence = [sender_domain(match.sender)]
    if match.candidate.kind == "magic_link":
        evidence.append(url_domain(match.candidate.value))
    evidence = [domain for domain in evidence if domain]
    return bool(evidence) and any(
        domains_related(provider_domain, domain) for domain in evidence
    )


def eligible_auth_matches(
    matches: list[AuthEmailMatch],
    *,
    not_before: datetime | None = None,
    provider_domain: str | None = None,
    skew_seconds: int = 60,
    excluded_message_ids: set[str] | None = None,
) -> list[AuthEmailMatch]:
    excluded = excluded_message_ids or set()
    floor = None
    if not_before is not None:
        if not_before.tzinfo is None:
            not_before = not_before.replace(tzinfo=timezone.utc)
        floor = not_before.astimezone(timezone.utc) - timedelta(seconds=max(0, skew_seconds))
    eligible = []
    for match in matches:
        received = _received_at_dt(match.received_at)
        if match.message_id in excluded or received is None:
            continue
        if floor is not None and received < floor:
            continue
        if not match_belongs_to_provider(match, provider_domain):
            continue
        eligible.append(match)
    return eligible
```

- [ ] **Step 4: Extend the watcher contract and query forwarding**

Extend `watch_gmail_for_auth_code` with:

```python
not_before: datetime | None = None,
provider_domain: str | None = None,
skew_seconds: int = 60,
```

Fetch with the bounded verification query:

```python
msgs = get_mail_source().fetch(
    since_days=since_days,
    max_messages=max_messages,
    gmail_raw_query=AUTH_GMAIL_RAW_QUERY,
)
```

Before sorting, filter both source paths:

```python
matches = eligible_auth_matches(
    matches,
    not_before=not_before,
    provider_domain=provider_domain,
    skew_seconds=skew_seconds,
)
```

- [ ] **Step 5: Pass local challenge context from the launcher**

In `_poll_inbox_auth_hint`, capture the start before creating the challenge and pass it to the watcher:

```python
challenge_started_at = datetime.now(timezone.utc)
match = inbox_auth.watch_gmail_for_auth_code(
    timeout_seconds=timeout,
    poll_seconds=poll,
    max_errors=max_errors,
    minutes=minutes,
    max_messages=max_messages,
    not_before=challenge_started_at,
    provider_domain=provider,
)
```

Change the local default `APPLYPILOT_INBOX_AUTH_MAX_MESSAGES` from `25` to `1000`.

- [ ] **Step 6: Route relay provider matching through the shared helper**

Delete the duplicate provider-group logic from `otp_relay.py` and implement:

```python
def _match_belongs_to_request(sender_hint: str | None, match) -> bool:
    return inbox_auth.match_belongs_to_provider(match, sender_hint)
```

Update the responder test double so existing Greenhouse tests carry the same
sender evidence as production matches:

```python
class _Match:
    def __init__(
        self,
        message_id,
        received_at,
        value,
        kind="code",
        sender="Greenhouse <no-reply@greenhouse-mail.io>",
    ):
        self.message_id = message_id
        self.received_at = received_at
        self.sender = sender
        self.candidate = _Cand(value, kind)
```

Tests for Oracle, Workday, or another provider must pass the corresponding
sender explicitly.

- [ ] **Step 7: Run the eligibility slices and verify GREEN**

```powershell
& "..\..\.conda-env\python.exe" -m pytest tests\test_inbox_auth_mail_source.py tests\test_inbox_auth_gmail.py tests\test_otp_relay_responder.py tests\test_launcher_inbox_relay.py -q
```

Expected: all tests pass.

- [ ] **Step 8: Commit the shared eligibility change**

```powershell
git add src/applypilot/inbox_auth.py src/applypilot/apply/launcher.py src/applypilot/fleet/otp_relay.py tests/test_inbox_auth_mail_source.py tests/test_otp_relay_responder.py tests/test_launcher_inbox_relay.py
git commit -m "fix: share request-relative OTP eligibility"
```

### Task 4: Persist message idempotency and reuse active requests

**Files:**
- Modify: `src/applypilot/fleet/schema_v3.sql:286`
- Modify: `src/applypilot/fleet/otp_relay.py:84,160`
- Test: `tests/test_otp_relay_schema.py`
- Test: `tests/test_otp_relay_worker.py`
- Test: `tests/test_otp_relay_responder.py`

- [ ] **Step 1: Add failing schema and reuse tests**

Extend the required column tuple in `test_otp_request_has_transport_columns`
with `"matched_message_id"` and `"wait_started_at"`, then add:

```python
def test_request_code_reuses_active_request(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        fleet_schema.ensure_schema_v3(conn)
        first = otp_relay.request_code(
            conn, worker_id="m4-0", job_url="j",
            application_url="https://greenhouse.io/a", ttl_seconds=300,
        )
        second = otp_relay.request_code(
            conn, worker_id="m4-0", job_url="j",
            application_url="https://greenhouse.io/a", ttl_seconds=300,
        )
    assert second == first


def test_poll_marks_wait_started_at_once(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        fleet_schema.ensure_schema_v3(conn)
        request_id = otp_relay.request_code(
            conn, worker_id="m4-0", job_url="j",
            application_url="https://greenhouse.io/a", ttl_seconds=300,
        )
        assert otp_relay.poll_for_code(
            conn, request_id, timeout_seconds=0, poll_seconds=0,
        ) is None
        with conn.cursor() as cur:
            cur.execute(
                "SELECT wait_started_at FROM otp_request WHERE id=%s",
                (request_id,),
            )
            first_wait = cur.fetchone()["wait_started_at"]
        assert otp_relay.poll_for_code(
            conn, request_id, timeout_seconds=0, poll_seconds=0,
        ) is None
        with conn.cursor() as cur:
            cur.execute(
                "SELECT wait_started_at FROM otp_request WHERE id=%s",
                (request_id,),
            )
            second_wait = cur.fetchone()["wait_started_at"]
    assert first_wait is not None
    assert second_wait == first_wait
```

Add a cross-cycle message reuse regression using a `_Match` with sender metadata:

```python
def test_matched_message_id_cannot_answer_later_request(fleet_db, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    match = _Match("same-message", _rfc(now + dt.timedelta(seconds=5)), "123456")
    monkeypatch.setattr(otp_relay.inbox_auth, "scan_gmail_for_auth_codes", lambda **kw: [match])
    with _fresh(fleet_db) as conn:
        first = _pending(conn, worker_id="m4-0")
        assert otp_relay.answer_pending(conn, _FakeGmail([match])) == 1
        assert otp_relay.poll_for_code(conn, first, timeout_seconds=1, poll_seconds=0.01)
        second = _pending(conn, worker_id="m4-1")
        assert otp_relay.answer_pending(conn, _FakeGmail([match])) == 0
        assert otp_relay.poll_for_code(conn, second, timeout_seconds=1, poll_seconds=0.01) is None
```

- [ ] **Step 2: Run the new tests and verify RED**

```powershell
& "..\..\.conda-env\python.exe" -m pytest tests\test_otp_relay_schema.py tests\test_otp_relay_worker.py::test_request_code_reuses_active_request tests\test_otp_relay_worker.py::test_poll_marks_wait_started_at_once tests\test_otp_relay_responder.py::test_matched_message_id_cannot_answer_later_request -q
```

Expected: missing columns, different request IDs, unchanged null wait timestamp,
and message reuse failure.

- [ ] **Step 3: Add the idempotent schema migration**

Add to `schema_v3.sql`:

```sql
ALTER TABLE otp_request ADD COLUMN IF NOT EXISTS matched_message_id TEXT;
ALTER TABLE otp_request ADD COLUMN IF NOT EXISTS wait_started_at TIMESTAMPTZ;
CREATE UNIQUE INDEX IF NOT EXISTS idx_otp_matched_message_unique
    ON otp_request (matched_message_id)
    WHERE matched_message_id IS NOT NULL;
```

Update the outdated table comment to state that the code is stored only between answer and atomic consume.

- [ ] **Step 4: Reuse active requests under a transaction advisory lock**

At the start of `request_code`, normalize `target = application_url or job_url`, then:

```python
lock_key = f"applypilot:otp_request:{worker_id}:{target}"
with conn.cursor() as cur:
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))
    cur.execute(
        "SELECT id FROM otp_request "
        "WHERE worker_id=%s AND url=%s AND consumed_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > now()) "
        "ORDER BY requested_at DESC LIMIT 1",
        (worker_id, target),
    )
    existing = cur.fetchone()
    if existing:
        rid = existing["id"]
    else:
        cur.execute(
            "INSERT INTO otp_request (worker_id, url, sender_hint, expires_at) "
            "VALUES (%s, %s, %s, now() + make_interval(secs => %s)) RETURNING id",
            (worker_id, target, _apply_domain(application_url), ttl_seconds),
        )
        rid = cur.fetchone()["id"]
conn.commit()
return rid
```

- [ ] **Step 5: Exclude and persist matched message IDs**

Before the polling loop in `poll_for_code`, stamp active demand exactly once:

```python
with conn.cursor() as cur:
    cur.execute(
        "UPDATE otp_request SET wait_started_at=COALESCE(wait_started_at, now()) "
        "WHERE id=%s AND consumed_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > now())",
        (request_id,),
    )
conn.commit()
```

Then persist message IDs during response matching.

During the pending-row query, also load used IDs:

```python
cur.execute(
    "SELECT matched_message_id FROM otp_request WHERE matched_message_id IS NOT NULL"
)
persisted_used = {row["matched_message_id"] for row in cur.fetchall()}
```

Initialize `used_messages` from that set. Extend the guarded answer update:

```python
"UPDATE otp_request SET code=%s, code_kind=%s, matched_email_ts=%s, "
"matched_message_id=%s, answered_at=now(), "
"expires_at=GREATEST(expires_at, now() + make_interval(secs => %s)) "
"WHERE id=%s AND code IS NULL AND consumed_at IS NULL "
"AND (expires_at IS NULL OR expires_at > now())"
```

Pass `chosen.message_id` before the answered TTL parameter. Import the exact
database exception:

```python
from psycopg import errors as pg_errors
```

Guard the write without logging candidate values:

```python
try:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE otp_request SET code=%s, code_kind=%s, matched_email_ts=%s, "
            "matched_message_id=%s, answered_at=now(), "
            "expires_at=GREATEST(expires_at, now() + make_interval(secs => %s)) "
            "WHERE id=%s AND code IS NULL AND consumed_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > now())",
            (
                chosen.candidate.value,
                chosen.candidate.kind,
                _parse_email_dt(chosen.received_at),
                chosen.message_id,
                answered_ttl_seconds,
                req["id"],
            ),
        )
        wrote = bool(cur.rowcount)
    conn.commit()
except pg_errors.UniqueViolation:
    conn.rollback()
    used_messages.add(chosen.message_id)
    continue
if wrote:
    answered += 1
```

- [ ] **Step 6: Run the relay schema/worker/responder tests and verify GREEN**

```powershell
& "..\..\.conda-env\python.exe" -m pytest tests\test_otp_relay_schema.py tests\test_otp_relay_worker.py tests\test_otp_relay_responder.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit persistent idempotency**

```powershell
git add src/applypilot/fleet/schema_v3.sql src/applypilot/fleet/otp_relay.py tests/test_otp_relay_schema.py tests/test_otp_relay_worker.py tests/test_otp_relay_responder.py
git commit -m "fix: make OTP request and message delivery idempotent"
```

### Task 5: Serialize responder scans and fail closed on ambiguous mappings

**Files:**
- Modify: `src/applypilot/fleet/otp_relay.py:160`
- Test: `tests/test_otp_relay_responder.py`
- Test: `tests/test_inbox_auth_mail_source.py:205`

- [ ] **Step 1: Add failing singleton and ambiguity tests**

Add a two-connection real-Postgres test:

```python
def test_second_responder_skips_scan_while_lock_is_owned(fleet_db, monkeypatch):
    scans = []
    monkeypatch.setattr(
        otp_relay.inbox_auth,
        "scan_gmail_for_auth_codes",
        lambda **kw: scans.append(kw) or [],
    )
    with _fresh(fleet_db) as owner, _fresh(fleet_db) as contender:
        _pending(owner)
        with owner.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_lock(hashtext('applypilot:otp_responder'))"
            )
        assert otp_relay.answer_pending(contender, _FakeGmail([])) == 0
        with owner.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_unlock(hashtext('applypilot:otp_responder'))"
            )
    assert scans == []
```

Add an ambiguity test with two pending Greenhouse requests and one eligible Greenhouse message; assert neither request is answered.

- [ ] **Step 2: Run both new tests and verify RED**

```powershell
& "..\..\.conda-env\python.exe" -m pytest tests\test_otp_relay_responder.py::test_second_responder_skips_scan_while_lock_is_owned tests\test_otp_relay_responder.py::test_ambiguous_same_provider_requests_remain_unanswered -q
```

Expected: the second responder scans despite the held lock and the message is assigned to one ambiguous request.

- [ ] **Step 3: Add responder lock helpers**

In `otp_relay.py`:

```python
_RESPONDER_LOCK_KEY = "applypilot:otp_responder"


def _try_responder_lock(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS acquired",
            (_RESPONDER_LOCK_KEY,),
        )
        return bool(cur.fetchone()["acquired"])


def _release_responder_lock(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_unlock(hashtext(%s))",
            (_RESPONDER_LOCK_KEY,),
        )
```

Rename the current `answer_pending` implementation to
`_answer_pending_locked` and give it this exact signature while keeping its body
in place for the next step:

```python
def _answer_pending_locked(
    conn,
    gmail_service=None,
    *,
    window_minutes: int,
    max_messages: int,
    skew_seconds: int,
    answered_ttl_seconds: int | None,
) -> int:
```

Then add the public lock-owning wrapper:

```python
def answer_pending(
    conn,
    gmail_service=None,
    *,
    window_minutes: int = 15,
    max_messages: int = _DEFAULT_SCAN_MAX_MESSAGES,
    skew_seconds: int = 60,
    answered_ttl_seconds: int | None = None,
) -> int:
if not _try_responder_lock(conn):
    return 0
try:
    return _answer_pending_locked(
        conn,
        gmail_service,
        window_minutes=window_minutes,
        max_messages=max_messages,
        skew_seconds=skew_seconds,
        answered_ttl_seconds=answered_ttl_seconds,
    )
finally:
    _release_responder_lock(conn)
```

Move the current implementation into `_answer_pending_locked` without changing the public signature.

- [ ] **Step 4: Implement deterministic unique-pair elimination**

Add a pure helper:

```python
def _eligible_for_request(request, match, received_at, *, skew_seconds):
    requested_at = request["requested_at"]
    if requested_at.tzinfo is None:
        requested_at = requested_at.replace(tzinfo=_dt.timezone.utc)
    floor = requested_at.astimezone(_dt.timezone.utc) - _dt.timedelta(
        seconds=max(0, skew_seconds)
    )
    return (
        received_at >= floor
        and _match_belongs_to_request(request.get("sender_hint"), match)
    )


def _unique_assignments(pending, parsed, used_message_ids, *, skew_seconds):
    remaining_requests = {row["id"]: row for row in pending}
    remaining_messages = {
        match.message_id: (match, received_at)
        for match, received_at in parsed
        if match.message_id not in used_message_ids
    }
    assigned = []
    while remaining_requests and remaining_messages:
        edges = {
            request_id: {
                message_id
                for message_id, (match, received_at) in remaining_messages.items()
                if _eligible_for_request(
                    request,
                    match,
                    received_at,
                    skew_seconds=skew_seconds,
                )
            }
            for request_id, request in remaining_requests.items()
        }
        reverse = {
            message_id: {
                request_id for request_id, message_ids in edges.items()
                if message_id in message_ids
            }
            for message_id in remaining_messages
        }
        pairs = [
            (request_id, next(iter(message_ids)))
            for request_id, message_ids in edges.items()
            if len(message_ids) == 1
            and len(reverse[next(iter(message_ids))]) == 1
        ]
        if not pairs:
            break
        for request_id, message_id in pairs:
            assigned.append((remaining_requests.pop(request_id), remaining_messages.pop(message_id)[0]))
    return assigned
```

Call the helper from `_answer_pending_locked`:

```python
assignments = _unique_assignments(
    pending,
    parsed,
    used_messages,
    skew_seconds=skew_seconds,
)
for req, chosen in assignments:
    used_messages.add(chosen.message_id)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE otp_request SET code=%s, code_kind=%s, matched_email_ts=%s, "
                "matched_message_id=%s, answered_at=now(), "
                "expires_at=GREATEST(expires_at, now() + make_interval(secs => %s)) "
                "WHERE id=%s AND code IS NULL AND consumed_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > now())",
                (
                    chosen.candidate.value,
                    chosen.candidate.kind,
                    _parse_email_dt(chosen.received_at),
                    chosen.message_id,
                    answered_ttl_seconds,
                    req["id"],
                ),
            )
            wrote = bool(cur.rowcount)
        conn.commit()
    except pg_errors.UniqueViolation:
        conn.rollback()
        continue
    if wrote:
        answered += 1
return answered
```

Do not retain the old greedy request/message loop.

- [ ] **Step 5: Update lightweight fake-connection tests for lock SQL**

In `tests/test_inbox_auth_mail_source.py`, make `_FakeCursor.execute` return `{"acquired": True}` for `pg_try_advisory_lock`, accept `pg_advisory_unlock`, and keep the existing pending-row behavior. This test must still assert no mailbox fetch occurs when the pending list is empty.

- [ ] **Step 6: Run responder and mail-source integration tests**

```powershell
& "..\..\.conda-env\python.exe" -m pytest tests\test_otp_relay_responder.py tests\test_inbox_auth_mail_source.py tests\test_otp_responder_main.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit singleton and ambiguity handling**

```powershell
git add src/applypilot/fleet/otp_relay.py tests/test_otp_relay_responder.py tests/test_inbox_auth_mail_source.py tests/test_otp_responder_main.py
git commit -m "fix: serialize and disambiguate OTP responder matching"
```

### Task 6: Paginate Gmail API fallback exactly to budget

**Files:**
- Modify: `src/applypilot/mail_source.py:265`
- Test: `tests/test_mail_source_factory.py:28`

- [ ] **Step 1: Make the fake Gmail list endpoint page-aware**

Change `_FakeMessages` to accept `list_results` and:

```python
def list(self, userId, q, maxResults, pageToken=None):
    self.list_calls.append((userId, q, maxResults, pageToken))
    result = self._list_results[pageToken]
    return _Execable(result)
```

Update existing assertions to include the fourth `None` element.

- [ ] **Step 2: Add the failing pagination and exact-budget test**

```python
def test_gmail_api_mail_source_paginates_to_exact_budget():
    list_results = {
        None: {
            "messages": [{"id": str(i), "threadId": str(i)} for i in range(500)],
            "nextPageToken": "page-2",
        },
        "page-2": {
            "messages": [{"id": str(i), "threadId": str(i)} for i in range(500, 700)]
        },
    }
    fake_service = _FakeGmailService(
        list_results,
        lambda message_id: {
            "id": message_id,
            "threadId": message_id,
            "payload": _gmail_payload_with_text_plain("Verification body"),
        },
    )

    result = GmailApiMailSource(build_service=lambda: fake_service).fetch(
        since_days=1,
        max_messages=650,
        gmail_raw_query="verification",
    )

    assert len(result) == 650
    assert fake_service.messages_obj.list_calls == [
        ("me", "newer_than:1d (verification)", 500, None),
        ("me", "newer_than:1d (verification)", 150, "page-2"),
    ]
```

Adapt `_FakeMessages.get` to call `_get_result(id)` when the configured result is callable.

- [ ] **Step 3: Run the pagination test and verify RED**

```powershell
& "..\..\.conda-env\python.exe" -m pytest tests\test_mail_source_factory.py::test_gmail_api_mail_source_paginates_to_exact_budget -q
```

Expected: the current one-page implementation returns only 500 messages or the fake rejects the old call shape.

- [ ] **Step 4: Implement bounded page iteration**

In `GmailApiMailSource.fetch`:

```python
remaining = max(0, int(max_messages))
refs = []
page_token = None
while remaining > 0:
    page_size = min(500, remaining)
    request = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=page_size,
        pageToken=page_token,
    )
    page = request.execute()
    page_refs = page.get("messages", [])[:remaining]
    refs.extend(page_refs)
    remaining -= len(page_refs)
    page_token = page.get("nextPageToken")
    if not page_token or not page_refs:
        break
```

Iterate `refs` in the existing normalization loop. Preserve `max_messages=0` as an empty bounded fetch rather than an unbounded API read.

- [ ] **Step 5: Run all mail-source tests and verify GREEN**

```powershell
& "..\..\.conda-env\python.exe" -m pytest tests\test_mail_source_factory.py tests\test_mail_source_imap.py tests\test_inbox_auth_mail_source.py tests\test_outcome_scan.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit pagination**

```powershell
git add src/applypilot/mail_source.py tests/test_mail_source_factory.py
git commit -m "fix: paginate Gmail fallback within scan budget"
```

### Task 7: Alert on pending demand and stalled delivery

**Files:**
- Modify: `src/applypilot/fleet/deadman.py:162`
- Test: `tests/test_deadman_check.py:489`

- [ ] **Step 1: Add failing demand-aware alert tests**

Add a helper in `tests/test_deadman_check.py`:

```python
def _otp_request(cur, requested_at, *, waiting=True):
    cur.execute(
        "INSERT INTO otp_request "
        "(worker_id, url, sender_hint, requested_at, wait_started_at, expires_at) "
        "VALUES ('m4-0', 'https://greenhouse.io/a', 'greenhouse.io', %s, %s, %s)",
        (
            requested_at,
            requested_at if waiting else None,
            requested_at + dt.timedelta(minutes=15),
        ),
    )
```

Add:

```python
def test_otp_relay_down_when_request_pending_without_responder(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            _otp_request(cur, NOW - dt.timedelta(seconds=30))
        conn.commit()
        alerts, _ = deadman.deadman_check(conn, now=NOW, gmail_token_ok=True)
    assert "otp_relay_down" in _kinds(alerts)


def test_otp_delivery_stalled_with_fresh_responder(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_OTP_STALL_SECONDS", "120")
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            _heartbeat(cur, "otp_responder", NOW - dt.timedelta(seconds=10))
            _otp_request(cur, NOW - dt.timedelta(seconds=180))
        conn.commit()
        alerts, _ = deadman.deadman_check(conn, now=NOW, gmail_token_ok=True)
    assert "otp_delivery_stalled" in _kinds(alerts)


def test_prearmed_request_does_not_false_alarm(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_OTP_STALL_SECONDS", "120")
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            _otp_request(cur, NOW - dt.timedelta(minutes=5), waiting=False)
        conn.commit()
        alerts, _ = deadman.deadman_check(conn, now=NOW, gmail_token_ok=True)
    assert "otp_relay_down" not in _kinds(alerts)
    assert "otp_delivery_stalled" not in _kinds(alerts)
```

Keep `test_otp_relay_ok_when_token_alive_and_no_responder_row` as the no-demand/no-noise contract.

- [ ] **Step 2: Run the new tests and verify RED**

```powershell
& "..\..\.conda-env\python.exe" -m pytest tests\test_deadman_check.py::test_otp_relay_down_when_request_pending_without_responder tests\test_deadman_check.py::test_otp_delivery_stalled_with_fresh_responder tests\test_deadman_check.py::test_prearmed_request_does_not_false_alarm -q
```

Expected: neither new alert exists.

- [ ] **Step 3: Add pending summary and threshold helpers**

In `deadman.py`:

```python
def _otp_pending_summary(conn, now):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n, MIN(wait_started_at) AS oldest "
            "FROM otp_request WHERE code IS NULL AND consumed_at IS NULL "
            "AND wait_started_at IS NOT NULL "
            "AND requested_at <= %s "
            "AND (expires_at IS NULL OR expires_at > %s)",
            (now, now),
        )
        row = cur.fetchone()
    return int(row["n"]), row["oldest"]


def _otp_stall_seconds() -> int:
    try:
        return max(30, int(os.environ.get("APPLYPILOT_OTP_STALL_SECONDS", "120")))
    except (TypeError, ValueError):
        return 120
```

- [ ] **Step 4: Return demand-aware OTP alerts**

Change `_check_otp_relay` to return `list[Alert]`. Preserve the token-dead and stale-historical-heartbeat behavior, then add:

```python
pending_count, oldest_pending = _otp_pending_summary(conn, now)
otp_beat = _max_last_beat(conn, "otp_responder", negate=False)
if pending_count and otp_beat is None:
    reasons.append(f"{pending_count} pending OTP request(s) but responder never heartbeated")
if reasons:
    alerts.append(Alert(kind="otp_relay_down", severity="critical", detail="OTP relay down: " + "; ".join(reasons)))
if (
    pending_count
    and otp_beat is not None
    and otp_beat >= now - dt.timedelta(minutes=STALE_MIN)
    and gmail_token_ok is True
    and oldest_pending is not None
    and oldest_pending < now - dt.timedelta(seconds=_otp_stall_seconds())
):
    age = int((now - oldest_pending).total_seconds())
    alerts.append(Alert(
        kind="otp_delivery_stalled",
        severity="critical",
        detail=f"OTP delivery stalled: {pending_count} pending request(s); oldest age={age}s",
    ))
return alerts
```

In `deadman_check`, replace the single append with:

```python
alerts.extend(_check_otp_relay(conn, now, gmail_token_ok))
```

- [ ] **Step 5: Run the complete DeadMan tests and verify GREEN**

```powershell
& "..\..\.conda-env\python.exe" -m pytest tests\test_deadman_check.py tests\test_fleet_console_doctor.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit monitoring hardening**

```powershell
git add src/applypilot/fleet/deadman.py tests/test_deadman_check.py
git commit -m "fix: alert on unserved OTP demand"
```

### Task 8: Document, verify, integrate, and roll out

**Files:**
- Modify: `docs/fleet-otp-relay-runbook.md`
- Test: `tests/test_otp_runbooks_and_bootstrap.py`

- [ ] **Step 1: Add runbook assertions before editing documentation**

Extend `tests/test_otp_runbooks_and_bootstrap.py` to require these literal operational concepts:

```python
assert "matched_message_id" in runbook
assert "otp_delivery_stalled" in runbook
assert "X-GM-RAW" in runbook
assert "controlled end-to-end" in runbook.lower()
```

- [ ] **Step 2: Run the runbook test and verify RED**

```powershell
& "..\..\.conda-env\python.exe" -m pytest tests\test_otp_runbooks_and_bootstrap.py -q
```

Expected: one or more new runbook assertions fail.

- [ ] **Step 3: Update the runbook with exact migration and verification commands**

Document:

```powershell
$env:PYTHONPATH = (Join-Path $PWD "src")
@'
from applypilot.apply import pgqueue
from applypilot.fleet import schema
dsn = "host=localhost port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5"
with pgqueue.connect(dsn) as conn:
    schema.ensure_schema_v3(conn)
print("schema=ok")
'@ | .\.conda-env\python.exe -
```

Also document responder restart, the privacy-safe `X-GM-RAW` canary, queries that print only pending count/oldest age/heartbeat/matched-message presence, `otp_relay_down` and `otp_delivery_stalled` interpretation, and the controlled end-to-end sequence. Do not print codes, subjects, senders, magic links, credentials, or DSNs containing passwords.

- [ ] **Step 4: Run all focused and broader tests**

```powershell
& "..\..\.conda-env\python.exe" -m pytest tests\test_inbox_auth_mail_source.py tests\test_inbox_auth_gmail.py tests\test_mail_source_imap.py tests\test_mail_source_factory.py tests\test_otp_relay_worker.py tests\test_otp_relay_responder.py tests\test_otp_relay_schema.py tests\test_otp_responder_main.py tests\test_otp_responder_startup_scripts.py tests\test_launcher_inbox_relay.py tests\test_apply_channel.py tests\test_deadman_check.py tests\test_fleet_console_doctor.py tests\test_otp_runbooks_and_bootstrap.py tests\test_worker_inbox_relay_scripts.py -q
& "..\..\.conda-env\python.exe" -m pytest tests\test_gmail_outcomes.py tests\test_outcome_scan.py tests\test_outcomes_integration_cli.py -q
git diff --check
```

Expected: zero failures and clean diff check.

- [ ] **Step 5: Commit documentation and final test adjustments**

```powershell
git add docs/fleet-otp-relay-runbook.md tests/test_otp_runbooks_and_bootstrap.py
git commit -m "docs: add OTP relay mission-grade verification"
```

- [ ] **Step 6: Integrate only focused commits into the dirty live checkout**

From the original checkout, first confirm `HEAD` is still the plan commit and inspect the focused branch range:

```powershell
git status -sb
git log --oneline 59b3ae4..codex/email-auth-defense-in-depth
git diff --check 59b3ae4..codex/email-auth-defense-in-depth
```

Apply the branch diff to the original checkout without staging unrelated local changes:

```powershell
git diff --no-ext-diff --full-index 59b3ae4..codex/email-auth-defense-in-depth | git apply --whitespace=nowarn
git diff --no-ext-diff --full-index 59b3ae4..codex/email-auth-defense-in-depth | git apply --cached --whitespace=nowarn
git diff --cached --name-only
```

Expected staged names are only the files listed in this plan. Verify `git diff --cached` contains the OTP hardening and `git diff` still contains the pre-existing unrelated changes. If the first apply succeeds but the cached apply fails, reverse only the focused working-tree patch with the inverse branch diff, then merge the affected hunk manually with `apply_patch`; never discard the existing working-tree version or use checkout/reset on a dirty file.

- [ ] **Step 7: Apply the schema and restart the live responder**

Run the runbook schema command, then restart only the responder through the existing launcher:

```powershell
Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine -like "*applypilot-fleet-otp-home*"
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
& .\run-otp-responder.ps1
```

Verify exactly one `applypilot-fleet-otp-home.exe` process and a fresh `otp_responder` heartbeat. Do not alter apply/discovery workers.

- [ ] **Step 8: Run the live IMAP and controlled OTP checks**

Run the privacy-safe IMAP canary from the runbook. Then execute a controlled end-to-end challenge using a real application verification email or the owner-approved test message. Prove only these non-secret facts:

```text
request_created=yes
responder_answered=yes
worker_consumed=yes
code_cleared=yes
matched_message_id_retained=yes
assisted_retry_terminal=yes
deadman_otp_alerts=0
```

If a controlled message cannot be delivered, do not claim the live cycle passed; report that exact external blocker while retaining all automated and mailbox-canary evidence.

- [ ] **Step 9: Commit the focused integrated change and publish**

After the original checkout's focused staged diff and fresh tests pass:

```powershell
git commit -m "fix: harden job application email authentication"
git push -u myfork HEAD:codex/email-auth-defense-in-depth
```

Open a draft PR through the GitHub connector. If connector authorization still returns 403 and `gh auth status` remains unauthenticated, provide the compare URL and state that PR creation is the only external publishing blocker.

## Completion Gate

Do not mark the goal complete until every acceptance criterion in
`docs/superpowers/specs/2026-07-10-email-auth-defense-in-depth-design.md` has
current evidence. In particular, unit tests and a mailbox count canary do not
substitute for the controlled request -> answer -> consume -> retry cycle.
