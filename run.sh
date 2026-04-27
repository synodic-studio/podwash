#!/usr/bin/env bash
set -euo pipefail

# Resolve ANTHROPIC_API_KEY from pass (password-store), fall back to env var
if command -v pass &>/dev/null; then
    ANTHROPIC_API_KEY=$(pass show anthropic-api-key 2>/dev/null || true)
fi

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    echo "WARNING: Could not resolve ANTHROPIC_API_KEY from pass" >&2
fi

export ANTHROPIC_API_KEY
exec docker compose "$@"
