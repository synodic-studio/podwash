"""Stdlib-only classifier evals. Scoring is offline; only `run` calls a model."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]


def expand(ranges):
    return {i for first, last in ranges for i in range(first, last + 1)}


def load_cases():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    transcripts = {}
    for name, source in manifest["transcripts"].items():
        raw = gzip.decompress((ROOT / source["path"]).read_bytes())
        if hashlib.sha256(raw).hexdigest() != source["transcript_sha256"]:
            raise ValueError(f"Transcript checksum mismatch: {name}")
        transcripts[name] = json.loads(raw)
    result = {}
    for definition in json.loads((ROOT / "cases.json").read_text()):
        include = expand(definition["include_ranges"])
        remove = expand(definition["remove_ranges"])
        keep = expand(definition["keep_ranges"])
        segments = []
        index = 0
        for segment in transcripts[definition["transcript"]]:
            words = []
            for word in segment["words"]:
                if index in include:
                    words.append(dict(word, id=index))
                index += 1
            if words:
                segments.append(dict(segment, words=words))
        words = [word for segment in segments for word in segment["words"]]
        ids = {word["id"] for word in words}
        if ids != include or remove & keep or not remove | keep <= ids:
            raise ValueError(f"Inconsistent labels: {definition['name']}")
        result[definition["name"]] = dict(
            definition,
            segments=segments,
            words=words,
            remove_ids=sorted(remove),
            keep_ids=sorted(keep),
        )
    return result


def score(case, payload, policy):
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    words = case["words"]
    valid = {word["id"] for word in words}
    selected = set()
    field = "ad_segments" if policy == "current" else "ranges"
    spans = payload.get(field)
    if not isinstance(spans, list):
        raise ValueError(f"Missing or invalid {field}")
    for span in spans:
        if not isinstance(span, dict):
            raise ValueError("Each range must be an object")
        if policy != "current":
            first, last = span.get("first"), span.get("last")
            if type(first) is not int or type(last) is not int:
                raise ValueError("Word IDs must be integers")
            if first > last or first not in valid or last not in valid:
                raise ValueError("Invalid word range")
            ids = set(range(first, last + 1))
            if not ids <= valid:
                raise ValueError("Range crosses omitted transcript words")
            selected.update(ids)
        else:
            start, end, confidence = (
                span.get("start"),
                span.get("end"),
                span.get("confidence", 1),
            )
            if any(
                type(value) not in (int, float) or not math.isfinite(value)
                for value in (start, end, confidence)
            ):
                raise ValueError("Timestamps and confidence must be finite numbers")
            if start > end or not 0 <= confidence <= 1:
                raise ValueError("Invalid timestamps or confidence")
            if confidence >= 0.7:
                selected.update(
                    word["id"]
                    for word in words
                    if start <= (word["start"] + word["end"]) / 2 <= end
                )
    remove, keep = set(case["remove_ids"]), set(case["keep_ids"])
    missed, lost = remove - selected, keep & selected
    return {
        "missed_removable_words": len(missed),
        "lost_content_words": len(lost),
        "removable_words": len(remove),
        "content_words": len(keep),
        "missed_ids": sorted(missed),
        "lost_ids": sorted(lost),
        "unscored_selected_ids": sorted(selected - remove - keep),
    }


def request_body(model, prompt):
    body = {
        "model": model,
        "max_tokens": 16000,
        "messages": [{"role": "user", "content": prompt}],
        "cache": {"no-cache": True, "no-store": True},
    }
    if model == "dsf":
        body.update(
            thinking={"type": "disabled"}, extra_body={"thinking": {"type": "disabled"}}
        )
    if model in ("gpt", "glm"):
        body["reasoning_effort"] = "low"
    return body


def build_prompt(case, policy):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", policy):
        raise ValueError("Invalid prompt name")
    template = (ROOT / "prompts" / f"{policy}.txt").read_text()
    if policy == "current":
        transcript = "\n".join(
            f"[{segment['start']:.1f}s - {segment['end']:.1f}s] {segment['text']}"
            for segment in case["segments"]
        )
        return template.format(transcript=transcript)
    transcript = "\n".join(
        " ".join(f"[{word['id']}]{word['word'].strip()}" for word in segment["words"])
        for segment in case["segments"]
    )
    return template + transcript


def score_saved(paths, cases):
    rows = []
    for path in paths:
        records = json.loads(path.read_text())
        if isinstance(records, dict):
            records = [records]
        for saved in records:
            if saved.get("excluded"):
                continue
            row = {
                key: saved.get(key)
                for key in ("model", "policy", "case", "cost_usd", "error")
            }
            row["run"] = str(path)
            if not row["error"]:
                try:
                    row.update(
                        score(cases[saved["case"]], saved["parsed"], saved["policy"])
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    row["error"] = str(exc)
            rows.append(row)
    return rows


def live_run(args, cases):
    selected = args.case or [
        name for name, case in cases.items() if case["split"] == "development"
    ]
    unknown = set(selected) - set(cases)
    if unknown:
        raise ValueError(f"Unknown cases: {sorted(unknown)}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = args.output or PROJECT / "data" / "classification-eval" / "runs" / stamp
    if not args.dry_run:
        output.mkdir(parents=True, exist_ok=True)
    failed = False
    for name in selected:
        case = cases[name]
        prompt = build_prompt(case, args.prompt)
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "case": name,
                        "model": args.model,
                        "prompt": args.prompt,
                        "input_characters": len(prompt),
                        "api_called": False,
                    }
                )
            )
            continue
        safe_model = re.sub(r"[^a-zA-Z0-9_.-]", "_", args.model)
        path = output / f"{safe_model}-{args.prompt}-{name}.json"
        if path.exists():
            raise ValueError(f"Refusing to overwrite saved run: {path}")
        row = dict(
            case=name,
            model=args.model,
            policy=args.prompt,
            prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        headers = {"Content-Type": "application/json"}
        if key := os.environ.get("PODWASH_EVAL_API_KEY"):
            headers["Authorization"] = f"Bearer {key}"
        request = urllib.request.Request(
            args.api_base.rstrip("/") + "/chat/completions",
            data=json.dumps(request_body(args.model, prompt)).encode(),
            headers=headers,
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=240) as response:
                data = json.load(response)
                cost = response.headers.get("x-litellm-response-cost")
                row["cost_usd"] = float(cost) if cost is not None else None
                row["cache_headers"] = {
                    k: v for k, v in response.headers.items() if "cache" in k.lower()
                }
            row.update(
                response_id=data.get("id"),
                response_model=data.get("model"),
                usage=data.get("usage"),
            )
            choice = data["choices"][0]
            if choice.get("finish_reason") not in ("stop", "end_turn"):
                raise ValueError(f"Incomplete output: {choice.get('finish_reason')}")
            text = choice["message"].get("content") or ""
            row["output_text"] = text
            row["parsed"] = json.loads(text[text.index("{") : text.rindex("}") + 1])
            row["score"] = score(case, row["parsed"], args.prompt)
        except (
            ValueError,
            KeyError,
            TypeError,
            urllib.error.URLError,
            TimeoutError,
        ) as exc:
            row["error"] = str(exc)
            failed = True
        row["seconds"] = round(time.monotonic() - started, 3)
        with path.open("x") as handle:
            json.dump(row, handle, indent=2)
            handle.write("\n")
        print(
            json.dumps(
                {
                    key: row[key]
                    for key in ("case", "model", "cost_usd", "score", "error")
                    if key in row
                }
            )
        )
    if not args.dry_run:
        print(f"Saved runs: {output}")
    return int(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "check",
        help="Validate saved transcript checksums and reference labels; no API calls",
    )
    scoring = commands.add_parser(
        "score", help="Score recorded predictions locally; no API calls"
    )
    scoring.add_argument("--results", type=Path, default=ROOT / "runs")
    scoring.add_argument("--json", action="store_true")
    running = commands.add_parser(
        "run", help="Explicitly call the configured model API (may incur charges)"
    )
    running.add_argument("--model", default="dsf")
    running.add_argument(
        "--prompt",
        default="current",
        help="Template name under prompts/: current uses timestamps, others use word IDs",
    )
    running.add_argument("--case", action="append")
    running.add_argument("--output", type=Path)
    running.add_argument("--api-base", default="http://localhost:4000/v1")
    running.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    cases = load_cases()
    if args.command == "check":
        print(
            f"Validated {len(cases)} cases and all transcript checksums. No API calls."
        )
        return 0
    if args.command == "run":
        return live_run(args, cases)
    paths = (
        sorted(args.results.glob("*.json")) if args.results.is_dir() else [args.results]
    )
    if not paths:
        raise ValueError("No saved results found")
    rows = score_saved(paths, cases)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for row in rows:
            outcome = (
                ("ERROR: " + row["error"])
                if row["error"]
                else (
                    f"lost={row['lost_content_words']}/{row['content_words']} "
                    f"missed={row['missed_removable_words']}/{row['removable_words']}"
                )
            )
            cost = "?" if row["cost_usd"] is None else f"${row['cost_usd']:.5f}"
            print(
                f"{Path(row['run']).stem}: {row['model']} {row['policy']} {row['case']}: {outcome} cost={cost}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
