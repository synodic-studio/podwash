import json
import time
from pathlib import Path
import torch
from whisperx.alignment import load_align_model, align
from whisperx.audio import load_audio

torch.set_num_threads(4)
p = Path("data/slicing-real")
m, meta = load_align_model(language_code="en", device="cpu")
s = json.loads((p / "opening-baseline.json").read_text())
t = time.monotonic()
r = align(
    s, m, meta, load_audio(str(p / "opening.wav")), "cpu", return_char_alignments=False
)
(p / "opening-whisperx.json").write_text(json.dumps(r["segments"], indent=2))
print("alignment seconds", time.monotonic() - t)
