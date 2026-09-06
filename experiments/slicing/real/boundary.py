import json
import subprocess
from pathlib import Path
from faster_whisper import WhisperModel
import stable_whisper

p = Path("data/slicing-real")
a = p / "opening.wav"
subprocess.run(
    [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-i",
        str(p / "original.mp3"),
        "-t",
        "90",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(a),
    ],
    check=True,
)
m = WhisperModel("base", device="cpu", compute_type="int8")
s, _ = m.transcribe(str(a), word_timestamps=True)
r = [
    {
        "start": s.start,
        "end": s.end,
        "text": s.text,
        "words": [{"start": w.start, "end": w.end, "word": w.word} for w in s.words],
    }
    for s in s
]
(p / "opening-baseline.json").write_text(json.dumps(r, indent=2))
m = stable_whisper.load_faster_whisper("base", device="cpu", compute_type="int8")
r = m.transcribe(str(a), verbose=False)
(p / "opening-stable.json").write_text(json.dumps(r.to_dict()["segments"], indent=2))
m.refine(str(a), r, verbose=False)
(p / "opening-refine.json").write_text(json.dumps(r.to_dict()["segments"], indent=2))
