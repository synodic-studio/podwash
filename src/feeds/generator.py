"""Generate output RSS XML for proxy feeds."""

from datetime import datetime, timezone
from xml.sax.saxutils import escape

from src.database.models import Episode, EpisodeStatus, Feed

# Status prefixes for episode titles
_STATUS_PREFIX = {
    EpisodeStatus.NEW: "\u25cb",  # ○ tap to request ad-free version
    EpisodeStatus.PENDING: "\u25d0",  # ◐ queued for worker
    EpisodeStatus.DOWNLOADING: "\u25d0",  # ◐
    EpisodeStatus.TRANSCRIBING: "\u25d0",  # ◐
    EpisodeStatus.CLASSIFYING: "\u25d0",  # ◐
    EpisodeStatus.EDITING: "\u25d0",  # ◐
    EpisodeStatus.COMPLETED: "\u25cf",  # ●
    EpisodeStatus.FAILED: "\u2718",  # ✘
}

def generate_feed_xml(
    feed: Feed,
    episodes: list[Episode],
    base_url: str,
) -> str:
    """
    Generate RSS 2.0 XML for a proxy feed.

    All episodes point to our proxy audio URL. The audio endpoint handles:
    - Completed: serves processed (ad-free) audio
    - In progress: serves a short notification clip
    - Pending: serves a short notification clip and triggers processing

    Title prefixes indicate status:
    - ○ = not yet processed (tap to trigger)
    - ◐ = currently processing
    - ● = processed and ready
    - ✘ = failed (tap to retry)
    """
    feed_url = f"{base_url}/feeds/{feed.slug}.xml"
    display_name = f"\u2702 {feed.name}"  # ✂ scissors prefix

    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">',
        "  <channel>",
        f"    <title>{escape(display_name)}</title>",
        f"    <description>Ad-free proxy of {escape(feed.name)}</description>",
        f"    <link>{escape(feed_url)}</link>",
        "    <language>en-us</language>",
        f"    <lastBuildDate>{_rfc2822_now()}</lastBuildDate>",
        f'    <atom:link href="{escape(feed_url)}" rel="self" type="application/rss+xml" xmlns:atom="http://www.w3.org/2005/Atom"/>',
    ]

    if feed.image_url:
        img = escape(feed.image_url)
        xml_parts.append(f'    <itunes:image href="{img}"/>')
        xml_parts.append("    <image>")
        xml_parts.append(f"      <url>{img}</url>")
        xml_parts.append(f"      <title>{escape(feed.name)}</title>")
        xml_parts.append(f"      <link>{escape(feed_url)}</link>")
        xml_parts.append("    </image>")

    for episode in episodes:
        # A source episode has one stable RSS identity (episode.guid).
        # Before completion, the enclosure is /audio/{feed}/{episode}.mp3;
        # after completion, the enclosure changes to /audio/clean/{token}.mp3.
        # Keeping the GUID stable avoids podcast apps showing duplicate rows.
        if episode.status == EpisodeStatus.COMPLETED and episode.clean_token:
            audio_url = f"{base_url}/audio/clean/{episode.clean_token}.mp3"
        else:
            audio_url = f"{base_url}/audio/{feed.id}/{episode.id}.mp3"
        feed_guid = episode.guid
        pub_date = _format_pub_date(episode.pub_date)
        desc = (
            escape(episode.description)
            if episode.description
            else escape(episode.title)
        )
        prefix = _STATUS_PREFIX.get(episode.status, "○")
        title = f"{prefix} {episode.title}"

        xml_parts.append("    <item>")
        xml_parts.append(f"      <title>{escape(title)}</title>")
        xml_parts.append(f"      <description>{desc}</description>")
        xml_parts.append(f"      <pubDate>{pub_date}</pubDate>")
        # Estimate byte length from duration (128kbps MP3 = 16KB/s)
        length = (episode.duration_seconds or 0) * 16000
        xml_parts.append(
            f'      <enclosure url="{escape(audio_url)}" length="{length}" type="audio/mpeg"/>'
        )
        xml_parts.append(f'      <guid isPermaLink="false">{escape(feed_guid)}</guid>')

        if episode.duration_seconds:
            xml_parts.append(
                f"      <itunes:duration>{_format_duration(episode.duration_seconds)}</itunes:duration>"
            )

        xml_parts.append("    </item>")

    xml_parts.append("  </channel>")
    xml_parts.append("</rss>")

    return "\n".join(xml_parts)


def _rfc2822_now() -> str:
    """Current time in RFC 2822 format."""
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def _format_pub_date(dt: datetime | None) -> str:
    """Format a datetime as RFC 2822, or return current time if None."""
    if dt is None:
        return _rfc2822_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")


def _format_duration(seconds: int) -> str:
    """Format seconds as HH:MM:SS."""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"
