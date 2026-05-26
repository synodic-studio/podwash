# Podwash Code Review Findings

> Review date: 2026-05-26. Repo: `/Users/bryancostanza/Developer/podwash`.

## Baseline verified

```bash
uv run pytest -q
# 77 passed in 3.68s

uv run ruff check src tests
# All checks passed!
```

## Critical / user-visible

a. **Placeholder and clean publications are not modeled as mutually exclusive**
   - Files: `src/feeds/generator.py:63-76`, `src/database/queries.py:346-374`, `CLAUDE.md:92-100`.
   - Intended behavior: incomplete items use `episode.guid`; completed items use `episode.clean_token` as RSS `<guid>` so the clean version appears as a new episode.
   - Bug: Podwash does not reliably remove/hide the placeholder publication from generated RSS when the clean publication becomes current, especially when duplicate/stale rows exist.
   - Fix plan: `01-feed-identity-and-stale-pruning.md`.

b. **Dedup only keys on `(feed_id, guid)`; unstable source GUIDs/URLs create permanent duplicate rows**
   - Files: `src/database/db.py:20-40`, `src/database/queries.py:89-113`, `src/feeds/parser.py:39`.
   - Current behavior: `parse_feed()` picks `entry.id or entry.link or audio_url`; `upsert_episode()` only `INSERT OR IGNORE`s by `feed_id + guid`.
   - Impact: if source feed GUID/link/tracking URL changes for the same episode, Podwash inserts another row, which can keep a placeholder visible after the clean publication should have replaced it.
   - Fix plan: normalized source identity + merge/update polling in `01-feed-identity-and-stale-pruning.md`.

c. **Polling never updates/prunes existing rows; RSS emits every historical row**
   - Files: `src/scheduler.py:187-198`, `src/database/queries.py:116-122`, `src/api/routes/feeds.py:22-28`.
   - Current behavior: polling only inserts new rows and feed output emits all rows for a feed.
   - Impact: any duplicate or stale row that ever enters the DB remains visible indefinitely.
   - Fix plan: active/current window + visible feed query in `01-feed-identity-and-stale-pruning.md`.

d. **`max_episodes` limits parsing, not generated RSS**
   - Files: `src/scheduler.py:179-187`, `src/feeds/parser.py:57-60`, `src/database/queries.py:116-122`.
   - Impact: configured feed window does not limit visible RSS once historical rows are in DB.
   - Fix plan: apply `max_episodes` to `get_visible_episodes_for_feed()` / feed route in `01-feed-identity-and-stale-pruning.md`.

## Correctness / race conditions

a. **Stale worker result/fail can overwrite newer work**
   - Files: `src/api/routes/jobs.py:85-139`, `src/database/queries.py:346-419`.
   - Current behavior: result/fail only requires the shared worker token and episode id; it does not verify claim ownership/current claim attempt.
   - Impact: a stale worker can upload an old result after claim reset or mark a completed episode failed.
   - Fix plan: `02-worker-claim-ownership.md`.

b. **Retention cleanup uses `created_at` and leaves stale `clean_token`**
   - Files: `src/scheduler.py:234-264`.
   - Impact: old episodes completed recently can be reset earlier than intended; reprocessing can churn clean publication URLs/tokens.
   - Fix plan: add `completed_at`, cleanup by completion time, clear stale token if file removed in `01-feed-identity-and-stale-pruning.md`.

c. **Idle watchdog can crash on timezone-aware timestamps**
   - File: `src/worker/idle_watchdog.py:83-91`.
   - Impact: `datetime.now()` naive minus parsed aware ISO timestamp raises `TypeError`.
   - Fix plan: `04-worker-pipeline-deploy-reliability.md`.

## Security / hardening

a. **Management API is unauthenticated**
   - Files: `src/api/routes/management.py:62-229`, `src/api/app.py:58-62`.
   - Impact: if exposed, anyone can add/delete feeds, trigger work, inspect logs, and use feed submission as a fetch primitive.
   - Fix plan: admin token auth in `03-api-and-file-safety.md`.

b. **Feed submission is SSRF-prone and sync-blocking**
   - Files: `src/api/routes/management.py:39-104`, `src/feeds/parser.py:24-30`.
   - Impact: arbitrary URL fetch with no private-IP block, explicit timeout, size cap, or async-safe fetch.
   - Fix plan: safe feed fetcher in `03-api-and-file-safety.md`.

c. **Audio serving and cleanup trust DB paths**
   - Files: `src/api/routes/audio.py:38-44`, `src/api/routes/audio.py:73-80`, `src/scheduler.py:253-258`.
   - Impact: corrupted/compromised DB path can escape `data_dir` for file serving or unlink.
   - Fix plan: central safe path resolver in `03-api-and-file-safety.md`.

d. **Worker upload is unbounded**
   - File: `src/api/routes/jobs.py:85-117`.
   - Impact: token-bearing buggy/compromised worker can fill disk; zero-byte/invalid uploads can be marked completed.
   - Fix plan: size/content/temp-file validation in `03-api-and-file-safety.md`.

e. **Worker token compare is not constant-time**
   - File: `src/api/routes/jobs.py:32-42`.
   - Impact: low practical risk, easy hardening.
   - Fix plan: `secrets.compare_digest()` in `03-api-and-file-safety.md`.

## Reliability / deploy

a. **Default tests may depend on worker extras**
   - Files: `pyproject.toml`, `tests/test_preflight.py`.
   - Impact: thin server/dev env can fail tests if real `anthropic` / `faster_whisper` imports are required.
   - Fix plan: hermetic preflight tests in `04-worker-pipeline-deploy-reliability.md`.

b. **Non-default deploy port is likely broken**
   - File: `scripts/deploy.sh`.
   - Impact: `PODWASH_PORT=9090` maps host 9090 to container 9090 while app likely still listens on 8080.
   - Fix plan: clarify host/container port semantics in `04-worker-pipeline-deploy-reliability.md`.

c. **Deploy lacks rollback after old container removal**
   - File: `scripts/deploy.sh`.
   - Impact: failed health check can leave service down.
   - Fix plan: rollback/blue-green in `04-worker-pipeline-deploy-reliability.md`.

d. **ffmpeg copy branches and ffprobe errors are underchecked**
   - File: `src/pipeline/editor.py`.
   - Impact: misleading downstream failures or success with missing/zero-byte output.
   - Fix plan: return-code/output-size checks in `04-worker-pipeline-deploy-reliability.md`.

e. **install-worker plist templating is fragile**
   - File: `deploy/install-worker.sh`.
   - Impact: sed replacement can corrupt values containing `&`, backslashes, or delimiter-like characters.
   - Fix plan: Python/plist-safe templating + `plutil -lint` in `04-worker-pipeline-deploy-reliability.md`.

## Suggested priority

1. Feed identity/stale pruning.
2. Worker claim ownership.
3. API/file safety.
4. Pipeline/deploy reliability.
5. Docs/verification.
