"""Local cached-model readback of an explicitly selected audio interval.

Example (from repository root):
uv run --extra worker python experiments/journal-2026-09-16/verify_bilingual.py \
  --input data/journal-investigation-2026-09-16/8226112/served.mp3 \
  --start 440 --duration 50 --languages es en --output-dir /tmp/journal-readback

This is a cropped-context diagnostic, not the original production transcript or
an acoustic boundary measurement. Models must already exist in the local cache.
"""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--start", type=float, required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--languages", nargs="+", default=["es", "en"])
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    clip = args.output_dir / "clip.wav"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-ss", str(args.start), "-i",
        str(args.input), "-t", str(args.duration), "-ar", "16000", "-ac", "1",
        str(clip),
    ], check=True)
    from faster_whisper import WhisperModel

    model = WhisperModel("base", device="cpu", compute_type="int8", local_files_only=True)
    metadata = {
        "input": str(args.input),
        "input_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
        "clip_sha256": hashlib.sha256(clip.read_bytes()).hexdigest(),
        "start": args.start, "duration": args.duration,
        "model": "base", "device": "cpu", "compute_type": "int8",
        "local_files_only": True, "word_timestamps": True,
        "condition_on_previous_text": False, "vad_filter": False,
        "languages": args.languages,
    }
    (args.output_dir / "settings.json").write_text(json.dumps(metadata, indent=2))
    for language in args.languages:
        segments, _ = model.transcribe(
            str(clip), language=language, word_timestamps=True,
            condition_on_previous_text=False, vad_filter=False,
        )
        output = []
        for segment in segments:
            record = {
                "start": args.start + segment.start,
                "end": args.start + segment.end,
                "text": segment.text,
                "words": [{"start": args.start + word.start,
                           "end": args.start + word.end, "word": word.word}
                          for word in segment.words or []],
            }
            output.append(record)
            print(language, record["start"], record["end"], record["text"], flush=True)
        (args.output_dir / f"base-{language}.json").write_text(
            json.dumps(output, indent=2, ensure_ascii=False)
        )


if __name__ == "__main__":
    main()
