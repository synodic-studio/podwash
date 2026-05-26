"""Editor: ffmpeg/ffprobe error and output-validity checks."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.pipeline import editor


def test_compute_keep_segments_bounds_and_overlaps():
    duration = 100.0
    ads = [{"start": 10, "end": 20}, {"start": 50, "end": 60}]
    keep = editor._compute_keep_segments(ads, duration, padding=0.0)
    assert keep == [(0.0, 10.0), (20.0, 50.0), (60.0, 100.0)]


def test_compute_keep_segments_drops_tiny_remnants():
    keep = editor._compute_keep_segments(
        [{"start": 0, "end": 99.95}], duration=100.0, padding=0.0
    )
    # leftover < 0.1s should be discarded
    assert keep == []


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
