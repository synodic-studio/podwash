# Post-Claude Review: Remaining Podwash Blockers

**Context:** Claude committed five implementation commits on `develop` after `a2431d3`:

- `ba22ec8 fix: stabilize feed episode identity`
- `5d5069a fix: reject stale worker job completions`
- `432fd15 fix: harden management and file-serving APIs`
- `cb47436 fix: harden worker pipeline and deploy checks`
- `5db85fc docs: document feed update and operations semantics`

**Baseline verified after Claude:**

```bash
uv run pytest -q
# 140 passed

uv run ruff check src tests
# All checks passed

git status --short
# clean
```

## Verdict

Not ready to ship. Test/lint pass, but review found remaining blocking correctness/security issues. The largest product bug is still possible on upgraded live DBs: a completed clean item can disappear and be replaced by a placeholder after a source GUID/tracking change because `source_identity` is not backfilled.

## Blocker 1: Backfill/merge legacy feed identities

**Problem:** Existing upgraded DB rows get new columns but no backfilled `source_identity`.

**Evidence:**

- `src/database/db.py:117-129` adds `source_identity`, `is_active`, `publication_state`, etc.
- No migration backfills `source_identity` from existing `source_audio_url`.
- `src/database/queries.py:179-190` matches polls by `source_identity`, then `(feed_id, guid)`.
- If an existing completed row has `source_identity=NULL` and the source GUID changes, poll inserts a new placeholder row and `mark_feed_poll_visibility()` hides the old clean row.

**Reproduced:** A simulated legacy completed row with `clean_token='clean-old'` and `source_identity=NULL` was hidden after a poll with a changed GUID; generated RSS emitted the new placeholder `○` item instead of the existing clean `●` item.

**Fix:**

a. In `_migrate(conn)` after adding columns, backfill:

```sql
UPDATE episodes
SET source_identity = normalized_source_audio_url(source_audio_url)
WHERE source_identity IS NULL OR source_identity = '';
```

Because `normalize_audio_url()` is Python, implement a migration helper in Python:

```python
def _backfill_source_identity(conn):
    from src.feeds.parser import normalize_audio_url
    rows = conn.execute("SELECT id, source_audio_url FROM episodes WHERE source_identity IS NULL OR source_identity = ''").fetchall()
    for row in rows:
        conn.execute(
            "UPDATE episodes SET source_identity = ? WHERE id = ?",
            (normalize_audio_url(row["source_audio_url"]), row["id"]),
        )
```

b. Backfill `publication_state` for legacy rows:

- completed + clean_token → `clean`
- non-completed active rows → `placeholder`
- rows already inactive/hidden stay hidden

c. Add regression test using an old-schema/legacy row:

```python
def test_migration_backfills_completed_source_identity_so_clean_item_survives_guid_rotation(tmp_path):
    # Create DB with completed row source_identity NULL and clean_token set.
    # Run _migrate/init path.
    # Poll same source episode with changed source guid/tracking URL.
    # Assert visible RSS still contains one clean item, not a placeholder.
```

## Blocker 2: Hide duplicate completed clean rows for same source

**Problem:** Duplicate clean rows remain visible because duplicate hiding excludes completed rows.

**Evidence:**

- `src/database/queries.py:552-557` hides duplicates only when `status != 'completed'`.
- `src/database/queries.py:620-624` same issue in claim-safe completion path.
- `src/database/queries.py:139-159` emits all active non-hidden rows; it does not collapse by `source_identity`.

**Reproduced:** Two completed rows with the same `source_identity` and different `clean_token`s both appeared in `get_visible_episodes_for_feed()`.

**Fix:**

a. Add a canonicalization helper:

```python
def hide_superseded_publications(conn, episode_id: int) -> int:
    """For the completed/canonical row, hide all other rows with same feed_id + source_identity."""
```

b. Hide all non-canonical rows with same `source_identity`, including completed rows. Keep the newest/current completed row visible; hide older clean rows and placeholders.

c. Call it from both `mark_completed()` and `mark_completed_if_claimed()`.

d. Also call it during migration/backfill to clean existing duplicates.

e. Add tests:

```python
def test_duplicate_completed_clean_rows_collapse_to_one_visible_item(): ...
def test_completion_hides_prior_completed_and_placeholder_rows_for_same_source_identity(): ...
```

## Blocker 3: Scheduled polling still bypasses SSRF-safe fetcher

**Problem:** Feed submission validates/fetches safely, but scheduled polling later calls `feedparser.parse(url)` directly.

**Evidence:**

- `src/scheduler.py:188` calls `parse_feed(feed.source_url, ...)`.
- `src/feeds/parser.py:94` calls `feedparser.parse(url)`, letting feedparser fetch the URL.
- `src/feeds/parser.py:77` does the same for `extract_feed_image(url)`.

**Impact:** A stored public hostname can later DNS-rebind or redirect to private/internal addresses during scheduled polling.

**Fix:**

a. Make parser accept bytes/string content, not fetch URLs itself:

```python
def parse_feed_content(content: bytes | str, feed_id: int, max_episodes: int = 0) -> list[Episode]: ...
```

b. Update scheduler:

```python
content = await/sync wrapper fetch_public_feed(feed.source_url)
episodes = parse_feed_content(content, feed.id, max_episodes=max_episodes)
image_url = extract_feed_image_from_content(content)
```

Since scheduler is sync, either:

- provide `fetch_public_feed_sync()` wrapper around the async fetcher, or
- make scheduler job async-safe explicitly.

c. Remove direct URL fetches from `parse_feed()` / `extract_feed_image()` or make them clearly internal-only and unused for stored/user-submitted URLs.

d. Add regression tests proving scheduler calls the safe fetcher and never `feedparser.parse(url)` for stored source URLs.

## Blocker 4: Redirect/DNS SSRF guard validates after the redirected request already happened

**Problem:** `fetch_public_feed()` validates the initial hostname, then lets `httpx` follow redirects. It validates `response.url.host` only after the redirected request has already been made.

**Evidence:**

- `src/feeds/fetcher.py:85-86` validates initial host.
- `src/feeds/fetcher.py:91-96` uses `follow_redirects=True`.
- `src/feeds/fetcher.py:98-103` validates final host after the response exists.

**Fix:**

a. Disable automatic redirects.

b. Manually follow redirects up to a small cap, validating each `Location` target before connecting.

c. To reduce DNS rebinding risk, either:

- resolve and connect to the validated IP while preserving `Host`/SNI where possible, or
- at minimum re-resolve immediately before each request and reject if any resolved address is private; document residual DNS-rebinding risk.

Test cases:

```python
def test_fetcher_rejects_private_redirect_before_request(monkeypatch): ...
def test_fetcher_limits_redirect_depth(): ...
def test_scheduler_poll_uses_safe_fetcher_for_stored_feed_url(): ...
```

## Blocker 5: `mark_failed_if_claimed` is not atomic

**Problem:** It checks claim ownership with `SELECT`, then updates by `WHERE id = ?` only.

**Evidence:**

- `src/database/queries.py:644-651` SELECT checks `claimed_by`, `claim_token`, status.
- `src/database/queries.py:661-668` UPDATE does not recheck those conditions.

**Impact:** A stale failure can race after another worker reclaims/completes and clear the newer claim or overwrite completed state.

**Fix:**

a. Make failure update conditional in the `UPDATE` itself:

```sql
UPDATE episodes
SET ...
WHERE id = ?
  AND claimed_by = ?
  AND claim_token = ?
  AND status IN (...)
RETURNING status
```

b. If rowcount is 0, return `None` / 409.

c. Add a race regression test that simulates state change between SELECT and UPDATE, or refactor to a single conditional statement and assert stale fail returns 409.

## Blocker 6: Concurrent result uploads share the same temp path

**Problem:** Result upload temp path is deterministic per claim token.

**Evidence:**

- `src/api/routes/jobs.py:123` uses `processed.mp3.tmp-{claim_token}`.
- Upload writes before claim validation at `src/api/routes/jobs.py:128-138`.
- Final replace occurs at `src/api/routes/jobs.py:156`.

**Impact:** Two concurrent result submissions for the same valid claim write the same temp file and can corrupt/clobber final audio.

**Fix:**

a. Use a unique temp path per request, e.g. `processed.mp3.tmp-{claim_token}-{uuid4}`.

b. Validate claim before opening the temp file where possible, and validate again atomically at completion.

c. Consider a DB status transition to a terminal/finishing state or rely on `mark_completed_if_claimed()` clearing claim fields atomically; only one request should win.

Test:

```python
def test_concurrent_same_claim_uploads_use_distinct_temp_paths_and_only_one_wins(): ...
```

## Final verification

After fixes:

```bash
uv run pytest -q
uv run ruff check src tests
```

Also manually re-run the two reproduced feed simulations:

- legacy completed row with `source_identity=NULL` + changed GUID must keep the clean item visible.
- duplicate completed rows with same `source_identity` must collapse to one visible item.
