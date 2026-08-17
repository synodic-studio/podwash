# Beads Issues — Candidates Worth Doing

Curated from this repo's open/in-progress beads before beads was removed (2026-07-10).
Dropped as regenerable: test-coverage/logging/CLAUDE.md-audit tasks, stale-branch cleanup.
Bead IDs kept for reference.

## 🔴 Security
- Rotate the Anthropic API key in `.env` — gitignored but exposed on disk. *(r3y)*

## Feature / perf
- Run Whisper **natively on host with MLX** instead of CPU-only inside Docker (M1 has no Docker GPU passthrough) — mlx-whisper sidecar the container calls out to. *(wuf)*

## Real bugs worth fixing
- SQLite connection uses `check_same_thread=False` shared across async handlers + APScheduler thread with no locking → corruption risk under concurrency. *(0t3)*
- `/api/feeds` POST and `/submit` are public with no rate limiting or auth. *(lwn)*
- A completed episode's proxy RSS item still carries the source `itunes:duration`
  and a `length` estimated from it, so a podcast app shows the uncut runtime for
  audio that is minutes shorter.
