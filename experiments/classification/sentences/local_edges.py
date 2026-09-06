"""Classify clauses only within mixed boundary sentences, retaining block interior."""

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
    clauses = units(case["words"], clauses=True)
    spans = []
    rows = []
    chosen = []
    for r in sorted(prior["parsed"]["ranges"], key=lambda r: r["first"]):
        if spans and r["first"] == spans[-1]["last"] + 1:
            spans[-1]["last"] = r["last"]
        else:
            spans.append(dict(first=r["first"], last=r["last"]))
    template = (
        Path("experiments/classification/sentences/prompt-v2.txt")
        .read_text()
        .replace("sentence", "clause")
        .replace("Sentence", "Clause")
    )
    for span in spans:
        cut = dict(span)
        for edge in ["first", "last"]:
            sentence = next(
                s for s in sentences if s["first"] <= span[edge] <= s["last"]
            )
            parts = [
                c
                for c in clauses
                if sentence["first"] <= c["first"] <= sentence["last"]
            ]
            if len(parts) < 2:
                continue
            context = clauses[max(0, parts[0]["id"] - 3) : parts[-1]["id"] + 4]
            valid = {c["id"] for c in context}
            prompt = (
                template
                + "\n".join(f"[{c['id']}] {c['text']}" for c in context)
                + f"\nReturn exactly {len(context)} decisions for IDs {context[0]['id']} through {context[-1]['id']} inclusive."
            )
            t = time.monotonic()
            r = requests.post(
                "http://localhost:4000/v1/chat/completions",
                json=request_body("dsf", prompt),
                timeout=180,
            )
            r.raise_for_status()
            d = r.json()
            text = d["choices"][0]["message"]["content"]
            row = dict(
                span=span,
                edge=edge,
                output_text=text,
                response_id=d["id"],
                cost_usd=float(r.headers.get("x-litellm-response-cost", 0)),
                usage=d.get("usage"),
                prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                seconds=time.monotonic() - t,
            )
            try:
                assert d["choices"][0]["finish_reason"] == "stop"
                p = json.loads(text[text.index("{") : text.rindex("}") + 1])
                labels = {a["id"]: a["label"] for a in p["decisions"]}
                assert (
                    len(p["decisions"]) == len(valid)
                    and set(labels) == valid
                    and all(v in {"E", "A", "P", "C", "T"} for v in labels.values())
                )
                selected = [c for c in parts if labels[c["id"]] != "E"]
                # Only partial-edge adjustment. If the entire boundary disagrees, preserve
                # the original block and record disagreement rather than deleting the block.
                if selected:
                    cut[edge] = (
                        selected[0]["first"]
                        if edge == "first"
                        else selected[-1]["last"]
                    )
                row["labels"] = labels
                row["whole_boundary_disagreement"] = not bool(selected)
            except (ValueError, KeyError, AssertionError):
                row["error"] = "Invalid clause decisions; retained original boundary"
            rows.append(row)
            (output / "detail.json").write_text(json.dumps(rows, indent=2))
            print(
                json.dumps(
                    {
                        k: v
                        for k, v in row.items()
                        if k
                        in [
                            "edge",
                            "span",
                            "error",
                            "whole_boundary_disagreement",
                            "cost_usd",
                        ]
                    }
                ),
                flush=True,
            )
        if cut["first"] > cut["last"]:
            cut = dict(span)
        chosen.append(cut)
    result = dict(
        case=case["name"],
        model="dsf",
        policy=prior["policy"] + "-local-clause-edges",
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
