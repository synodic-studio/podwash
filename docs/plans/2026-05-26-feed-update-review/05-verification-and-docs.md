# Final Verification and Docs Plan

> **For Claude Code:** Run after plans 01-04. This is the release-readiness pass.

**Goal:** Prove the fix set works end-to-end, update operator docs, and leave the repo clean.

## Task 1: Run full quality gates

From repo root:

```bash
uv run pytest -q
uv run ruff check src tests
git status --short
git log --oneline -5
```

Expected:
- pytest passes.
- ruff passes.
- only expected docs/code changes are present, or everything is already committed.

If tests fail, do not paper over them. Fix the root cause or revert the specific broken change.

## Task 2: Add/verify feed behavior integration test

**File:** `tests/test_feed_identity.py`

Ensure there is one high-level test that simulates the actual bug:

```python
def test_repeated_polls_completion_and_guid_change_do_not_emit_duplicate_rss_items(...):
    # 1. Create feed.
    # 2. First poll: episode guid g1, audio URL with tracking A.
    # 3. Generate RSS: one placeholder item with source guid.
    # 4. Mark completed: clean_token URL/guid appears and placeholder guid disappears.
    # 5. Second poll: same episode guid g2, audio URL with tracking B.
    # 6. Generate RSS: still one visible item.
    # 7. Assert DB may retain hidden rows only if merge required, but visible RSS has one item.
```

Run:

```bash
uv run pytest tests/test_feed_identity.py -q
```

## Task 3: Update docs for new feed semantics

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`

Update the existing **Feed Status Markers** section. Replace the old claim that both GUID and URL change with:

- Completion intentionally publishes the cleaned audio as a new RSS item with clean-token GUID and `/audio/clean/{uuid}.mp3`.
- The placeholder item must disappear from generated RSS once the clean item is ready.
- Status prefix changes from `○`/`◐` to `●` on the current clean item.
- Feed generation hides rows no longer active in the current source feed window.
- `max_episodes` limits visible RSS output.
- Podcast clients may still retain old played items in history/archive, but Podwash should no longer publish placeholder and clean items at the same time.

Also document any new config/env vars:
- `ADMIN_TOKEN` or chosen admin token name.
- `WORKER_MAX_UPLOAD_MB` or chosen upload limit name.
- Any deploy port semantics (`PODWASH_PORT` host port vs container port).

## Task 4: Add operator recovery notes

**File:** `README.md` or `docs/operations.md`

Add a short section:

```markdown
## Duplicate/stale feed recovery

1. Run `uv run python scripts/...` or sqlite query to inspect duplicate identities.
2. Confirm `/feeds/{slug}.xml` emits only active rows.
3. If old duplicate rows exist, mark `is_active=0`; do not delete completed audio unless intentionally reclaiming disk.
4. Restart/redeploy server and force client refresh.
```

If no admin script exists, include explicit sqlite snippets only if safe and tested.

## Task 5: Manual smoke commands

Run locally if feasible:

```bash
uv run podwash --help
uv run python - <<'PY'
from src.config import Settings
from src.api.app import create_app
app = create_app(Settings())
print('app ok', bool(app))
PY
```

If a local server smoke is cheap:

```bash
uv run podwash --reload
# In another shell, curl /health and one generated feed fixture if available.
```

Do not leave background servers running.

## Task 6: Final commit or status

If docs changed after implementation commits:

```bash
git add CLAUDE.md README.md docs tests src scripts deploy
git commit -m "docs: document feed update and operations semantics"
```

Final status:

```bash
git status --short
git log --oneline -8
```

## Acceptance criteria

- Feed duplicate regression is covered by tests.
- Docs no longer say clean completion changes RSS GUID.
- New security/deploy env vars are documented.
- Full tests/lint pass.
- Repo is clean or contains only intentionally uncommitted work clearly listed.
