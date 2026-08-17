"""Beat 2 of scripts/demo.sh: run the real classifier through the proxy.

This calls src.pipeline.classifier.classify_ads -- the same function the worker
calls in production -- with backend="litellm", so what the audience sees is the
shipped code path and not a demo-only reimplementation.

A separate one-token probe reads the routing headers back off the proxy, which
is the only way to show which upstream vendor actually served the request.
"""

import asyncio
import json
import os
import sys
import time

import httpx

from src.config import LiteLLMConfig, ProcessingConfig
from src.pipeline.classifier import classify_ads

TRANSCRIPT = "scripts/demo-transcript.json"
# Only these come from the proxy's response headers. The full header set
# includes spend fields; never dump it to a shared screen.
ROUTING_HEADERS = (
    "x-litellm-model-group",
    "x-litellm-model-api-base",
    "x-litellm-response-cost",
    "x-litellm-response-duration-ms",
    "x-litellm-attempted-retries",
)


def auth() -> dict:
    key = os.environ.get("DEMO_KEY", "")
    return {"Authorization": f"Bearer {key}"} if key else {}


def routing_probe(proxy: str, model: str, nonce: str) -> float | None:
    """One-token completion, purely to read the routing headers back.

    The nonce plus no-cache make every run a distinct upstream request, so the
    duration and cost below are always a real round trip. Returns that upstream
    duration in ms, which is the yardstick for spotting a cached classify.
    """
    body = {
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": f"ok {nonce}"}],
        "cache": {"no-cache": True},
    }
    for attempt in (1, 2):
        resp = httpx.post(
            f"{proxy}/chat/completions", json=body, headers=auth(), timeout=60
        )
        # A 429 means retry this same alias. Escalating to a bigger model to
        # dodge rate limits is how a cheap pipeline quietly stops being cheap.
        if resp.status_code == 429 and attempt == 1:
            print("  429 from the proxy -- retrying the same alias in 2s")
            time.sleep(2)
            continue
        break
    if resp.status_code != 200:
        print(f"  routing probe failed: HTTP {resp.status_code} {resp.text[:200]}")
        return None
    for name in ROUTING_HEADERS:
        if name in resp.headers:
            print(f"  {name:<32} {resp.headers[name]}")
    try:
        return float(resp.headers["x-litellm-response-duration-ms"])
    except (KeyError, ValueError):
        return None


async def main() -> int:
    proxy = os.environ["PROXY"]
    model = os.environ["MODEL"]
    run_dir = os.environ["RUN"]
    live = bool(os.environ.get("HAVE_PROXY"))

    defaults = LiteLLMConfig()
    threshold = ProcessingConfig().confidence_threshold
    segments = json.load(open(TRANSCRIPT))

    print(f"backend        litellm  ->  {proxy}/chat/completions")
    print(f"model alias    {model}")
    print(f"max_tokens     {defaults.max_tokens}   (src/config.py LiteLLMConfig default)")
    print(f"threshold      {threshold}   (segments below this confidence are dropped)")

    if not live:
        print("\nProxy is down, so nothing was sent. That request above is what would go.")
        return 0

    print("\nrouting, straight off the response headers:")
    upstream_ms = routing_probe(proxy, model, os.path.basename(run_dir.rstrip("/")))

    print("\n-> classify_ads(segments, backend='litellm', ...)")
    started = time.monotonic()
    try:
        ads, raw, log = await classify_ads(
            segments,
            api_key=os.environ.get("DEMO_KEY", ""),
            model=model,
            max_tokens=defaults.max_tokens,
            confidence_threshold=threshold,
            backend="litellm",
            base_url=proxy,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            print("  429 -- retrying the same alias in 2s")
            time.sleep(2)
            ads, raw, log = await classify_ads(
                segments,
                api_key=os.environ.get("DEMO_KEY", ""),
                model=model,
                max_tokens=defaults.max_tokens,
                confidence_threshold=threshold,
                backend="litellm",
                base_url=proxy,
            )
        else:
            print(f"  classify failed: HTTP {exc.response.status_code} {exc.response.text[:300]}")
            return 0
    except Exception as exc:  # noqa: BLE001 -- a demo prints failures, it does not raise
        print(f"  classify failed: {type(exc).__name__}: {exc}")
        return 0

    wall = time.monotonic() - started
    # log.message quotes the model's own summary back, which is the part a
    # hardcoded number could not fake.
    print(f"\nProcessingLog  {log.stage}/{log.status}  {log.duration_ms}ms "
          f"(wall {wall:.2f}s)")
    print(f"               {log.message}")
    # classify_ads is production code and cannot opt out of the proxy's
    # response cache. Say so rather than letting a 40ms round trip read as a
    # fast model -- the probe above is this run's real upstream latency.
    if upstream_ms and log.duration_ms < upstream_ms / 2:
        print(f"               served from the proxy's response cache "
              f"(a real upstream call this run took {upstream_ms:.0f}ms)")
    print()
    print(f"{'start':>8} {'end':>8} {'type':<14} {'conf':>5}  sponsor / reason")
    total = 0.0
    for ad in ads:
        total += ad["end"] - ad["start"]
        reason = str(ad.get("reason", ""))[:60]
        print(f'{ad["start"]:>8.1f} {ad["end"]:>8.1f} {ad.get("type", "?"):<14} '
              f'{ad.get("confidence", 0):>5.2f}  {ad.get("sponsor", "?")} — {reason}')
    print(f"\n{total:.0f}s of {segments[-1]['end']:.0f}s would be cut ({total / segments[-1]['end'] * 100:.0f}%)")

    out = os.path.join(run_dir, "classify.json")
    with open(out, "w") as fh:
        json.dump({"kept": ads, "raw_model_response": json.loads(raw)}, fh, indent=2)
    print(f"\n  {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
