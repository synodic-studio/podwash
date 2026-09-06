import subprocess
import json
from pathlib import Path
from faster_whisper import WhisperModel

p = Path("data/slicing-real/daily")
d = float(
    subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(p / "original.mp3"),
        ]
    )
)
o = max(0, d - 210)
subprocess.run(
    [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-ss",
        str(o),
        "-i",
        str(p / "original.mp3"),
        "-ar",
        "16000",
        "-ac",
        "1",
        str(p / "tail.wav"),
    ],
    check=True,
)
m = WhisperModel("base", device="cpu", compute_type="int8")
s, _ = m.transcribe(str(p / "tail.wav"), word_timestamps=True)
r = []
for a in s:
    r.append(
        {
            "start": a.start,
            "end": a.end,
            "text": a.text,
            "words": [
                {"word": w.word, "start": w.start, "end": w.end} for w in a.words
            ],
        }
    )
    print(f"{a.start:.2f}-{a.end:.2f} {a.text}", flush=True)
(p / "tail-baseline.json").write_text(json.dumps(r, indent=2))
(p / "tail-offset.json").write_text(json.dumps({"offset": o}))
