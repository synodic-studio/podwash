"""Parse source RSS feeds using feedparser."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser

from src.database.models import Episode


def extract_feed_image(url: str) -> str | None:
    """Extract the channel-level artwork URL from a feed."""
    parsed = feedparser.parse(url)
    feed_info = parsed.feed
    # Try itunes:image first, then standard image
    if href := feed_info.get("image", {}).get("href"):
        return href
    if img := feed_info.get("image"):
        if isinstance(img, dict) and img.get("url"):
            return img["url"]
    return None


def parse_feed(url: str, feed_id: int, max_episodes: int = 0) -> list[Episode]:
    """
    Fetch and parse an RSS feed, returning Episode models for each entry.

    Episodes are returned newest-first (by pub_date).
    """
    parsed = feedparser.parse(url)
    episodes: list[Episode] = []

    for entry in parsed.entries:
        # Find audio enclosure
        audio_url = _extract_audio_url(entry)
        if not audio_url:
            continue

        guid = entry.get("id") or entry.get("link") or audio_url
        title = entry.get("title", "Untitled")
        description = entry.get("summary", "")
        pub_date = _parse_date(entry)
        duration = _parse_duration(entry)

        episodes.append(
            Episode(
                feed_id=feed_id,
                guid=guid,
                title=title,
                source_audio_url=audio_url,
                pub_date=pub_date,
                duration_seconds=duration,
                description=description,
            )
        )

    episodes.sort(key=lambda e: e.pub_date or datetime.min, reverse=True)
    if max_episodes > 0:
        episodes = episodes[:max_episodes]
    return episodes


def _extract_audio_url(entry) -> str | None:
    """Extract the audio enclosure URL from a feed entry."""
    for link in entry.get("links", []):
        if link.get("rel") == "enclosure" and "audio" in link.get("type", ""):
            return link["href"]

    for enclosure in entry.get("enclosures", []):
        enc_type = enclosure.get("type", "")
        if "audio" in enc_type:
            return enclosure.get("href")

    # Fallback: any enclosure with common audio extensions
    for enclosure in entry.get("enclosures", []):
        href = enclosure.get("href", "")
        if any(href.lower().endswith(ext) for ext in (".mp3", ".m4a", ".ogg", ".opus")):
            return href

    return None


def _parse_date(entry) -> datetime | None:
    """Parse publication date from a feed entry."""
    date_str = entry.get("published") or entry.get("updated")
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str)
    except (ValueError, TypeError):
        pass
    # Feedparser's parsed date tuple
    date_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if date_parsed:
        try:
            return datetime(*date_parsed[:6], tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass
    return None


def _parse_duration(entry) -> int | None:
    """Parse iTunes duration from a feed entry."""
    duration_str = entry.get("itunes_duration", "")
    if not duration_str:
        return None
    try:
        parts = duration_str.split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        return int(parts[0])
    except (ValueError, IndexError):
        return None
