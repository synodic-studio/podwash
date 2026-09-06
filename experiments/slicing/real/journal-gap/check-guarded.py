import subprocess
import json
from pathlib import Path
from faster_whisper import WhisperModel

p = Path("data/classification-eval/journal-new")
model = WhisperModel("small.en", device="cpu", compute_type="int8")
rows = []
for label, a, b in [("guarded-opening", 35, 115)]:
    out = p / (label + "-source.wav")
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            str(a),
            "-i",
            str(p / "guarded-clean.mp3"),
            "-t",
            str(b - a),
            "-ar",
            "16000",
            "-ac",
            "1",
            str(out),
        ],
        check=True,
    )
    segs, _ = model.transcribe(
        str(out),
        word_timestamps=True,
        condition_on_previous_text=False,
        vad_filter=True,
    )
    row = dict(
        label=label,
        source_offset=a,
        segments=[
            dict(
                start=s.start + a,
                end=s.end + a,
                text=s.text,
                words=[
                    dict(word=w.word, start=w.start + a, end=w.end + a) for w in s.words
                ],
            )
            for s in segs
        ],
    )
    rows.append(row)
    print(json.dumps(row), flush=True)
(p / "guarded-opening-readback.json").write_text(json.dumps(rows, indent=2))
