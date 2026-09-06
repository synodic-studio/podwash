import json
import requests
from pathlib import Path

p = Path("data/slicing-real")
s = json.loads((p / "baseline.json").read_text())
words = [w for s in s for w in s["words"]]
selected = [(i, w) for i, w in enumerate(words) if w["start"] < 90 or w["start"] > 2590]
prompt = """Identify sponsor advertisements in these opening and closing excerpts of a podcast. Preserve actual show introductions, topic explanations, recurring opening bits and guest introductions. Remove sponsor mentions, promotional pitches and calls to action. The sponsor can be mentioned briefly inside otherwise valuable content: in that case remove only that mention. Select precise first and last word IDs, inclusive. Do not remove neighboring content to fit a transcript segment. Return JSON only: {"ranges":[{"first_word_id":0,"last_word_id":10,"reason":"..."}]}. Word IDs are global across the episode; there is a large omitted middle section. Do not create a range spanning that omitted section.\n"""
prompt += "\n".join(
    f"{i} [{w['start']:.2f}-{w['end']:.2f}] {w['word']}" for i, w in selected
)
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
x = r.json()
(p / "word-classification.json").write_text(json.dumps(x, indent=2))
text = x["choices"][0]["message"]["content"]
print(text, flush=True)
d = json.loads(text[text.index("{") : text.rindex("}") + 1])
valid = {i for i, w in selected}
for r in d["ranges"]:
    a, b = r["first_word_id"], r["last_word_id"]
    assert a <= b and all(i in valid for i in range(a, b + 1))
    print(
        words[a]["start"],
        words[b]["end"],
        "".join(w["word"] for w in words[a : b + 1]),
        flush=True,
    )
