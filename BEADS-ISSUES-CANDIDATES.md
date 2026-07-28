# Beads Issues — Candidates Worth Doing

Curated from this repo's open/in-progress beads before beads was removed (2026-07-10).
Dropped as regenerable: test-coverage/logging/CLAUDE.md-audit tasks, stale-branch cleanup.
Bead IDs kept for reference.

## 🔴 Security
- Rotate the leaked Anthropic API key in `.env` (`sk-ant-api03-FaBzK…`) — gitignored but exposed on disk. *(r3y)*

## Feature / perf
- Run Whisper **natively on host with MLX** instead of CPU-only inside Docker (M1 has no Docker GPU passthrough) — mlx-whisper sidecar the container calls out to. *(wuf)*

## Real bugs worth fixing
- SQLite connection uses `check_same_thread=False` shared across async handlers + APScheduler thread with no locking → corruption risk under concurrency. *(0t3)*
- `/api/feeds` POST and `/submit` are public with no rate limiting or auth. *(lwn)*
- Ollama URL mismatch between `config.yml` (docker host) and `config.example.yml` (localhost) — document both. *(bil)*
