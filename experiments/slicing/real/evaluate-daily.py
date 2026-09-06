import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from src.pipeline.editor import _resolve_cut_ranges, _speech_intervals
from faster_whisper import WhisperModel

p = Path("data/slicing-real/daily")
m = WhisperModel("small.en", device="cpu", compute_type="int8")
rows = []
for label, source, lo, hi in [
    ("network-credit", "opening", 0.82, 2.14),
    ("break-transition", "tail", 67.62, 68.76),
]:
    segs = json.loads((p / f"{source}-baseline.json").read_text())
    words = [w for s in segs for w in s["words"]]
    first = min(range(len(words)), key=lambda i: abs(words[i]["start"] - lo))
    last = min(range(len(words)), key=lambda i: abs(words[i]["end"] - hi))
    start, end = words[first]["start"], words[last]["end"]
    duration = 240 if source == "opening" else 210
    current = _resolve_cut_ranges(
        [{"start": start, "end": end}], _speech_intervals(segs), duration, 0.5
    )[0]
    prototype = (
        (words[first - 1]["end"] + start) / 2 if first else 0,
        (end + words[last + 1]["start"]) / 2,
    )
    for cutter, (a, b) in [("current", current), ("prototype", prototype)]:
        out = p / f"{label}-{cutter}.wav"
        begin = max(0, a - 5)
        finish = min(duration, b + 12)
        filters = (
            f"[0:a]atrim=start={b}:end={finish},asetpts=PTS-STARTPTS[o]"
            if a == 0
            else f"[0:a]atrim=start={begin}:end={a},asetpts=PTS-STARTPTS[l];[0:a]atrim=start={b}:end={finish},asetpts=PTS-STARTPTS[r];[l][r]concat=n=2:v=0:a=1[o]"
        )
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(p / f"{source}.wav"),
                "-filter_complex",
                filters,
                "-map",
                "[o]",
                str(out),
            ],
            check=True,
        )
        s, _ = m.transcribe(str(out), condition_on_previous_text=False)
        row = dict(
            case=label,
            cutter=cutter,
            cut=[a, b],
            readback=" ".join(a.text.strip() for a in s),
            audio=str(out),
        )
        rows.append(row)
        print(json.dumps(row), flush=True)
(p / "evaluation.json").write_text(json.dumps(rows, indent=2))
