# Worker Claim Ownership Implementation Plan

> **For Claude Code:** Use TDD. This prevents stale workers from corrupting feed state after the feed identity fix.

**Goal:** Ensure only the worker that currently owns a claim can submit result/failure for that claim.

**Architecture:** Add a per-claim token/version stored on the episode row. `claim_next` returns it. `/result` and `/fail` require worker id + claim token and update rows conditionally. Stale submissions return `409 Conflict` without mutating state or files.

## Current evidence

- `src/api/routes/jobs.py:85-117` accepts result upload with only shared bearer token and `episode_id`.
- `src/api/routes/jobs.py:120-139` accepts failure with only shared bearer token and `episode_id`.
- `src/database/queries.py:346-374` `mark_completed()` updates unconditionally and regenerates `clean_token`.
- `src/database/queries.py:387-419` `mark_failed()` updates unconditionally.
- `src/database/queries.py:294-343` claims rows with `claimed_by` but no token/version.

## Task 1: Add failing stale result/failure tests

**Files:**
- Create/modify: `tests/test_job_claims.py`
- Modify later: `src/api/routes/jobs.py`, `src/database/db.py`, `src/database/models.py`, `src/database/queries.py`

Test helpers:
- Use `create_app(Settings())` or direct DB query tests, matching existing test style.
- Set `settings.worker.token = "test-token"`.
- Use `TestClient` for HTTP route behavior.

Tests:

```python
def test_result_for_unclaimed_episode_returns_409_and_does_not_complete(client, conn):
    # Insert pending/new episode.
    # POST /api/jobs/{id}/result with valid bearer but no matching claim headers.
    # Assert 409.
    # Assert row is not completed and no processed file remains.
```

```python
def test_stale_worker_result_after_reclaim_returns_409(client, conn):
    # Worker A claims -> token A.
    # Force stale reset or directly reclaim as Worker B -> token B.
    # Worker B submits result successfully.
    # Worker A submits result with token A.
    # Assert 409 and clean_token/processed path still belong to Worker B result.
```

```python
def test_stale_worker_failure_after_completion_returns_409(client, conn):
    # Worker A claims -> token A.
    # Worker B later completes.
    # Worker A fail request returns 409.
    # Completed row remains completed.
```

Run:

```bash
uv run pytest tests/test_job_claims.py -q
```

Expected before implementation: fails because routes mutate unconditionally.

## Task 2: Add claim token schema/model fields

**Files:**
- Modify: `src/database/db.py`
- Modify: `src/database/models.py`

Add `claim_token TEXT` column to `episodes`, idempotently migrated.

Update `Episode`:

```python
claim_token: str | None = None
```

Add index:

```sql
CREATE INDEX IF NOT EXISTS idx_episodes_claim_token ON episodes(claim_token);
```

Run:

```bash
uv run pytest tests/test_watchdog.py tests/test_idle_watchdog.py -q
```

## Task 3: Return claim token from claim_next_pending

**File:** `src/database/queries.py`

In `claim_next_pending()`:
- Generate `claim_token = str(uuid.uuid4())`.
- Set `claimed_at`, `claimed_by`, `claim_token` with the in-flight status.
- `reset_stale_claims()` must clear `claim_token` when resetting stale work.
- `reset_orphaned_in_flight()` should clear `claimed_at`, `claimed_by`, and `claim_token`, not only status.

Update queue health if useful, but do not expose claim token there.

**File:** `src/api/routes/jobs.py`

Update `GET /api/jobs/next` response to include:

```python
"claim_token": episode.claim_token
```

Test:

```python
def test_claim_next_returns_claim_token_and_stores_it(client, conn): ...
```

Run:

```bash
uv run pytest tests/test_job_claims.py -q
```

## Task 4: Conditional completion/failure helpers

**File:** `src/database/queries.py`

Add:

```python
def mark_completed_if_claimed(conn, episode_id, worker_id, claim_token, processed_audio_path, ad_segments_json) -> str | None:
    # Only update WHERE id=? AND claimed_by=? AND claim_token=?
    # AND status IN ('downloading','transcribing','classifying','editing')
    # Set completed, processed path, clean_token, completed_at, clear claim fields.
    # Return token if rowcount == 1 else None.
```

```python
def mark_failed_if_claimed(conn, episode_id, worker_id, claim_token, error_message, max_retries=3) -> EpisodeStatus | None:
    # First select row under matching id/worker/token and in-flight status.
    # If none, return None.
    # Then apply same retry logic conditionally.
```

Preserve existing `mark_completed`/`mark_failed` only if other tests/code use them directly. Prefer updating call sites to safe helpers.

## Task 5: Require worker id and claim token in result/fail routes

**File:** `src/api/routes/jobs.py`

For `submit_result`:
- Add headers:

```python
worker: str = Header(..., alias="X-Worker-Id")
claim_token: str = Header(..., alias="X-Claim-Token")
```

- Write upload to a temp file first, e.g. `processed.mp3.tmp-{claim_token}`.
- Call `mark_completed_if_claimed(...)` before final rename if possible, or after writing but before replacing existing final file.
- If stale/mismatched, delete temp file and return `409`.
- Atomically replace final path only after claim validation succeeds.

For `submit_failure`:
- Require same headers.
- Call `mark_failed_if_claimed`.
- Return `409` if `None`.

Backwards compatibility: update `src/worker/client.py` to store `claim_token` from job response and send `X-Worker-Id` / `X-Claim-Token` on result/fail.

## Task 6: Update worker client tests

**Files:**
- Create/modify: `tests/test_worker_client.py` if not present.
- Modify: `src/worker/client.py`.

Tests:

```python
def test_queue_client_sends_worker_id_and_claim_token_on_result(monkeypatch): ...
def test_queue_client_sends_worker_id_and_claim_token_on_failure(monkeypatch): ...
def test_queue_client_requires_claim_token_in_job_payload(): ...
```

Run:

```bash
uv run pytest tests/test_job_claims.py tests/test_worker_client.py -q
```

## Task 7: Full checks and commit

Run:

```bash
uv run pytest -q
uv run ruff check src tests
git diff --stat
git add src tests
git commit -m "fix: reject stale worker job completions"
```

## Acceptance criteria

- Result/fail for unclaimed or stale claims returns 409.
- Stale workers cannot regenerate clean tokens or overwrite completed rows.
- Worker client sends claim token for result/fail.
- Existing queue/watchdog behavior still works.
