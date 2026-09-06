"""Cut ad segments from audio using ffmpeg."""

import asyncio
import subprocess
import time
from pathlib import Path

from src.database.models import ProcessingLog

# Correct only the rounding error from the classifier's one-decimal timestamps.
# A wider tolerance can swallow an entire short neighboring word.
_SNAP_TOLERANCE = 0.050001

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
        padding: Extra trim when no transcript speech timings are available.
        transcript_segments: Whisper transcript segments. When supplied, ad
            adjacent gaps are checked with local VAD before cut expansion.
            Detected speech or a failed check preserves the gap.

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
    speech = _speech_intervals(transcript_segments)
    cut_ranges = _resolve_cut_ranges(ads, speech, duration, padding)
    if speech:
        boundaries = [_ad_boundaries(ad, speech) for ad in ads]
        cut_ranges = await _verify_gap_extensions(
            audio_path, boundaries, cut_ranges, duration
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
        filter_parts.append(
            f"[0:a]atrim=start={seg_start}:end={seg_end},asetpts=PTS-STARTPTS[s{i}]"
        )
        concat_inputs.append(f"[s{i}]")

    n = len(keep_segments)
    filter_complex = (
        ";".join(filter_parts) + f";{''.join(concat_inputs)}concat=n={n}:v=0:a=1[out]"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(audio_path),
        "-filter_complex",
        filter_complex,
        "-map",
        "[out]",
        "-c:a",
        "libmp3lame",
        "-q:a",
        "2",
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
    """Return sorted speech spans, retaining individual word boundaries.

    Segment spans are a fallback when word timings are unavailable. Missing
    transcript speech is not proof of silence; this remains a timestamp heuristic.
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

    return sorted(raw)


def _resolve_cut_ranges(
    ads: list[dict],
    speech: list[tuple[float, float]],
    duration: float,
    padding: float,
) -> list[tuple[float, float]]:
    """Absorb adjacent dead space without padding into known speech.

    Snap only within timestamp rounding error. If a boundary is genuinely inside
    speech, retain that requested boundary rather than expanding the damage.
    Padding is used only when no speech map is available.
    """
    ranges = []
    for ad in ads:
        ad_start, ad_end = _ad_boundaries(ad, speech)

        if speech:
            prev_speech_end = _speech_end_before(speech, ad_start)
            cut_start = (
                max(ad_start - _MAX_DEAD_SPACE_EXTEND, prev_speech_end)
                if prev_speech_end < ad_start
                else ad_start
            )
            next_speech_start = _speech_start_after(speech, ad_end, duration)
            cut_end = (
                min(ad_end + _MAX_DEAD_SPACE_EXTEND, next_speech_start)
                if next_speech_start > ad_end
                else ad_end
            )
        else:
            cut_start = ad_start - padding
            cut_end = ad_end + padding

        ranges.append((max(0.0, cut_start), min(duration, cut_end)))
    return ranges


def _ad_boundaries(ad: dict, speech: list[tuple[float, float]]) -> tuple[float, float]:
    start, end = float(ad["start"]), float(ad["end"])
    snapped_start = _nearest_edge(speech, start, 0)
    snapped_end = _nearest_edge(speech, end, 1)
    return (snapped_start, snapped_end) if snapped_start < snapped_end else (start, end)


async def _verify_gap_extensions(
    audio_path: Path,
    boundaries: list[tuple[float, float]],
    ranges: list[tuple[float, float]],
    duration: float,
) -> list[tuple[float, float]]:
    """Veto gap extensions containing speech omitted by transcription.

    VAD only decides whether extending is safe; its coarse timestamps never
    choose a word boundary. If unavailable, retain the classified boundaries.
    """
    verified = []
    for (start, end), (cut_start, cut_end) in zip(boundaries, ranges, strict=True):
        start, end = max(0.0, start), min(duration, end)
        for side, a, b in [("start", cut_start, start), ("end", end, cut_end)]:
            if b <= a:
                continue
            try:
                has_speech = await asyncio.to_thread(
                    _gap_has_speech, audio_path, a, b, duration
                )
            except Exception as exc:
                print(
                    f"[edit] Gap verification unavailable ({type(exc).__name__}); preserving gap"
                )
                has_speech = True
            if has_speech:
                if side == "start":
                    cut_start = start
                else:
                    cut_end = end
        verified.append((cut_start, cut_end))
    return verified


def _gap_has_speech(
    audio_path: Path, start: float, end: float, duration: float
) -> bool:
    """Check a proposed extension with the worker's bundled Silero VAD, offline."""
    import numpy as np
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    offset = max(0.0, start - 0.5)
    stop = min(duration, end + 0.5)
    audio = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            str(offset),
            "-i",
            str(audio_path),
            "-t",
            str(stop - offset),
            "-f",
            "f32le",
            "-ac",
            "1",
            "-ar",
            "16000",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    samples = np.frombuffer(audio.stdout, dtype="<f4")
    if not samples.size:
        raise ValueError("No decoded audio for gap verification")
    spans = get_speech_timestamps(
        samples, VadOptions(min_silence_duration_ms=100, speech_pad_ms=100)
    )
    return any(
        offset + span["start"] / 16000 < end and offset + span["end"] / 16000 > start
        for span in spans
    )


def _nearest_edge(speech: list[tuple[float, float]], point: float, edge: int) -> float:
    """Choose the nearest start/end, resolving ties toward a smaller cut."""
    candidates = [
        span[edge] for span in speech if abs(span[edge] - point) <= _SNAP_TOLERANCE
    ]
    if not candidates:
        return point
    return min(
        candidates,
        key=lambda value: (abs(value - point), -value if edge == 0 else value),
    )


def _speech_end_before(speech: list[tuple[float, float]], point: float) -> float:
    """Last preceding speech edge, or point when speech crosses it."""
    last_end = 0.0
    for start, end in speech:
        if start >= point:
            break
        if end > point:
            return point
        last_end = max(last_end, end)
    return last_end


def _speech_start_after(
    speech: list[tuple[float, float]], point: float, duration: float
) -> float:
    """Next speech edge, or point when speech crosses it."""
    for start, end in speech:
        if end <= point:
            continue
        if start < point:
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
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-c",
        "copy",
        str(dst),
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
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed (rc={proc.returncode}): {stderr.decode()[:300]}"
        )
    raw = stdout.decode().strip()
    if not raw:
        raise RuntimeError(f"ffprobe returned empty duration for {audio_path}")
    try:
        duration = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"ffprobe returned non-numeric duration {raw!r}") from exc
    if duration <= 0 or duration != duration:  # NaN guard
        raise RuntimeError(f"ffprobe returned non-positive duration {duration}")
    return duration
