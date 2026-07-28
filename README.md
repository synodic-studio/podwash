# podwash

Self-hosted podcast ad-skipping proxy. Subscribe to podcast feeds,
transcribe episodes with Whisper, identify ads with Claude, cut them
with ffmpeg, and re-serve clean RSS feeds to your podcast app of choice.

> **Status:** works for me. APIs and config may still shift.

## How it works

```
RSS feed ─► download MP3 ─► Whisper transcribe ─► Claude classify ─► ffmpeg cut ─► clean RSS
```

- Polls subscribed RSS feeds for new episodes.
- New episodes are processed eagerly (download → transcribe → classify
  → cut). Older episodes are processed on-demand when your podcast app
  requests them.
- Until an episode is processed, podwash serves a short placeholder
  clip and queues the work. When processing finishes, the episode gets
  a new unique URL in the feed so your podcast client re-downloads the
  real audio instead of replaying the cached placeholder.
- Re-publishes each podcast as a proxy RSS feed at
  `/feeds/{slug}.xml`. Subscribe to that URL in your podcast app
  instead of the original feed.

The architecture is two-process:

- **Server** (Python, FastAPI) — RSS polling, RSS generation, queue
  API, file serving. Designed to run as a small Docker container on
  any always-on host.
- **Worker** (Python, optional `worker` extra) — does the heavy
  lifting (Whisper transcription, Claude classification, ffmpeg
  editing). Pulls jobs from the server's queue API.

Both halves can run on the same machine for local use, or on separate
machines (e.g. a small VPS for the server, a Mac at home for the
worker that has more CPU/RAM than you want to pay for in the cloud).

## Feed status markers

Episode titles in the proxy feed are prefixed with a status symbol:

- `○` — not yet processed; tap in your app to queue it
- `◐` — currently downloading / transcribing / classifying / editing
- `●` — **done, ad-free audio is ready**
- `✘` — failed; tap to retry
- `🧼` — prefix on the channel title (not an episode status)

When an episode completes, podwash keeps the source episode's RSS
`<guid>` stable so podcast apps update the existing row instead of
showing a stale `○` placeholder next to a fresh `●` clean item. The
enclosure URL changes to `/audio/clean/{token}.mp3`, and the placeholder
enclosure disappears from the generated feed — only the current cleaned
publication remains visible. Old played placeholder items may still live
in the client's local history/archive.

Feed identity is durable: source GUID or tracking-URL changes don't
create duplicate items. Polls maintain an active window (`max_episodes`
limits what's emitted), and rows the source no longer surfaces drop
out of the visible RSS. If the placeholder clip plays all the way
through before processing finishes, some apps auto-archive the
episode — find the finished version in your archive/history.

## Quick start (local, single machine)

Requirements: Python 3.12, [`uv`](https://github.com/astral-sh/uv),
`ffmpeg`, an Anthropic API key.

```bash
git clone https://github.com/synodic-studio/podwash.git
cd podwash

cp config.example.yml config.yml   # edit feeds + base_url
cp .env.example .env               # then put your Anthropic API key in .env

# Server (one terminal)
uv run podwash --reload

# Worker (another terminal — installs Whisper + anthropic on first run).
# Set ANTHROPIC_API_KEY in your environment first; WORKER_TOKEN is any
# shared secret the server and worker agree on.
WORKER_TOKEN="$(uuidgen)" uv run --extra worker podwash-worker
```

Then open `http://localhost:8080/submit` to add a feed, or subscribe
to an existing slug at `http://localhost:8080/feeds/{slug}.xml` from
your podcast app.

> **Heads up:** Whisper models download on first run (a few hundred MB
> for `base`). The default model is `base` with `int8` compute — tweak
> in `config.yml` under `processing` for better accuracy or faster
> processing.

## Run the server in Docker

`docker-compose.yml` ships a minimal server image. The worker is
deliberately not containerized here — it needs a beefier host.

```bash
cp config.example.yml config.yml   # edit
cp .env.example .env               # ANTHROPIC_API_KEY=...
docker compose up --build -d
```

Subscribe at `http://localhost:8080/feeds/{slug}.xml`.

## Configuration

Two files, both gitignored:

- `config.yml` — feeds list, processing settings, Claude/Whisper
  parameters. See `config.example.yml` for the full schema with
  comments.
- `.env` — secrets only. Minimum: `ANTHROPIC_API_KEY`.

A handful of env vars override config.yml at runtime: `BASE_URL`,
`DATA_DIR`, `HOST`, `PORT`, `CONFIG_PATH`. The worker also reads
`WORKER_TOKEN` (shared secret between server and worker),
`WORKER_SERVER_URL` (where to poll the queue API), and
`WORKER_MAX_UPLOAD_MB` (server-side cap on the uploaded MP3,
defaults to 500).

The management API (`/api/feeds`, `/api/episodes/*`) is gated by
`ADMIN_TOKEN` (or `PODWASH_ADMIN_TOKEN`, or `pass show
podwash-admin-token`). When unset, mutation endpoints return 503.
The public `/feeds/*.xml`, `/audio/*`, and `/health` stay open.

### Optional: Telegram alerts

Worker preflight, crash escalation, and the server-side watchdog can
optionally page you on Telegram when something is broken. Set these in
`.env`:

```
ALERT_TELEGRAM_BOT_TOKEN=...
ALERT_TELEGRAM_CHAT_ID=...
ALERT_TELEGRAM_THREAD_ID=...   # optional
```

If unset, alerts still print to stderr but no Telegram message is
sent. Verify the pipe with:

```bash
uv run python -m src.alerting --note 'wiring check'
```

If you use [`pass`](https://www.passwordstore.org/) (password-store),
podwash will pull the bot token from `pass show telegram-bot-token`
when the env var is empty — handy for keeping plaintext secrets out of
launchd plists or other process managers.

## Two-host deployment

If you want to run the server on a small VPS and the worker on
something beefier (e.g. a home Mac), the repo includes:

- `scripts/deploy.sh` — push-deploy the server image to a remote host
  via `docker save | ssh | docker load`. Fully env-driven; see the
  header comment for variables.
- `deploy/podwash-worker.plist` and `deploy/podwash-worker-watchdog.plist`
  — launchd templates for running the worker (and idle watchdog) on
  macOS. Install with `bash deploy/install-worker.sh`.

The worker's preflight, a crash-burst self-heal wrapper, and an idle
watchdog will use a headless `claude -p` session to try to repair the
repo before paging you on Telegram. None of this is required to run
podwash — the worker also runs fine in the foreground for local use.

See `CLAUDE.md` for the full recovery design.

## API reference

- `GET  /submit` — HTML form for adding feeds.
- `GET  /feeds/{slug}.xml` — proxy RSS feed (subscribe to this).
- `GET  /audio/{feed_id}/{episode_id}.mp3` — placeholder or in-progress audio.
- `GET  /audio/clean/{uuid}.mp3` — completed ad-free audio (URL from feed).
- `GET  /api/feeds` — list all feeds. **Requires `ADMIN_TOKEN`.**
- `POST /api/feeds` — add a new feed. **Requires `ADMIN_TOKEN`.**
- `GET  /api/feeds/{id}/episodes` — list episodes. **Requires `ADMIN_TOKEN`.**
- `POST /api/episodes/{id}/process` — manually trigger processing. **Requires `ADMIN_TOKEN`.**
- `GET  /health` — health check.

Plus a small `/api/jobs/*` family used internally by the worker.

## Development

```bash
uv sync --extra worker --extra dev   # full dev environment
uv run pytest                        # tests
uv run ruff check src/               # lint
```

## Caveats and limitations

- Ad detection is fundamentally a judgment call by an LLM. False
  positives (cutting non-ad content) and false negatives (leaving an
  ad in) both happen. Tune `processing.confidence_threshold` and
  `processing.ad_boundary_padding` in `config.yml` to taste.
- Re-serving feeds you don't own is a personal-use thing. Don't run a
  public podwash instance for podcasts you don't have rights to.
- Whisper is slow on CPU. A modern Mac can keep up with a few daily
  shows; a low-end VPS will fall behind. That's why the worker is
  separate from the server.

## License

MIT — see `LICENSE`.

Built by Adrien Lipari ([@kj6dev](https://github.com/kj6dev)).
