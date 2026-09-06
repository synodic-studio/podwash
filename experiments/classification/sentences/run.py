"""Exploratory sentence classification; run from repo root. API: cheap dsf only."""

import argparse
import json
import re
import sys
import time
import hashlib
from pathlib import Path
import requests

sys.path.insert(0, str(Path.cwd()))
from experiments.classification.evaluate import load_cases, request_body, score


def units(words, clauses=False):
    result = []
    pending = []
    for index, w in enumerate(words):
        following = words[index + 1]["word"].strip() if index + 1 < len(words) else ""
        # Split coordinated clauses with an explicit new subject; retain the
        # conjunction with the clause it introduces. No ad vocabulary involved.
        if (
            clauses
            and len(pending) >= 4
            and w["word"].strip().lower() in {"and", "but", "so"}
            and (
                following.lower().split("'")[0]
                in {"i", "we", "you", "they", "it", "he", "she", "this", "that"}
                or following[:1].isupper()
            )
        ):
            result.append(
                dict(
                    id=len(result),
                    first=pending[0]["id"],
                    last=pending[-1]["id"],
                    text=" ".join(x["word"].strip() for x in pending),
                )
            )
            pending = []
        pending.append(w)
        if (
            re.search(r"[.!?][\"\u201d\u2019]*$", w["word"].strip())
            or len(pending) >= 65
        ):
            result.append(
                dict(
                    id=len(result),
                    first=pending[0]["id"],
                    last=pending[-1]["id"],
                    text=" ".join(x["word"].strip() for x in pending),
                )
            )
            pending = []
    if pending:
        result.append(
            dict(
                id=len(result),
                first=pending[0]["id"],
                last=pending[-1]["id"],
                text=" ".join(x["word"].strip() for x in pending),
            )
        )
    return result


def run(case, prompt_path, output, clauses=False):
    output.mkdir(parents=True, exist_ok=False)
    sentences = units(case["words"], clauses=clauses)
    (output / "units.json").write_text(json.dumps(sentences, indent=2))
    template = prompt_path.read_text()
    rows = []
    chosen = []
    for start in range(0, len(sentences), 35):
        core = sentences[start : start + 35]
        context = sentences[max(0, start - 5) : start + 40]
        valid = {s["id"] for s in context}
        coreids = {s["id"] for s in core}
        prompt = (
            template
            + "\n".join(f"[{s['id']}] {s['text']}" for s in context)
            + f"\nReturn exactly {len(context)} decisions, for IDs {context[0]['id']} through {context[-1]['id']} inclusive."
        )
        t = time.monotonic()
        response = requests.post(
            "http://localhost:4000/v1/chat/completions",
            json=request_body("dsf", prompt),
            timeout=180,
        )
        response.raise_for_status()
        d = response.json()
        (output / f"raw-{start}.json").write_text(
            json.dumps(
                dict(
                    response=d, cost_usd=response.headers.get("x-litellm-response-cost")
                ),
                indent=2,
            )
        )
        choice = d["choices"][0]
        text = choice["message"]["content"]
        p = json.loads(text[text.index("{") : text.rindex("}") + 1])
        decisions = p["decisions"]
        assert choice["finish_reason"] == "stop"
        assert (
            len({a["id"] for a in decisions}) == len(decisions)
            and {a["id"] for a in decisions} <= valid
        )
        missing = sorted(valid - {a["id"] for a in decisions})
        # A malformed omission is a recorded preserve-content fallback, never an implicit successful label.
        decisions += [dict(id=i, label="E") for i in missing]
        assert all(a["label"] in "EAPCT" for a in decisions)
        for a in decisions:
            if a["id"] in coreids and a["label"] != "E":
                s = sentences[a["id"]]
                chosen.append(dict(first=s["first"], last=s["last"], label=a["label"]))
        row = dict(
            missing_ids=missing,
            core=[core[0]["id"], core[-1]["id"]],
            decisions=decisions,
            cost_usd=float(response.headers.get("x-litellm-response-cost", 0)),
            usage=d.get("usage"),
            response_id=d["id"],
            prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
            seconds=time.monotonic() - t,
        )
        rows.append(row)
        (output / "detail.json").write_text(json.dumps(rows, indent=2))
        print("Window", start, "cost", row["cost_usd"], flush=True)
    result = dict(
        case=case["name"],
        model="dsf",
        policy=prompt_path.stem + ("-clauses" if clauses else "-sentences"),
        parsed=dict(ranges=chosen),
        cost_usd=sum(r["cost_usd"] for r in rows),
    )
    result["score"] = score(case, result["parsed"], "words")
    (output / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result["score"]), flush=True)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--clauses", action="store_true")
    p.add_argument("--case", default="ologies_full")
    p.add_argument(
        "--prompt",
        type=Path,
        default=Path("experiments/classification/sentences/prompt-v1.txt"),
    )
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    run(load_cases()[a.case], a.prompt, a.output, a.clauses)
