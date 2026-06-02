"""Deterministic guardrails for ad classification."""

from __future__ import annotations

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
