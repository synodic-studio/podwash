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

    # Use Python rather than sed so values containing &, \, or sed
    # delimiters round-trip cleanly. Each replacement is a literal
    # string + XML-escape so the resulting plist remains valid even
    # if (e.g.) the server URL contains query string &.
    LABEL="$label" UV_PATH="$UV_BIN" REPO_PATH="$REPO" \
    LOGDIR_PATH="$LOGDIR" HOME_PATH="$HOME" \
    SERVER_URL_VAL="$SERVER_URL" \
    ALERT_CHAT_ID_VAL="$ALERT_CHAT_ID" \
    ALERT_THREAD_ID_VAL="$ALERT_THREAD_ID" \
    python3 - "$src" "$dst" <<'PY'
import os
import sys
from xml.sax.saxutils import escape

src, dst = sys.argv[1], sys.argv[2]
mapping = {
    "__LABEL__": os.environ["LABEL"],
    "__UV__": os.environ["UV_PATH"],
    "__REPO__": os.environ["REPO_PATH"],
    "__LOGDIR__": os.environ["LOGDIR_PATH"],
    "__HOME__": os.environ["HOME_PATH"],
    "__SERVER_URL__": os.environ["SERVER_URL_VAL"],
    "__ALERT_CHAT_ID__": os.environ["ALERT_CHAT_ID_VAL"],
    "__ALERT_THREAD_ID__": os.environ["ALERT_THREAD_ID_VAL"],
}
with open(src) as f:
    body = f.read()
for placeholder, value in mapping.items():
    body = body.replace(placeholder, escape(value))
with open(dst, "w") as f:
    f.write(body)
PY

    if command -v plutil >/dev/null 2>&1; then
        if ! plutil -lint "$dst" >/dev/null; then
            echo "error: rendered plist failed plutil -lint: $dst" >&2
            exit 1
        fi
    fi

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
