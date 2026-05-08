"""Audio file serving routes.

Processing happens on the Mac worker. If a request arrives before the
worker has finished (or for an old episode the worker hasn't seen yet),
we serve the placeholder clip — the worker will pick the row up on its
next claim. Nothing in this file spawns background work.
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from src.alerting import send_alert
from src.database import queries
from src.database.models import EpisodeStatus

router = APIRouter()

PROCESSING_CLIP = Path(__file__).parent.parent.parent / "static" / "processing.mp3"


@router.get("/audio/{feed_id}/{episode_id}.mp3")
async def get_audio(feed_id: int, episode_id: int, request: Request):
    """Serve processed audio if ready, else the placeholder clip."""
    conn = request.app.state.db
    settings = request.app.state.settings

    episode = queries.get_episode_by_id(conn, episode_id)
    if episode is None or episode.feed_id != feed_id:
        raise HTTPException(status_code=404, detail="Episode not found")

    # Completed: serve the processed audio
    if episode.status == EpisodeStatus.COMPLETED and episode.processed_audio_path:
        file_path = Path(settings.data_dir) / episode.processed_audio_path
        if file_path.exists():
            return FileResponse(
                path=str(file_path),
                media_type="audio/mpeg",
                headers={"Cache-Control": "public, max-age=86400"},
            )
        # DB says completed but the file is gone (manual delete, disk
        # corruption, retention bug). Reset to NEW so the very next tap
        # triggers reprocessing. One alert per occurrence so we hear
        # about it the first time but a podcast app retrying every
        # 30s doesn't spam.
        queries.update_episode_status(conn, episode_id, EpisodeStatus.NEW)
        send_alert(
            subsystem="server-audio",
            kind="completed audio missing",
            problem=(
                f"Episode {episode_id} (feed {feed_id}) is marked "
                f"completed but {file_path} does not exist. Reset to "
                "NEW so the next tap reprocesses it."
            ),
            fix=(
                "If this fires for many episodes at once, check disk "
                "health on the server (`df -h`, inspect the data dir) "
                "and the cleanup job for an off-by-one. One-offs "
                "usually self-heal on the next tap."
            ),
            context={"episode_id": episode_id, "missing_path": str(file_path)},
        )

    # User tapped an episode that isn't ready. Flip NEW/FAILED to PENDING so
    # the worker picks it up on its next claim. Worker never processes rows
    # that stay in 'new' — this keeps processing strictly on-demand.
    if episode.status in {EpisodeStatus.NEW, EpisodeStatus.FAILED}:
        queries.update_episode_status(conn, episode_id, EpisodeStatus.PENDING)

    return FileResponse(
        path=str(PROCESSING_CLIP),
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/audio/clean/{clean_token}.mp3")
async def get_clean_audio(clean_token: str, request: Request):
    """Serve a completed episode's processed audio by its clean token."""
    conn = request.app.state.db
    settings = request.app.state.settings

    episode = queries.get_episode_by_clean_token(conn, clean_token)
    if episode is None or episode.status != EpisodeStatus.COMPLETED or not episode.processed_audio_path:
        raise HTTPException(status_code=404, detail="Episode not found")

    file_path = Path(settings.data_dir) / episode.processed_audio_path
    if file_path.exists():
        return FileResponse(
            path=str(file_path),
            media_type="audio/mpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    # File gone — reset so the next tap on the old URL reprocesses it.
    queries.update_episode_status(conn, episode.id, EpisodeStatus.NEW)
    send_alert(
        subsystem="server-audio",
        kind="completed audio missing",
        problem=(
            f"Episode {episode.id} is marked completed but "
            f"{file_path} does not exist. Reset to NEW."
        ),
        fix=(
            "Check disk health on the server (`df -h`, inspect the data dir) "
            "and the cleanup job for an off-by-one."
        ),
        context={"episode_id": episode.id, "missing_path": str(file_path)},
    )
    raise HTTPException(status_code=404, detail="Episode not found")
