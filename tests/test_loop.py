"""Tests for src.worker.loop: a hung pipeline stage must not hang the worker forever."""

import asyncio
from unittest.mock import MagicMock

import pytest

from src.config import Settings
from src.worker.client import Job
from src.worker.loop import run_once


def _make_job() -> Job:
    return Job(
        episode_id=1,
        feed_id=1,
        guid="guid-1",
        title="Test Episode",
        source_audio_url="https://example.com/ep1.mp3",
        duration_seconds=60,
        claim_token="token-1",
    )


@pytest.mark.asyncio
async def test_run_once_times_out_when_pipeline_hangs(monkeypatch):
    """A pipeline stage that never returns must be treated as a failure, not
    left running forever — this is what let a whisper hallucination loop
    keep the worker mid-job for hours while the server's queue watchdog had
    already reset the claim back to pending."""
    settings = Settings()
    settings.worker.stale_minutes = 0  # forces an immediate timeout in the test

    client = MagicMock()
    client.claim_next.return_value = _make_job()

    async def _hang(*args, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr("src.worker.loop._process_and_upload", _hang)

    had_job = await run_once(client, settings)

    assert had_job is True
    client.submit_failure.assert_called_once()
    args, kwargs = client.submit_failure.call_args
    assert kwargs["claim_token"] == "token-1"
    assert "TimeoutError" in args[1] or "TimeoutError" in kwargs.get("error", "")
