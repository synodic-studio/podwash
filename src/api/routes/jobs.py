"""Worker-queue API: hand out pending episodes, receive processed MP3s.

Flow:
  GET  /api/jobs/next          → claim oldest pending episode, return job spec
  POST /api/jobs/{id}/result   → upload cleaned MP3 + metadata, mark completed
  POST /api/jobs/{id}/fail     → record worker failure, retry or mark failed

Auth: shared bearer token (settings.worker.token). Workers set
WORKER_TOKEN; the Vultr server reads the same value.

Claim ownership: the claim returned by /next includes an opaque
``claim_token``. Result/fail submissions must echo it back via
``X-Claim-Token`` along with ``X-Worker-Id``. A stale or reset claim
gets 409 — the row is mutated only when the caller still owns it.
"""

import secrets
import uuid
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
    provided = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(provided, expected):
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
        "claim_token": episode.claim_token,
    }


@router.post("/{episode_id}/result")
async def submit_result(
    episode_id: int,
    request: Request,
    audio: UploadFile = File(..., description="Cleaned MP3"),
    ad_segments_json: str | None = Form(None),
    worker: str | None = Header(None, alias="X-Worker-Id"),
    claim_token: str | None = Header(None, alias="X-Claim-Token"),
    _: None = Depends(_require_token),
):
    """Worker uploads the cleaned MP3. We persist it and mark the episode completed."""
    conn = request.app.state.db
    settings = request.app.state.settings

    if not worker or not claim_token:
        raise HTTPException(
            status_code=409,
            detail="Missing X-Worker-Id or X-Claim-Token",
        )

    episode = queries.get_episode_by_id(conn, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="Episode not found")

    data_dir = Path(settings.data_dir)
    ep_dir = data_dir / f"feed_{episode.feed_id}" / f"ep_{episode.id}"
    ep_dir.mkdir(parents=True, exist_ok=True)
    final_path = ep_dir / "processed.mp3"
    # Per-request unique temp path: two concurrent uploads for the same
    # claim token must not share a temp file (one would clobber the
    # other's bytes before mark_completed_if_claimed runs).
    request_id = uuid.uuid4().hex
    temp_path = ep_dir / f"processed.mp3.tmp-{claim_token}-{request_id}"

    max_bytes = max(1, settings.worker.max_upload_mb) * 1024 * 1024
    total = 0
    try:
        with temp_path.open("wb") as f:
            while chunk := await audio.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"Upload exceeds {settings.worker.max_upload_mb} MB limit"
                        ),
                    )
                f.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="Empty upload")

        rel_path = str(final_path.relative_to(data_dir))
        new_token = queries.mark_completed_if_claimed(
            conn,
            episode_id,
            worker,
            claim_token,
            rel_path,
            ad_segments_json,
        )
        if new_token is None:
            raise HTTPException(
                status_code=409,
                detail="Claim no longer owned by this worker",
            )
        temp_path.replace(final_path)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass

    size_mb = final_path.stat().st_size / (1024 * 1024)
    print(
        f"[jobs] Episode {episode_id} '{episode.title}' completed via worker ({size_mb:.1f}MB)"
    )
    return {"status": "completed", "processed_audio_path": rel_path}


@router.post("/{episode_id}/fail")
async def submit_failure(
    episode_id: int,
    request: Request,
    payload: dict,
    worker: str | None = Header(None, alias="X-Worker-Id"),
    claim_token: str | None = Header(None, alias="X-Claim-Token"),
    _: None = Depends(_require_token),
):
    """Worker reports a pipeline failure. Retries up to worker.max_retries."""
    conn = request.app.state.db
    settings = request.app.state.settings

    if not worker or not claim_token:
        raise HTTPException(
            status_code=409,
            detail="Missing X-Worker-Id or X-Claim-Token",
        )

    if queries.get_episode_by_id(conn, episode_id) is None:
        raise HTTPException(status_code=404, detail="Episode not found")

    error = str(payload.get("error", ""))[:500] or "unknown worker error"
    new_status = queries.mark_failed_if_claimed(
        conn,
        episode_id,
        worker,
        claim_token,
        error,
        max_retries=settings.worker.max_retries,
    )
    if new_status is None:
        raise HTTPException(
            status_code=409,
            detail="Claim no longer owned by this worker",
        )
    print(f"[jobs] Episode {episode_id} failed → {new_status.value}: {error}")
    return {"status": new_status.value, "error": error}
