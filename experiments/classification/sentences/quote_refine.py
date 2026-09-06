import argparse
import json
import sys
import time
import hashlib
from pathlib import Path
import requests

sys.path.insert(0, str(Path.cwd()))
from experiments.classification.evaluate import load_cases, request_body, score
from experiments.classification.sentences.run import units


def refine(case, input_dir, output):
    output.mkdir(parents=True, exist_ok=False)
    prior = json.loads((input_dir / "result.json").read_text())
    sentences = units(case["words"])
    spans = []
    rows = []
    chosen = []
    for r in sorted(prior["parsed"]["ranges"], key=lambda r: r["first"]):
        if spans and r["first"] == spans[-1]["last"] + 1:
            spans[-1]["last"] = r["last"]
        else:
            spans.append(dict(first=r["first"], last=r["last"]))
    template = Path(
        "experiments/classification/sentences/quote-boundary-prompt.txt"
    ).read_text()
    for r in spans:
        first = next(s for s in sentences if s["first"] == r["first"])
        last = next(s for s in sentences if s["last"] == r["last"])
        context = sentences[max(0, first["id"] - 2) : last["id"] + 3]
        prompt = (
            template
            + "\nCONTEXT:\n"
            + "\n".join(s["text"] for s in context)
            + "\nFIRST SENTENCE: "
            + first["text"]
            + "\nLAST SENTENCE: "
            + last["text"]
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
            a = p["first_remove"].split()
            b = p["last_remove"].split()
            fw = first["text"].split()
            lw = last["text"].split()
            assert a and b and fw[-len(a) :] == a and lw[: len(b)] == b
            # Map via exact original word strings, not whitespace token counts (STT may group tokens).
            firstwords = [
                w for w in case["words"] if first["first"] <= w["id"] <= first["last"]
            ]
            lastwords = [
                w for w in case["words"] if last["first"] <= w["id"] <= last["last"]
            ]
            lo = next(
                w["id"]
                for j, w in enumerate(firstwords)
                if " ".join(x["word"].strip() for x in firstwords[j:]).split() == a
            )
            hi = next(
                w["id"]
                for j, w in enumerate(lastwords)
                if " ".join(x["word"].strip() for x in lastwords[: j + 1]).split() == b
            )
            assert lo <= hi
            cut = dict(first=lo, last=hi)
            row["parsed"] = cut
            chosen.append(cut)
        except (ValueError, KeyError, AssertionError, StopIteration) as e:
            row["error"] = "Invalid exact quote: " + str(e)
            chosen.append(r)
        rows.append(row)
        (output / "detail.json").write_text(json.dumps(rows, indent=2))
        print(
            json.dumps(
                {
                    k: v
                    for k, v in row.items()
                    if k in ["original", "parsed", "error", "cost_usd", "output_text"]
                }
            ),
            flush=True,
        )
    result = dict(
        case=case["name"],
        model="dsf",
        policy=prior["policy"] + "-quote-boundary-v1",
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
