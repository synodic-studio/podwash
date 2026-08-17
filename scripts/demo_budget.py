"""Beat 3 of scripts/demo.sh: the same prompt at two max_tokens values.

Uses the pipeline's own prompt loader and transcript formatter, so the request
is byte-for-byte what the worker sends -- only max_tokens changes between the
two runs. Nothing here asserts an outcome; it prints whatever finish_reason and
token count come back, and the operator narrates.
"""

import json
import os
import sys
import time

import httpx

from src.config import LiteLLMConfig
from src.pipeline.classifier import (
    _clean_json_response,
    _format_transcript_for_prompt,
    _load_prompt,
)

TRANSCRIPT = "scripts/demo-transcript.json"
# Small enough that the answer cannot fit. The contrast is the whole beat: this
# one fails loudly, and the budget that costs ad segments does not.
TRUNCATING_BUDGET = 256


def post(proxy: str, body: dict, key: str) -> httpx.Response:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    for attempt in (1, 2):
        resp = httpx.post(
            f"{proxy}/chat/completions", json=body, headers=headers, timeout=180
        )
        # Retry the same alias. Escalating to a bigger model on a 429 is how a
        # cheap pipeline quietly stops being cheap.
        if resp.status_code == 429 and attempt == 1:
            print("  429 -- retrying the same alias in 2s")
            time.sleep(2)
            continue
        break
    return resp


def run(proxy: str, model: str, key: str, max_tokens: int, prompt: str, run_dir: str) -> None:
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "thinking": {"type": "disabled"},
        "extra_body": {"thinking": {"type": "disabled"}},
        # An A/B run against the proxy's response cache is not an A/B. This is
        # the only field here that differs from what the worker sends.
        "cache": {"no-cache": True},
    }
    started = time.monotonic()
    resp = post(proxy, body, key)
    elapsed = time.monotonic() - started

    if resp.status_code != 200:
        print(f"max_tokens={max_tokens:<6} HTTP {resp.status_code}  {resp.text[:160]}")
        return

    payload = resp.json()
    choice = payload["choices"][0]
    text = choice["message"].get("content") or ""
    finish = choice.get("finish_reason")
    used = payload.get("usage", {}).get("completion_tokens")

    try:
        parsed = json.loads(_clean_json_response(text))
        verdict = f"{len(parsed.get('ad_segments', []))} ad segments"
        summary = parsed.get("summary", "")
    except Exception as exc:  # noqa: BLE001 -- the failure IS the artifact here
        parsed = None
        verdict = f"UNPARSEABLE ({type(exc).__name__})"
        summary = text[-70:].replace("\n", " ") if text else "(empty content)"

    print(f"max_tokens={max_tokens:<6} finish_reason={str(finish):<8} "
          f"completion_tokens={str(used):<6} {elapsed:.1f}s  ->  {verdict}")
    print(f'{"":<12} model says: {summary}')

    out = os.path.join(run_dir, f"budget-{max_tokens}.json")
    with open(out, "w") as fh:
        json.dump(
            {
                "max_tokens": max_tokens,
                "finish_reason": finish,
                "completion_tokens": used,
                "elapsed_seconds": round(elapsed, 2),
                "parsed": parsed,
                "raw_content": text,
            },
            fh,
            indent=2,
        )
    print(f'{"":<12} {out}\n')


def main() -> int:
    proxy = os.environ["PROXY"]
    model = os.environ["MODEL"]
    key = os.environ.get("DEMO_KEY", "")
    run_dir = os.environ["RUN"]

    segments = json.load(open(TRANSCRIPT))
    prompt = _load_prompt().format(transcript=_format_transcript_for_prompt(segments))
    print(f"prompt         {len(prompt):,} chars, identical in both runs\n")

    for budget in (TRUNCATING_BUDGET, LiteLLMConfig().max_tokens):
        run(proxy, model, key, budget, prompt, run_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
