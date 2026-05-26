# API and File Safety Implementation Plan

> **For Claude Code:** Use TDD. Keep public podcast consumption routes public; protect management and internal mutation routes.

**Goal:** Harden Podwash against unauthenticated mutation, SSRF, unsafe DB file paths, and unbounded uploads.

**Architecture:** Add centralized auth dependencies and safety utilities. Reuse them in routes and cleanup rather than sprinkling ad-hoc checks.

## Current evidence

- Management routes are unauthenticated: `src/api/routes/management.py:62-229`, included in `src/api/app.py:58-62`.
- `POST /api/feeds` fetches arbitrary URLs with `feedparser.parse(url)` in an async route and no SSRF/timeout controls: `src/api/routes/management.py:84-104`, Apple lookup at `src/api/routes/management.py:39-59`.
- Audio serving trusts `processed_audio_path`: `src/api/routes/audio.py:38-44`, `src/api/routes/audio.py:73-80`.
- Cleanup unlinks `Path(settings.data_dir) / path_str` without containment: `src/scheduler.py:253-258`.
- Worker token comparison is normal string comparison: `src/api/routes/jobs.py:32-42`.
- Upload route streams unbounded content to disk: `src/api/routes/jobs.py:85-117`.
- `_slugify()` can return empty slug: `src/api/routes/management.py:28-33`.

## Task 1: Add admin token auth for management endpoints

**Files:**
- Modify: `src/config.py`
- Create/modify: `src/api/auth.py`
- Modify: `src/api/routes/management.py`
- Test: `tests/test_management_auth.py`

Config:
- Add `settings.admin.token` or `settings.server.admin_token` with env override `ADMIN_TOKEN` / `PODWASH_ADMIN_TOKEN`.
- If unset, mutation endpoints should be disabled or return 503. Prefer explicit 503 for mutation endpoints to avoid accidental open admin.

Auth utility:

```python
import secrets
from fastapi import Header, HTTPException, Request

def require_admin_token(request: Request, authorization: str | None = Header(None)) -> None:
    expected = request.app.state.settings.admin.token
    if not expected:
        raise HTTPException(status_code=503, detail="Admin API disabled")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    if not secrets.compare_digest(authorization.removeprefix("Bearer ").strip(), expected):
        raise HTTPException(status_code=403, detail="Invalid admin token")
```

Apply to:
- `GET /api/feeds` if it exposes management metadata.
- `POST /api/feeds`.
- `DELETE /api/feeds/{feed_id}`.
- `GET /api/feeds/{feed_id}/episodes`.
- `POST /api/episodes/{episode_id}/process`.
- `GET /api/episodes/{episode_id}/logs`.

Do **not** protect:
- `/feeds/{slug}.xml`.
- `/audio/...`.
- `/health`.

Tests:

```python
def test_post_feed_requires_admin_token(): ...
def test_invalid_admin_token_does_not_create_feed(): ...
def test_valid_admin_token_allows_management_request(): ...
def test_public_rss_and_audio_routes_stay_public(): ...
```

Run:

```bash
uv run pytest tests/test_management_auth.py -q
```

## Task 2: Make worker token constant-time

**File:** `src/api/routes/jobs.py`

Replace string equality with `secrets.compare_digest()`.

Tests can live in `tests/test_job_claims.py` or `tests/test_jobs_auth.py`:

```python
def test_worker_token_missing_wrong_correct(): ...
```

Run:

```bash
uv run pytest tests/test_jobs_auth.py -q
```

## Task 3: Centralize safe path resolution

**Files:**
- Create: `src/safe_paths.py` or `src/storage.py`
- Modify: `src/api/routes/audio.py`
- Modify: `src/scheduler.py`
- Test: `tests/test_safe_paths.py`

Implement:

```python
from pathlib import Path

class UnsafeRelativePath(ValueError):
    pass

def resolve_under_data_dir(data_dir: str | Path, relative_path: str) -> Path:
    root = Path(data_dir).resolve()
    rel = Path(relative_path)
    if rel.is_absolute():
        raise UnsafeRelativePath("absolute paths are not allowed")
    candidate = (root / rel).resolve()
    if not candidate.is_relative_to(root):
        raise UnsafeRelativePath("path escapes data dir")
    return candidate
```

Use it in:
- `get_clean_audio()`.
- `get_audio()`.
- `_cleanup_old()`.

Behavior:
- Unsafe audio path: log/alert, reset row to `new` or return 404, but never serve outside file.
- Unsafe cleanup path: skip unlink, alert/log, do not unlink outside data dir.

Tests:

```python
def test_audio_route_rejects_dotdot_processed_path(app): ...
def test_audio_route_rejects_absolute_processed_path(app): ...
def test_cleanup_does_not_unlink_outside_data_dir(tmp_path): ...
def test_safe_path_accepts_normal_feed_episode_path(tmp_path): ...
```

Run:

```bash
uv run pytest tests/test_safe_paths.py tests/test_audio_recovery.py -q
```

## Task 4: Add SSRF-safe feed fetching

**Files:**
- Create: `src/net_safety.py` or `src/feeds/fetcher.py`
- Modify: `src/api/routes/management.py`
- Modify: `src/feeds/parser.py` only if useful to parse fetched bytes/text.
- Test: `tests/test_feed_submission_safety.py`

Requirements:
- Accept only `http` and `https` schemes.
- Reject `localhost`, loopback, link-local, private, multicast, unspecified, and metadata IPs (`169.254.169.254` included).
- Resolve DNS before fetching; validate all resolved addresses.
- Use `httpx.AsyncClient(timeout=httpx.Timeout(10.0), follow_redirects=True)`.
- Revalidate redirected final URL host/IP if possible.
- Enforce max feed body size, e.g. 5 MB.
- Pass fetched bytes/text to `feedparser.parse()` instead of letting feedparser fetch arbitrary URLs.

Implementation shape:

```python
async def fetch_public_feed(url: str, *, max_bytes: int = 5_000_000) -> bytes:
    parsed = validate_public_http_url(url)
    await validate_public_hostname(parsed.hostname)
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        async with client.stream("GET", url, headers={"User-Agent": "Podwash/1.0"}) as response:
            response.raise_for_status()
            chunks = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise FeedFetchError("feed too large")
                chunks.append(chunk)
            return b"".join(chunks)
```

Tests:

```python
def test_rejects_file_url(): ...
def test_rejects_localhost_url(): ...
def test_rejects_private_ip_url(): ...
def test_rejects_metadata_ip_url(): ...
def test_rejects_oversized_feed(monkeypatch): ...
def test_valid_public_feed_is_parsed_from_fetched_bytes(monkeypatch): ...
```

## Task 5: Bound worker result uploads

**Files:**
- Modify: `src/config.py`
- Modify: `src/api/routes/jobs.py`
- Test: `tests/test_job_upload_limits.py`

Config:
- Add `settings.worker.max_upload_mb` default, e.g. 500 MB.

Route behavior:
- Validate content type if provided: allow `audio/mpeg`, `audio/mp3`, `application/octet-stream` only if magic/ffprobe later confirms; start pragmatic with size limit.
- Stream chunks and track total bytes.
- If over limit, delete temp file and return 413.
- If final file is zero bytes, return 400 and do not mark completed.
- Use the claim-token temp file flow from `02-worker-claim-ownership.md` if already implemented.

Tests:

```python
def test_upload_over_limit_returns_413_and_does_not_complete(): ...
def test_zero_byte_upload_returns_400_and_does_not_complete(): ...
def test_interrupted_invalid_upload_does_not_overwrite_existing_processed_file(): ...
```

## Task 6: Fix empty slug generation

**File:** `src/api/routes/management.py`

Change `_slugify(title)` so empty slug gets a safe fallback:
- Prefer hostname from source URL if available.
- Else `feed` plus a suffix or id after insert.

Tests:

```python
def test_emoji_punctuation_title_gets_non_empty_slug(): ...
def test_duplicate_fallback_slugs_get_unique_suffixes(): ...
```

## Task 7: Full checks and commit

Run:

```bash
uv run pytest -q
uv run ruff check src tests
git diff --stat
git add src tests config.example.yml .env.example README.md CLAUDE.md
git commit -m "fix: harden management and file-serving APIs"
```

Update examples/docs with new env vars:
- `ADMIN_TOKEN` / chosen name.
- `WORKER_MAX_UPLOAD_MB` if added.

## Acceptance criteria

- Unauthenticated management mutation fails.
- Public RSS/audio remain public.
- Feed submission cannot fetch local/internal URLs and has time/size limits.
- Unsafe DB paths never serve/delete outside data dir.
- Oversized/empty uploads do not complete episodes.
- Worker/admin token comparisons use `secrets.compare_digest()`.
