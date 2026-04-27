"""Worker-queue API: hand out pending episodes, receive processed MP3s.

Flow:
  GET  /api/jobs/next          → claim oldest pending episode, return job spec
  POST /api/jobs/{id}/result   → upload cleaned MP3 + metadata, mark completed
  POST /api/jobs/{id}/fail     → record worker failure, retry or mark failed

Auth: shared bearer token (settings.worker.token). Workers set
WORKER_TOKEN; the Vultr server reads the same value.
"""

from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)

from src.database import queries

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _require_token(request: Request, authorization: str | None = Header(None)) -> None:
    expected = request.app.state.settings.worker.token
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Queue API disabled (no WORKER_TOKEN configured)",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    if authorization.removeprefix("Bearer ").strip() != expected:
        raise HTTPException(status_code=403, detail="Invalid worker token")


@router.get("/queue/health")
async def queue_health(
    request: Request,
    _: None = Depends(_require_token),
):
    """Mac-side idle watchdog reads this every few minutes.

    If pending > 0 and last_claim_at is silent past its threshold, the
    Mac runs its own self-heal. The Vultr watchdog is the backstop for
    when the Mac itself is unreachable.
    """
    return queries.queue_health_snapshot(request.app.state.db)


@router.get("/next")
async def claim_next(
    request: Request,
    worker: str = Header("unknown", alias="X-Worker-Id"),
    _: None = Depends(_require_token),
):
    """Claim the oldest pending episode. 204 if queue is empty."""
    conn = request.app.state.db
    settings = request.app.state.settings

    episode = queries.claim_next_pending(
        conn, worker_id=worker, stale_minutes=settings.worker.stale_minutes
    )
    if episode is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return {
        "episode_id": episode.id,
        "feed_id": episode.feed_id,
        "guid": episode.guid,
        "title": episode.title,
        "source_audio_url": episode.source_audio_url,
        "duration_seconds": episode.duration_seconds,
    }


@router.post("/{episode_id}/result")
async def submit_result(
    episode_id: int,
    request: Request,
    audio: UploadFile = File(..., description="Cleaned MP3"),
    ad_segments_json: str | None = Form(None),
    _: None = Depends(_require_token),
):
    """Worker uploads the cleaned MP3. We persist it and mark the episode completed."""
    conn = request.app.state.db
    settings = request.app.state.settings

    episode = queries.get_episode_by_id(conn, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="Episode not found")

    data_dir = Path(settings.data_dir)
    ep_dir = data_dir / f"feed_{episode.feed_id}" / f"ep_{episode.id}"
    ep_dir.mkdir(parents=True, exist_ok=True)
    processed_path = ep_dir / "processed.mp3"

    # Stream to disk so we don't buffer the whole file in memory.
    with processed_path.open("wb") as f:
        while chunk := await audio.read(1024 * 1024):
            f.write(chunk)

    rel_path = str(processed_path.relative_to(data_dir))
    queries.mark_completed(conn, episode_id, rel_path, ad_segments_json)
    size_mb = processed_path.stat().st_size / (1024 * 1024)
    print(
        f"[jobs] Episode {episode_id} '{episode.title}' completed via worker ({size_mb:.1f}MB)"
    )
    return {"status": "completed", "processed_audio_path": rel_path}


@router.post("/{episode_id}/fail")
async def submit_failure(
    episode_id: int,
    request: Request,
    payload: dict,
    _: None = Depends(_require_token),
):
    """Worker reports a pipeline failure. Retries up to worker.max_retries."""
    conn = request.app.state.db
    settings = request.app.state.settings

    if queries.get_episode_by_id(conn, episode_id) is None:
        raise HTTPException(status_code=404, detail="Episode not found")

    error = str(payload.get("error", ""))[:500] or "unknown worker error"
    new_status = queries.mark_failed(
        conn, episode_id, error, max_retries=settings.worker.max_retries
    )
    print(f"[jobs] Episode {episode_id} failed → {new_status.value}: {error}")
    return {"status": new_status.value, "error": error}
