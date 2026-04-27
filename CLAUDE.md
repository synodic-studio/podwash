# podwash

Self-hosted podcast ad-skipping proxy. Fetches RSS feeds, transcribes
episodes with Whisper, detects ads with Claude, cuts them out with
ffmpeg, and re-serves clean RSS feeds.

## Architecture

**Pipeline**: RSS poll → download MP3 → Whisper transcription → Claude
ad classification → ffmpeg ad removal → serve via proxy RSS feed.

**Processing is hybrid**: new episodes discovered during RSS polling
are processed eagerly (immediately after discovery). Older/existing
episodes are processed on-demand when a podcast client requests audio
(via the `/audio/` route).

**Two-host topology**: a thin server image (FastAPI, RSS, queue API,
file serving) is meant to live on a small always-on host. The heavy
pipeline (Whisper + Claude + ffmpeg) runs as a separate worker process
that claims jobs from the server's queue API. Both halves can also run
on a single machine for local development — the worker just polls
`http://localhost:8080`.

### Key Modules

- `src/api/` — FastAPI app, routes (feeds, audio, management, queue)
- `src/pipeline/` — Processing stages: downloader, transcriber, classifier, editor, orchestrator
- `src/feeds/` — RSS parsing (feedparser) and generation (feedgen)
- `src/database/` — SQLite via raw sqlite3, Pydantic models, CRUD queries
- `src/scheduler.py` — APScheduler background jobs (poll every 15min, cleanup every 6hr, watchdog every 5min)
- `src/alerting.py` — Telegram alerter (env-configured, used by worker + watchdog)
- `src/worker/` — Worker loop, preflight, crash wrapper, idle watchdog
- `src/heal.py` — Headless self-heal CLI; spawns `claude -p` to fix the repo
- `src/config.py` — YAML config + env var overrides
- `prompts/ad_detection.txt` — Claude prompt for ad + credits classification
- `src/static/submit.html` — Web UI for submitting feeds

## Running

For local development the server and worker can run side-by-side on
one machine:

```bash
# Local development (auto-reload)
uv run podwash --reload

# Local docker (server image only — exercises Dockerfile + compose)
docker compose up --build -d
docker compose down
```

The server listens on `http://localhost:8080`. To run the worker
locally, set `ANTHROPIC_API_KEY` and `WORKER_TOKEN` in your environment
(or in `pass`), then:

```bash
uv run --extra worker podwash-worker
```

## Deploying

Deployment is push-based: build the image on the developer's Mac,
`docker save | ssh | docker load` on the remote, restart the container.
The remote never pulls from a registry — it only receives artifacts
streamed over SSH.

```bash
PODWASH_REMOTE_SSH=user@host scripts/deploy.sh               # full deploy
PODWASH_REMOTE_SSH=user@host scripts/deploy.sh --skip-tests  # emergency only
```

`scripts/deploy.sh` is fully env-driven — see the comment block at the
top of the script for all variables (`PODWASH_REMOTE_DIR`,
`PODWASH_CONTAINER`, `PODWASH_IMAGE`, `PODWASH_PORT`,
`PODWASH_PUBLIC_HEALTH_URL`).

The remote is expected to already have `config.yml` and `.env` in
place at `$PODWASH_REMOTE_DIR` (defaults to `/opt/podwash`) — they
hold secrets and host-specific values and are intentionally NOT
transferred by the deploy script.

## Key URLs

- `/submit` — Web form for adding new podcast feeds
- `/feeds/{slug}.xml` — Proxy RSS feed (subscribe in podcast app)
- `/audio/{feed_id}/{episode_id}.mp3` — Processed audio endpoint
- `/api/feeds` — JSON list of all feeds (GET) / submit new feed (POST)
- `/api/feeds/{id}/episodes` — Episode list for a feed
- `/api/episodes/{id}/process` — Manually trigger episode processing
- `/health` — Health check

## Configuration

- `config.yml` — main config (gitignored, copy from `config.example.yml`)
- `.env` — `ANTHROPIC_API_KEY` (required for ad classification)
- Env overrides: `BASE_URL`, `DATA_DIR`, `HOST`, `PORT`, `CONFIG_PATH`

### Self-healing alerts

The worker preflight, the wrapper's crash escalation, and the
server-side watchdog all route through `src/alerting.py`. Telegram is
optional — when these env vars are unset, alerts still print to stderr
(launchd `.err` files or `docker logs podwash`), but no Telegram
message is sent.

- `ALERT_TELEGRAM_BOT_TOKEN` — bot API token. If `pass` (password-store)
  is installed, the alerter can also pull this from
  `pass show telegram-bot-token`, which lets the launchd plist stay free
  of plaintext secrets.
- `ALERT_TELEGRAM_CHAT_ID` — target chat (e.g. `-100…` for supergroups)
- `ALERT_TELEGRAM_THREAD_ID` — optional `message_thread_id`

For the macOS worker, `deploy/install-worker.sh` substitutes
`ALERT_TELEGRAM_CHAT_ID` / `ALERT_TELEGRAM_THREAD_ID` into the plist
from `PODWASH_ALERT_CHAT_ID` / `PODWASH_ALERT_THREAD_ID` env vars at
install time.

For a containerized server, put all three env vars in the remote
`.env` (the docker-compose `env_file` picks them up).

Verify the pipe anytime with
`uv run python -m src.alerting --note 'wiring check'` — sends one
clearly-marked test message; the throttle is bypassed.

## Self-healing layers

The recovery posture is **self-heal first, Telegram second** — operator
attention is the expensive resource, a headless Claude Code session is
the cheap one.

1. **Worker preflight (`src/worker/preflight.py`)** — runs on every
   worker start. Verifies `anthropic`/`faster_whisper` import,
   `WORKER_TOKEN`+`ANTHROPIC_API_KEY` are set, server `/health`
   responds, and Anthropic `messages.count_tokens` succeeds. Any
   failure prints a structured `PREFLIGHT_FAILURE` block to stderr and
   exits 1. **Preflight never sends Telegram itself** — escalation
   lives in one place (the wrapper) so we can't double-page.
2. **Crash escalation wrapper (`src/worker/wrapper.py`)** — stdlib-only
   Python module that launchd points at via
   `/usr/bin/python3 -m src.worker.wrapper`. Runs under system Python
   so a broken project venv can't keep the wrapper itself from booting.
   Each boot runs `uv run --extra worker podwash-worker` (so a missing
   dep self-heals on next boot). After ≥`MAX_CRASHES` (default 3)
   crashes in `WINDOW_SECONDS` (default 600) the wrapper:
   - writes the stderr tail + exit code to an incident file under
     `/tmp/podwash-worker-wrapper/`,
   - shells out to `python -m src.heal --incident-file=… --telegram-on-escalate`,
     which spawns headless `claude -p` (with
     `--permission-mode bypassPermissions` so the agent can run Bash
     without prompting, and `--max-budget-usd 5` so a confused agent
     can't burn dollars in a loop) and lets it diagnose + fix in-repo
     (`uv sync`, network checks, config repair, etc.),
   - on `SUCCESS`: clears the crash counter and lets launchd restart
     fresh — no Telegram.
   - on `ESCALATE` / `TIMEOUT` / `UNAVAILABLE`: the heal CLI sends the
     Telegram alert (with the heal agent's notes + transcript path),
     then sleeps `SLEEP_AFTER_BURST_SECONDS` (default 1hr).
   - **Daily cap:** if there are already `PODWASH_HEAL_MAX_PER_DAY`
     (default 6) heal attempts in the last 24h, the next failure
     escalates directly to Telegram instead of spawning Claude —
     something is genuinely flapping.
3. **Mac-side idle watchdog (`src/worker/idle_watchdog.py`)** —
   separate launchd job (`StartInterval=300`) that runs as a one-shot
   every 5 min. The crash wrapper only catches *crashing* workers;
   this catches *alive-but-stuck* workers (hung HTTP, deadlocked
   Whisper, mis-claimed token, network partition). Each tick it hits
   the server's `GET /api/jobs/queue/health` endpoint, applies the
   same "pending > 0 AND silent ≥30 min" rule the server-side watchdog
   uses, and on trip writes an incident and shells out to
   `python -m src.heal --telegram-on-escalate`. Stdlib-only under
   `/usr/bin/python3` so a broken project venv can't keep the watchdog
   from booting. Per-process cooldown (default 30 min,
   `IDLE_COOLDOWN_SECONDS`) keeps it from re-firing every tick during
   an outage; the heal CLI's daily cap still applies on top.
4. **Server-side watchdog (`_watchdog` in `src/scheduler.py`)** — runs
   every 5 min on the server as the **backstop**. By the time it
   fires, both the worker AND the Mac-side idle watchdog have failed
   to act, which usually means the worker host itself is offline. Also
   fires on stale in-flight claim resets and terminal-failure
   bursts — those represent a different failure class (data/pipeline
   rot, not worker-host liveness) and stay Telegram-direct.

Alerts are throttled per `(subsystem, kind)` to one per hour (state in
`/tmp/podwash-alerts/`), so a long outage doesn't spam. Self-heal
transcripts are kept in `~/Library/Logs/podwash-worker/heal/` for
after-the-fact review.

## Dependencies

Python 3.12, managed by uv. Key deps: FastAPI, faster-whisper,
anthropic, feedparser, feedgen, ffmpeg (system).

## Database

SQLite at `{data_dir}/podwash.db`. Tables: `feeds`, `episodes`,
`processing_log`. No migrations system — schema is created via
`CREATE TABLE IF NOT EXISTS` in `src/database/db.py`.

## Testing

```bash
uv run pytest
```

Linting: `uv run ruff check src/`

## Data Flow

1. Scheduler polls RSS feeds, inserts new episodes as `pending`.
2. New episodes are eagerly processed right after discovery
   (download → transcribe → classify → cut).
3. Older episodes are processed on-demand when a podcast client
   requests `/audio/{feed_id}/{episode_id}.mp3`.
4. If not yet processed: serves a short "processing" clip and triggers
   the pipeline in the background.
5. Pipeline: download → transcribe (Whisper) → classify ads+credits
   (Claude) → cut (ffmpeg).
6. Next request serves the cleaned audio.
7. Intermediate files (original MP3, transcript) are cleaned up after
   processing.
8. Processed audio is cleaned up after `retention_days` (default 30);
   the episode resets to `pending` and will reprocess on next request.
