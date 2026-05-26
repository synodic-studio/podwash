#!/usr/bin/env bash
# Deploy the podwash server image to a remote host via image push.
#
# Model: build the image locally, `docker save | ssh | docker load` on
# the remote, stop/remove old container, start new one with the same
# volumes/env. The remote never pulls from a registry — it only
# receives artifacts streamed over SSH.
#
# Configuration (env vars):
#   PODWASH_REMOTE_SSH        Required. e.g. user@host.example.com
#   PODWASH_REMOTE_DIR        Default: /opt/podwash
#   PODWASH_CONTAINER         Default: podwash
#   PODWASH_IMAGE             Default: podwash
#   PODWASH_PORT              Host port to expose. Default: 8080
#   PODWASH_CONTAINER_PORT    Port the container listens on. Default: 8080.
#                             The app reads PORT from the env we pass.
#   PODWASH_PUBLIC_HEALTH_URL Optional. If set, curl'd at the end as a
#                             final smoke test (e.g. through a tunnel
#                             or load balancer). Empty = skip.
#
# The remote is expected to already have $PODWASH_REMOTE_DIR/config.yml
# and $PODWASH_REMOTE_DIR/.env in place — they are NOT transferred by
# this script (they hold secrets and host-specific values).
#
# Usage:
#   PODWASH_REMOTE_SSH=user@host scripts/deploy.sh               # full deploy
#   PODWASH_REMOTE_SSH=user@host scripts/deploy.sh --skip-tests  # skip ruff+pytest

set -euo pipefail

REMOTE_SSH="${PODWASH_REMOTE_SSH:-}"
REMOTE_DIR="${PODWASH_REMOTE_DIR:-/opt/podwash}"
CONTAINER="${PODWASH_CONTAINER:-podwash}"
IMAGE_NAME="${PODWASH_IMAGE:-podwash}"
HOST_PORT="${PODWASH_PORT:-8080}"
CONTAINER_PORT="${PODWASH_CONTAINER_PORT:-8080}"
PUBLIC_HEALTH_URL="${PODWASH_PUBLIC_HEALTH_URL:-}"

if [[ -z "$REMOTE_SSH" ]]; then
    echo "error: set PODWASH_REMOTE_SSH (e.g. user@host.example.com)" >&2
    exit 2
fi

SKIP_TESTS=0
case "${1:-}" in
    --skip-tests) SKIP_TESTS=1 ;;
    "") ;;
    *) echo "unknown arg: $1"; exit 2 ;;
esac

log() { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[deploy] FAIL:\033[0m %s\n' "$*" >&2; exit 1; }

cd "$(git rev-parse --show-toplevel)"

log "1/7 preflight: branch clean and pushed"
CURRENT_BRANCH=$(git branch --show-current)
[ "$CURRENT_BRANCH" = "develop" ] || die "must be on develop (on $CURRENT_BRANCH)"
git diff --quiet && git diff --cached --quiet || die "uncommitted changes — commit or stash"
git fetch -q origin develop
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/develop)
[ "$LOCAL" = "$REMOTE" ] || die "local ($LOCAL) != origin/develop ($REMOTE) — push or pull"
SHA=$(git rev-parse --short HEAD)

if [ "$SKIP_TESTS" = "0" ]; then
    log "2/7 ruff"
    uv run ruff check src/ || die "ruff failed"
    log "3/7 pytest"
    # exit 5 = no tests collected; treat as pass
    set +e
    uv run pytest -q
    rc=$?
    set -e
    [ $rc -eq 0 ] || [ $rc -eq 5 ] || die "pytest failed (exit $rc)"
else
    log "2-3/7 skipped (--skip-tests)"
fi

log "4/7 reach remote"
ssh -o BatchMode=yes -o ConnectTimeout=5 "$REMOTE_SSH" "echo ok" >/dev/null \
    || die "cannot SSH to $REMOTE_SSH (network down? key issue?)"

log "5/7 build image (linux/amd64) and transfer"
docker build --platform=linux/amd64 \
    -t "${IMAGE_NAME}:${SHA}" \
    -t "${IMAGE_NAME}:latest" \
    . >/dev/null || die "docker build failed"

# Stream save | load. Pipes errors from either side abort via set -o pipefail.
docker save "${IMAGE_NAME}:${SHA}" "${IMAGE_NAME}:latest" \
    | ssh "$REMOTE_SSH" "docker load" >/dev/null \
    || die "image transfer failed"

log "6/7 restart container on remote (with rollback on failure)"
ssh "$REMOTE_SSH" bash -s <<EOF || die "remote restart failed"
set -euo pipefail

mkdir -p "$REMOTE_DIR/data"
[ -f "$REMOTE_DIR/config.yml" ] || { echo "[remote] missing $REMOTE_DIR/config.yml"; exit 1; }
[ -f "$REMOTE_DIR/.env" ]       || { echo "[remote] missing $REMOTE_DIR/.env"; exit 1; }

# Capture the currently-running image id so we can roll back if the
# new container fails its health check. If nothing is running, this
# is empty and rollback is a no-op.
OLD_IMAGE=\$(docker inspect --format='{{.Image}}' "$CONTAINER" 2>/dev/null || true)

if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    docker stop "$CONTAINER" >/dev/null 2>&1 || true
    docker rm   "$CONTAINER" >/dev/null 2>&1 || true
fi

start_container() {
    local image="\$1"
    docker run -d \\
        --name "$CONTAINER" \\
        --restart unless-stopped \\
        -p ${HOST_PORT}:${CONTAINER_PORT} \\
        -v "$REMOTE_DIR/config.yml:/app/config.yml:ro" \\
        -v "$REMOTE_DIR/data:/data" \\
        --env-file "$REMOTE_DIR/.env" \\
        -e DATA_DIR=/data \\
        -e CONFIG_PATH=/app/config.yml \\
        -e PORT=${CONTAINER_PORT} \\
        --memory=512m --memory-swap=768m \\
        "\$image" >/dev/null
}

start_container "${IMAGE_NAME}:${SHA}"

echo "[remote] waiting for /health"
HEALTHY=0
for i in \$(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${HOST_PORT}/health" >/dev/null 2>&1; then
        echo "[remote] healthy after \${i}s"
        HEALTHY=1
        break
    fi
    sleep 1
done

if [ "\$HEALTHY" = "1" ]; then
    exit 0
fi

echo "[remote] health check timed out; tail of new container logs:"
docker logs --tail=50 "$CONTAINER" || true

if [ -n "\$OLD_IMAGE" ]; then
    echo "[remote] rolling back to previous image \$OLD_IMAGE"
    docker stop "$CONTAINER" >/dev/null 2>&1 || true
    docker rm   "$CONTAINER" >/dev/null 2>&1 || true
    start_container "\$OLD_IMAGE"
    for i in \$(seq 1 30); do
        if curl -fsS "http://127.0.0.1:${HOST_PORT}/health" >/dev/null 2>&1; then
            echo "[remote] rolled back successfully after \${i}s"
            break
        fi
        sleep 1
    done
fi
exit 1
EOF

if [[ -n "$PUBLIC_HEALTH_URL" ]]; then
    log "7/7 public health check ($PUBLIC_HEALTH_URL)"
    curl -fsS "$PUBLIC_HEALTH_URL" >/dev/null \
        || die "public health endpoint failed"
else
    log "7/7 skipped (PODWASH_PUBLIC_HEALTH_URL unset)"
fi

log "deploy complete: ${IMAGE_NAME}:${SHA} live on $REMOTE_SSH:${HOST_PORT} (container :${CONTAINER_PORT})"
