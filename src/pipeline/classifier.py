"""Classify ad segments in transcripts using Claude API."""

import json
import time
from pathlib import Path

from anthropic import Anthropic

from src.database.models import ProcessingLog

PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"


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
        lines.append(f"[{start:.1f}s - {end:.1f}s] {text}")
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


async def classify_ads(
    transcript_path: Path,
    api_key: str,
    model: str = "claude-sonnet-4-5-20250929",
    max_tokens: int = 4096,
    confidence_threshold: float = 0.7,
) -> tuple[list[dict], str, ProcessingLog]:
    """
    Send transcript to Claude for ad segment classification.

    Args:
        transcript_path: Path to transcript JSON file.
        api_key: Anthropic API key.
        model: Claude model to use.
        max_tokens: Max response tokens.
        confidence_threshold: Minimum confidence to keep a segment.

    Returns:
        Tuple of (filtered_ad_segments, raw_json_response, ProcessingLog).
    """
    start = time.monotonic()

    with open(transcript_path) as f:
        segments = json.load(f)

    transcript_text = _format_transcript_for_prompt(segments)
    prompt_template = _load_prompt()
    prompt = prompt_template.format(transcript=transcript_text)

    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    raw_text = response.content[0].text
    cleaned = _clean_json_response(raw_text)
    result = json.loads(cleaned)

    # Filter by confidence threshold
    all_segments = result.get("ad_segments", [])
    filtered = [s for s in all_segments if s.get("confidence", 0) >= confidence_threshold]

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
