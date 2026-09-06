import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from src.pipeline.editor import _speech_intervals, _resolve_cut_ranges
from faster_whisper import WhisperModel

p = Path("data/slicing-real/daily")
m = WhisperModel("small.en", device="cpu", compute_type="int8")
rows = []
for method in ["baseline", "whisperx-padded"]:
    segs = json.loads((p / f"tail-{method}.json").read_text())
    w = [w for s in segs for w in s["words"] if "start" in w and "end" in w]
    first = next(
        i
        for i, a in enumerate(w)
        if a["word"].strip().lower() == "we'll" and 67 < a["start"] < 69
    )
    last = first + 3
    s, e = w[first]["start"], w[last]["end"]
    prev = w[first - 1]["end"]
    nxt = w[last + 1]["start"]
    current = _resolve_cut_ranges(
        [{"start": s, "end": e}], _speech_intervals(segs), 210, 0.5
    )[0]
    midpoint = ((prev + s) / 2, (e + nxt) / 2)
    guarded = (min(prev + 0.08, midpoint[0]), max(nxt - 0.08, midpoint[1]))
    for cutter, (a, b) in [
        ("current", current),
        ("midpoint", midpoint),
        ("guarded", guarded),
    ]:
        out = p / f"break-{method}-{cutter}.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(p / "tail.wav"),
                "-filter_complex",
                f"[0:a]atrim=start=55:end={a},asetpts=PTS-STARTPTS[l];[0:a]atrim=start={b}:end=84,asetpts=PTS-STARTPTS[r];[l][r]concat=n=2:v=0:a=1[o]",
                "-map",
                "[o]",
                str(out),
            ],
            check=True,
        )
        seg, _ = m.transcribe(
            str(out), condition_on_previous_text=False, vad_filter=True
        )
        row = dict(
            method=method,
            cutter=cutter,
            cut=[a, b],
            readback=" ".join(a.text.strip() for a in seg),
        )
        rows.append(row)
        print(json.dumps(row), flush=True)
(p / "refined-evaluation.json").write_text(json.dumps(rows, indent=2))
