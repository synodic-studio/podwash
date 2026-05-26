"""SSRF-safe RSS fetcher validation."""

from __future__ import annotations

import pytest

from src.feeds.fetcher import FeedFetchError, fetch_public_feed


@pytest.mark.asyncio
async def test_rejects_file_scheme():
    with pytest.raises(FeedFetchError):
        await fetch_public_feed("file:///etc/passwd")


@pytest.mark.asyncio
async def test_rejects_localhost():
    with pytest.raises(FeedFetchError):
        await fetch_public_feed("http://localhost/feed.xml")


@pytest.mark.asyncio
async def test_rejects_loopback_ip():
    with pytest.raises(FeedFetchError):
        await fetch_public_feed("http://127.0.0.1/feed.xml")


@pytest.mark.asyncio
async def test_rejects_private_ip():
    with pytest.raises(FeedFetchError):
        await fetch_public_feed("http://192.168.1.1/feed.xml")


@pytest.mark.asyncio
async def test_rejects_metadata_ip():
    with pytest.raises(FeedFetchError):
        await fetch_public_feed("http://169.254.169.254/latest/meta-data/")


@pytest.mark.asyncio
async def test_rejects_empty_url():
    with pytest.raises(FeedFetchError):
        await fetch_public_feed("")
