import json
import torch
from pathlib import Path
from whisperx.alignment import load_align_model, align
from whisperx.audio import load_audio

torch.set_num_threads(4)
p = Path("data/slicing-real/daily")
m, meta = load_align_model(language_code="en", device="cpu")
s = [
    s
    for s in json.loads((p / "tail-baseline.json").read_text())
    if 45 < s["start"] < 90
]
r = align(
    s, m, meta, load_audio(str(p / "tail.wav")), "cpu", return_char_alignments=False
)
(p / "tail-whisperx.json").write_text(json.dumps(r["segments"], indent=2))
print(
    [
        (w["word"], w.get("start"), w.get("end"))
        for s in r["segments"]
        for w in s["words"]
        if 58 < w.get("start", 0) < 74
    ]
)
