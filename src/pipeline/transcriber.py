"""Transcribe audio using faster-whisper with word-level timestamps."""

import asyncio
import json
import time
from pathlib import Path

from src.database.models import ProcessingLog


def _transcribe_sync(audio_path: str, model_size: str, compute_type: str) -> list[dict]:
    """Synchronous transcription using faster-whisper. Runs in executor."""
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, compute_type=compute_type)
    segments, info = model.transcribe(audio_path, word_timestamps=True)

    result = []
    for segment in segments:
        seg_data = {
            "start": segment.start,
            "end": segment.end,
            "text": segment.text.strip(),
            "words": [],
        }
        if segment.words:
            for word in segment.words:
                seg_data["words"].append({
                    "start": word.start,
                    "end": word.end,
                    "word": word.word,
                    "probability": round(word.probability, 3),
                })
        result.append(seg_data)

    return result


async def transcribe_episode(
    audio_path: Path,
    output_path: Path,
    model_size: str = "base",
    compute_type: str = "int8",
) -> ProcessingLog:
    """
    Transcribe an audio file, writing timestamped JSON output.

    Runs the CPU-bound whisper model in an executor to avoid blocking the event loop.

    Args:
        audio_path: Path to the audio file.
        output_path: Path to write the transcript JSON.
        model_size: Whisper model size (tiny, base, small, medium, large-v3).
        compute_type: Compute type (int8, float16, float32).

    Returns:
        ProcessingLog with transcription stats.
    """
    start = time.monotonic()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_event_loop()
    segments = await loop.run_in_executor(
        None, _transcribe_sync, str(audio_path), model_size, compute_type
    )

    with open(output_path, "w") as f:
        json.dump(segments, f, indent=2)

    total_words = sum(len(s.get("words", [])) for s in segments)
    elapsed_ms = int((time.monotonic() - start) * 1000)

    return ProcessingLog(
        episode_id=0,
        stage="transcribe",
        status="success",
        message=f"Transcribed {len(segments)} segments, {total_words} words in {elapsed_ms / 1000:.1f}s",
        duration_ms=elapsed_ms,
    )
