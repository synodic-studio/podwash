import json
import subprocess
from pathlib import Path
from faster_whisper import WhisperModel

root = Path("data/slicing-real")
result = json.loads(
    Path("experiments/slicing/real/fixed-selection-results.json").read_text()
)
model = WhisperModel("small.en", device="cpu", compute_type="int8")
rows = []
for r in result["rows"]:
    if (
        r["source"] != "ologies"
        or r["selection"] not in [[97, 178], [6807, 6939]]
        or r["method"] == "current_rounded"
    ):
        continue
    a, b = r["cut"]
    start, end = r["requested"]
    lo = start - 7
    hi = end + 7
    out = root / ("fixed-" + str(r["selection"][0]) + "-" + r["method"] + ".wav")
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(root / "original.mp3"),
            "-filter_complex",
            f"[0:a]atrim=start={lo}:end={a},asetpts=PTS-STARTPTS[l];[0:a]atrim=start={b}:end={hi},asetpts=PTS-STARTPTS[r];[l][r]concat=n=2:v=0:a=1[o]",
            "-map",
            "[o]",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(out),
        ],
        check=True,
    )
    segments, _ = model.transcribe(
        str(out), beam_size=5, condition_on_previous_text=False, vad_filter=True
    )
    row = dict(
        selection=r["selection"],
        method=r["method"],
        cut=r["cut"],
        context=[lo, hi],
        readback=" ".join(s.text.strip() for s in segments),
        audio=str(out),
    )
    rows.append(row)
    print(json.dumps(row), flush=True)
Path("experiments/slicing/real/fixed-selection-readback.json").write_text(
    json.dumps(rows, indent=2) + "\n"
)
