import json
import requests
import time
from pathlib import Path

p = Path("data/slicing-real")
while not (p / "baseline.json").exists():
    time.sleep(2)
s = json.loads((p / "baseline.json").read_text())
transcript = "\n".join(f"[{a['start']:.1f}s - {a['end']:.1f}s] {a['text']}" for a in s)
prompt = Path("prompts/ad_detection.txt").read_text().format(transcript=transcript)
r = requests.post(
    "http://localhost:4000/v1/chat/completions",
    json={
        "model": "dsf",
        "max_tokens": 16384,
        "messages": [{"role": "user", "content": prompt}],
        "thinking": {"type": "disabled"},
        "extra_body": {"thinking": {"type": "disabled"}},
    },
    timeout=180,
)
r.raise_for_status()
data = r.json()
(p / "full-classification.json").write_text(json.dumps(data, indent=2))
print(data["choices"][0]["message"]["content"])
