# Worker, Pipeline, and Deploy Reliability Implementation Plan

> **For Claude Code:** Use TDD. Keep these fixes pragmatic and local.

**Goal:** Remove reliability traps found in worker/pipeline/deploy review after feed and API fixes land.

## Current evidence

- `uv run pytest` currently passes locally, but `tests/test_preflight.py` asserts real worker extras imports; clean thin-server envs may fail without worker extras.
- Deploy maps `${PODWASH_PORT}:${PODWASH_PORT}` but does not pass `PORT=${PODWASH_PORT}` into the container: `scripts/deploy.sh:105-115`.
- Deploy removes old container before new health passes: `scripts/deploy.sh:100-125`.
- `src/pipeline/editor.py` copy/no-ad branches do not check ffmpeg return codes or output size.
- `_get_duration()` ignores ffprobe return code/stderr and raises unhelpful `ValueError` on empty stdout.
- `src/worker/idle_watchdog.py:83-91` can subtract aware `last_claim_at` from naive `now` and crash.
- `deploy/install-worker.sh:56-65` uses fragile sed substitution without escaping/plist validation.
- Pipeline and worker client direct unit coverage is thin.

## Task 1: Make preflight import tests hermetic

**Files:**
- Modify: `tests/test_preflight.py`
- Maybe modify: `src/worker/preflight.py` only if testability needs a tiny seam.

Problem: test should not require actual `anthropic` / `faster_whisper` installation in thin server/dev env.

Implementation:
- Monkeypatch `builtins.__import__` or `importlib.import_module` to simulate modules present/missing.
- Keep one missing-import test.
- Keep preflight production behavior unchanged.

Run:

```bash
uv run pytest tests/test_preflight.py -q
```

Acceptance: `uv run pytest -q` passes without installing worker extras.

## Task 2: Fix idle watchdog aware/naive timestamp crash

**Files:**
- Modify: `src/worker/idle_watchdog.py`
- Modify: `tests/test_idle_watchdog.py`

Add tests:

```python
def test_evaluate_handles_timezone_aware_last_claim_at():
    now = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
    old = "2026-04-27T10:00:00+00:00"
    should, reason = idle_watchdog.evaluate(_snapshot(pending=1, last_claim_at=old), no_claim_minutes=30, now=now)
    assert should is True
```

```python
def test_evaluate_handles_aware_last_claim_with_naive_now():
    # Should not raise TypeError. Normalize to UTC.
```

Implementation:
- Normalize parsed timestamps and `now` to UTC-aware before subtraction.
- Catch both `ValueError` and `TypeError` defensively.

Run:

```bash
uv run pytest tests/test_idle_watchdog.py -q
```

## Task 3: Harden editor ffmpeg/ffprobe handling

**Files:**
- Modify: `src/pipeline/editor.py`
- Create: `tests/test_editor.py`

Tests:

```python
async def test_cut_ads_no_ad_copy_raises_on_ffmpeg_failure(monkeypatch, tmp_path): ...
async def test_cut_ads_all_content_copy_raises_on_ffmpeg_failure(monkeypatch, tmp_path): ...
def test_compute_keep_segments_bounds_and_overlaps(): ...
async def test_get_duration_raises_descriptive_error_on_ffprobe_failure(monkeypatch): ...
async def test_get_duration_rejects_empty_or_nonpositive_output(monkeypatch): ...
```

Implementation:
- In every ffmpeg branch, check `proc.returncode`.
- Include stderr text in `RuntimeError`.
- After ffmpeg success, verify output exists and `stat().st_size > 0`.
- `_get_duration()` should:
  - check return code,
  - include stderr/stdout on failure,
  - parse float safely,
  - reject non-positive/non-finite duration.

Run:

```bash
uv run pytest tests/test_editor.py -q
```

## Task 4: Add classifier/downloader/client smoke tests

**Files:**
- Create: `tests/test_classifier.py`
- Create: `tests/test_downloader.py`
- Create/modify: `tests/test_worker_client.py`

Classifier tests:
- fenced JSON response parses.
- extra prose around JSON parses.
- malformed/no JSON raises clear error.

Downloader tests:
- HTTP non-200 raises.
- streaming failure cleans partial file or raises clearly.
- parent dirs are created.

QueueClient tests (coordinate with `02-worker-claim-ownership.md`):
- 204 from `/next` returns `None`.
- invalid JSON/schema fails clearly.
- result/failure include auth headers, worker id, and claim token.

Run:

```bash
uv run pytest tests/test_classifier.py tests/test_downloader.py tests/test_worker_client.py -q
```

## Task 5: Fix deploy non-default port handling

**File:** `scripts/deploy.sh`

Decide one explicit semantic:
- Recommended: `PODWASH_PORT` is host/public port; container always listens on `8080` unless `CONTAINER_PORT` is set.

Implementation option:

```bash
HOST_PORT="${PODWASH_PORT:-8080}"
CONTAINER_PORT="${PODWASH_CONTAINER_PORT:-8080}"
# docker run -p "${HOST_PORT}:${CONTAINER_PORT}" -e PORT="${CONTAINER_PORT}"
```

Update comments at top of script.

Add a lightweight test script if there is no shell test framework:
- Create `tests/test_deploy_script.py` that reads `scripts/deploy.sh` and/or runs it with a fake `ssh` script in `PATH` to capture remote docker commands.
- Assert non-default `PODWASH_PORT=9090` either maps `9090:8080` or passes `PORT=9090` consistently.

Run:

```bash
uv run pytest tests/test_deploy_script.py -q
```

## Task 6: Add deploy rollback or blue-green swap

**File:** `scripts/deploy.sh`

Current issue: old container is stopped/removed before new health passes.

Pragmatic fix:
- Before stopping old container, record old image id/name:

```bash
OLD_IMAGE=$(docker inspect --format='{{.Image}}' "$CONTAINER" 2>/dev/null || true)
```

- Start new container with temp name and temp host port if possible, health-check it, then swap.
- If temp port is too complex, at least rollback on failure:
  1. stop/remove old,
  2. start new,
  3. if health fails, stop/remove new and restart old image/container config.

Preferred blue-green shape:
- `NEW_CONTAINER="${CONTAINER}-next"`
- Run new on `${HOST_PORT}` only after old stopped, but keep old image/config available.
- On health failure, restart old image.

Tests:
- Fake remote `docker` and `curl` commands so health fails.
- Assert script attempts to restart old image/container.

Run:

```bash
uv run pytest tests/test_deploy_script.py -q
```

## Task 7: Replace fragile sed plist templating

**File:** `deploy/install-worker.sh`

Problem: sed replacement can corrupt values containing `&`, backslashes, or delimiters.

Implementation:
- Use Python to read template and replace literal placeholders safely:

```bash
python3 - "$TEMPLATE" "$DEST" <<'PY'
# read os.environ values; str.replace placeholders; write output
PY
```

- Run `plutil -lint "$DEST"` on macOS if `plutil` exists.
- Abort if lint fails.

Tests:
- Add a shell/Python test that renders with `PODWASH_WORKER_SERVER_URL='https://example.com/a?b=1&c=2'` and asserts output includes literal `&` correctly escaped for XML/plist if needed.
- If test environment lacks `plutil`, test should skip plist lint assertion.

Run:

```bash
uv run pytest tests/test_install_worker.py -q
```

## Task 8: Full checks and commit

Run:

```bash
uv run pytest -q
uv run ruff check src tests
git diff --stat
git add src tests scripts deploy CLAUDE.md README.md
git commit -m "fix: harden worker pipeline and deploy checks"
```

## Acceptance criteria

- Default test suite does not require worker extras.
- Idle watchdog handles timezone-aware timestamps.
- ffmpeg/ffprobe failures produce clear errors and do not report success with missing/zero-byte output.
- Deploy works with non-default host port.
- Failed deploy attempts leave a rollback path or restore old service.
- install-worker plist rendering survives special characters and validates output where possible.
