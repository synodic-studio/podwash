"""Deterministic guardrails for ad classification."""

from __future__ import annotations

import json

import pytest

from src.pipeline import classifier
from src.pipeline.classifier import _detect_obvious_ad_segments


def test_detects_brought_to_you_by_preroll_ad():
    segments = [
        {"start": 87.9, "end": 91.6, "text": "Coming up on the show, Franklin Leonard on Hollywood."},
        {"start": 100.2, "end": 102.9, "text": "This message is brought to you by Mariner."},
        {"start": 102.9, "end": 108.9, "text": "You've built your book. You've built your reputation."},
        {"start": 120.5, "end": 128.2, "text": "Take the next step in your career at joinmariner.com."},
        {"start": 137.0, "end": 142.0, "text": "It is so much more fun recording this podcast here."},
    ]

    ads = _detect_obvious_ad_segments(segments)

    assert ads == [
        {
            "start": 100.2,
            "end": 128.2,
            "type": "pre_roll",
            "confidence": 0.99,
            "sponsor": "unknown",
            "reason": "Obvious sponsor message matched deterministic ad phrase",
        }
    ]


def test_obvious_ad_detector_does_not_flag_editorial_mentions():
    segments = [
        {"start": 10.0, "end": 20.0, "text": "Today's story is about Uber's safety policies."},
        {"start": 20.0, "end": 30.0, "text": "The company says its app has changed."},
    ]

    assert _detect_obvious_ad_segments(segments) == []


class _StubAnthropic:
    """Records the rendered prompt and replays a canned classification."""

    last_prompt: str | None = None

    def __init__(self, api_key):
        self.messages = self

    def create(self, model, max_tokens, messages):
        type(self).last_prompt = messages[0]["content"]
        body = json.dumps(
            {
                "summary": "1 ad",
                "ad_segments": [
                    {"start": 100.2, "end": 128.2, "confidence": 0.95, "type": "pre_roll"},
                    {"start": 200.0, "end": 210.0, "confidence": 0.10, "type": "mid_roll"},
                ],
            }
        )
        return type(
            "R", (), {"content": [type("C", (), {"text": body})()]}
        )()


@pytest.mark.asyncio
async def test_classify_ads_takes_parsed_segments_and_filters_by_confidence(monkeypatch):
    """The worker parses the transcript once and hands the segments in."""
    monkeypatch.setattr(classifier, "Anthropic", _StubAnthropic)
    segments = [
        {"start": 100.2, "end": 102.9, "text": "This message is brought to you by Mariner."},
        {"start": 120.5, "end": 128.2, "text": "Take the next step at joinmariner.com."},
        {"start": 200.0, "end": 210.0, "text": "Some ordinary discussion."},
    ]

    filtered, raw_json, log = await classifier.classify_ads(
        segments, api_key="k", confidence_threshold=0.7
    )

    # Transcript reached the prompt at one decimal place — _SNAP_TOLERANCE in
    # the editor is sized against exactly this rendering.
    assert "[100.2s - 102.9s] This message is brought to you by Mariner." in (
        _StubAnthropic.last_prompt
    )
    # The 0.10-confidence segment is dropped; the sponsor read survives.
    assert [(s["start"], s["end"]) for s in filtered] == [(100.2, 128.2)]
    assert json.loads(raw_json)["summary"] == "1 ad"
    assert log.stage == "classify" and log.status == "success"
