"""Editor: ffmpeg/ffprobe error and output-validity checks."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.pipeline import editor


def test_compute_keep_segments_bounds_and_overlaps():
    keep = editor._compute_keep_segments([(10, 20), (50, 60)], duration=100.0)
    assert keep == [(0.0, 10.0), (20.0, 50.0), (60.0, 100.0)]


def test_compute_keep_segments_merges_overlapping_cuts():
    # Snapping can push two cuts into each other; the union must be removed once.
    keep = editor._compute_keep_segments([(10, 55), (50, 60)], duration=100.0)
    assert keep == [(0.0, 10.0), (60.0, 100.0)]


def test_compute_keep_segments_drops_tiny_remnants():
    keep = editor._compute_keep_segments([(0, 99.95)], duration=100.0)
    # leftover < 0.1s should be discarded
    assert keep == []


# --- speech interval extraction -------------------------------------------


def _spoken(*spans):
    """Build a transcript with word-level timings for the given spans."""
    return [
        {
            "start": s,
            "end": e,
            "text": "words",
            "words": [{"start": s, "end": e, "word": "w"}],
        }
        for s, e in spans
    ]


def test_speech_intervals_absent_transcript_is_empty():
    assert editor._speech_intervals(None) == []
    assert editor._speech_intervals([]) == []


def test_speech_intervals_merges_and_sorts():
    assert editor._speech_intervals(_spoken((10, 12), (0, 5), (4, 8))) == [
        (0.0, 8.0),
        (10.0, 12.0),
    ]


def test_speech_intervals_falls_back_to_segment_span_without_words():
    segs = [{"start": 3.0, "end": 9.0, "text": "no word timings", "words": []}]
    assert editor._speech_intervals(segs) == [(3.0, 9.0)]


def test_speech_intervals_skips_words_missing_timestamps():
    segs = [
        {
            "start": 1.0,
            "end": 4.0,
            "text": "partial",
            "words": [
                {"start": None, "end": 2.0, "word": "a"},
                {"start": 2.0, "end": 4.0, "word": "b"},
            ],
        }
    ]
    assert editor._speech_intervals(segs) == [(2.0, 4.0)]


# --- dead-space snapping ---------------------------------------------------


def test_no_transcript_falls_back_to_plain_padding():
    ranges = editor._resolve_cut_ranges(
        [{"start": 50, "end": 60}], speech=[], duration=100.0, padding=0.5
    )
    # With no speech map, everything before is "dead space" and gets absorbed,
    # so the boundary snaps to the file edges rather than padding.
    assert ranges == [(0.0, 100.0)]


def test_boundary_inside_speech_keeps_padding():
    # Ad runs 50-60 with words straddling both boundaries: no dead space.
    speech = [(48.0, 52.0), (58.0, 62.0)]
    ranges = editor._resolve_cut_ranges(
        [{"start": 50, "end": 60}], speech, duration=100.0, padding=0.5
    )
    assert ranges == [(49.5, 60.5)]


def test_outro_music_before_post_roll_is_absorbed():
    # Content ends at 1290, music until the post-roll ad's first word at 1328.
    speech = [(1200.0, 1290.0), (1328.0, 1371.0)]
    ranges = editor._resolve_cut_ranges(
        [{"start": 1328, "end": 1371}], speech, duration=1400.0, padding=0.5
    )
    # Cut starts at the last real word, not 0.5s before the ad — and no
    # padding is applied there, so the final word survives intact.
    assert ranges == [(1290.0, 1400.0)]


def test_trailing_dead_space_absorbed_up_to_next_speech():
    speech = [(100.0, 200.0), (260.0, 300.0)]
    ranges = editor._resolve_cut_ranges(
        [{"start": 205, "end": 240}], speech, duration=400.0, padding=0.5
    )
    assert ranges == [(200.0, 260.0)]


def test_short_pause_is_not_dead_space():
    # 0.2s gaps either side are normal speech rhythm, below _MIN_DEAD_SPACE.
    speech = [(40.0, 49.8), (50.0, 60.0), (60.2, 70.0)]
    ranges = editor._resolve_cut_ranges(
        [{"start": 50, "end": 60}], speech, duration=100.0, padding=0.5
    )
    assert ranges == [(49.5, 60.5)]


def test_pre_roll_absorbs_leading_audio_to_zero():
    # No speech before the pre-roll: intro theme music goes with it.
    speech = [(12.0, 40.0), (50.0, 90.0)]
    ranges = editor._resolve_cut_ranges(
        [{"start": 12, "end": 40}], speech, duration=100.0, padding=0.5
    )
    assert ranges == [(0.0, 50.0)]


def test_ad_at_end_of_file_absorbs_trailing_audio():
    speech = [(10.0, 80.0)]
    ranges = editor._resolve_cut_ranges(
        [{"start": 60, "end": 80}], speech, duration=100.0, padding=0.5
    )
    assert ranges == [(59.5, 100.0)]


def test_two_ads_split_by_dead_space_close_the_gap():
    # Credits 1292-1308, silence, post-roll 1328-1371 — the 20s between goes.
    speech = [(1000.0, 1292.0), (1292.0, 1308.0), (1328.0, 1371.0)]
    ranges = editor._resolve_cut_ranges(
        [{"start": 1292, "end": 1308}, {"start": 1328, "end": 1371}],
        speech,
        duration=1400.0,
        padding=0.5,
    )
    keep = editor._compute_keep_segments(ranges, duration=1400.0)
    # Nothing survives after the last content word: credits, the 20s of dead
    # space, and the post-roll all collapse into one cut running to EOF.
    # 1291.5 rather than 1292.0 because credits begin mid-speech, so the
    # normal padding still applies on that boundary.
    assert keep == [(0.0, 1291.5)]


def test_extension_is_capped_by_max_dead_space():
    # A pathological transcript (one word at t=0) must not gut the whole file.
    speech = [(0.0, 1.0)]
    ranges = editor._resolve_cut_ranges(
        [{"start": 5000, "end": 5010}],
        speech,
        duration=9000.0,
        padding=0.5,
    )
    assert ranges == [
        (5000.0 - editor._MAX_DEAD_SPACE_EXTEND, 5010.0 + editor._MAX_DEAD_SPACE_EXTEND)
    ]


class _FakeProc:
    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


@pytest.mark.asyncio
async def test_get_duration_raises_on_nonzero_returncode(monkeypatch):
    async def _exec(*args, **kw):
        return _FakeProc(1, b"", b"corrupt")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    with pytest.raises(RuntimeError, match="ffprobe failed"):
        await editor._get_duration(Path("x.mp3"))


@pytest.mark.asyncio
async def test_get_duration_raises_on_empty_output(monkeypatch):
    async def _exec(*args, **kw):
        return _FakeProc(0, b"", b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    with pytest.raises(RuntimeError, match="empty duration"):
        await editor._get_duration(Path("x.mp3"))


@pytest.mark.asyncio
async def test_get_duration_raises_on_nonpositive(monkeypatch):
    async def _exec(*args, **kw):
        return _FakeProc(0, b"0\n", b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    with pytest.raises(RuntimeError, match="non-positive"):
        await editor._get_duration(Path("x.mp3"))


@pytest.mark.asyncio
async def test_no_ad_copy_raises_on_ffmpeg_failure(monkeypatch, tmp_path):
    async def _exec(*args, **kw):
        return _FakeProc(1, b"", b"explode")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    with pytest.raises(RuntimeError, match="ffmpeg copy failed"):
        await editor.cut_ads(
            tmp_path / "in.mp3", tmp_path / "out.mp3", ad_segments=[]
        )


@pytest.mark.asyncio
async def test_no_ad_copy_raises_on_empty_output(monkeypatch, tmp_path):
    out = tmp_path / "out.mp3"

    async def _exec(*args, **kw):
        # Simulate ffmpeg "success" but produce a zero-byte file.
        out.write_bytes(b"")
        return _FakeProc(0, b"", b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
    with pytest.raises(RuntimeError, match="empty output"):
        await editor.cut_ads(tmp_path / "in.mp3", out, ad_segments=[])
