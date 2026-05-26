"""Parse source RSS feeds using feedparser."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser

from src.database.models import Episode

# Tracking parameters podcast hosts append; same audio with different
# values is the same source episode.
_TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "rss_app",
    "_from",
}

_TRACKING_PREFIXES = ("utm",)


def _is_tracking_param(key: str) -> bool:
    k = key.lower()
    if k in _TRACKING_PARAMS:
        return True
    return any(k == p or k.startswith(p + "_") or k == p for p in _TRACKING_PREFIXES)


def normalize_audio_url(url: str) -> str:
    """Lowercase scheme/host and strip common tracking query params.

    Two URLs that differ only by tracking parameters or scheme/host
    case must normalize to the same string so dedup can spot them as
    the same source episode.
    """
    if not url:
        return ""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    netloc = host.lower()
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    if parts.username:
        cred = parts.username
        if parts.password is not None:
            cred = f"{cred}:{parts.password}"
        netloc = f"{cred}@{netloc}"
    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(k)
    ]
    query = urlencode(query_pairs)
    return urlunsplit((scheme, netloc, parts.path, query, ""))


def build_source_identity(entry, audio_url: str) -> str:
    """Build a durable identity for a source episode.

    Prefers normalized audio URL because source GUIDs and links rotate
    when podcast hosts re-stamp tracking. Falls back to a stable tuple
    when no audio URL is available.
    """
    normalized = normalize_audio_url(audio_url) if audio_url else ""
    if normalized:
        return normalized
    title = (entry.get("title") or "").strip().lower()
    date = entry.get("published") or entry.get("updated") or ""
    duration = entry.get("itunes_duration") or ""
    return f"title:{title}|date:{date}|dur:{duration}"


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
                source_identity=build_source_identity(entry, audio_url),
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
