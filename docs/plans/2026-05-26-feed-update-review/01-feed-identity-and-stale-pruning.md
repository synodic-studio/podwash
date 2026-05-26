# Feed Identity and Stale Pruning Implementation Plan

> **For Claude Code:** Use TDD. This is the highest-priority user-visible fix.

**Goal:** Make RSS feed updates deterministic so repeated polls and completion transitions do not leave duplicate/stale items visible.

**Architecture:** Introduce a durable normalized source identity separate from RSS publication identity. A source episode may have two publication identities over its life: placeholder (`episode.guid`) and cleaned (`clean_token`). Generated RSS must emit only the current publication identity, never both.

## Current evidence

- Completed episodes change GUID to `episode.clean_token`: `src/feeds/generator.py:63-76`.
- DB uniqueness is only `(feed_id, guid)`: `src/database/db.py:20-40`.
- Parsed GUID fallback is `entry.id or entry.link or audio_url`: `src/feeds/parser.py:39`.
- Polling inserts only; it does not update existing rows or hide absent rows: `src/scheduler.py:187-198`.
- Feed route emits all rows: `src/api/routes/feeds.py:22-28`, `src/database/queries.py:116-122`.
- Cleanup uses `created_at`, not completion time, and leaves `clean_token`: `src/scheduler.py:234-264`.

## Desired semantics

a. **Cleaned item should become the current new episode:** On completion, the generated RSS should publish the cleaned item with a fresh GUID (`clean_token`) and `/audio/clean/{clean_token}.mp3`.

b. **Placeholder must disappear from generated RSS:** The placeholder publication identity (`episode.guid` + `/audio/{feed_id}/{episode_id}.mp3`) must not remain in `/feeds/{slug}.xml` after completion. This keeps the staleness signal at the feed-refresh layer in the podcast app.

c. **Durable source identity is internal only:** Dedup should survive unstable source GUIDs/links/tracking params by comparing normalized audio URL and/or `(normalized title, pub_date, duration)`. This identity decides which DB row represents the source episode; it is not necessarily the RSS `<guid>`.

d. **Current visibility:** Polling should maintain an active/current window. Old rows outside the latest source feed window or `max_episodes` should not be emitted by `/feeds/{slug}.xml`.

e. **No destructive cleanup by default:** Hide/archive stale duplicates; do not delete completed audio unless the existing retention cleanup intentionally does so.

## Task 1: Add failing publication-transition tests

**Files:**
- Create/modify: `tests/test_feed_identity.py`
- Modify later: `src/feeds/generator.py`, `src/database/queries.py`

**Test cases:**

```python
def test_completion_replaces_placeholder_publication_with_clean_publication():
    # Build Feed + Episode with guid="source-guid" and status NEW.
    # Generate feed XML and assert one item whose guid is source-guid and enclosure is /audio/{feed}/{episode}.mp3.
    # Mark same source episode COMPLETED with clean_token="clean-token".
    # Generate again.
    # Assert one item total for that source episode.
    # Assert guid is clean-token and enclosure is /audio/clean/clean-token.mp3.
    # Assert source-guid placeholder guid/enclosure no longer appears.
```

```python
def test_generated_feed_never_contains_placeholder_and_clean_item_for_same_source_episode():
    # Seed/construct rows that could represent both placeholder and cleaned publication.
    # Generate RSS.
    # Assert only the current cleaned publication is emitted.
```

Run:

```bash
uv run pytest tests/test_feed_identity.py -q
```

Expected before implementation: fails when stale placeholder rows remain visible, or when duplicate DB rows produce both placeholder and clean items.

## Task 2: Make publication identity explicit in feed generation

**File:** `src/feeds/generator.py:63-76`

Keep the existing completion policy that completed episodes use `clean_token` as RSS GUID and `/audio/clean/{token}.mp3` as enclosure. Clarify the code comment:

- Completed clean audio is intentionally a new podcast-client item.
- Placeholder identity must disappear from generated RSS once the source episode is completed.
- Dedup/currentness must be handled before generation, not by making clean and placeholder share a GUID.

Implementation shape:

```python
if episode.status == EpisodeStatus.COMPLETED and episode.clean_token:
    audio_url = f"{base_url}/audio/clean/{episode.clean_token}.mp3"
    feed_guid = episode.clean_token
else:
    audio_url = f"{base_url}/audio/{feed.id}/{episode.id}.mp3"
    feed_guid = episode.guid
```

Run:

```bash
uv run pytest tests/test_feed_identity.py -q
uv run pytest tests/test_audio_recovery.py -q
```

## Task 3: Add schema columns for active/current identity metadata

**Files:**
- Modify: `src/database/db.py`
- Modify: `src/database/models.py`

Add columns to `episodes`:

- `source_identity TEXT` — normalized durable identity used for dedup/merge.
- `is_active INTEGER NOT NULL DEFAULT 1` — whether the source episode is inside the current source/max_episodes window.
- `publication_state TEXT NOT NULL DEFAULT 'placeholder'` — current feed publication state: `placeholder`, `clean`, or `hidden`.
- `last_seen_at TEXT` — poll timestamp when source feed last included this episode.
- `completed_at TEXT` — timestamp set when worker marks completed.

Add idempotent `_migrate()` column adds for existing DBs.

Add indexes:

```sql
CREATE INDEX IF NOT EXISTS idx_episodes_feed_active_pub ON episodes(feed_id, is_active, pub_date);
CREATE INDEX IF NOT EXISTS idx_episodes_source_identity ON episodes(feed_id, source_identity);
```

Do not add a unique index on `source_identity` until migration code can merge existing duplicates safely. Use application-level merge first.

Update `Episode` model with optional/default fields:

```python
source_identity: str | None = None
is_active: bool = True
publication_state: str = "placeholder"
last_seen_at: datetime | None = None
completed_at: datetime | None = None
```

Run:

```bash
uv run pytest tests/test_audio_recovery.py tests/test_feed_poll_alerting.py -q
```

## Task 4: Compute normalized source identity in parser

**Files:**
- Modify: `src/feeds/parser.py`
- Test: `tests/test_feed_identity.py`

Add helpers:

```python
def normalize_audio_url(url: str) -> str:
    # lower scheme/host, strip fragment, strip common tracking query params
    # keep path and meaningful query params if needed
```

```python
def build_source_identity(entry, audio_url: str) -> str:
    # Prefer explicit GUID if present and non-empty.
    # Also store normalized audio URL separately on Episode.source_identity.
    # If GUID is missing/unstable, fallback to normalized audio URL.
    # Final fallback: normalized title + pub date + duration.
```

Pragmatic implementation:
- Keep `Episode.guid` as current source-facing GUID.
- Set `Episode.source_identity` to normalized audio URL when audio URL exists; otherwise stable tuple hash.
- Add tests for:
  - same audio URL with `utm_*`/tracking params normalizes to same identity.
  - missing GUID with same normalized audio URL maps to same identity.
  - different episodes do not collide when title/pub_date differ.

Run:

```bash
uv run pytest tests/test_feed_identity.py -q
```

## Task 5: Replace insert-only polling with merge/update polling

**Files:**
- Modify: `src/database/queries.py`
- Modify: `src/scheduler.py`
- Test: `tests/test_feed_identity.py`

Add query helpers:

```python
def upsert_episode_from_poll(conn, episode: Episode, *, seen_at: datetime) -> tuple[int, bool]:
    """Insert or update an episode seen in the latest poll.

    Returns (episode_id, inserted).
    Match order:
    1. feed_id + source_identity when source_identity is set
    2. feed_id + guid
    3. optional feed_id + normalized title/pub_date/duration fallback
    """
```

On update:
- Preserve processing fields: `status`, `processed_audio_path`, `clean_token`, `ad_segments_json`, `retry_count`, `publication_state` unless source audio truly changed and episode is not completed.
- Refresh metadata: `guid`, `title`, `source_audio_url`, `pub_date`, `duration_seconds`, `description`, `last_seen_at`, `is_active=1`.
- If an old duplicate row exists, merge to a canonical row rather than emit both. Prefer completed/clean row over placeholder/incomplete row; prefer lower id if equal. Hide superseded placeholder rows (`is_active=0` or `publication_state=hidden`).

Add helper:

```python
def mark_feed_poll_visibility(conn, feed_id: int, active_episode_ids: list[int], *, seen_at: datetime) -> int:
    """Set is_active=0 for rows outside the current source/max_episodes window."""
```

Modify `src/scheduler.py:187-198`:
- Collect ids returned by `upsert_episode_from_poll`.
- Call `mark_feed_poll_visibility` after successful parse.
- Only count inserts as new.
- Still update `last_polled_at`.

Tests:

```python
def test_poll_merges_same_episode_when_guid_changes_but_audio_url_normalizes(monkeypatch, conn):
    # First poll returns guid g1 URL with utm=a.
    # Second poll returns guid g2 same URL with utm=b.
    # Assert one row, latest metadata, active=1.
```

```python
def test_poll_hides_rows_absent_from_latest_feed(monkeypatch, conn):
    # Existing two active rows.
    # Latest parse returns one.
    # Assert get visible feed episodes returns one; hidden row remains in DB.
```

## Task 6: Generate RSS from active bounded rows only

**Files:**
- Modify: `src/database/queries.py`
- Modify: `src/api/routes/feeds.py`
- Test: `tests/test_feed_identity.py`

Replace or extend `get_episodes_for_feed`:

```python
def get_visible_episodes_for_feed(conn, feed_id: int, *, limit: int | None = None) -> list[Episode]:
    SELECT * FROM episodes
    WHERE feed_id = ? AND is_active = 1 AND publication_state != 'hidden'
    ORDER BY pub_date DESC, id DESC
    LIMIT ?
```

Update feed route to determine `limit` from `settings.feeds` matching the feed slug. If no config match or `max_episodes <= 0`, use no limit.

Tests:

```python
def test_feed_route_only_emits_active_rows(client): ...
def test_max_episodes_limits_generated_rss_not_just_parser_input(client): ...
```

## Task 7: Fix retention cleanup semantics

**Files:**
- Modify: `src/database/queries.py`
- Modify: `src/scheduler.py`
- Test: `tests/test_feed_identity.py` or `tests/test_cleanup.py`

Change `mark_completed()` to set `completed_at = now`.

Change `_cleanup_old()` query to use `completed_at < cutoff`, not `created_at < cutoff`.

When cleanup resets a completed episode:
- Clear `processed_audio_path`.
- Set status to `new`.
- Decide on `clean_token` policy after tests:
  - Preferred: clear `clean_token` if no file exists so `/audio/clean/{old}.mp3` stops resolving.
  - Preserve the explicit publication transition: after cleanup, no stale clean URL should resolve unless a current clean file exists; generated RSS still emits only one current publication for the source episode.

Add tests:

```python
def test_cleanup_uses_completed_at_not_created_at(): ...
def test_cleanup_clears_stale_clean_token_after_file_removal(): ...
```

## Task 8: Full checks and commit

Run:

```bash
uv run pytest -q
uv run ruff check src tests
git diff --stat
git status --short
git add src tests
git commit -m "fix: stabilize feed episode identity"
```

## Acceptance criteria

- Repeated polls with changed source GUID/tracking URL produce one visible RSS item.
- Completion changes the published RSS GUID to the clean token and removes the placeholder publication from generated RSS.
- `/feeds/{slug}.xml` filters hidden/superseded rows and respects `max_episodes`.
- Cleanup does not resurrect duplicate client-visible identities.
- Existing tests still pass.
