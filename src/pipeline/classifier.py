"""Classify ad segments in transcripts via Claude or an OpenAI-compatible API."""

import json
import re
import time
from pathlib import Path

import httpx
from anthropic import Anthropic

from src.database.models import ProcessingLog

PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"
_OBVIOUS_AD_PHRASES = (
    "this message is brought to you by",
    "this episode is brought to you by",
    "today's sponsor is",
    "todays sponsor is",
)

# The other standard sponsor-read opener, which names the show between the
# two halves ("Support for The Daily comes from Instagram"). The show name
# varies per feed, so this needs a pattern rather than a literal.
#
# What sits between the halves is the discriminator and why this is
# case-sensitive: a sponsor read names a show ("The Daily", "this podcast"),
# while ordinary speech names a thing ("support for the bill comes from
# both parties"). Matching case-insensitively would flag the latter.
_OBVIOUS_AD_PATTERNS = (
    re.compile(
        r"[Ss]upport for "
        r"(?:this (?:podcast|show|series)|[A-Z][\w']*(?:\s+[A-Z][\w']*){0,3})"
        r" comes from"
    ),
    re.compile(r"this (?:episode|podcast) is sponsored by", re.I),
)


def _is_obvious_ad_opener(text: str) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in _OBVIOUS_AD_PHRASES) or any(
        pattern.search(text) for pattern in _OBVIOUS_AD_PATTERNS
    )

# A sponsor read ends on its call to action, so these close the segment.
_CTA_MARKERS = (".com", "learn more", "visit")

# Bounds on how far a sponsor read may extend past its opening phrase.
_MAX_LOOKAHEAD_SEGMENTS = 7
_MAX_LOOKAHEAD_GAP_SECONDS = 12

# An ad starting this early is a pre-roll rather than a mid-roll.
_PRE_ROLL_CUTOFF_SECONDS = 180

# Decimal places used when rendering timestamps for the prompt. The editor's
# boundary snapping must tolerate at least this much rounding error — see
# `_SNAP_TOLERANCE` in src/pipeline/editor.py before changing it.
_TIMESTAMP_DECIMALS = 1


def _load_prompt() -> str:
    """Load the ad detection prompt template."""
    return (PROMPTS_DIR / "ad_detection.txt").read_text(encoding="utf-8")


def _format_transcript_for_prompt(segments: list[dict]) -> str:
    """Format transcript segments into a readable text with timestamps."""
    lines = []
    for seg in segments:
        start = seg["start"]
        end = seg["end"]
        text = seg["text"]
        lines.append(
            f"[{start:.{_TIMESTAMP_DECIMALS}f}s - {end:.{_TIMESTAMP_DECIMALS}f}s] {text}"
        )
    return "\n".join(lines)


def _clean_json_response(text: str) -> str:
    """Strip markdown code blocks to extract raw JSON."""
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0]
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        return text[start : end + 1]
    return text.strip()


def _detect_obvious_ad_segments(segments: list[dict]) -> list[dict]:
    """Catch high-confidence sponsor reads the model occasionally misses.

    This is intentionally narrow: it only starts on explicit sponsor-read
    phrases, then extends through nearby transcript segments until a CTA/URL
    or a large timestamp gap. Editorial discussion of a company should not
    match because it lacks the sponsor-read phrase.
    """
    ads: list[dict] = []
    for idx, seg in enumerate(segments):
        if not _is_obvious_ad_opener(str(seg.get("text", ""))):
            continue

        start = float(seg["start"])
        end = float(seg["end"])
        for next_seg in segments[idx + 1 : idx + 1 + _MAX_LOOKAHEAD_SEGMENTS]:
            if float(next_seg["start"]) - end > _MAX_LOOKAHEAD_GAP_SECONDS:
                break
            end = float(next_seg["end"])
            next_text = str(next_seg.get("text", "")).lower()
            if any(marker in next_text for marker in _CTA_MARKERS):
                break

        ads.append(
            {
                "start": start,
                "end": end,
                "type": "pre_roll" if start < _PRE_ROLL_CUTOFF_SECONDS else "mid_roll",
                "confidence": 0.99,
                "sponsor": "unknown",
                "reason": "Obvious sponsor message matched deterministic ad phrase",
            }
        )
    return ads


def _merge_ad_segments(model_segments: list[dict], guardrail_segments: list[dict]) -> list[dict]:
    merged = list(model_segments)
    for guardrail in guardrail_segments:
        for existing in merged:
            if guardrail["start"] <= existing["end"] and existing["start"] <= guardrail["end"]:
                existing["start"] = min(existing["start"], guardrail["start"])
                existing["end"] = max(existing["end"], guardrail["end"])
                existing["confidence"] = max(existing.get("confidence", 0), guardrail["confidence"])
                existing["reason"] = f"{existing.get('reason', '')}; {guardrail['reason']}".strip("; ")
                break
        else:
            merged.append(guardrail)
    return sorted(merged, key=lambda s: s["start"])


def _call_anthropic(prompt: str, *, api_key: str, model: str, max_tokens: int) -> str:
    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def _call_openai_compatible(
    prompt: str, *, base_url: str, model: str, max_tokens: int, api_key: str = ""
) -> str:
    """Classify via an OpenAI-compatible /chat/completions endpoint.

    Reasoning is explicitly disabled. A reasoning model asked to emit JSON
    will happily spend the entire output budget thinking and return empty
    content with finish_reason='length' -- the answer never gets written.
    Disabling it is also faster and produces better segments here.
    """
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "thinking": {"type": "disabled"},
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    with httpx.Client(timeout=600) as client:
        resp = client.post(
            f"{base_url.rstrip('/')}/chat/completions", json=body, headers=headers
        )
    resp.raise_for_status()
    payload = resp.json()
    choice = payload["choices"][0]
    text = choice["message"].get("content") or ""
    if not text:
        raise RuntimeError(
            f"{model} returned empty content "
            f"(finish_reason={choice.get('finish_reason')}, "
            f"completion_tokens={payload.get('usage', {}).get('completion_tokens')}). "
            "Raise max_tokens or confirm reasoning is disabled."
        )
    return text


async def classify_ads(
    segments: list[dict],
    api_key: str,
    model: str = "claude-sonnet-4-5-20250929",
    max_tokens: int = 4096,
    confidence_threshold: float = 0.7,
    backend: str = "claude",
    base_url: str = "",
) -> tuple[list[dict], str, ProcessingLog]:
    """
    Send transcript to a model for ad segment classification.

    Args:
        segments: Parsed Whisper transcript segments. The editor needs these
            too as its speech map, so the caller owns loading them.
        api_key: API key for the selected backend.
        model: Model identifier.
        max_tokens: Max response tokens.
        confidence_threshold: Minimum confidence to keep a segment.
        backend: "claude" for the Anthropic SDK, or "litellm"/"openai" for an
            OpenAI-compatible endpoint at `base_url`.
        base_url: Endpoint root for the OpenAI-compatible backend.

    Returns:
        Tuple of (filtered_ad_segments, raw_json_response, ProcessingLog).
    """
    start = time.monotonic()

    transcript_text = _format_transcript_for_prompt(segments)
    prompt_template = _load_prompt()
    prompt = prompt_template.format(transcript=transcript_text)

    if backend == "claude":
        raw_text = _call_anthropic(
            prompt, api_key=api_key, model=model, max_tokens=max_tokens
        )
    else:
        raw_text = _call_openai_compatible(
            prompt,
            base_url=base_url,
            model=model,
            max_tokens=max_tokens,
            api_key=api_key,
        )
    cleaned = _clean_json_response(raw_text)
    result = json.loads(cleaned)

    # Filter by confidence threshold, then add narrow deterministic guardrails
    # for obvious sponsor reads that are unacceptable to miss.
    all_segments = result.get("ad_segments", [])
    model_filtered = [
        s for s in all_segments if s.get("confidence", 0) >= confidence_threshold
    ]
    filtered = _merge_ad_segments(
        model_filtered,
        _detect_obvious_ad_segments(segments),
    )

    elapsed_ms = int((time.monotonic() - start) * 1000)
    summary = result.get("summary", f"Found {len(filtered)} ad segments")

    return (
        filtered,
        json.dumps(result, indent=2),
        ProcessingLog(
            episode_id=0,
            stage="classify",
            status="success",
            message=f"{summary} ({len(all_segments)} total, {len(filtered)} above threshold)",
            duration_ms=elapsed_ms,
        ),
    )
