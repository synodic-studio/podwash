from faster_whisper import WhisperModel
import requests
import xml.etree.ElementTree as ET
import json
import hashlib
import subprocess
from pathlib import Path

p = Path("data/slicing-real/daily")
p.mkdir(exist_ok=True)
i = ET.parse("data/slicing-real/daily-feed.xml").findall("channel/item")[2]
u = i.find("enclosure").get("url")
r = requests.get(u, timeout=120)
r.raise_for_status()
(p / "original.mp3").write_bytes(r.content)
(p / "source.json").write_text(
    json.dumps(
        {
            "title": i.findtext("title"),
            "url": u,
            "sha256": hashlib.sha256(r.content).hexdigest(),
        },
        indent=2,
    )
)
print(i.findtext("title"), flush=True)
subprocess.run(
    [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-i",
        str(p / "original.mp3"),
        "-t",
        "240",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(p / "opening.wav"),
    ],
    check=True,
)

m = WhisperModel("base", device="cpu", compute_type="int8")
segs, _ = m.transcribe(str(p / "opening.wav"), word_timestamps=True)
s = [
    {
        "start": a.start,
        "end": a.end,
        "text": a.text,
        "words": [{"word": w.word, "start": w.start, "end": w.end} for w in a.words],
    }
    for a in segs
]
(p / "opening-baseline.json").write_text(json.dumps(s, indent=2))
print("\n".join(f"{a['start']:.2f}-{a['end']:.2f} {a['text']}" for a in s), flush=True)
