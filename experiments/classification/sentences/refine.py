"""Refine only boundary sentences of sentence-classified spans using cheap dsf."""

import json
import sys
import hashlib
import argparse
import time
from pathlib import Path
import requests

sys.path.insert(0, str(Path.cwd()))
from experiments.classification.evaluate import load_cases, request_body, score
from experiments.classification.sentences.run import units


def refine(case, input_dir, output):
    output.mkdir(parents=True, exist_ok=False)
    prior = json.loads((input_dir / "result.json").read_text())
    sentences = units(case["words"])
    words = {w["id"]: w for w in case["words"]}
    spans = []
    for r in sorted(prior["parsed"]["ranges"], key=lambda r: r["first"]):
        if spans and r["first"] == spans[-1]["last"] + 1:
            spans[-1]["last"] = r["last"]
        else:
            spans.append(dict(first=r["first"], last=r["last"]))
    template = Path(
        "experiments/classification/sentences/boundary-prompt-v1.txt"
    ).read_text()
    rows = []
    chosen = []
    for index, r in enumerate(spans):
        first = next(s for s in sentences if s["first"] == r["first"])
        last = next(s for s in sentences if s["last"] == r["last"])
        context = sentences[max(0, first["id"] - 2) : last["id"] + 3]
        prompt = (
            template
            + "\nFirst-sentence bounds: "
            + json.dumps([first["first"], first["last"]])
            + "\nLast-sentence bounds: "
            + json.dumps([last["first"], last["last"]])
            + "\nOriginal passage: "
            + json.dumps(r)
            + "\nTRANSCRIPT:\n"
            + "\n".join(
                " ".join(
                    f"[{i}]{words[i]['word'].strip()}"
                    for i in range(s["first"], s["last"] + 1)
                )
                for s in context
            )
        )
        t = time.monotonic()
        response = requests.post(
            "http://localhost:4000/v1/chat/completions",
            json=request_body("dsf", prompt),
            timeout=180,
        )
        response.raise_for_status()
        d = response.json()
        text = d["choices"][0]["message"]["content"]
        row = dict(
            original=r,
            response_id=d["id"],
            output_text=text,
            cost_usd=float(response.headers.get("x-litellm-response-cost", 0)),
            usage=d.get("usage"),
            prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
            seconds=time.monotonic() - t,
        )
        try:
            assert d["choices"][0]["finish_reason"] == "stop"
            p = json.loads(text[text.index("{") : text.rindex("}") + 1])
            assert type(p["first"]) is int and type(p["last"]) is int
            assert (
                first["first"] <= p["first"] <= first["last"]
                and last["first"] <= p["last"] <= last["last"]
                and p["first"] <= p["last"]
            )
            chosen.append(p)
            row["parsed"] = p
        except (ValueError, KeyError, AssertionError) as e:
            row["error"] = "Invalid boundary response: " + str(e)
            chosen.append(r)
        rows.append(row)
        (output / "detail.json").write_text(json.dumps(rows, indent=2))
        print(
            json.dumps(
                {
                    k: v
                    for k, v in row.items()
                    if k in ["original", "parsed", "error", "cost_usd"]
                }
            ),
            flush=True,
        )
    result = dict(
        case=case["name"],
        model="dsf",
        policy=prior["policy"] + "-boundary-v1",
        parsed=dict(ranges=chosen),
        cost_usd=prior["cost_usd"] + sum(r["cost_usd"] for r in rows),
        boundary_errors=sum("error" in r for r in rows),
    )
    result["score"] = score(case, result["parsed"], "words")
    (output / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result["score"]), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--case", default="ologies_full")
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    refine(load_cases()[a.case], a.input, a.output)
