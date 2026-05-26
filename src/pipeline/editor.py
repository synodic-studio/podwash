"""Cut ad segments from audio using ffmpeg."""

import asyncio
import time
from pathlib import Path

from src.database.models import ProcessingLog


async def cut_ads(
    audio_path: Path,
    output_path: Path,
    ad_segments: list[dict],
    padding: float = 0.5,
) -> ProcessingLog:
    """
    Remove ad segments from audio using ffmpeg filter_complex.

    Strategy: compute keep-segments (gaps between ads), use atrim on each,
    then concat into a single output. Single-pass, no temp files.

    Args:
        audio_path: Path to the original audio file.
        output_path: Path for the processed output.
        ad_segments: List of ad segment dicts with 'start' and 'end' keys.
        padding: Seconds to trim extra around ad boundaries.

    Returns:
        ProcessingLog with editing stats.
    """
    start_time = time.monotonic()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not ad_segments:
        # No ads to cut — just copy the file
        await _run_ffmpeg_copy(audio_path, output_path)
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        return ProcessingLog(
            episode_id=0,
            stage="edit",
            status="success",
            message="No ads to cut, copied original",
            duration_ms=elapsed_ms,
        )

    # Sort ad segments by start time
    ads = sorted(ad_segments, key=lambda s: s["start"])

    # Get audio duration via ffprobe
    duration = await _get_duration(audio_path)

    # Build keep-segments (the parts we want to keep)
    keep_segments = _compute_keep_segments(ads, duration, padding)

    if not keep_segments:
        # Everything is an ad? Just copy original as safety measure
        await _run_ffmpeg_copy(audio_path, output_path)
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        return ProcessingLog(
            episode_id=0,
            stage="edit",
            status="success",
            message="All content flagged as ads — kept original as safety measure",
            duration_ms=elapsed_ms,
        )

    # Build ffmpeg filter_complex
    filter_parts = []
    concat_inputs = []
    for i, (seg_start, seg_end) in enumerate(keep_segments):
        filter_parts.append(f"[0:a]atrim=start={seg_start}:end={seg_end},asetpts=PTS-STARTPTS[s{i}]")
        concat_inputs.append(f"[s{i}]")

    n = len(keep_segments)
    filter_complex = ";".join(filter_parts) + f";{''.join(concat_inputs)}concat=n={n}:v=0:a=1[out]"

    cmd = [
        "ffmpeg", "-y",
        "-i", str(audio_path),
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-c:a", "libmp3lame", "-q:a", "2",
        str(output_path),
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {stderr.decode()[:500]}")
    _assert_nonempty(output_path)

    total_cut = sum(s["end"] - s["start"] for s in ads)
    elapsed_ms = int((time.monotonic() - start_time) * 1000)

    return ProcessingLog(
        episode_id=0,
        stage="edit",
        status="success",
        message=f"Cut {len(ads)} ad segments ({total_cut:.0f}s removed), kept {len(keep_segments)} segments",
        duration_ms=elapsed_ms,
    )


def _compute_keep_segments(
    ads: list[dict],
    duration: float,
    padding: float,
) -> list[tuple[float, float]]:
    """Compute the segments to keep (inverse of ad segments with padding)."""
    keep = []
    cursor = 0.0

    for ad in ads:
        ad_start = max(0.0, ad["start"] - padding)
        ad_end = min(duration, ad["end"] + padding)

        if ad_start > cursor:
            keep.append((cursor, ad_start))
        cursor = max(cursor, ad_end)

    if cursor < duration:
        keep.append((cursor, duration))

    # Filter out tiny segments (< 0.1s)
    return [(s, e) for s, e in keep if e - s > 0.1]


async def _run_ffmpeg_copy(src: Path, dst: Path) -> None:
    """Stream-copy src to dst with ffmpeg, raising on any failure."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(src), "-c", "copy", str(dst),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg copy failed: {stderr.decode()[:500]}")
    _assert_nonempty(dst)


def _assert_nonempty(path: Path) -> None:
    if not path.exists():
        raise RuntimeError(f"ffmpeg reported success but output missing: {path}")
    if path.stat().st_size <= 0:
        raise RuntimeError(f"ffmpeg produced empty output: {path}")


async def _get_duration(audio_path: Path) -> float:
    """Get audio duration in seconds via ffprobe."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed (rc={proc.returncode}): "
            f"{stderr.decode()[:300]}"
        )
    raw = stdout.decode().strip()
    if not raw:
        raise RuntimeError(f"ffprobe returned empty duration for {audio_path}")
    try:
        duration = float(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"ffprobe returned non-numeric duration {raw!r}"
        ) from exc
    if duration <= 0 or duration != duration:  # NaN guard
        raise RuntimeError(f"ffprobe returned non-positive duration {duration}")
    return duration
