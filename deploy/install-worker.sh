#!/usr/bin/env bash
# Install (or reinstall) the podwash worker + idle watchdog as launchd
# agents on this Mac.
#
# Configuration via environment variables (all optional except where
# noted):
#   PODWASH_LAUNCHD_PREFIX  Reverse-DNS-style label prefix.
#                           Default: local.podwash
#                           Resulting labels:
#                             ${prefix}.worker
#                             ${prefix}.worker-watchdog
#   PODWASH_WORKER_SERVER_URL
#                           URL the watchdog uses to poll the queue API.
#                           Default: http://localhost:8080
#   PODWASH_ALERT_CHAT_ID   Telegram chat id for self-heal alerts.
#                           Empty (default) disables Telegram.
#   PODWASH_ALERT_THREAD_ID Optional Telegram thread id.
#
# Secrets stay out of the plist:
#   - ANTHROPIC_API_KEY     Read from `pass show anthropic-api-key` if
#                           pass is installed, else from environment.
#   - WORKER_TOKEN          Read from `pass show podwash-worker-token`
#                           if pass is installed, else from environment.
#   - ALERT_TELEGRAM_BOT_TOKEN
#                           Read from `pass show telegram-bot-token` if
#                           pass is installed, else from environment.
#
# Reinstall is idempotent — old labels under the same prefix are
# unloaded before the new ones are loaded.

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOGDIR="$HOME/Library/Logs/podwash-worker"
UV_BIN="$(command -v uv || true)"

LAUNCHD_PREFIX="${PODWASH_LAUNCHD_PREFIX:-local.podwash}"
SERVER_URL="${PODWASH_WORKER_SERVER_URL:-http://localhost:8080}"
ALERT_CHAT_ID="${PODWASH_ALERT_CHAT_ID:-}"
ALERT_THREAD_ID="${PODWASH_ALERT_THREAD_ID:-}"

if [[ -z "$UV_BIN" ]]; then
    echo "error: uv not found in PATH" >&2
    exit 1
fi

mkdir -p "$LOGDIR" "$HOME/Library/LaunchAgents"

install_one() {
    local template_basename="$1"   # podwash-worker | podwash-worker-watchdog
    local label_suffix="$2"        # worker | worker-watchdog
    local label="${LAUNCHD_PREFIX}.${label_suffix}"
    local src="$REPO/deploy/${template_basename}.plist"
    local dst="$HOME/Library/LaunchAgents/${label}.plist"

    sed \
        -e "s|__LABEL__|$label|g" \
        -e "s|__UV__|$UV_BIN|g" \
        -e "s|__REPO__|$REPO|g" \
        -e "s|__LOGDIR__|$LOGDIR|g" \
        -e "s|__HOME__|$HOME|g" \
        -e "s|__SERVER_URL__|$SERVER_URL|g" \
        -e "s|__ALERT_CHAT_ID__|$ALERT_CHAT_ID|g" \
        -e "s|__ALERT_THREAD_ID__|$ALERT_THREAD_ID|g" \
        "$src" > "$dst"

    if launchctl list | grep -q "$label"; then
        echo "Unloading existing $label"
        launchctl unload "$dst" 2>/dev/null || true
    fi

    launchctl load "$dst"
    echo "Loaded $label from $dst"
}

# The worker itself (long-running, KeepAlive).
install_one "podwash-worker" "worker"

# The idle watchdog (one-shot every 5 min). First line of defense
# against stuck-but-alive workers — see src/worker/idle_watchdog.py.
install_one "podwash-worker-watchdog" "worker-watchdog"

echo
echo "Logs: $LOGDIR/"
echo "Worker tail:    tail -f $LOGDIR/podwash-worker.log"
echo "Watchdog tail:  tail -f $LOGDIR/idle-watchdog.log"
