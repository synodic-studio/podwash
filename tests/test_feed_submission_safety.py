"""SSRF-safe RSS fetcher validation."""

from __future__ import annotations

import pytest

from src.feeds.fetcher import (
    DEFAULT_MAX_BYTES,
    FeedFetchError,
    _trim_to_complete_rss_prefix,
    fetch_public_feed,
)


def test_default_feed_size_limit_stays_small():
    assert DEFAULT_MAX_BYTES == 5_000_000


def test_trim_to_complete_rss_prefix_returns_well_formed_recent_items():
    partial = (
        b"<?xml version='1.0'?><rss><channel><title>x</title>"
        b"<item><title>newest</title></item>"
        b"<item><title>older</title></item>"
        b"<item><title>truncated"
    )

    trimmed = _trim_to_complete_rss_prefix(partial)

    assert trimmed == (
        b"<?xml version='1.0'?><rss><channel><title>x</title>"
        b"<item><title>newest</title></item>"
        b"<item><title>older</title></item>"
        b"</channel></rss>"
    )


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
