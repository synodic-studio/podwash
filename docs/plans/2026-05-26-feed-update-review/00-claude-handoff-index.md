# Podwash Feed Update + Whole-Repo Fix Plan

> **For Claude Code:** Execute this plan directory in order. Make code changes, add regression tests, run checks, and commit in small coherent commits. Do not ask Adrien for choices unless a test proves two strategies are mutually exclusive.

**Goal:** Stop podcast-client-visible duplicate/stale feed entries, then harden the queue/API/deploy paths found during review.

**Root cause summary:** Completion is supposed to publish the cleaned audio as a new RSS item while removing the placeholder item from the feed. The failure is that Podwash lacks a durable internal source identity/current-publication model: DB dedup only uses `(feed_id, guid)` (`src/database/db.py:20-40`, `src/database/queries.py:89-113`), polling never updates/prunes existing rows (`src/scheduler.py:187-198`), and `/feeds/{slug}.xml` emits every historical row (`src/api/routes/feeds.py:22-28`). Once a placeholder/duplicate/stale row exists, feed refreshes keep surfacing it.

**Tech stack:** Python 3.12, FastAPI, SQLite/raw `sqlite3`, Pydantic, feedparser, pytest, uv, ruff.

## Baseline before changing anything

Run from repo root:

```bash
cd /Users/bryancostanza/Developer/podwash
git status --short
uv run pytest -q
uv run ruff check src tests
```

Expected current baseline from Hermes review: `77 passed`, `All checks passed!`, clean git status.

## Files in this plan set

- `00-claude-handoff-index.md` — execution order and acceptance criteria.
- `01-feed-identity-and-stale-pruning.md` — user-visible duplicate fix.
- `02-worker-claim-ownership.md` — stale worker race fix.
- `03-api-and-file-safety.md` — admin auth, SSRF, path, upload hardening.
- `04-worker-pipeline-deploy-reliability.md` — worker/editor/deploy reliability.
- `05-verification-and-docs.md` — final gates and docs update.
- `06-code-review-findings.md` — concise review findings with file references.

## Ordered implementation sequence

a. **Fix feed identity/update semantics first**
   - Plan: `01-feed-identity-and-stale-pruning.md`
   - This is the user-visible duplicate issue.
   - Commit: `fix: stabilize feed episode identity`

b. **Fix queue claim ownership races**
   - Plan: `02-worker-claim-ownership.md`
   - Prevents stale workers from regenerating clean tokens or overwriting newer results.
   - Commit: `fix: reject stale worker job completions`

c. **Add API/path/security hardening**
   - Plan: `03-api-and-file-safety.md`
   - Adds admin auth, SSRF controls, safe path resolution, upload limits.
   - Commit: `fix: harden management and file-serving APIs`

d. **Fix worker/pipeline/deploy reliability issues**
   - Plan: `04-worker-pipeline-deploy-reliability.md`
   - Addresses ffmpeg error handling, timezone watchdog bug, deploy port/rollback, hermetic tests.
   - Commit: `fix: harden worker pipeline and deploy checks`

e. **Final regression pass and docs update**
   - Plan: `05-verification-and-docs.md`
   - Commit: `docs: document feed update and operations semantics`

## Cross-plan acceptance criteria

- `/feeds/{slug}.xml` emits exactly one current-publication item per source episode after repeated polls with changed source GUID/tracking URL.
- Completion intentionally publishes a cleaned item with a fresh GUID/clean URL and removes/hides the placeholder item from generated RSS.
- `max_episodes` limits generated RSS visibility/currentness, not just parser input.
- Stale worker result/fail submissions cannot mutate episodes after claim reset or another worker completion.
- Management mutation endpoints are protected by admin token.
- Audio serving/cleanup cannot read/unlink paths outside `settings.data_dir`.
- `uv run pytest -q` passes in a default dev environment without worker extras.
- `uv run ruff check src tests` passes.
- `git status --short` is clean except for intentional committed changes.
