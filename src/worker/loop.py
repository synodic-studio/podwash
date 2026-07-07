"""Main worker loop: claim → download → transcribe → classify → cut → upload."""

import asyncio
import shutil
import tempfile
import time
import traceback
from pathlib import Path

from src.config import Settings
from src.pipeline.classifier import classify_ads
from src.pipeline.downloader import download_episode
from src.pipeline.editor import cut_ads
from src.pipeline.transcriber import transcribe_episode

from .client import Job, QueueClient


async def _process_and_upload(
    client: QueueClient, job: Job, settings: Settings, tmp_root: Path
) -> None:
    """Run the full pipeline for one job and upload the cleaned MP3."""
    original_path = tmp_root / "original.mp3"
    transcript_path = tmp_root / "transcript.json"
    processed_path = tmp_root / "processed.mp3"

    # Stage 1: Download source MP3 directly from the podcast CDN
    await download_episode(job.source_audio_url, original_path)

    # Stage 2: Whisper transcribe
    await transcribe_episode(
        original_path,
        transcript_path,
        model_size=settings.processing.whisper_model,
        compute_type=settings.processing.whisper_compute_type,
    )

    # Stage 3: Claude ad classification
    ad_segments, raw_json, _ = await classify_ads(
        transcript_path,
        api_key=settings.anthropic_api_key,
        model=settings.claude.model,
        max_tokens=settings.claude.max_tokens,
        confidence_threshold=settings.processing.confidence_threshold,
    )

    # Stage 4: ffmpeg cut
    await cut_ads(
        original_path,
        processed_path,
        ad_segments,
        padding=settings.processing.ad_boundary_padding,
    )

    # Upload — the server flips status to completed and owns the file.
    client.submit_result(
        job.episode_id, processed_path, raw_json, claim_token=job.claim_token
    )
    size_mb = processed_path.stat().st_size / (1024 * 1024)
    print(f"[worker] Episode {job.episode_id} uploaded ({size_mb:.1f}MB)")


async def run_once(client: QueueClient, settings: Settings) -> bool:
    """Process one job. Returns False if the queue was empty, True otherwise."""
    job = client.claim_next()
    if job is None:
        return False

    print(
        f"[worker] Claimed episode {job.episode_id} '{job.title[:60]}' "
        f"(feed {job.feed_id})"
    )
    started = time.monotonic()
    tmp_root = Path(tempfile.mkdtemp(prefix=f"podwash-{job.episode_id}-"))

    try:
        await asyncio.wait_for(
            _process_and_upload(client, job, settings, tmp_root),
            timeout=settings.worker.stale_minutes * 60,
        )
    except Exception as exc:
        if isinstance(exc, TimeoutError):
            err = (
                f"TimeoutError: pipeline exceeded stale_minutes="
                f"{settings.worker.stale_minutes} without finishing"
            )
        else:
            err = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
        print(f"[worker] Episode {job.episode_id} failed: {err}")
        try:
            client.submit_failure(job.episode_id, err, claim_token=job.claim_token)
        except Exception as api_exc:
            print(f"[worker] Could not report failure to server: {api_exc}")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        print(
            f"[worker] Episode {job.episode_id} done in "
            f"{time.monotonic() - started:.1f}s"
        )

    return True


async def run_forever(settings: Settings, worker_id: str) -> None:
    """Poll the queue indefinitely, backing off on server errors."""
    if not settings.worker.token:
        raise RuntimeError(
            "Missing WORKER_TOKEN (set via env or pass 'podwash-worker-token')"
        )
    if not settings.anthropic_api_key:
        raise RuntimeError(
            "Missing ANTHROPIC_API_KEY (set via env or pass 'anthropic-api-key')"
        )

    client = QueueClient(
        base_url=settings.worker.server_url,
        token=settings.worker.token,
        worker_id=worker_id,
    )
    print(
        f"[worker] Starting loop as '{worker_id}' → {settings.worker.server_url} "
        f"(idle={settings.worker.idle_poll_seconds}s, "
        f"whisper={settings.processing.whisper_model})"
    )
    backoff = 5
    try:
        while True:
            try:
                had_job = await run_once(client, settings)
                backoff = 5
            except Exception as exc:
                print(f"[worker] Poll error: {type(exc).__name__}: {exc}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
                continue

            if not had_job:
                await asyncio.sleep(settings.worker.idle_poll_seconds)
    finally:
        client.close()
