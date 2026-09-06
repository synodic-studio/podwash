import json
import sys
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from src.pipeline.editor import _resolve_cut_ranges, _speech_intervals
from faster_whisper import WhisperModel

p = Path("data/slicing-real")
m = WhisperModel("small.en", device="cpu", compute_type="int8")
rows = []
for method in ["baseline", "stable", "refine", "whisperx"]:
    segs = json.loads((p / f"opening-{method}.json").read_text())
    words = [w for s in segs for w in s["words"] if "start" in w and "end" in w]
    for label, lo, hi in [("short-sponsor", 3.5, 4.44), ("sponsor-read", 34.5, 63.02)]:
        first = min(range(len(words)), key=lambda i: abs(words[i]["start"] - lo))
        last = min(range(len(words)), key=lambda i: abs(words[i]["end"] - hi))
        start, end = words[first]["start"], words[last]["end"]
        current = _resolve_cut_ranges(
            [{"start": start, "end": end}], _speech_intervals(segs), 90, 0.5
        )[0]
        prototype = (
            (words[first - 1]["end"] + start) / 2,
            (end + words[last + 1]["start"]) / 2,
        )
        context_start = max(0, start - 4)
        context_end = min(90, end + 9)
        for cutter, (a, b) in [("current", current), ("prototype", prototype)]:
            out = p / f"{label}-{method}-{cutter}.wav"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(p / "opening.wav"),
                    "-filter_complex",
                    f"[0:a]atrim=start={context_start}:end={a},asetpts=PTS-STARTPTS[l];[0:a]atrim=start={b}:end={context_end},asetpts=PTS-STARTPTS[r];[l][r]concat=n=2:v=0:a=1[o]",
                    "-map",
                    "[o]",
                    str(out),
                ],
                check=True,
            )
            transcript, _ = m.transcribe(
                str(out), beam_size=5, condition_on_previous_text=False
            )
            text = " ".join(s.text.strip() for s in transcript)
            row = dict(
                case=label,
                method=method,
                cutter=cutter,
                selected_words=[words[first]["word"], words[last]["word"]],
                cut=[a, b],
                readback=text,
                audio=str(out),
            )
            rows.append(row)
            print(json.dumps(row), flush=True)
(p / "opening-evaluation.json").write_text(json.dumps(rows, indent=2))
