"""Cut ad segments from audio using ffmpeg."""

import asyncio
import time
from pathlib import Path

from src.database.models import ProcessingLog

# How far a reported boundary may sit inside a word and still count as being
# on that word's edge. Must stay comfortably above the rounding error from
# `_TIMESTAMP_DECIMALS` in src/pipeline/classifier.py, which is what the model
# sees: at one decimal place a boundary is already off by up to 0.05s before
# the model's own imprecision is added.
_SNAP_TOLERANCE = 0.3

# Safety net: never let a single boundary walk more than this far across
# non-speech audio. Real outro music runs ~30-60s; anything past this means
# the transcript is broken, and we would rather under-cut than gut the file.
_MAX_DEAD_SPACE_EXTEND = 600.0


async def cut_ads(
    audio_path: Path,
    output_path: Path,
    ad_segments: list[dict],
    padding: float = 0.5,
    transcript_segments: list[dict] | None = None,
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
        transcript_segments: Whisper transcript segments. When supplied, ad
            boundaries that abut non-speech audio (outro music, silence,
            stingers) are extended across it so the dead space goes too.

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

    # A segment starting past the end of the audio cannot be cut, and would
    # otherwise clamp to zero length and vanish with no signal. Transcript and
    # duration come from the same file, so this should never fire — if it
    # does, they have diverged and the whole cut list is suspect.
    for dropped in [a for a in ads if float(a["start"]) >= duration]:
        print(
            f"[edit] WARNING: ad segment {dropped['start']:.1f}-{dropped['end']:.1f}s "
            f"starts past end of audio ({duration:.1f}s) and cannot be cut — "
            f"likely transcript timestamp drift. type={dropped.get('type')} "
            f"reason={dropped.get('reason', '')[:80]}"
        )

    # Resolve each ad into a concrete cut range, absorbing abutting dead space
    cut_ranges = _resolve_cut_ranges(
        ads, _speech_intervals(transcript_segments), duration, padding
    )

    # Build keep-segments (the parts we want to keep)
    keep_segments = _compute_keep_segments(cut_ranges, duration)

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

    # Derive from what we actually keep — snapped ranges can overlap each
    # other, so summing the ad spans would double-count.
    total_cut = duration - sum(e - s for s, e in keep_segments)
    elapsed_ms = int((time.monotonic() - start_time) * 1000)

    return ProcessingLog(
        episode_id=0,
        stage="edit",
        status="success",
        message=f"Cut {len(ads)} ad segments ({total_cut:.0f}s removed), kept {len(keep_segments)} segments",
        duration_ms=elapsed_ms,
    )


def _speech_intervals(
    transcript_segments: list[dict] | None,
) -> list[tuple[float, float]]:
    """Flatten a Whisper transcript into merged, sorted spoken-word intervals.

    Whisper only emits timestamps where it heard speech, so the inverse of
    these intervals is exactly the non-dialog audio (music, silence, stingers).
    Word-level timing is preferred; segments transcribed without words fall
    back to their own span.
    """
    if not transcript_segments:
        return []

    raw: list[tuple[float, float]] = []
    for seg in transcript_segments:
        for span in seg.get("words") or [seg]:
            start, end = span.get("start"), span.get("end")
            if start is None or end is None:
                continue
            start, end = float(start), float(end)
            if end >= start:
                raw.append((start, end))

    raw.sort()
    merged: list[tuple[float, float]] = []
    for start, end in raw:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _resolve_cut_ranges(
    ads: list[dict],
    speech: list[tuple[float, float]],
    duration: float,
    padding: float,
) -> list[tuple[float, float]]:
    """Turn ad segments into the concrete time ranges to remove.

    Whenever there is any gap between the ad boundary and the nearest spoken
    word outside it, the cut lands exactly on that word's edge. This does two
    jobs at once:

    - Non-dialog audio (outro music, silence, stingers) butting up against a
      cut is absorbed into it, however long the gap.
    - The cut can never eat into neighboring speech. Whisper lines are
      separated by 0.1-0.7s pauses, so blindly padding by 0.5s would clip the
      first or last words of the surrounding content.

    Padding remains only as the fallback for a boundary that lands mid-word,
    where there is no gap to cut in and some slack is safer than none.

    Without a transcript there is no speech map, so every boundary falls back
    to padding and behavior matches the pre-snapping editor.
    """
    ranges = []
    for ad in ads:
        ad_start = float(ad["start"])
        ad_end = float(ad["end"])

        if speech:
            prev_speech_end = _speech_end_before(speech, ad_start)
            cut_start = (
                max(ad_start - _MAX_DEAD_SPACE_EXTEND, prev_speech_end)
                if prev_speech_end < ad_start
                else ad_start - padding
            )
            next_speech_start = _speech_start_after(speech, ad_end, duration)
            cut_end = (
                min(ad_end + _MAX_DEAD_SPACE_EXTEND, next_speech_start)
                if next_speech_start > ad_end
                else ad_end + padding
            )
        else:
            cut_start = ad_start - padding
            cut_end = ad_end + padding

        ranges.append((max(0.0, cut_start), min(duration, cut_end)))
    return ranges


def _speech_end_before(speech: list[tuple[float, float]], point: float) -> float:
    """End of the last word clearly finishing before `point` (0.0 if none).

    A word genuinely spanning `point` returns `point` itself — the boundary is
    inside speech, so there is no gap to cut in. Words beginning within
    `_SNAP_TOLERANCE` of `point` are treated as belonging to the segment that
    starts there, not as content to preserve.
    """
    last_end = 0.0
    for start, end in speech:
        if start >= point - _SNAP_TOLERANCE:
            break
        if end > point + _SNAP_TOLERANCE:
            return point
        last_end = end
    return last_end


def _speech_start_after(
    speech: list[tuple[float, float]], point: float, duration: float
) -> float:
    """Start of the first word clearly beginning after `point` (EOF if none).

    Mirror of `_speech_end_before`: words ending within `_SNAP_TOLERANCE` of
    `point` belong to the segment ending there, and a word genuinely spanning
    the boundary returns `point`.
    """
    for start, end in speech:
        if end <= point + _SNAP_TOLERANCE:
            continue
        if start < point - _SNAP_TOLERANCE:
            return point
        return start
    return duration


def _compute_keep_segments(
    cut_ranges: list[tuple[float, float]],
    duration: float,
) -> list[tuple[float, float]]:
    """Compute the segments to keep (the inverse of the cut ranges)."""
    keep = []
    cursor = 0.0

    for cut_start, cut_end in sorted(cut_ranges):
        if cut_start > cursor:
            keep.append((cursor, cut_start))
        cursor = max(cursor, cut_end)

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
