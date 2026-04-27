"""Download podcast audio files via streaming HTTP."""

import time
from pathlib import Path

import httpx

from src.database.models import ProcessingLog

CHUNK_SIZE = 64 * 1024  # 64KB chunks


async def download_episode(
    url: str,
    output_path: Path,
    timeout: float = 300.0,
) -> ProcessingLog:
    """
    Stream-download a podcast episode audio file.

    Args:
        url: Source audio URL.
        output_path: Local path to write the file.
        timeout: Total download timeout in seconds.

    Returns:
        ProcessingLog with download stats.
    """
    start = time.monotonic()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            total_bytes = 0
            with open(output_path, "wb") as f:
                async for chunk in response.aiter_bytes(chunk_size=CHUNK_SIZE):
                    f.write(chunk)
                    total_bytes += len(chunk)

    elapsed_ms = int((time.monotonic() - start) * 1000)
    return ProcessingLog(
        episode_id=0,  # Caller sets this
        stage="download",
        status="success",
        message=f"Downloaded {total_bytes / (1024 * 1024):.1f}MB",
        duration_ms=elapsed_ms,
    )
