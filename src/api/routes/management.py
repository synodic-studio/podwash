"""JSON management API for the iOS app."""

import re
from pathlib import Path

import feedparser
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.api.auth import require_admin_token
from src.database import queries
from src.database.models import EpisodeStatus, Feed
from src.feeds.fetcher import FeedFetchError, fetch_public_feed

router = APIRouter(prefix="/api", tags=["management"])


class FeedSubmission(BaseModel):
    url: str


PROCESSING_STATUSES = {
    EpisodeStatus.DOWNLOADING,
    EpisodeStatus.TRANSCRIBING,
    EpisodeStatus.CLASSIFYING,
    EpisodeStatus.EDITING,
}


def _slugify(text: str) -> str:
    """Convert text to a URL-safe slug. May return an empty string."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")


def _slug_for(title: str, source_url: str) -> str:
    """Return a non-empty slug seed for a feed.

    Falls back to the source hostname (then to ``"feed"``) so emoji- or
    punctuation-only titles still get a usable slug — the uniqueness
    suffix loop later guarantees the final slug doesn't collide.
    """
    slug = _slugify(title)
    if slug:
        return slug
    try:
        from urllib.parse import urlsplit

        host = urlsplit(source_url).hostname or ""
        slug = _slugify(host)
    except ValueError:
        slug = ""
    return slug or "feed"


_APPLE_PODCASTS_RE = re.compile(r"https?://podcasts\.apple\.com/.+?/id(\d+)")


async def _resolve_apple_podcasts_url(url: str) -> str:
    """If url is an Apple Podcasts link, resolve it to the RSS feed URL via iTunes Lookup API."""
    match = _APPLE_PODCASTS_RE.search(url)
    if not match:
        return url
    podcast_id = match.group(1)
    import httpx

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        resp = await client.get(
            f"https://itunes.apple.com/lookup?id={podcast_id}&entity=podcast"
        )
        resp.raise_for_status()
        data = resp.json()
    results = data.get("results", [])
    if not results or "feedUrl" not in results[0]:
        raise HTTPException(
            status_code=400,
            detail="Could not find RSS feed for this Apple Podcasts link.",
        )
    return results[0]["feedUrl"]


@router.get("/feeds")
async def list_feeds(request: Request, _: None = Depends(require_admin_token)):
    conn = request.app.state.db
    feeds = queries.get_all_feeds(conn, enabled_only=False)
    stats = queries.get_feed_episode_stats(conn)
    return [
        {
            "id": f.id,
            "name": f.name,
            "slug": f.slug,
            "source_url": f.source_url,
            "enabled": f.enabled,
            "last_polled_at": f.last_polled_at,
            "episode_count": stats.get(f.id, {}).get("total", 0),
            "pending_count": stats.get(f.id, {}).get("pending", 0),
            "completed_count": stats.get(f.id, {}).get("completed", 0),
            "failed_count": stats.get(f.id, {}).get("failed", 0),
        }
        for f in feeds
    ]


@router.post("/feeds", status_code=201)
async def submit_feed(
    body: FeedSubmission,
    request: Request,
    _: None = Depends(require_admin_token),
):
    """Submit a new podcast RSS feed URL for processing."""
    conn = request.app.state.db
    url = await _resolve_apple_podcasts_url(body.url.strip())

    # Check if feed already exists
    existing = conn.execute(
        "SELECT id, name, slug FROM feeds WHERE source_url = ?", (url,)
    ).fetchone()
    if existing:
        return {
            "id": existing["id"],
            "name": existing["name"],
            "slug": existing["slug"],
            "message": "Feed already exists",
            "already_existed": True,
        }

    # Fetch via the SSRF-safe fetcher (timeouts, size cap, public IP only)
    # and pass the bytes to feedparser. Never let feedparser dereference
    # arbitrary URLs.
    try:
        feed_bytes = await fetch_public_feed(url)
    except FeedFetchError as exc:
        raise HTTPException(status_code=400, detail=f"Could not fetch feed: {exc}")
    parsed = feedparser.parse(feed_bytes)
    if parsed.bozo and not parsed.entries:
        raise HTTPException(
            status_code=400,
            detail="Could not parse RSS feed. Check the URL and try again.",
        )

    feed_title = parsed.feed.get("title", "").strip()
    if not feed_title:
        raise HTTPException(
            status_code=400,
            detail="Feed has no title. Is this a valid podcast RSS feed?",
        )

    slug = _slug_for(feed_title, url)

    # Ensure slug is unique
    suffix = 0
    candidate = slug
    while queries.get_feed_by_slug(conn, candidate) is not None:
        suffix += 1
        candidate = f"{slug}-{suffix}"
    slug = candidate

    feed_id = queries.upsert_feed(
        conn,
        Feed(name=feed_title, source_url=url, slug=slug),
    )

    # Store artwork if available
    image_url = parsed.feed.get("image", {}).get("href")
    if image_url:
        queries.update_feed_image(conn, feed_id, image_url)

    return {
        "id": feed_id,
        "name": feed_title,
        "slug": slug,
        "message": "Feed added successfully",
        "already_existed": False,
    }


@router.delete("/feeds/{feed_id}")
async def delete_feed(
    feed_id: int, request: Request, _: None = Depends(require_admin_token)
):
    """Remove a feed and all its episodes."""
    conn = request.app.state.db
    settings = request.app.state.settings
    feed = queries.get_feed_by_id(conn, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")

    # Clean up processed audio files on disk
    data_dir = Path(settings.data_dir)
    feed_dir = data_dir / f"feed_{feed_id}"
    if feed_dir.exists():
        import shutil

        shutil.rmtree(feed_dir, ignore_errors=True)

    queries.delete_feed(conn, feed_id)
    return {"message": f"Feed '{feed.name}' deleted", "id": feed_id}


@router.get("/feeds/{feed_id}/episodes")
async def list_episodes(
    feed_id: int, request: Request, _: None = Depends(require_admin_token)
):
    conn = request.app.state.db
    feed = queries.get_feed_by_id(conn, feed_id)
    if feed is None:
        raise HTTPException(status_code=404, detail="Feed not found")
    episodes = queries.get_episodes_for_feed(conn, feed_id)
    return [
        {
            "id": e.id,
            "feed_id": e.feed_id,
            "title": e.title,
            "pub_date": e.pub_date.isoformat() if e.pub_date else None,
            "duration_seconds": e.duration_seconds,
            "status": e.status.value,
            "error_message": e.error_message,
            "retry_count": e.retry_count,
        }
        for e in episodes
    ]


@router.post("/episodes/{episode_id}/process", status_code=202)
async def trigger_processing(
    episode_id: int, request: Request, _: None = Depends(require_admin_token)
):
    """Enqueue an episode for the worker by flipping status back to pending."""
    conn = request.app.state.db

    episode = queries.get_episode_by_id(conn, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    if episode.status in PROCESSING_STATUSES:
        raise HTTPException(
            status_code=409, detail="Episode is already being processed"
        )

    queries.update_episode_status(conn, episode_id, EpisodeStatus.PENDING)

    return {
        "episode_id": episode_id,
        "status": "pending",
        "message": "Queued — worker will pick up on next poll",
    }


@router.get("/episodes/{episode_id}/logs")
async def get_episode_logs(
    episode_id: int, request: Request, _: None = Depends(require_admin_token)
):
    conn = request.app.state.db
    episode = queries.get_episode_by_id(conn, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    logs = queries.get_processing_logs(conn, episode_id)
    return [
        {
            "id": log.id,
            "stage": log.stage,
            "status": log.status,
            "message": log.message,
            "duration_ms": log.duration_ms,
            "created_at": log.created_at.isoformat() if log.created_at else None,
        }
        for log in logs
    ]
