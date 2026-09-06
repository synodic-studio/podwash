import json
import subprocess
import time
from pathlib import Path
import numpy as np
from faster_whisper.vad import get_speech_timestamps, VadOptions

cases = [
    (
        "journal-hidden-speech",
        Path("data/classification-eval/journal-new/original.mp3"),
        150.72,
        168.7,
    ),
    (
        "daily-music",
        Path("data/slicing-real/daily/original.mp3"),
        2226.911 + 60.66,
        2226.911 + 70.54,
    ),
]
rows = []
for name, path, a, b in cases:
    start = max(0, a - 1)
    end = b + 1
    t = time.monotonic()
    r = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            str(start),
            "-i",
            str(path),
            "-t",
            str(end - start),
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
    )
    audio = np.frombuffer(r.stdout, dtype=np.float32)
    spans = get_speech_timestamps(
        audio, VadOptions(min_silence_duration_ms=100, speech_pad_ms=100)
    )
    spans = [
        dict(start=start + s["start"] / 16000, end=start + s["end"] / 16000)
        for s in spans
    ]
    row = dict(case=name, extension=[a, b], speech=spans, seconds=time.monotonic() - t)
    rows.append(row)
    print(json.dumps(row), flush=True)
Path("data/classification-eval/sentences/vad-gap-result.json").write_text(
    json.dumps(rows, indent=2)
)
