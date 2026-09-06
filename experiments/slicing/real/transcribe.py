import json
import time
from pathlib import Path
from faster_whisper import WhisperModel

p = Path("data/slicing-real")
t = time.monotonic()
m = WhisperModel("base", device="cpu", compute_type="int8")
s, info = m.transcribe(str(p / "original.mp3"), word_timestamps=True)
r = []
with (p / "transcript.txt").open("w") as out:
    for a in s:
        r.append(
            {
                "start": a.start,
                "end": a.end,
                "text": a.text,
                "words": [
                    {"start": w.start, "end": w.end, "word": w.word} for w in a.words
                ],
            }
        )
        out.write(f"{a.start:.2f}-{a.end:.2f} {a.text}\n")
        out.flush()
(p / "baseline.json").write_text(json.dumps(r, indent=2))
print("seconds", time.monotonic() - t, flush=True)
